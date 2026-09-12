import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HealthHandler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/health':
            self._send_json(200, {'status': 'ok', 'service': 'sivka-burka-poc'})
        else:
            self._send_json(404, {'error': 'not_found'})

    def log_message(self, format, *args):
        pass


def make_server(host='127.0.0.1', port=0):
    return ThreadingHTTPServer((host, port), HealthHandler)
