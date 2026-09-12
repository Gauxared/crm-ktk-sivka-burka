import http.client
import importlib.util
import json
from pathlib import Path
import threading
import unittest


class HealthWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[2] / 'apps/api/health.py'
        spec = importlib.util.spec_from_file_location('poc_health_worker', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.server = module.make_server()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        if cls.thread.is_alive():
            raise AssertionError('Server thread did not stop')

    def request(self, method, path):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            conn.request(method, path)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_health(self):
        status, headers, body = self.request('GET', '/health')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'application/json; charset=utf-8')
        self.assertEqual(int(headers['Content-Length']), len(body))
        self.assertEqual(json.loads(body), {'status': 'ok', 'service': 'sivka-burka-poc'})

    def test_not_found(self):
        for path in ('/', '/unknown', '/health?x=1', '/health/'):
            with self.subTest(path=path):
                status, headers, body = self.request('GET', path)
                self.assertEqual(status, 404)
                self.assertEqual(headers['Content-Type'], 'application/json; charset=utf-8')
                self.assertEqual(int(headers['Content-Length']), len(body))
                self.assertEqual(json.loads(body), {'error': 'not_found'})

    def test_repeatable_body(self):
        self.assertEqual(self.request('GET', '/health')[2], self.request('GET', '/health')[2])

    def test_loopback_and_post(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        self.assertEqual(self.request('POST', '/health')[0], 501)
