import unittest
from app.core.postmortem import generate
from tests.unit.test_session5_gates import prepared, Rec, GOOD_SAMPLES, BAD_SAMPLES


class PostmortemV2(unittest.TestCase):
    def test_required_sections_present_and_copied_from_records(self):
        m = Rec(); eng, iid = prepared(m)
        pm, _ = eng.verify_recovery_for_incident(iid, lambda s, since: (GOOD_SAMPLES, {"source": "prometheus", "services": s}))
        for k in ("incident_id", "service", "environment", "detection_time", "timeline", "user_impact", "evidence_collected", "hypotheses",
                  "rejected_hypotheses", "remediation_and_approvals", "recovery_evidence", "outcome", "lessons_learned", "preventive_actions", "memory_retention"):
            self.assertIn(k, pm, k)
        self.assertEqual(pm["environment"], "production"); self.assertEqual(pm["detection_time"], pm["timeline"][0]["t"])
        self.assertEqual(pm["remediation_and_approvals"][0]["adapter"], "demo"); self.assertEqual(pm["recovery_evidence"]["independent_source"]["source"], "prometheus")
        self.assertEqual(pm["memory_retention"]["status"], "retained")

    def test_nothing_invented_lessons_and_prevention_stay_empty(self):
        eng, iid = prepared(Rec()); pm, _ = eng.verify_and_close(iid, GOOD_SAMPLES, False)
        self.assertEqual(pm["lessons_learned"], []); self.assertEqual(pm["preventive_actions"], [])

    def test_unverified_recovery_not_labelled_successful(self):
        eng, iid = prepared(Rec()); pm, _ = eng.verify_and_close(iid, BAD_SAMPLES, False)
        self.assertEqual(pm["outcome"], "not_recovered_or_unverified"); self.assertEqual(pm["root_cause"]["status"], "unverified_hypothesis")

    def test_hypotheses_never_promoted_without_causal_evidence(self):
        eng, iid = prepared(Rec()); pm, _ = eng.verify_and_close(iid, GOOD_SAMPLES, False)
        self.assertEqual(pm["root_cause"]["status"], "unverified_hypothesis")  # recovery + remediation alone do not confirm a cause

    def test_corrections_preserve_history(self):
        eng, iid = prepared(Rec()); eng.verify_and_close(iid, BAD_SAMPLES, False); first = eng.store.incidents[iid]["postmortem"]
        eng.verify_and_close(iid, GOOD_SAMPLES, False); inc = eng.store.incidents[iid]
        self.assertEqual(len(inc["postmortem_history"]), 1); self.assertEqual(inc["postmortem_history"][0]["outcome"], first["outcome"])
        self.assertNotEqual(inc["postmortem"]["outcome"], first["outcome"])


if __name__ == "__main__":
    unittest.main()
