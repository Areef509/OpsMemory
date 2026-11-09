"""Tamper-evidence tests on a DISPOSABLE in-memory chain / temp JSON exports (never a real audit table).
Not covered here (needs PostgreSQL): the SQL append path, the immutability trigger and role grants."""
import copy, json, os, sys, tempfile, threading, unittest
from pathlib import Path
from app.core.audit_chain import AuditChain, GENESIS, build_entry, entry_hash, verify_chain
from app.store_memory import InMemoryStore

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import verify_audit_chain as cli


def make(n=10):
    c = AuditChain()
    for i in range(n):
        c.append(f"user{i % 3}", "approval_decision" if i % 2 else "remediation_execution", resource_id=f"res{i}",
                 correlation_id=f"req-{i}", detail={"request_id": f"r{i}", "state": "approved", "n": i})
    return c


def kinds(report): return sorted({i["type"] for i in report["issues"]})


class ChainBasics(unittest.TestCase):
    def test_intact_chain_verifies_and_links(self):
        c = make(); r = verify_chain(c.entries)
        self.assertTrue(r["ok"], r); self.assertEqual(r["checked"], 10); self.assertEqual(c.entries[0]["prev_hash"], GENESIS)
        for a, b in zip(c.entries, c.entries[1:]): self.assertEqual(b["prev_hash"], a["hash"])

    def test_empty_chain_ok(self): self.assertTrue(verify_chain([])["ok"])

    def test_canonical_and_hash_are_deterministic(self):
        e = build_entry(1, GENESIS, "a", "x", "r", "c", {"b": 1, "a": 2}, ts="2026-01-01T00:00:00+00:00")
        e2 = build_entry(1, GENESIS, "a", "x", "r", "c", {"a": 2, "b": 1}, ts="2026-01-01T00:00:00+00:00")
        self.assertEqual(e["hash"], e2["hash"]); self.assertEqual(e["hash"], entry_hash(GENESIS, e["canonical"]))


class TamperDetection(unittest.TestCase):
    def setUp(self): self.good = make().entries

    def test_modified_column_detected(self):
        t = copy.deepcopy(self.good); t[4]["actor"] = "mallory"
        r = verify_chain(t); self.assertFalse(r["ok"]); self.assertIn("field_differs_from_signed_record", kinds(r)); self.assertEqual(r["issues"][0]["seq"], 5)

    def test_modified_detail_detected(self):
        t = copy.deepcopy(self.good); t[2]["detail"]["state"] = "rejected"
        self.assertIn("field_differs_from_signed_record", kinds(verify_chain(t)))

    def test_modified_signed_record_without_rehash_detected(self):
        t = copy.deepcopy(self.good); rec = json.loads(t[3]["canonical"]); rec["actor"] = "mallory"
        t[3]["canonical"] = json.dumps(rec, sort_keys=True, separators=(",", ":")); t[3]["actor"] = "mallory"
        self.assertIn("hash_mismatch", kinds(verify_chain(t)))

    def test_modified_and_rehashed_entry_breaks_next_link(self):
        t = copy.deepcopy(self.good); t[3]["actor"] = "mallory"
        fields = {k: t[3][k] for k in ("seq", "ts", "actor", "action", "resource_id", "correlation_id", "detail", "prev_hash")}
        from app.core.audit_chain import canonical
        t[3]["canonical"] = canonical(fields); t[3]["hash"] = entry_hash(t[3]["prev_hash"], t[3]["canonical"])
        r = verify_chain(t); self.assertFalse(r["ok"]); self.assertIn("chain_break", kinds(r)); self.assertEqual([i["seq"] for i in r["issues"]], [5])

    def test_deleted_middle_entry_detected(self):
        t = copy.deepcopy(self.good); del t[5]
        r = verify_chain(t); self.assertFalse(r["ok"]); self.assertIn("sequence_gap_or_reorder", kinds(r)); self.assertIn("chain_break", kinds(r))

    def test_reordered_entries_detected(self):
        t = copy.deepcopy(self.good); t[2], t[3] = t[3], t[2]
        self.assertIn("sequence_gap_or_reorder", kinds(verify_chain(t)))

    def test_invalid_hash_detected(self):
        t = copy.deepcopy(self.good); t[1]["hash"] = "not-a-hash"
        self.assertIn("invalid_hash_format", kinds(verify_chain(t)))

    def test_inserted_forged_entry_detected(self):
        t = copy.deepcopy(self.good); forged = build_entry(5, t[3]["hash"], "mallory", "approval_decision", "x", None, {"state": "approved"})
        t.insert(4, forged); self.assertFalse(verify_chain(t)["ok"])

    def test_tail_truncation_needs_external_anchor(self):
        c = make(); head, count = c.head, len(c.entries); t = copy.deepcopy(c.entries)[:-3]
        self.assertTrue(verify_chain(t)["ok"], "documented limitation: chain alone cannot see removal of the newest rows")
        r = verify_chain(t, expect_head=head, expect_count=count); self.assertFalse(r["ok"]); self.assertEqual(kinds(r), ["count_mismatch", "head_mismatch"])

    def test_full_chain_recomputation_needs_external_anchor(self):
        anchor = make().head; forged = AuditChain()
        for i in range(10): forged.append("mallory", "rewrite", detail={"n": i})
        self.assertTrue(verify_chain(forged.entries)["ok"], "documented limitation")
        self.assertFalse(verify_chain(forged.entries, expect_head=anchor)["ok"])


class Cli(unittest.TestCase):
    def _export(self, entries):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(entries, f); f.close(); self.addCleanup(os.unlink, f.name); return f.name

    def test_exit_codes(self):
        good = make().entries
        self.assertEqual(cli.main(["--json", self._export(good)]), 0)
        t = copy.deepcopy(good); t[0]["detail"]["n"] = 99
        self.assertEqual(cli.main(["--json", self._export(t)]), 1)
        self.assertEqual(cli.main(["--json", "/nonexistent/path.json"]), 2)
        self.assertEqual(cli.main(["--json", self._export(good), "--expect-count", "11"]), 1)


class StoreIntegration(unittest.TestCase):
    def test_in_memory_store_chains_every_audit_event(self):
        s = InMemoryStore(); s.append_audit("ann", "approval_decision", {"request_id": "r1", "state": "approved"})
        s.append_audit("ann", "remediation_execution", {"id": "r1", "state": "executed"})
        self.assertEqual([e["resource_id"] for e in s.chain.entries], ["r1", "r1"]); self.assertTrue(verify_chain(s.chain.entries)["ok"])
        self.assertEqual(len(s.audit), 2)

    def test_concurrent_appends_keep_one_linear_chain(self):
        s = InMemoryStore()
        def w(k):
            for i in range(25): s.append_audit(f"u{k}", "x", {"i": i})
        ts = [threading.Thread(target=w, args=(k,)) for k in range(8)]; [t.start() for t in ts]; [t.join() for t in ts]
        r = verify_chain(s.chain.entries); self.assertTrue(r["ok"], r); self.assertEqual(r["checked"], 200)

    def test_engine_lifecycle_events_land_in_chain(self):
        from tests.unit.test_engine import DEMO, make as mk, ok, llm_answer
        eng, _ = mk(type("M", (), {"recall_similar": lambda s, q: []})(), None, [ok(llm_answer(["x"]))])
        iid = eng.ingest([{**e, "synthetic": False} for e in DEMO["events"]])[0][0]
        ev = [t["event_id"] for t in eng.store.incidents[iid]["timeline"]]
        eng, _ = mk(type("M", (), {"recall_similar": lambda s, q: []})(), None, [ok(llm_answer(ev[:1]))])
        iid = eng.ingest([{**e, "synthetic": False} for e in DEMO["events"]])[0][0]; eng.investigate(iid)
        r = eng.request_remediation(iid, 0, "demo"); eng.decide(r.id, "ann", "sre", True); eng.execute(r.id, "ann")
        actions = [e["action"] for e in eng.store.chain.entries]
        self.assertIn("approval_decision", actions); self.assertIn("remediation_execution", actions)
        self.assertTrue(verify_chain(eng.store.chain.entries)["ok"])


if __name__ == "__main__":
    unittest.main()
