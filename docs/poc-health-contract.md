# POC-001 approved contract (bootstrap only)

Implement apps/api/health.py using only the Python standard library.
Export make_server(host="127.0.0.1", port=0), returning an unstarted
http.server.ThreadingHTTPServer. The caller controls serve_forever/shutdown/server_close.
Do not start threads/servers or bind sockets at import time.

GET /health returns status 200, Content-Type application/json; charset=utf-8,
and exactly the JSON object {"status":"ok","service":"sivka-burka-poc"}.
Any other GET path, including /health?x=1 and /health/, returns status 404,
the same content type, and exactly {"error":"not_found"}.
Every response includes Content-Length equal to the encoded body byte length.
No timestamps, environment values, database connections, dependencies or business logic.
Repeated requests must yield identical response bodies. Bind loopback by default.
POST is unsupported; the stdlib default 501 response is acceptable.

Also implement tests/poc/test_health_worker.py with unittest coverage of health and 404.
Independent lead tests in tests/poc/test_health_contract.py may not be changed.
Test servers must be shut down, closed and their threads joined even on assertion failure.
No requirements for CLI deployment or product framework choice are implied by this POC.
