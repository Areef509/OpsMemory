"""Adapter registry + engine enforcement. Adapters here are TEST DOUBLES for target-service operations (the registry
logic is what is verified); no real system is touched."""
import time, unittest
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from app.core.adapter_registry import AdapterRegistry, AdapterSpec, SpecError, check_preconditions, validate_spec
from app.core.remediation import AuthorizationError
from app.engine import Engine
from app.store_memory import InMemoryStore
from tests.unit.test_engine import DEMO, ok, llm_answer
from tests.unit.test_pipeline import serve
from app.integrations.groq_client import GroqClient

SPEC = AdapterSpec(id="payments-fixture-op", target_service="payment-api", permitted_action="reset_fixture_pool", required_role="sre",
    risk_level="low", timeout_s=2.0, idempotent=True,
    preconditions=("incident_service_matches_target", "incident_not_synthetic", "environment_in_scope", "incident_investigated"),
    environment_scope=("production",), rollback="documented rollback", audit_fields=("incident_id", "approver"), verification="independent metrics after execution")


class SpecValidation(unittest.TestCase):
    def bad(self, **kw):
        with self.assertRaises(SpecError, msg=str(kw)): validate_spec(replace(SPEC, **kw))

    def test_valid_spec_ok(self): validate_spec(SPEC)

    def test_rejections(self):
        self.bad(id="Bad ID!"); self.bad(target_service="*"); self.bad(target_service="a,b"); self.bad(target_service="")
        self.bad(required_role="viewer"); self.bad(risk_level="extreme"); self.bad(timeout_s=0); self.bad(timeout_s=9999)
        self.bad(environment_scope=()); self.bad(environment_scope=("*",)); self.bad(preconditions=("incident_service_matches_target",))
        self.bad(preconditions=("incident_service_matches_target", "environment_in_scope", "run_anything"))
        self.bad(rollback=" "); self.bad(verification=""); self.bad(audit_fields=()); self.bad(risk_level="high", required_role="sre")
        self.bad(dry_run=True, simulated=False); self.bad(approval_ttl_s=1)

    def test_no_command_or_url_field_exists(self):
        self.assertFalse(any(f in AdapterSpec.__dataclass_fields__ for f in ("command", "cmd", "shell", "url", "script")))

    def test_duplicate_registration_and_non_callable_rejected(self):
        r = AdapterRegistry(); r.register(SPEC, lambda: {})
        with self.assertRaises(SpecError): r.register(SPEC, lambda: {})
        with self.assertRaises(SpecError): AdapterRegistry().register(SPEC, "rm -rf /")

    def test_dry_run_is_labelled_simulated(self):
        r = AdapterRegistry(); r.register(replace(SPEC, id="dry-run-op", dry_run=True, simulated=True), lambda: {"x": 1})
        self.assertTrue(r.adapters()["dry-run-op"].simulated)

    def test_timeout_enforced(self):
        r = AdapterRegistry(); r.register(replace(SPEC, timeout_s=0.1), lambda: time.sleep(1))
        with self.assertRaises(TimeoutError): r.adapters()["payments-fixture-op"].run()


def build(spec=SPEC, env="production", services=None, synthetic=False, runs=None):
    events = [{**e, "environment": env, "synthetic": synthetic} for e in DEMO["events"] if e["service"] == "payment-api"]
    if services: events = [{**e, "service": services[0]} for e in events]
    reg = AdapterRegistry(); calls = runs if runs is not None else []
    reg.register(spec, lambda: calls.append(1) or {"note": "test-double op"})
    def mk(answer):
        srv, _ = serve([answer]); return GroqClient("k", url=f"http://127.0.0.1:{srv.server_port}/", backoff=0)
    mem = type("M", (), {"recall_similar": lambda s, q: []})()
    e0 = Engine(InMemoryStore(), mk(ok(llm_answer(["x"]))), mem, reg.adapters(), registry=reg)
    iid = e0.ingest(events)[0][0]; ev = [t["event_id"] for t in e0.store.incidents[iid]["timeline"]]
    eng = Engine(InMemoryStore(), mk(ok(llm_answer(ev[:1]))), mem, reg.adapters(), registry=reg)
    iid = eng.ingest(events)[0][0]; eng.investigate(iid); return eng, iid, calls


class EngineEnforcement(unittest.TestCase):
    def test_happy_path_executes_bound_adapter_once(self):
        eng, iid, runs = build(); r = eng.request_remediation(iid, 0, "payments-fixture-op")
        eng.decide(r.id, "ann", "sre", True); eng.execute(r.id, "ann"); self.assertEqual(len(runs), 1)
        self.assertTrue(r.expires_at and r.created_at)

    def test_cross_service_invocation_refused(self):
        eng, iid, runs = build(services=["checkout-service"])
        with self.assertRaisesRegex(ValueError, "cross-service"): eng.request_remediation(iid, 0, "payments-fixture-op")
        self.assertEqual(runs, [])

    def test_wrong_environment_refused(self):
        eng, iid, _ = build(env="staging")
        with self.assertRaisesRegex(ValueError, "not in adapter scope"): eng.request_remediation(iid, 0, "payments-fixture-op")

    def test_synthetic_incident_refused(self):
        eng, iid, _ = build(synthetic=True)
        with self.assertRaisesRegex(ValueError, "synthetic"): eng.request_remediation(iid, 0, "payments-fixture-op")

    def test_duplicate_open_request_refused(self):
        eng, iid, _ = build(); eng.request_remediation(iid, 0, "payments-fixture-op")
        with self.assertRaisesRegex(ValueError, "open approval request"): eng.request_remediation(iid, 0, "payments-fixture-op")

    def test_admin_only_adapter_rejects_sre_and_audits(self):
        eng, iid, runs = build(spec=replace(SPEC, risk_level="high", required_role="admin")); r = eng.request_remediation(iid, 0, "payments-fixture-op")
        with self.assertRaises(AuthorizationError): eng.decide(r.id, "ann", "sre", True)
        eng.decide(r.id, "root", "admin", True); self.assertEqual(r.state if False else eng._get_approval(r.id).state, "approved")

    def test_expired_request_cannot_be_approved_or_executed(self):
        eng, iid, runs = build(); r = eng.request_remediation(iid, 0, "payments-fixture-op")
        r.expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(); eng._save_approval(r)
        with self.assertRaisesRegex(ValueError, "expired"): eng.decide(r.id, "ann", "sre", True)
        self.assertEqual(eng._get_approval(r.id).state, "expired")
        r2 = eng.request_remediation(iid, 0, "payments-fixture-op"); eng.decide(r2.id, "ann", "sre", True)
        r2 = eng._get_approval(r2.id); r2.expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(); eng._save_approval(r2)
        with self.assertRaisesRegex(ValueError, "expired"): eng.execute(r2.id, "ann")
        self.assertEqual(runs, [])

    def test_rejected_and_unapproved_never_execute(self):
        eng, iid, runs = build(); r = eng.request_remediation(iid, 0, "payments-fixture-op")
        with self.assertRaises(ValueError): eng.execute(r.id, "ann")
        eng.decide(r.id, "ann", "sre", False, "no")
        with self.assertRaises(ValueError): eng.execute(r.id, "ann")
        self.assertEqual(runs, [])

    def test_viewer_cannot_approve(self):
        eng, iid, _ = build(); r = eng.request_remediation(iid, 0, "payments-fixture-op")
        with self.assertRaises(AuthorizationError): eng.decide(r.id, "bob", "viewer", True)

    def test_unregistered_adapter_name_refused(self):
        eng, iid, _ = build()
        with self.assertRaisesRegex(ValueError, "not registered"): eng.request_remediation(iid, 0, "rm -rf /")

    def test_target_rechecked_at_execution_time(self):
        eng, iid, runs = build(); r = eng.request_remediation(iid, 0, "payments-fixture-op"); eng.decide(r.id, "ann", "sre", True)
        inc = eng._get_incident(iid); inc["services"] = ["some-other-service"]; eng._save_incident(inc)
        with self.assertRaisesRegex(ValueError, "preconditions failed at execution"): eng.execute(r.id, "ann")
        self.assertEqual(runs, [])

    def test_all_decisions_and_executions_are_chained(self):
        from app.core.audit_chain import verify_chain
        eng, iid, _ = build(); r = eng.request_remediation(iid, 0, "payments-fixture-op")
        try: eng.decide(r.id, "bob", "viewer", True)
        except AuthorizationError: pass
        eng.decide(r.id, "ann", "sre", True); eng.execute(r.id, "ann")
        acts = [e["action"] for e in eng.store.chain.entries]
        self.assertEqual(acts, ["approval_denied", "approval_decision", "remediation_execution"]); self.assertTrue(verify_chain(eng.store.chain.entries)["ok"])


if __name__ == "__main__":
    unittest.main()
