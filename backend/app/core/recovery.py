"""Trusted recovery evidence from a server-configured Prometheus-compatible endpoint."""
from __future__ import annotations
import json, math, os, time, urllib.parse, urllib.request

class RecoveryMetricsError(RuntimeError): pass

class PrometheusRecoveryVerifier:
    def __init__(self, base_url=None, error_query=None, p99_query=None, token=None,
                 timeout=8.0, step_s=15, samples=3):
        self.base_url = (base_url or os.environ.get("PROMETHEUS_BASE_URL", "")).rstrip("/")
        self.error_query = error_query or os.environ.get("RECOVERY_ERROR_RATE_QUERY", "")
        self.p99_query = p99_query or os.environ.get("RECOVERY_P99_SECONDS_QUERY", "")
        self.token = token or os.environ.get("PROMETHEUS_TOKEN", "")
        self.timeout, self.step_s, self.samples = timeout, step_s, samples
        if not self.base_url or not self.error_query or not self.p99_query:
            raise RecoveryMetricsError("trusted recovery metrics are not configured")
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "prometheus"}:
            raise RecoveryMetricsError("PROMETHEUS_BASE_URL must use HTTPS outside localhost")

    def _query_range(self, query, end):
        start = end - self.step_s * (self.samples - 1)
        params = urllib.parse.urlencode({"query": query, "start": start, "end": end, "step": self.step_s})
        req = urllib.request.Request(self.base_url + "/api/v1/query_range?" + params,
            headers={"Authorization": f"Bearer {self.token}"} if self.token else {})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read())
        except Exception as exc:
            raise RecoveryMetricsError(f"metrics provider request failed: {type(exc).__name__}") from exc
        if payload.get("status") != "success": raise RecoveryMetricsError("metrics provider returned non-success status")
        results = payload.get("data", {}).get("result", [])
        if len(results) != 1: raise RecoveryMetricsError("each recovery query must return exactly one time series")
        vals = results[0].get("values", [])
        parsed = []
        for pair in vals:
            try:
                ts, val = float(pair[0]), float(pair[1])
                if math.isfinite(ts) and math.isfinite(val): parsed.append((ts, val))
            except (TypeError, ValueError, IndexError): continue
        if len(parsed) < self.samples: raise RecoveryMetricsError(f"need {self.samples} metric samples; got {len(parsed)}")
        return parsed[-self.samples:]

    def collect(self):
        end = time.time()
        errors = self._query_range(self.error_query, end)
        p99 = self._query_range(self.p99_query, end)
        if any(v < 0 or v > 1 for _, v in errors): raise RecoveryMetricsError("error-rate query must return ratios in [0,1]")
        if any(v < 0 for _, v in p99): raise RecoveryMetricsError("p99 query must return non-negative seconds")
        if [x[0] for x in errors] != [x[0] for x in p99]:
            raise RecoveryMetricsError("error-rate and p99 sample timestamps do not align")
        return [{"timestamp": errors[i][0], "error_rate": errors[i][1], "p99_ms": p99[i][1] * 1000.0}
                for i in range(self.samples)]
