import json
import os
import hmac
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE = "opsmemory-canary"
lock = threading.Lock()
degraded = os.environ.get("CANARY_START_DEGRADED", "true").lower() == "true"

class Handler(BaseHTTPRequestHandler):
    def respond(self, status, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        global degraded
        if self.path != "/metrics":
            with lock:
                observed_requests.append(time.monotonic())
        with lock:
            bad=degraded
        if self.path == "/health":
            self.respond(503 if bad else 200,{"service":SERVICE,"status":"degraded" if bad else "healthy"})
        elif self.path == "/state":
            self.respond(200,{"service":SERVICE,"status":"degraded" if bad else "healthy",
                              "observed_at":datetime.now(timezone.utc).isoformat()})
        elif self.path == "/metrics":
            error_rate,p99=(0.05,1.2) if bad else (0.001,0.2)
            with lock:
                now=time.monotonic()
                while observed_requests and now-observed_requests[0]>60:
                    observed_requests.popleft()
                request_count=len(observed_requests)
                current_degraded=1 if degraded else 0
            body=(
                '# HELP canary_error_rate Controlled canary error ratio\\n'
                '# TYPE canary_error_rate gauge\\n'
                f'canary_error_rate{{service="{SERVICE}"}} {error_rate}\\n'
                '# HELP canary_p99_seconds Controlled canary p99 latency\\n'
                '# TYPE canary_p99_seconds gauge\\n'
                f'canary_p99_seconds{{service="{SERVICE}"}} {p99}\\n'
                '# HELP canary_requests_last_60s Observed requests in rolling 60 seconds\\n'
                '# TYPE canary_requests_last_60s gauge\\n'
                f'canary_requests_last_60s{{service="{SERVICE}"}} {request_count}\\n'
                '# HELP canary_degraded Current canary fault state\\n'
                '# TYPE canary_degraded gauge\\n'
                f'canary_degraded{{service="{SERVICE}"}} {current_degraded}\\n'
            )
            self.respond(200,body.encode(),"text/plain; version=0.0.4")
        else:
            self.respond(404,{"error":"not found"})

    def do_POST(self):
        global degraded
        if self.path != "/remediate":
            self.respond(404, {"error": "not found"})
            return
        expected = os.environ.get("CANARY_REMEDIATION_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        if not expected or not hmac.compare_digest(supplied, "Bearer " + expected):
            self.respond(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        with lock:
            was_degraded = degraded
            degraded = False
        self.respond(200, {
            "service": SERVICE,
            "operation": "clear_canary_fault",
            "previous_state": "degraded" if was_degraded else "healthy",
            "current_state": "healthy",
            "changed": was_degraded,
            "executed_at": datetime.now(timezone.utc).isoformat()
        })

    def log_message(self, fmt, *args):
        print("CANARY", fmt % args, flush=True)

if __name__ == "__main__":
    print(f"Starting {SERVICE}; initial degraded={degraded}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8081), Handler).serve_forever()
