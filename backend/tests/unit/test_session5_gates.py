"""Session-5 gates: independent-recovery binding, post-remediation window, no self-metrics, claimed execution,
failure-aware retention, structured Hindsight metadata. Prometheus = local stub HTTP server (tests our verifier
logic, NOT a real monitoring system). Memory = test doubles (NOT Hindsight)."""
import json, threading, time, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from app.core.recovery import (BindingError, PrometheusRecoveryVerifier, RecoveryMetricsError,
                               collect_for_services, load_bindings)
from app.core.remediation import Adapter
from app.engine import VerificationBlocked
from app.integrations.hindsight_memory import HindsightMemory
from tests.unit.test_engine import DEMO, make, ok, llm_answer


def prom_server(labels, offset=0):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            now = int(time.time()) - offset
            ts = [now - 30, now - 15, now]
            v = [[str(t), "0.001"] for t in ts]
            if "p99" in self.path: v = [[str(t), "0.2"] for t in ts]
            elif "traffic" in self.path: v = [[str(t), "5"] for t in ts]
            elif "degraded" in self.path: v = [[str(t), "0"] for t in ts]
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({"status": "success", "data": {"result": [{"metric": labels, "values": v}]}}).encode())
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


BIND = {"payment-gateway": {"error_query": "err", "p99_query": "p99", "traffic_query": "traffic", "degraded_query": "degraded", "required_labels": {"service": "payment-gateway"}}}


def factory(srv):
    return lambda **kw: PrometheusRecoveryVerifier(f"http://127.0.0.1:{srv.server_port}", kw["error_query"], kw["p99_query"], kw["traffic_query"], kw["degraded_query"], samples=3)


class Bindings(unittest.TestCase):
    def test_empty_means_nothing_verifiable(self):
        self.assertEqual(load_bindings(""), {})
        with self.assertRaises(BindingError): collect_for_services(["payment-gateway"], 0, {})

    def test_invalid_bindings_rejected(self):
        for raw in ("{bad", "[]", json.dumps({"s": {"error_query": "e"}}),
                    json.dumps({"s": {"error_query": "e", "p99_query": "p", "required_labels": {}}}),
                    json.dumps({"s": {"error_query": "rate(opsmemory_http_requests_total[1m])", "p99_query": "p", "required_labels": {"a": "b"}}})):
            with self.assertRaises(BindingError, msg=raw): load_bindings(raw)

    def test_unbound_service_fails_closed_and_names_it(self):
        with self.assertRaisesRegex(BindingError, "payment-gateway"): collect_for_services(["payment-gateway"], 0, {"other": BIND["payment-gateway"]})

    def test_success_requires_matching_identity_and_postdating_window(self):
        srv = prom_server({"service": "payment-gateway"})
        merged, ev = collect_for_services(["payment-gateway"], time.time() - 120, BIND, factory(srv))
        self.assertEqual(len(merged), 3); self.assertEqual(ev["source"], "prometheus")

    def test_wrong_service_labels_rejected(self):
        srv = prom_server({"service": "opsmemory-backend"})
        with self.assertRaisesRegex(RecoveryMetricsError, "required service labels"):
            collect_for_services(["payment-gateway"], 0, BIND, factory(srv))

    def test_window_before_remediation_rejected(self):
        srv = prom_server({"service": "payment-gateway"})
        with self.assertRaisesRegex(RecoveryMetricsError, "pre-dates"):
            collect_for_services(["payment-gateway"], time.time() + 3600, BIND, factory(srv))

    def test_stale_samples_rejected(self):
        srv = prom_server({"service": "payment-gateway"}, offset=600)
        with self.assertRaisesRegex(RecoveryMetricsError, "stale"):
            collect_for_services(["payment-gateway"], 0, BIND, factory(srv))


GOOD_SAMPLES = [{"timestamp": 1.0, "error_rate": 0.001, "p99_ms": 200.0, "request_count": 5, "degraded": 0}] * 3
BAD_SAMPLES = [{"timestamp": 1.0, "error_rate": 0.3, "p99_ms": 3000.0, "request_count": 5, "degraded": 0}] * 3


class Rec:
    def __init__(self, fail=False): self.calls, self.fail = [], fail
    def recall_similar(self, q): return []
    def retain_post_mortem(self, iid, text, *, verified, resolved_by=None, extra_metadata=None):
        if self.fail: raise ConnectionError("hindsight unreachable")
        self.calls.append({"text": text, "verified": verified, "meta": extra_metadata}); return True


def prepared(mem, adapter_simulated=False, execute_it=True, synthetic=False):
    events = [{**e, "synthetic": synthetic} for e in DEMO["events"]]
    eng, _ = make(mem, None, [ok(llm_answer(["x"]))]); iid = eng.ingest(events)[0][0]
    ev = [t["event_id"] for t in eng.store.incidents[iid]["timeline"]]
    eng, _ = make(mem, None, [ok(llm_answer(ev[:1]))]); iid = eng.ingest(events)[0][0]; eng.investigate(iid)
    eng.adapters = {"demo": Adapter("demo", lambda: {"note": "test"}, adapter_simulated)}
    if execute_it:
        r = eng.request_remediation(iid, 0, "demo"); eng.decide(r.id, "ann", "sre", True); eng.execute(r.id, "ann")
    return eng, iid


class VerificationGate(unittest.TestCase):
    def test_blocked_without_any_executed_remediation(self):
        eng, iid = prepared(Rec(), execute_it=False)
        with self.assertRaisesRegex(VerificationBlocked, "no executed non-simulated remediation"):
            eng.verify_recovery_for_incident(iid, lambda s, since: (GOOD_SAMPLES, {"source": "prometheus"}))

    def test_blocked_when_only_simulated_remediation(self):
        eng, iid = prepared(Rec(), adapter_simulated=True)
        with self.assertRaises(VerificationBlocked):
            eng.verify_recovery_for_incident(iid, lambda s, since: (GOOD_SAMPLES, {}))

    def test_collector_gets_services_and_post_execution_since(self):
        m = Rec(); eng, iid = prepared(m); seen = {}
        def collector(services, since): seen.update(services=services, since=since); return GOOD_SAMPLES, {"source": "prometheus"}
        t0 = time.time(); eng.verify_recovery_for_incident(iid, collector)
        self.assertEqual(seen["services"], eng.store.incidents[iid]["services"]); self.assertLessEqual(seen["since"], t0)

    def test_verified_recovery_retained_with_structured_metadata(self):
        m = Rec(); eng, iid = prepared(m)
        pm, retained = eng.verify_recovery_for_incident(iid, lambda s, since: (GOOD_SAMPLES, {"source": "prometheus"}))
        c = m.calls[0]; self.assertTrue(c["verified"]); self.assertEqual(pm["root_cause"]["status"], "unverified_hypothesis")
        for k in ("service", "severity", "environment", "root_cause_status", "evidence_event_ids", "verification_source", "outcome", "retained_at"):
            self.assertIn(k, c["meta"], k)
        self.assertEqual(c["meta"]["environment"], "production"); self.assertEqual(c["meta"]["verification_source"], "prometheus")
        inc = eng.store.incidents[iid]; self.assertEqual(inc["state"], "resolved"); self.assertEqual(inc["memory_retention"]["status"], "retained")

    def test_failed_verification_retained_only_as_failed_attempt(self):
        m = Rec(); eng, iid = prepared(m)
        pm, _ = eng.verify_recovery_for_incident(iid, lambda s, since: (BAD_SAMPLES, {"source": "prometheus"}))
        self.assertEqual(len(m.calls), 1); self.assertFalse(m.calls[0]["verified"]); self.assertIn("FAILED RESOLUTION ATTEMPT", m.calls[0]["text"])
        self.assertNotIn("Confirmed root cause", m.calls[0]["text"]); self.assertEqual(pm["root_cause"]["status"], "unverified_hypothesis")
        self.assertEqual(eng.store.incidents[iid]["state"], "unresolved")

    def test_synthetic_incident_never_retained(self):
        m = Rec(); eng, iid = prepared(m, synthetic=True)
        eng.verify_and_close(iid, GOOD_SAMPLES, synthetic=True); self.assertEqual(m.calls, [])

    def test_retention_failure_visible_not_claimed(self):
        m = Rec(fail=True); eng, iid = prepared(m)
        _, retained = eng.verify_recovery_for_incident(iid, lambda s, si: (GOOD_SAMPLES, {"source": "prometheus"}))
        self.assertIsNone(retained); r = eng.store.incidents[iid]["memory_retention"]
        self.assertEqual(r["status"], "failed"); self.assertIn("unreachable", r["error"])

    def test_recovery_without_remediation_does_not_confirm_root_cause(self):
        from app.core.postmortem import generate
        inc = {"id": "i", "timeline": [], "severity": "SEV-2", "synthetic": False, "approvals": [],
               "verification": {"resolved": True}, "investigation": {"hypotheses": [{"cause": "c"}]}}
        pm = generate(inc); self.assertEqual(pm["root_cause"]["status"], "unverified_hypothesis")
        self.assertTrue(pm["recovery_observed"]); self.assertFalse(pm["retain_to_hindsight"])


class ExecutionIdempotency(unittest.TestCase):
    def test_duplicate_execute_rejected_and_adapter_runs_once(self):
        eng, iid = prepared(Rec(), execute_it=False); runs = []
        eng.adapters = {"demo": Adapter("demo", lambda: runs.append(1) or {"ok": 1}, False)}
        r = eng.request_remediation(iid, 0, "demo"); eng.decide(r.id, "ann", "sre", True)
        eng.execute(r.id, "ann")
        with self.assertRaises(ValueError): eng.execute(r.id, "ann")
        self.assertEqual(len(runs), 1)

    def test_concurrent_execute_runs_adapter_once(self):
        eng, iid = prepared(Rec(), execute_it=False); runs = []
        def slow(): runs.append(1); time.sleep(0.2); return {"ok": 1}
        eng.adapters = {"demo": Adapter("demo", slow, False)}
        r = eng.request_remediation(iid, 0, "demo"); eng.decide(r.id, "ann", "sre", True); errs = []
        def go():
            try: eng.execute(r.id, "ann")
            except ValueError as e: errs.append(e)
        ts = [threading.Thread(target=go) for _ in range(4)]; [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(len(runs), 1); self.assertEqual(len(errs), 3)

    def test_executed_entry_records_actor_and_time(self):
        eng, iid = prepared(Rec()); a = eng.store.incidents[iid]["approvals"][0]
        self.assertEqual(a["executed_by"], "ann"); self.assertTrue(a["executed_at"]); self.assertIs(a["simulated"], False)


class HindsightMetadata(unittest.TestCase):
    def test_extra_metadata_is_stringified_and_cannot_override_integrity_fields(self):
        class C:
            def retain(self, **kw): self.kw = kw; return {"ok": True}
        h = HindsightMemory(base_url="http://example.invalid", bank_id="b"); c = C(); h._get_client = lambda: c
        h.retain_post_mortem("i1", "t", verified=False, extra_metadata={"verified": "true", "incident_id": "evil", "confidence": 0.7})
        m = c.kw["metadata"]; self.assertEqual(m["verified"], "false"); self.assertEqual(m["incident_id"], "i1"); self.assertEqual(m["confidence"], "0.7")
        self.assertTrue(c.kw["content"].startswith("[UNVERIFIED"))



class MultiServiceAlignment(unittest.TestCase):
    def run_services(self, timestamps):
        services = ["payment-gateway", "inventory-api"]
        bindings = {
            svc: {
                "error_query": svc,
                "p99_query": svc,
                "traffic_query": svc,
                "degraded_query": svc,
                "required_labels": {"service": svc},
            }
            for svc in services
        }

        class Verifier:
            def __init__(self, svc):
                self.svc = svc

            def collect(self, since=None, required_labels=None):
                return [
                    {"timestamp": ts, "error_rate": rate, "p99_ms": latency, "request_count": 5, "degraded": 0}
                    for ts, rate, latency in timestamps[self.svc]
                ]

        def factory(**kwargs):
            return Verifier(kwargs["error_query"])

        return collect_for_services(services, 0, bindings, factory)

    def test_misaligned_service_timestamps_rejected(self):
        timestamps = {
            "payment-gateway": [(100, .01, 200), (115, .01, 200), (130, .01, 200)],
            "inventory-api": [(100, .02, 300), (115, .02, 300), (131, .02, 300)],
        }
        with self.assertRaisesRegex(RecoveryMetricsError, "timestamps do not align"):
            self.run_services(timestamps)

    def test_aligned_services_merge_worst_measurements(self):
        timestamps = {
            "payment-gateway": [(100, .01, 200), (115, .01, 200), (130, .01, 200)],
            "inventory-api": [(100, .02, 300), (115, .03, 400), (130, .02, 350)],
        }
        merged, evidence = self.run_services(timestamps)
        self.assertEqual([x["timestamp"] for x in merged], [100, 115, 130])
        self.assertEqual([x["error_rate"] for x in merged], [.02, .03, .02])
        self.assertEqual([x["p99_ms"] for x in merged], [300, 400, 350])
        self.assertEqual(evidence["source"], "prometheus")

if __name__ == "__main__":
    unittest.main()


class ABExperimentFailClosed(unittest.TestCase):
    """The experiment must refuse to touch the production bank and must never report success when blocked."""
    def _load(self):
        import importlib.util, pathlib
        path = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "run_ab_experiment.py"
        spec = importlib.util.spec_from_file_location("ab_exp", path); m = importlib.util.module_from_spec(spec)
        import sys; sys.path.insert(0, str(path.parents[1] / "backend")); spec.loader.exec_module(m); return m

    def test_refuses_when_experiment_bank_equals_production_bank(self):
        import os; from unittest.mock import patch
        m = self._load(); written = []
        env = {"GROQ_API_KEY": "x", "HINDSIGHT_BASE_URL": "http://127.0.0.1:1", "HINDSIGHT_BANK_ID": "opsmemory", "HINDSIGHT_EXPERIMENT_BANK_ID": "OpsMemory"}
        with patch.dict(os.environ, env), patch.object(m, "_write_status", lambda st, reason, **kw: written.append(st)):
            self.assertEqual(m.run_experiment(), 2)
        self.assertEqual(written, ["REFUSED"])

    def test_missing_config_is_nonzero(self):
        import os; from unittest.mock import patch
        m = self._load()
        with patch.dict(os.environ, {"GROQ_API_KEY": "", "HINDSIGHT_BASE_URL": ""}):
            self.assertNotEqual(m.run_experiment(), 0)
