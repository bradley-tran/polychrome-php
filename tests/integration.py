#!/usr/bin/env python3
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import time
import unittest

from fcgi import connect, encode_params, record, request, receive_response

REPO = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('POLYCHROME_BIN', REPO / 'dist/bin/polychrome')).resolve()


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError('Condition did not become true before deadline')


class Server:
    def __init__(self, root, workers=2, timeout=3, preload=False, tcp=False, extra=()):
        self.root = Path(root)
        self.endpoint = str(self.root / 'runtime.sock')
        if tcp:
            with socket.socket() as reserve:
                reserve.bind(('127.0.0.1', 0))
                self.endpoint = reserve.getsockname()
        endpoint = f'{self.endpoint[0]}:{self.endpoint[1]}' if tcp else 'unix:' + self.endpoint
        self.log_path = self.root / 'runtime.log'
        self.log = self.log_path.open('wb')
        command = [str(BINARY), '--root', str(root), '--listen', endpoint,
                   '--workers', str(workers), '--request-timeout', str(timeout),
                   '-d', 'session.save_path=' + str(root), '-d', 'precision=14']
        if not preload:
            command.append('--no-preload')
        self.process = subprocess.Popen(command + list(extra), stdout=self.log, stderr=self.log, start_new_session=True)
        try:
            wait_until(lambda: 'polychrome: ready ' in self.logs() or self.process.poll() is not None, timeout=30)
            if self.process.poll() is not None:
                raise AssertionError('Server failed to start:\n' + self.logs())
        except BaseException:
            self.close()
            raise

    def logs(self):
        return self.log_path.read_text(errors='replace')

    def children(self):
        path = Path(f'/proc/{self.process.pid}/task/{self.process.pid}/children')
        return [int(pid) for pid in path.read_text().split()] if path.exists() else []

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=35)
            except subprocess.TimeoutExpired:
                children = self.children()
                self.process.kill()
                for pid in children:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                self.process.wait()
                raise
        self.log.close()


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='polychrome-test-')
        self.root = Path(self.temp.name)
        self.script = self.root / 'index.php'
        shutil.copy(REPO / 'tests/fixtures/request.php', self.script)
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.close()
            self.assertNotIn('ERROR: AddressSanitizer', self.server.logs())
            self.assertNotIn('runtime error:', self.server.logs())
        self.temp.cleanup()

    def start(self, **options):
        self.server = Server(self.root, **options)
        return self.server

    def call(self, query='', **kwargs):
        return request(self.server.endpoint, self.script, query=query, **kwargs)

    def test_request_information_and_forms(self):
        self.start(tcp=True)
        status, _, body, _ = self.call('a=hello%20world', method='POST', body=b'field=value&list%5B%5D=a', params={
            'CONTENT_TYPE': 'application/x-www-form-urlencoded', 'HTTP_COOKIE': 'example=yes',
            'HTTP_AUTHORIZATION': 'Basic dXNlcjpwYXNz', 'PATH_INFO': '/extra', 'HTTPS': 'on'})
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data['post'], {'field': 'value', 'list': ['a']})
        self.assertEqual(data['get']['a'], 'hello world')
        self.assertEqual(data['cookies'], {'example': 'yes'})
        self.assertEqual(data['server']['PHP_AUTH_USER'], 'user')
        self.assertEqual(data['server']['PHP_AUTH_PW'], 'pass')
        self.assertEqual(data['server']['PHP_SELF'], '/index.php/extra')
        self.assertEqual(data['server']['HTTPS'], 'on')
        self.assertEqual(data['env_method'], 'POST')
        self.assertEqual(data['sapi'], 'polychrome')

    def test_json_and_multipart_upload(self):
        self.start()
        # Cross FastCGI's 65535-byte record boundary to verify streamed input.
        payload = json.dumps({'message': 'hello' * 20000}).encode()
        data = json.loads(self.call(method='POST', body=payload, params={'CONTENT_TYPE': 'application/json'})[2])
        self.assertEqual(data['input'], payload.decode())
        body = (b'--boundary\r\nContent-Disposition: form-data; name="title"\r\n\r\nhello\r\n'
                b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="hello.txt"\r\n'
                b'Content-Type: text/plain\r\n\r\nuploaded\r\n--boundary--\r\n')
        data = json.loads(self.call(method='POST', body=body, params={'CONTENT_TYPE': 'multipart/form-data; boundary=boundary'})[2])
        self.assertEqual(data['post']['title'], 'hello')
        self.assertEqual(data['files']['file'], {'name': 'hello.txt', 'error': 0, 'size': 8, 'uploaded': True, 'content': 'uploaded'})

    def test_headers_head_shutdown_and_sessions(self):
        self.start()
        status, headers, _, _ = self.call('action=headers')
        self.assertEqual(status, 303)
        self.assertIn(('location', '/next'), headers)
        self.assertEqual([v for k, v in headers if k == 'set-cookie'], ['a=1', 'b=2'])
        self.assertEqual(self.call(method='HEAD')[2], b'')
        self.assertEqual(self.call('action=shutdown')[2], b'body shutdown')
        _, headers, body, _ = self.call('action=session')
        self.assertEqual(body, b'1')
        cookie = next(v.split(';')[0] for k, v in headers if k == 'set-cookie')
        self.assertEqual(self.call('action=session', params={'HTTP_COOKIE': cookie})[2], b'2')

    def test_isolation_and_connection_closure(self):
        self.start(workers=1)
        values = [json.loads(self.call('action=isolation', keep=True)[2]) for _ in range(20)]
        self.assertEqual(len({v['pid'] for v in values}), 20)
        for value in values:
            self.assertEqual(value['counter'], 1)
            self.assertFalse(value['environment'])
            self.assertIsNone(value['global'])
            self.assertEqual(value['precision'], '14')
            self.assertEqual(value['cwd'], str(self.root))

    def test_script_boundaries(self):
        self.start()
        self.assertEqual(request(self.server.endpoint, self.root / 'missing.php')[0], 404)
        (self.root / 'plain.txt').write_text('<?php echo "bad";')
        self.assertEqual(request(self.server.endpoint, self.root / 'plain.txt')[0], 403)
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'outside.php'
            target.write_text('<?php echo "outside";')
            (self.root / 'escape.php').symlink_to(target)
            self.assertEqual(request(self.server.endpoint, self.root / 'escape.php')[0], 403)
        self.assertEqual(self.call(params={'CONTENT_LENGTH': '-1'})[0], 400)

    def test_fatals_crashes_and_timeout_recovery(self):
        self.start(workers=1, timeout=1)
        self.assertEqual(self.call('action=fatal')[0], 500)
        for action in ['crash', 'hang']:
            with self.assertRaises((EOFError, ConnectionError)):
                self.call('action=' + action)
            self.assertEqual(self.call()[0], 200)
        wait_until(lambda: len(self.server.children()) == 1)
        self.assertIsNone(self.server.process.poll())

    def test_stalled_connections_and_malformed_records(self):
        self.start(workers=1, timeout=1)
        started = time.monotonic()
        with connect(self.server.endpoint) as sock:
            self.assertEqual(sock.recv(1), b'')
        self.assertLess(time.monotonic() - started, 2.5)
        with connect(self.server.endpoint) as sock:
            sock.sendall(b'\x02\x01\x00\x01\x00\x00\x00\x00')
            try:
                self.assertEqual(sock.recv(1), b'')
            except ConnectionResetError:
                pass
        self.assertEqual(self.call()[0], 200)

    def test_early_finish_keeps_worker_busy(self):
        self.start(workers=1, timeout=2)
        started = time.monotonic()
        self.assertEqual(self.call('action=finish')[2], b'finished')
        self.assertLess(time.monotonic() - started, 0.4)
        self.assertEqual(self.call()[0], 200)
        self.assertGreaterEqual(time.monotonic() - started, 0.45)
        self.assertEqual((self.root / 'after-finish').read_text(), 'done')
        self.assertEqual(self.call('action=finish-hang')[2], b'finished')
        self.assertEqual(self.call(timeout=5)[0], 200)

    def test_streaming(self):
        self.start()
        with connect(self.server.endpoint) as sock:
            params = {'SCRIPT_FILENAME': str(self.script), 'REQUEST_METHOD': 'GET', 'QUERY_STRING': 'action=stream'}
            sock.sendall(record(1, struct.pack('!HB5x', 1, 0)) + record(4, encode_params(params)) + record(4) + record(5))
            started = time.monotonic()
            first = sock.recv(65536)
            self.assertIn(b'first', first)
            self.assertNotIn(b'second', first)
            self.assertLess(time.monotonic() - started, 0.25)

    def test_saturation_and_graceful_shutdown(self):
        self.start(workers=2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            requests = [pool.submit(self.call, 'action=slow') for _ in range(4)]
            wait_until(lambda: (self.root / 'started').exists())
            self.assertLessEqual(len(self.server.children()), 2)
            for future in requests:
                self.assertEqual(future.result()[2], b'completed')
            (self.root / 'started').unlink()
            future = pool.submit(self.call, 'action=slow')
            wait_until(lambda: (self.root / 'started').exists())
            children = self.server.children()
            self.server.process.terminate()
            self.assertEqual(future.result()[2], b'completed')
        self.assertEqual(self.server.process.wait(timeout=5), 0)
        self.assertFalse(Path(self.server.endpoint).exists())
        self.assertTrue(all(not Path(f'/proc/{pid}').exists() for pid in children))

    def test_preload_inheritance_and_request_side_effects(self):
        (self.root / 'lib').mkdir()
        (self.root / 'composer.json').write_text(json.dumps({'autoload': {'psr-4': {'': 'lib/'}}}))
        (self.root / 'lib/Demo.php').write_text('<?php class PreloadedDemo { const VALUE = 42; } $GLOBALS["side_effects"] = ($GLOBALS["side_effects"] ?? 0) + 1;')
        self.start(preload=True)
        state = json.loads(self.call('action=opcache')[2])
        self.assertTrue(state['opcache_enabled'])
        self.assertIn('PreloadedDemo', state['preload_statistics']['classes'])
        for _ in range(2):
            self.assertEqual(json.loads(self.call('action=preload')[2]), {'before': True, 'value': 42, 'side_effects': 1})
        self.server.close()
        self.server = None
        self.start(preload=False)
        self.assertEqual(json.loads(self.call('action=preload')[2])['before'], False)

    def test_terminal_interrupt_drains_active_child(self):
        self.start(workers=1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.call, 'action=slow')
            wait_until(lambda: (self.root / 'started').exists())
            os.killpg(self.server.process.pid, signal.SIGINT)
            self.assertEqual(future.result()[2], b'completed')
        self.assertEqual(self.server.process.wait(timeout=5), 0)

    def test_bad_preload_and_conflicting_ini_fail_startup(self):
        (self.root / 'lib').mkdir()
        (self.root / 'composer.json').write_text('{"autoload":{"classmap":["lib/"]}}')
        (self.root / 'lib/Broken.php').write_text('<?php class Broken { syntax error }')
        with self.assertRaisesRegex(AssertionError, 'Server failed to start'):
            Server(self.root, preload=True)
        ini = self.root / 'custom.ini'
        ini.write_text('opcache.preload=/does/not/exist.php\n')
        with self.assertRaisesRegex(AssertionError, 'reserved'):
            Server(self.root, extra=['-c', str(ini)])


if __name__ == '__main__':
    unittest.main(verbosity=2)
