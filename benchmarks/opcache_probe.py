#!/usr/bin/env python3
"""Probe code sharing between disposable children using temporary PHP fixtures."""
import argparse
import concurrent.futures
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tests'))
from fcgi import request
from integration import BINARY, Server, wait_until


ENTRY = r'''<?php
function snapshot(): array {
    $status = opcache_get_status(true);
    $scripts = [];
    foreach (['Base.php', 'Learned.php', 'Compiled.php'] as $name) {
        $file = __DIR__ . '/' . $name;
        $scripts[$name] = [
            'cached' => opcache_is_script_cached($file),
            'hits' => $status['scripts'][$file]['hits'] ?? null,
        ];
    }
    return [
        'scripts' => $scripts,
        'learned_class_visible' => class_exists('Learned', false),
        'learned_function_visible' => function_exists('learned_function'),
        'compiled_class_visible' => class_exists('Compiled', false),
        'preloaded_seed_visible' => class_exists('PreloadedSeed', false),
        'top_level' => $GLOBALS['learned_top_level'] ?? 0,
        'manual_restarts' => $status['opcache_statistics']['manual_restarts'],
        'restart_pending' => $status['restart_pending'],
    ];
}
$action = $_GET['action'] ?? 'status';
$out = ['pid' => getmypid(), 'before' => snapshot()];
if ($action === 'wait') {
    file_put_contents(__DIR__ . '/waiting', (string) getmypid());
    $deadline = microtime(true) + 10;
    while (!file_exists(__DIR__ . '/release')) {
        if (microtime(true) > $deadline) throw new RuntimeException('Gate timed out');
        usleep(1000);
    }
    $out['after_peer'] = snapshot();
}
if ($action === 'load' || $action === 'wait') {
    require __DIR__ . '/Base.php';
    require __DIR__ . '/Learned.php';
    $out['value'] = Learned::value();
    $out['static_property'] = ++Learned::$counter;
    $out['static_local'] = learned_function();
} elseif ($action === 'compile') {
    $out['compiled'] = opcache_compile_file(__DIR__ . '/Compiled.php');
} elseif ($action === 'reset') {
    $out['reset'] = opcache_reset();
}
$out['after'] = snapshot();
header('Content-Type: application/json');
echo json_encode($out, JSON_THROW_ON_ERROR);
'''


def write_php(path, content):
    path.write_text(content)
    # Keep the normal two-second file_update_protection setting. The fixtures
    # are complete before publication; age them so no real-time sleep is needed.
    stamp = time.time() - 10
    os.utime(path, (stamp, stamp))


def prepare(root):
    write_php(root / 'index.php', ENTRY)
    (root / 'lib').mkdir()
    (root / 'composer.json').write_text(json.dumps({'autoload': {'classmap': ['lib/']}}))
    write_php(root / 'lib/Seed.php', '<?php class PreloadedSeed {}')


def publish_learned(root):
    # Called only after the manager starts, so these cannot be in its preload.
    write_php(root / 'Base.php', '<?php class LearnedBase {}')
    write_php(root / 'Learned.php', '''<?php
class Learned extends LearnedBase {
    public static int $counter = 0;
    public static function value(): int { return 42; }
}
function learned_function(): int { static $counter = 0; return ++$counter; }
$GLOBALS['learned_top_level'] = ($GLOBALS['learned_top_level'] ?? 0) + 1;
''')
    write_php(root / 'Compiled.php', '<?php class Compiled {}')


def call(server, action):
    status, _, body, errors = request(server.endpoint, server.root / 'index.php',
                                     query='action=' + action, timeout=15)
    if status != 200 or errors:
        raise AssertionError(f'PHP response: {status}, {body!r}, {errors!r}\n{server.logs()}')
    return json.loads(body)


def check_loaded(row):
    assert row['before']['learned_class_visible'] is False, row
    assert row['before']['learned_function_visible'] is False, row
    assert row['before']['top_level'] == 0, row
    assert row['value'] == 42, row
    assert row['static_property'] == row['static_local'] == 1, row
    assert row['after']['top_level'] == 1, row
    assert row['after']['learned_class_visible'] is True, row
    assert row['after']['learned_function_visible'] is True, row
    for name in ('Base.php', 'Learned.php'):
        assert row['after']['scripts'][name]['cached'] is True, row


def sequential(preload):
    with tempfile.TemporaryDirectory(prefix='poly-opcache-') as temp:
        root = Path(temp)
        prepare(root)
        server = Server(root, workers=1, timeout=15, preload=preload)
        try:
            publish_learned(root)
            rows = []
            for action in ('status', 'load', 'load', 'compile', 'status', 'reset', 'status', 'load'):
                rows.append({'action': action, **call(server, action)})
            assert len({row['pid'] for row in rows}) == len(rows), rows
            assert rows[0]['before']['scripts']['Learned.php']['cached'] is False, rows
            assert rows[1]['after']['scripts']['Learned.php']['hits'] == 0, rows
            assert rows[2]['before']['scripts']['Learned.php']['cached'] is True, rows
            assert rows[2]['after']['scripts']['Learned.php']['hits'] == 1, rows
            for row in (rows[1], rows[2], rows[7]):
                check_loaded(row)
            assert rows[3]['compiled'] is True, rows
            assert rows[4]['before']['scripts']['Compiled.php']['cached'] is True, rows
            assert rows[4]['before']['compiled_class_visible'] is False, rows
            assert rows[5]['reset'] is True, rows
            assert rows[6]['before']['manual_restarts'] == 1, rows
            assert rows[6]['before']['restart_pending'] is False, rows
            assert rows[6]['before']['scripts']['Learned.php']['cached'] is False, rows
            assert all(row['before']['preloaded_seed_visible'] == preload for row in rows), rows
            return {'preload': preload, 'workers': 1, 'manager_pid': server.process.pid, 'requests': rows}
        finally:
            server.close()


def existing_sibling():
    with tempfile.TemporaryDirectory(prefix='poly-opcache-') as temp:
        root = Path(temp)
        prepare(root)
        server = Server(root, workers=2, timeout=15)
        try:
            publish_learned(root)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                waiting = pool.submit(call, server, 'wait')
                try:
                    wait_until(lambda: (root / 'waiting').exists())
                    writer = call(server, 'load')
                finally:
                    (root / 'release').touch()
                reader = waiting.result()
            assert writer['pid'] != reader['pid'], (writer, reader)
            assert reader['before']['scripts']['Learned.php']['cached'] is False, reader
            assert reader['after_peer']['scripts']['Learned.php']['cached'] is True, reader
            assert reader['after_peer']['learned_class_visible'] is False, reader
            assert reader['after']['scripts']['Learned.php']['hits'] == 1, reader
            check_loaded(writer)
            check_loaded(reader)
            return {'preload': False, 'workers': 2, 'writer': writer, 'reader': reader}
        finally:
            server.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=REPO / 'benchmarks/results/opcache-probe.json')
    args = parser.parse_args()
    result = {
        'date_utc': datetime.now(timezone.utc).isoformat(),
        'runtime': subprocess.check_output([BINARY, '--version'], text=True).strip(),
        'platform': platform.platform(),
        'sequential': [sequential(False), sequential(True)],
        'existing_sibling': existing_sibling(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(f'PASS: learned code sharing, request isolation, compile-only warming, cache reset, '
          f'and visibility to an existing sibling. Results: {args.output}')


if __name__ == '__main__':
    main()
