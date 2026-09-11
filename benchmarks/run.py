#!/usr/bin/env python3
"""Compare matched Polychrome/FPM builds. No third-party Python dependencies."""
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import platform
import pwd
import statistics
import subprocess
import sys
import tempfile
import threading
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tests'))
from fcgi import request


def process_tree(pid):
    result, pending = [], [pid]
    while pending:
        current = pending.pop()
        try:
            pending.extend(int(p) for p in Path(f'/proc/{current}/task/{current}/children').read_text().split())
            result.append(current)
        except FileNotFoundError:
            pass
    return result


def sample(pid):
    cpu, pss = 0.0, 0
    ticks = os.sysconf('SC_CLK_TCK')
    for child in process_tree(pid):
        try:
            fields = Path(f'/proc/{child}/stat').read_text().rsplit(')', 1)[1].split()
            # Own CPU plus already reaped children's CPU, to include disposable
            # processes without adding their retired memory to live PSS.
            cpu += sum(int(fields[index]) for index in (11, 12, 13, 14)) / ticks
            for line in Path(f'/proc/{child}/smaps_rollup').read_text().splitlines():
                if line.startswith('Pss:'):
                    pss += int(line.split()[1])
        except (FileNotFoundError, ProcessLookupError):
            pass
    return cpu, pss


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def measure(endpoint, script, query, params, pid, concurrency, count):
    stop = threading.Event()
    memory = []

    def monitor():
        while not stop.is_set():
            memory.append(sample(pid)[1])
            stop.wait(0.05)

    def once(_):
        started = time.perf_counter()
        response = request(endpoint, script, query=query, params=params, timeout=60)
        if response[0] != 200:
            raise RuntimeError(f'Benchmark request returned {response[0]}: {response[2][:200]!r}')
        return (time.perf_counter() - started) * 1000

    cpu_before = sample(pid)[0]
    monitor_thread = threading.Thread(target=monitor)
    monitor_thread.start()
    started = time.perf_counter()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            latency = list(pool.map(once, range(count)))
        elapsed = time.perf_counter() - started
        # Let the manager reap completed children before its cumulative CPU read.
        time.sleep(0.1)
        cpu_after = sample(pid)[0]
    finally:
        stop.set()
        monitor_thread.join()
    return {
        'requests': count, 'concurrency': concurrency,
        'elapsed_seconds': round(elapsed, 4), 'requests_per_second': round(count / elapsed, 2),
        'latency_ms': {label: round(percentile(latency, fraction), 3) for label, fraction in [('p50', .5), ('p95', .95), ('p99', .99)]},
        'cpu_seconds': round(max(0, cpu_after - cpu_before), 3),
        'pss_peak_kib': max(memory, default=0),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, default=REPO / 'dist')
    parser.add_argument('--root', type=Path, default=REPO / 'benchmarks/fixtures')
    parser.add_argument('--script', default='index.php')
    parser.add_argument('--query', default='')
    parser.add_argument('--uri', default=None)
    parser.add_argument('--cookie', default=None)
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--concurrency', default='1,2,4')
    parser.add_argument('--requests', type=int, default=300)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--output', type=Path, default=REPO / 'benchmarks/results/latest.json')
    args = parser.parse_args()
    if min(args.workers, args.requests, args.repeat) < 1:
        parser.error('workers, requests and repeat must be positive')
    concurrency = [int(value) for value in args.concurrency.split(',')]
    if not concurrency or min(concurrency) < 1:
        parser.error('concurrency must contain positive integers')
    root, prefix = args.root.resolve(), args.prefix.resolve()
    script = (root / args.script).resolve()
    params = {}
    if args.uri:
        params['REQUEST_URI'] = args.uri
    if args.cookie:
        params['HTTP_COOKIE'] = args.cookie
    version = subprocess.check_output([prefix / 'bin/polychrome', '--version'], text=True).strip()
    results = {
        'runtime': version, 'platform': platform.platform(), 'cpus': os.cpu_count(),
        'workload': {'root': str(root), 'script': args.script, 'query': args.query, 'uri': args.uri, 'authenticated': bool(args.cookie)},
        'workers': args.workers, 'samples': [],
        'notes': 'Direct FastCGI, warmed caches, same PHP build/INI/preload helper. CPU and summed live PSS sampled through /proc. No performance threshold.',
    }
    for runtime in ('polychrome', 'fpm', 'fpm-one-request'):
        for preload in (False, True):
            label = runtime + ('-preload' if preload else '-no-preload')
            for repeat in range(args.repeat):
                with tempfile.TemporaryDirectory(prefix='polybench-') as temp:
                    temp = Path(temp)
                    socket_path = str(temp / 'fcgi.sock')
                    ini = temp / 'php.ini'
                    ini.write_text('opcache.enable=1\nopcache.jit=disable\nopcache.memory_consumption=256\nopcache.max_accelerated_files=100000\n'
                                   'opcache.validate_timestamps=1\nopcache.revalidate_freq=2\ndisplay_errors=0\nlog_errors=1\n'
                                   'expose_php=0\nvariables_order=GPCS\nmax_execution_time=0\nmemory_limit=256M\n')
                    if runtime == 'polychrome':
                        command = [prefix / 'bin/polychrome', '--root', root, '--listen', 'unix:' + socket_path,
                                   '--workers', str(args.workers), '-c', ini, '--request-timeout', '60']
                        if not preload:
                            command.append('--no-preload')
                    else:
                        config = temp / 'fpm.conf'
                        user = pwd.getpwuid(os.geteuid()).pw_name
                        config.write_text('[global]\ndaemonize=no\nerror_log=/dev/stderr\n[www]\n'
                                          f'user={user}\nlisten={socket_path}\npm=static\npm.max_children={args.workers}\n'
                                          f'pm.max_requests={1 if runtime == "fpm-one-request" else 0}\n'
                                          'clear_env=no\nprocess.dumpable=yes\ncatch_workers_output=yes\nrequest_terminate_timeout=60s\n')
                        command = [prefix / 'sbin/php-fpm', '-F', '-y', config, '-c', ini]
                        if os.geteuid() == 0:
                            command.append('-R')
                        if preload:
                            command += ['-d', 'opcache.preload=' + str(prefix / 'share/polychrome/preload.php'), '-d', 'opcache.preload_user=' + user]
                    environment = os.environ | {'POLYCHROME_ROOT': str(root)}
                    with (temp / 'server.log').open('wb') as log:
                        process = subprocess.Popen(command, cwd=root, env=environment, stdout=log, stderr=log)
                        try:
                            deadline = time.monotonic() + 60
                            while not Path(socket_path).exists():
                                if process.poll() is not None or time.monotonic() > deadline:
                                    raise RuntimeError((temp / 'server.log').read_text())
                                time.sleep(.02)
                            for _ in range(20):
                                response = request(socket_path, script, query=args.query, params=params)
                                if response[0] != 200:
                                    raise RuntimeError(f'Warmup failed: {response[:3]}\n' + (temp / 'server.log').read_text())
                            for parallelism in concurrency:
                                row = measure(socket_path, script, args.query, params, process.pid, parallelism, args.requests)
                                row.update(runtime=label, repeat=repeat + 1)
                                results['samples'].append(row)
                                print(f'{label:28s} c={parallelism} run={repeat + 1} {row["requests_per_second"]:8.2f} req/s p95={row["latency_ms"]["p95"]}ms PSS={row["pss_peak_kib"]}KiB', flush=True)
                        finally:
                            process.terminate()
                            try:
                                process.wait(timeout=35)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + '\n')
    print(f'Results: {args.output}')


if __name__ == '__main__':
    main()
