import json, pathlib, unittest
from app.core.events import ingest, normalize, redact, ValidationError
from app.core.correlation import correlate
from app.core.severity import assess

DEMO = json.loads((pathlib.Path(__file__).parents[3] / "demo" / "payment_pool_exhaustion.json").read_text())


def _cands():
    evs, rej = ingest(DEMO["events"], set())
    assert not rej
    return evs, correlate(evs, DEMO["dependencies"])


class IngestTests(unittest.TestCase):
    def test_missing_fields_rejected(self):
        _, rej = ingest([{"service": "x"}], set())
        self.assertEqual(len(rej), 1)

    def test_bad_severity_and_timestamp(self):
        with self.assertRaises(ValidationError):
            normalize({**DEMO["events"][0], "severity": "loud"})
        with self.assertRaises(ValidationError):
            normalize({**DEMO["events"][0], "timestamp": "yesterday"})

    def test_idempotent(self):
        seen = set()
        a, _ = ingest(DEMO["events"], seen)
        b, _ = ingest(DEMO["events"], seen)
        self.assertEqual(len(a), 7)
        self.assertEqual(b, [])

    def test_redaction(self):
        self.assertNotIn("hunter2", redact("login password=hunter2"))
        self.assertNotIn("bob@example.com", redact("mail bob@example.com"))
        self.assertIn("REDACTED", redact("Authorization: Bearer abc.def.ghi"))


class CorrelationTests(unittest.TestCase):
    def test_demo_groups_into_incident_plus_unrelated(self):
        _, cands = _cands()
        self.assertEqual(len(cands), 3)  # pool incident (09:00-09:02), later timeout (09:18), email-worker
        main = cands[0]
        self.assertEqual(main.services, ["checkout-service", "payment-api", "postgres-primary"])
        self.assertEqual(len(main.events), 5)

    def test_repeated_signature_detected(self):
        _, cands = _cands()
        self.assertEqual(list(cands[0].repeated.values()), [2])

    def test_dependency_required_for_cross_service_link(self):
        evs, _ = _cands()
        no_deps = correlate(evs, {})
        self.assertGreater(len(no_deps), 3)

    def test_environment_isolation(self):
        a = normalize({**DEMO["events"][0], "environment": "staging"})
        b = normalize(DEMO["events"][0] | {"timestamp": "2026-11-05T09:00:10Z"})
        self.assertEqual(len(correlate([a, b])), 2)


class SeverityTests(unittest.TestCase):
    def test_payment_incident_is_sev1_with_explanation(self):
        _, cands = _cands()
        r = assess(cands[0], set(DEMO["critical_services"]))
        self.assertEqual(r.level, "SEV-1")
        self.assertTrue(any("users_affected=1800" in x for x in r.reasons))

    def test_unrelated_low_signal_is_sev3_or_lower_and_lists_unknowns(self):
        _, cands = _cands()
        r = assess(cands[2], set(DEMO["critical_services"]))
        self.assertIn(r.level, {"SEV-3", "SEV-4"})
        self.assertIn("users_affected not reported", r.unknowns)


if __name__ == "__main__":
    unittest.main()
