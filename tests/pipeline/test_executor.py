import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from executors import LocalExecutor
from policy import PipelineError


class LocalTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.requests = []
        self.response = {'model': 'fixture-model', 'choices': [{'finish_reason': 'stop',
            'message': {'content': json.dumps({'files': [], 'blocker': 'fixture blocker', 'handoff': {}})}}]}
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(owner.response).encode())
            def log_message(self, *args):
                pass
        self.server = HTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.settings = {'BASE_URL': f'http://127.0.0.1:{self.server.server_port}/v1',
                         'MODEL': 'fixture-model', 'API_KEY': 'fixture-not-a-secret',
                         'MAX_OUTPUT': 100, 'CONTEXT_LIMIT': 2048, 'TIMEOUT': 2}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def test_protocol_and_no_auth_in_log(self):
        result = LocalExecutor(self.settings, 'Bounded worker', self.temp.name).run({}, {'task': 'fixture'})
        self.assertEqual(result['model'], 'fixture-model')
        self.assertFalse(self.requests[0]['stream'])
        text = (Path(self.temp.name) / 'request.json').read_text()
        self.assertNotIn('fixture-not-a-secret', text)

    def test_incomplete_response_rejected_with_raw_evidence(self):
        self.response['choices'][0]['finish_reason'] = 'length'
        with self.assertRaises(PipelineError):
            LocalExecutor(self.settings, 'Bounded worker', self.temp.name).run({}, {})
        self.assertTrue((Path(self.temp.name) / 'response.json').exists())

    def test_oversized_context_rejected_before_network(self):
        with self.assertRaises(PipelineError):
            LocalExecutor(self.settings, 'Bounded worker', self.temp.name).run({}, {'source': 'x' * 5000})
        self.assertEqual(self.requests, [])
