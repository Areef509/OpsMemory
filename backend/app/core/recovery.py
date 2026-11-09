"""Trusted recovery evidence from a server-configured Prometheus-compatible endpoint."""
from __future__ import annotations
import json, math, os, time, urllib.parse, urllib.request

class RecoveryMetricsError(RuntimeError): pass

class PrometheusRecoveryVerifier:
    def __init__(self, base_url=None, error_query=None, p99_query=None,
                 traffic_query=None, degraded_query=None, token=None,
                 timeout=8.0, step_s=15, samples=3):
        self.base_url = (base_url or os.environ.get("PROMETHEUS_BASE_URL", "")).rstrip("/")
        self.error_query = error_query or os.environ.get("RECOVERY_ERROR_RATE_QUERY", "")
        self.p99_query = p99_query or os.environ.get("RECOVERY_P99_SECONDS_QUERY", "")
        self.traffic_query = traffic_query or ""
        self.degraded_query = degraded_query or ""
        self.token = token or os.environ.get("PROMETHEUS_TOKEN", "")
        self.timeout, self.step_s, self.samples = timeout, step_s, samples
        if not all((self.base_url,self.error_query,self.p99_query,self.traffic_query,self.degraded_query)):
            raise RecoveryMetricsError("recovery requires error, p99, traffic and degraded queries")
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme not in {"http","https"}:
            raise RecoveryMetricsError("PROMETHEUS_BASE_URL must use http or https")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost","127.0.0.1","prometheus"}:
            raise RecoveryMetricsError("PROMETHEUS_BASE_URL must use HTTPS outside localhost")

    @staticmethod
    def _labels_match(labels: dict, required: dict) -> bool:
        return bool(required) and all(labels.get(k) == v for k, v in required.items())

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
        self._last_labels = results[0].get("metric", {}) or {}
        vals = results[0].get("values", [])
        parsed = []
        for pair in vals:
            try:
                ts, val = float(pair[0]), float(pair[1])
                if math.isfinite(ts) and math.isfinite(val): parsed.append((ts, val))
            except (TypeError, ValueError, IndexError): continue
        if len(parsed) < self.samples: raise RecoveryMetricsError(f"need {self.samples} metric samples; got {len(parsed)}")
        return parsed[-self.samples:]

    def collect(self, since: float | None = None, required_labels: dict | None = None):
        end=time.time()
        queries={"error":self.error_query,"p99":self.p99_query,
                 "traffic":self.traffic_query,"degraded":self.degraded_query}
        series,labels={},{}
        for name,query in queries.items():
            series[name]=self._query_range(query,end)
            labels[name]=self._last_labels
        if required_labels is not None and not all(
            self._labels_match(labels[k],required_labels) for k in queries
        ):
            raise RecoveryMetricsError("metric series missing required service labels")
        timestamps=[x[0] for x in series["error"]]
        if any([x[0] for x in v] != timestamps for v in series.values()):
            raise RecoveryMetricsError("recovery metric timestamps do not align")
        if since is not None and any(x[0]<since for v in series.values() for x in v):
            raise RecoveryMetricsError("metric window pre-dates remediation")
        if end-timestamps[-1] > max(2*self.step_s,60):
            raise RecoveryMetricsError("metric samples are stale")
        if any(v<0 or v>1 for _,v in series["error"]):
            raise RecoveryMetricsError("error rate outside [0,1]")
        if any(v<0 for _,v in series["p99"]):
            raise RecoveryMetricsError("negative p99 latency")
        if any(v<1 for _,v in series["traffic"]):
            raise RecoveryMetricsError("no observed request traffic in every sample")
        if any(v!=0 for _,v in series["degraded"]):
            raise RecoveryMetricsError("service remains degraded")
        return [{
            "timestamp":timestamps[i],"error_rate":series["error"][i][1],
            "p99_ms":series["p99"][i][1]*1000,
            "request_count":series["traffic"][i][1],
            "degraded":series["degraded"][i][1]
        } for i in range(self.samples)]


# ---------------- per-service monitoring bindings (fail closed) ----------------
class BindingError(RecoveryMetricsError): pass


def load_bindings(raw: str | None = None) -> dict:
    """RECOVERY_BINDINGS_JSON = {"payment-gateway": {"error_query": "...", "p99_query": "...",
    "required_labels": {"service": "payment-gateway"}}}. Absent/invalid => no bindings => nothing can be verified."""
    raw = raw if raw is not None else os.environ.get("RECOVERY_BINDINGS_JSON", "")
    if not raw.strip(): return {}
    try: data = json.loads(raw)
    except json.JSONDecodeError as exc: raise BindingError("RECOVERY_BINDINGS_JSON is not valid JSON") from exc
    if not isinstance(data, dict): raise BindingError("RECOVERY_BINDINGS_JSON must be an object")
    for svc, b in data.items():
        if not isinstance(b,dict) or not all(b.get(k) for k in
            ("error_query","p99_query","traffic_query","degraded_query")):
            raise BindingError(f"binding for '{svc}' needs error, p99, traffic and degraded queries")
        if not isinstance(b.get("required_labels"), dict) or not b["required_labels"]:
            raise BindingError(f"binding for '{svc}' needs non-empty required_labels (series identity)")
        if any("opsmemory_" in b[k] for k in
            ("error_query","p99_query","traffic_query","degraded_query")):
            raise BindingError(f"binding for '{svc}' uses OpsMemory's own metrics")
    return data


def collect_for_services(services: list[str], since: float, bindings: dict | None = None, verifier_factory=None):
    """Collect aligned post-remediation samples for EVERY affected service; fail closed on gaps."""
    if not services:
        raise BindingError("no affected services were supplied for recovery verification")
    if len(set(services)) != len(services):
        raise BindingError("duplicate services supplied for recovery verification")
    bindings = load_bindings() if bindings is None else bindings
    unbound = [s for s in services if s not in bindings]
    if unbound:
        raise BindingError("no independent monitoring source is bound to: " + ", ".join(sorted(unbound)))

    per_service = {}
    for svc in services:
        b = bindings[svc]
        v = (verifier_factory or PrometheusRecoveryVerifier)(
            error_query=b["error_query"],p99_query=b["p99_query"],
            traffic_query=b["traffic_query"],degraded_query=b["degraded_query"]
        )
        per_service[svc] = v.collect(since=since, required_labels=b["required_labels"])

    # Never combine measurements from different time windows into one verdict.
    timestamps = {
        svc: [float(sample["timestamp"]) for sample in per_service[svc]]
        for svc in services
    }
    reference = timestamps[services[0]]
    if not reference:
        raise RecoveryMetricsError("monitoring returned no aligned samples")
    if any(sequence != reference for sequence in timestamps.values()):
        raise RecoveryMetricsError(
            "service monitoring sample timestamps do not align; recovery verification rejected"
        )

    merged = [
        {
            "timestamp": reference[i],
            "error_rate": max(per_service[s][i]["error_rate"] for s in services),
            "p99_ms": max(per_service[s][i]["p99_ms"] for s in services),
            "request_count": min(per_service[s][i]["request_count"] for s in services),
            "degraded": max(per_service[s][i]["degraded"] for s in services),
        }
        for i in range(len(reference))
    ]
    return merged, {
        "source": "prometheus",
        "services": sorted(services),
        "since": since,
        "queries": {
            s: {k:bindings[s][k] for k in
                ("error_query","p99_query","traffic_query","degraded_query")}
            for s in services
        },
    }
