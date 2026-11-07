"""Unit tests + MOCKED integration tests (local stub HTTP server standing in for Groq).
The stub tests verify OUR client's retry/timeout/parse behavior, NOT api.groq.com."""
import json, pathlib, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from app.core.events import ingest
from app.core.correlation import correlate
from app.core.investigation import investigate, validate, InvestigationError
from app.core.remediation import (ApprovalRequest, Adapter, AuthorizationError, VerificationCriteria,
                                  decide, execute, verify_recovery)
from app.core.postmortem import generate
from app.integrations.groq_client import GroqClient, LLMError

DEMO = json.loads((pathlib.Path(__file__).parents[3] / "demo" / "payment_pool_exhaustion.json").read_text())
EVS, _ = ingest(DEMO["events"], set())
MAIN = correlate(EVS, DEMO["dependencies"])[0]
IDS = [e.event_id for e in MAIN.events]


def llm_answer(ids, mem_idx=None):
    return {"summary": "s", "unknowns": [], "next_actions": [],
            "hypotheses": [{"cause": "pool exhaustion", "confidence": 0.7, "supporting_event_ids": ids,
                            "contradicting_event_ids": []}],
            "recommendations": [{"action": "raise pool size", "priority": "high", "reason": "r", "risk": "k",
                                 "verification": "v", "historical_memory_index": mem_idx}]}


def serve(responses):
    """responses: list of (status, body) served in order."""
    state = {"i": 0, "auth": []}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            state["auth"].append(self.headers.get("Authorization"))
            status, body = responses[min(state["i"], len(responses) - 1)]
            state["i"] += 1
            self.send_response(status); self.end_headers(); self.wfile.write(json.dumps(body).encode())
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


def ok(content):
    return (200, {"model": "stub-model", "choices": [{"message": {"content": json.dumps(content)}}]})


class InvestigationValidation(unittest.TestCase):
    def test_valid_hypothesis_kept_status_never_confirmed(self):
        r = validate(llm_answer(IDS[:2]), MAIN, 0)
        self.assertEqual(len(r["hypotheses"]), 1)
        self.assertEqual(r["status"], "hypothesis_only")

    def test_fabricated_event_id_dropped(self):
        r = validate(llm_answer(["deadbeef"]), MAIN, 0)
        self.assertEqual(r["hypotheses"], [])
        self.assertIn("unknown events", r["dropped_hypotheses"][0]["why"])

    def test_no_evidence_dropped(self):
        self.assertEqual(validate(llm_answer([]), MAIN, 0)["hypotheses"], [])

    def test_invalid_memory_reference_removed(self):
        r = validate(llm_answer(IDS[:1], mem_idx=5), MAIN, 1)
        self.assertIsNone(r["recommendations"][0]["historical_memory_index"])

    def test_missing_key_rejected(self):
        with self.assertRaises(InvestigationError):
            validate({"summary": "x"}, MAIN, 0)


class GroqClientStubTests(unittest.TestCase):
    def test_success_with_auth_header_and_model_reported(self):
        srv, st = serve([ok(llm_answer(IDS[:1]))])
        c = GroqClient("k123", url=f"http://127.0.0.1:{srv.server_port}/", backoff=0)
        res, meta = investigate(c, MAIN, [])
        self.assertEqual(meta["model"], "stub-model")
        self.assertEqual(st["auth"], ["Bearer k123"])
        self.assertEqual(len(res["hypotheses"]), 1)

    def test_retries_transient_then_succeeds(self):
        srv, st = serve([(503, {}), (429, {}), ok(llm_answer(IDS[:1]))])
        c = GroqClient("k", url=f"http://127.0.0.1:{srv.server_port}/", backoff=0)
        _, meta = c.chat_json("s", "u")
        self.assertEqual(meta["attempts"], 3)

    def test_non_retryable_fails_fast(self):
        srv, st = serve([(401, {})])
        c = GroqClient("bad", url=f"http://127.0.0.1:{srv.server_port}/", backoff=0)
        with self.assertRaises(LLMError):
            c.chat_json("s", "u")
        self.assertEqual(st["i"], 1)

    def test_gives_up_after_retries(self):
        srv, st = serve([(500, {})])
        c = GroqClient("k", url=f"http://127.0.0.1:{srv.server_port}/", backoff=0, max_retries=2)
        with self.assertRaises(LLMError):
            c.chat_json("s", "u")
        self.assertEqual(st["i"], 3)

    def test_unreachable_provider(self):
        c = GroqClient("k", url="http://127.0.0.1:1/", backoff=0, max_retries=1, timeout=1)
        with self.assertRaises(LLMError):
            c.chat_json("s", "u")

    def test_missing_key(self):
        import os
        os.environ.pop("GROQ_API_KEY", None)
        with self.assertRaises(LLMError):
            GroqClient()


def req():
    return ApprovalRequest("r1", "i1", "restart pool", "demo")


class RemediationTests(unittest.TestCase):
    def test_viewer_cannot_approve_and_is_audited(self):
        r = req()
        with self.assertRaises(AuthorizationError):
            decide(r, "bob", "viewer", True)
        self.assertEqual(r.state, "pending")
        self.assertEqual(r.audit[-1]["event"], "denied_unauthorized")

    def test_cannot_execute_unapproved_or_rejected(self):
        r = req()
        with self.assertRaises(ValueError):
            execute(r, {}, "x")
        decide(r, "ann", "sre", False, "too risky")
        self.assertEqual(r.state, "rejected")
        with self.assertRaises(ValueError):
            execute(r, {}, "x")

    def test_execute_labels_simulated(self):
        r = req(); decide(r, "ann", "sre", True)
        execute(r, {"demo": Adapter("demo", lambda: {"ok": True}, simulated=True)}, "ann")
        self.assertEqual(r.state, "executed")
        self.assertTrue(r.result["simulated"])

    def test_failed_execution_recorded_not_success(self):
        def boom(): raise RuntimeError("conn refused")
        r = req(); decide(r, "ann", "admin", True)
        execute(r, {"demo": Adapter("demo", boom, False)}, "ann")
        self.assertEqual(r.state, "execution_failed")

    def test_double_decision_blocked(self):
        r = req(); decide(r, "ann", "sre", True)
        with self.assertRaises(ValueError):
            decide(r, "ann", "sre", False)


class VerificationAndPostmortem(unittest.TestCase):
    good = {"error_rate": 0.001, "p99_ms": 300}
    bad = {"error_rate": 0.2, "p99_ms": 2400}

    def test_not_enough_samples(self):
        self.assertFalse(verify_recovery([self.good], VerificationCriteria(), True)["resolved"])

    def test_unstable_recovery_rejected(self):
        self.assertFalse(verify_recovery([self.good, self.bad, self.good], VerificationCriteria(), True)["resolved"])

    def test_stable_recovery_resolved_and_synthetic_flag_kept(self):
        r = verify_recovery([self.bad, self.good, self.good, self.good], VerificationCriteria(), True)
        self.assertTrue(r["resolved"]); self.assertTrue(r["synthetic"])

    def inc(self, resolved):
        return {"id": "i1", "timeline": MAIN.timeline(), "severity": "SEV-1", "synthetic": False,
                "investigation": validate(llm_answer(IDS[:1]), MAIN, 0),
                "verification": {"resolved": resolved},
                "approvals": [{"state": "executed"}, {"state": "rejected"}]}

    def test_unverified_root_cause_not_confirmed_and_not_retained(self):
        p = generate(self.inc(False))
        self.assertEqual(p["root_cause"]["status"], "unverified_hypothesis")
        self.assertFalse(p["retain_to_hindsight"])

    def test_verified_root_cause_confirmed(self):
        p = generate(self.inc(True))
        self.assertEqual(p["root_cause"]["status"], "confirmed")
        self.assertTrue(p["retain_to_hindsight"]); self.assertEqual(len(p["failed_attempts"]), 1)

    def test_synthetic_incident_is_never_retained_as_production_memory(self):
        inc = self.inc(True); inc["verification"] = {"resolved": True, "synthetic": True}
        p = generate(inc)
        self.assertFalse(p["retain_to_hindsight"])
        self.assertEqual(p["root_cause"]["status"], "unverified_hypothesis")


if __name__ == "__main__":
    unittest.main()
