import json
import os
import hmac
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE = "payment-gateway"
lock = threading.Lock()
degraded = os.environ.get("GATEWAY_START_DEGRADED", "true").lower() == "true"
requests = deque(maxlen=10000)

def record_request(failed, elapsed):
    now = time.time()
    with lock:
        requests.append((now, failed, elapsed))

def metrics_snapshot():
    now = time.time()
    with lock:
        recent = [(bad, duration) for ts, bad, duration in requests if now - ts <= 60]
        bad = sum(1 for failed, _ in recent if failed)
        total = len(recent)
        durations = sorted(duration for _, duration in recent)
        current_degraded = degraded
    error_rate = bad / total if total else 0.0
    if durations:
        index = min(len(durations) - 1, max(0, int(0.99 * len(durations) + 0.999999) - 1))
        p99 = durations[index]
    else:
        p99 = 0.0
    return error_rate, p99, total, current_degraded

class Handler(BaseHTTPRequestHandler):
    def respond(self, status, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            _, _, _, bad = metrics_snapshot()
            self.respond(503 if bad else 200, {
                "service": SERVICE, "status": "degraded" if bad else "healthy"
            })
        elif self.path == "/state":
            _, _, total, bad = metrics_snapshot()
            self.respond(200, {
                "service": SERVICE, "status": "degraded" if bad else "healthy",
                "requests_last_60s": total,
                "observed_at": datetime.now(timezone.utc).isoformat()
            })
        elif self.path == "/metrics":
            error_rate, p99, total, bad = metrics_snapshot()
            body = (
                '# HELP gateway_error_rate Actual failed-request ratio over the last 60 seconds\n'
                '# TYPE gateway_error_rate gauge\n'
                f'gateway_error_rate{{service="{SERVICE}"}} {error_rate}\n'
                '# HELP gateway_p99_seconds Actual rolling p99 request latency in seconds\n'
                '# TYPE gateway_p99_seconds gauge\n'
                f'gateway_p99_seconds{{service="{SERVICE}"}} {p99}\n'
                '# HELP gateway_requests_last_60s Actual requests in rolling 60-second window\n'
                '# TYPE gateway_requests_last_60s gauge\n'
                f'gateway_requests_last_60s{{service="{SERVICE}"}} {total}\n'
                '# HELP gateway_degraded Current gateway fault state (1=degraded)\n'
                '# TYPE gateway_degraded gauge\n'
                f'gateway_degraded{{service="{SERVICE}"}} {1 if bad else 0}\n'
            )
            self.respond(200, body.encode(), "text/plain; version=0.0.4")
        else:
            self.respond(404, {"error": "not found"})

    def do_POST(self):
        global degraded
        if self.path == "/charge":
            started = time.perf_counter()
            with lock:
                bad = degraded
            time.sleep(0.025)
            elapsed = time.perf_counter() - started
            record_request(bad, elapsed)
            self.respond(503 if bad else 200, {
                "service": SERVICE,
                "status": "rejected" if bad else "accepted",
                "test_environment": True,
                "request_duration_seconds": elapsed
            })
            return

        if self.path == "/remediate":
            expected = os.environ.get("GATEWAY_REMEDIATION_TOKEN", "")
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
                "operation": "restore_gateway_test_service",
                "previous_state": "degraded" if was_degraded else "healthy",
                "current_state": "healthy",
                "changed": was_degraded,
                "executed_at": datetime.now(timezone.utc).isoformat()
            })
            return
        self.respond(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        print("GATEWAY", fmt % args, flush=True)

if __name__ == "__main__":
    print(f"Starting {SERVICE}; local hackathon test service; initial degraded={degraded}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8082), Handler).serve_forever()
