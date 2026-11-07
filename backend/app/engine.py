"""Incident lifecycle orchestrator with durable-store support."""
from __future__ import annotations
import time, uuid
from datetime import datetime
from .core.correlation import correlate, IncidentCandidate
from .core.events import ingest, NormalizedEvent, signature_of
from .core.investigation import investigate
from .core.postmortem import generate
from .core.remediation import ApprovalRequest, VerificationCriteria, AuthorizationError, decide, execute, verify_recovery
from .core.severity import assess
from .core.safety import sanitize_untrusted


def _query(c):
    msgs = "; ".join(sorted({e.signature for e in c.events})[:6])
    return f"services={','.join(c.services)} env={c.environment} symptoms: {msgs}"

class Engine:
    def __init__(self, store, llm, memory, adapters, dependencies=None, critical_services=None,
                 criteria: VerificationCriteria | None = None):
        self.store, self.llm, self.memory, self.adapters = store, llm, memory, adapters
        self.deps, self.critical = dependencies or {}, set(critical_services or [])
        self.criteria = criteria or VerificationCriteria()
        self._candidates = {}

    def _get_incident(self, iid):
        if hasattr(self.store, "get_incident"): return self.store.get_incident(iid)
        return self.store.incidents[iid]

    def _save_incident(self, inc):
        if hasattr(self.store, "save_incident"): self.store.save_incident(inc)
        else: self.store.incidents[inc["id"]] = inc

    def _get_approval(self, rid):
        if hasattr(self.store, "get_approval"): return self.store.get_approval(rid)
        return self.store.approvals[rid]

    def _save_approval(self, req):
        if hasattr(self.store, "save_approval"): self.store.save_approval(req)
        else: self.store.approvals[req.id] = req

    def _candidate(self, iid):
        if iid in self._candidates: return self._candidates[iid]
        if not hasattr(self.store, "events_for_incident"): raise KeyError(iid)
        events = []
        for d in self.store.events_for_incident(iid):
            ts = d.get("timestamp", d.get("ts"))
            if isinstance(ts, str): ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            events.append(NormalizedEvent(event_id=d["event_id"], signature=signature_of(d["message"]),
                timestamp=ts, service=d["service"], environment=d["environment"], severity=d["severity"],
                message=d["message"], source=d["source"], metadata=d.get("metadata") or {},
                synthetic=d.get("synthetic", False), raw=d.get("raw") or {}))
        if not events: raise KeyError(iid)
        c = IncidentCandidate(events); self._candidates[iid] = c
        return c

    def ingest(self, batch):
        seen = self.store.seen_ids() if hasattr(self.store, "seen_ids") else self.store.seen
        events, rejected = ingest(batch, seen)
        ids = []
        for c in correlate(events, self.deps):
            sev = assess(c, self.critical); iid = uuid.uuid4().hex[:12]
            self._candidates[iid] = c
            inc = {"id": iid, "timeline": c.timeline(), "severity": sev.level,
                "severity_score": sev.score, "severity_reasons": sev.reasons, "severity_unknowns": sev.unknowns,
                "services": c.services, "synthetic": all(e.synthetic for e in c.events),
                "state": "open", "approvals": [], "investigation": None, "verification": None}
            if hasattr(self.store, "save_incident_with_events"):
                persisted = self.store.save_incident_with_events(inc, c.events)
                if not persisted:
                    rejected.append({"error": "concurrent duplicate event batch; retry this batch"})
                    continue
            else:
                if hasattr(self.store, "save_events"):
                    inserted = self.store.save_events(c.events)
                    if len(inserted) != len(c.events):
                        rejected.append({"error": "duplicate event batch; retry this batch"})
                        continue
                    self.store.save_events(c.events, iid)
                else:
                    self.store.seen.update(e.event_id for e in c.events)
                self._save_incident(inc)
            ids.append(iid)
        return ids, rejected

    def investigate(self, iid, use_memory=True):
        c, inc = self._candidate(iid), self._get_incident(iid)
        t0, memories, mem_err = time.monotonic(), [], None
        if use_memory:
            try:
                for item in self.memory.recall_similar(_query(c)):
                    clean, flagged = sanitize_untrusted(item.text, limit=3000)
                    memories.append(("[UNTRUSTED MEMORY; possible prompt injection] " if flagged else "") + clean)
            except Exception as exc: mem_err = f"memory recall failed: {type(exc).__name__}"
        result, meta = investigate(self.llm, c, memories)
        inc["investigation"] = {**result, "memories_used": memories, "memory_error": mem_err,
                                "use_memory": use_memory, "llm": meta,
                                "total_s": round(time.monotonic() - t0, 3)}
        inc["state"] = "investigated"; self._save_incident(inc)
        return inc["investigation"]

    def get_incident_approvals(self, iid):
        if hasattr(self.store, "approvals_for_incident"):
            return self.store.approvals_for_incident(iid)
        return [vars(a) if hasattr(a, "__dict__") else a
                for a in getattr(self.store, "approvals", {}).values()
                if getattr(a, "incident_id", None) == iid]

    def request_remediation(self, iid, rec_index, adapter):
        if adapter not in self.adapters: raise ValueError(f"adapter '{adapter}' not registered")
        inc = self._get_incident(iid)
        if inc.get("state") not in {"investigated", "remediation_pending"}: raise ValueError("incident must be investigated first")
        recs = (inc.get("investigation") or {}).get("recommendations", [])
        if not isinstance(rec_index, int) or rec_index < 0 or rec_index >= len(recs): raise ValueError("recommendation index out of range")
        req = ApprovalRequest(uuid.uuid4().hex[:10], iid, recs[rec_index]["action"], adapter)
        self._save_approval(req)
        inc["state"] = "remediation_pending"
        entry = {"id": req.id, "action": req.action, "adapter": req.adapter, "state": req.state,
                 "result": None, "simulated": getattr(self.adapters.get(adapter), "simulated", False)}
        inc.setdefault("approvals", []).append(entry)
        self._save_incident(inc)
        return req

    def decide(self, req_id, user, role, approve, reason=""):
        req = self._get_approval(req_id)
        try:
            decide(req, user, role, approve, reason)
        except AuthorizationError:
            self._save_approval(req)
            if hasattr(self.store, "append_audit"):
                self.store.append_audit(user, "approval_denied", {"request_id": req_id, "role": role})
            raise
        self._save_approval(req)
        inc = self._get_incident(req.incident_id)
        for a in inc.get("approvals", []):
            if a.get("id") == req.id:
                a["state"] = req.state
                a["reason"] = reason
        self._save_incident(inc)
        if hasattr(self.store, "append_audit"): self.store.append_audit(user, "approval_decision", {"request_id": req_id, "state": req.state, "reason": reason})
        return req

    def execute(self, req_id, user):
        req = self._get_approval(req_id); req = execute(req, self.adapters, user); self._save_approval(req)
        inc = self._get_incident(req.incident_id)
        entry = {"id": req.id, "action": req.action, "adapter": req.adapter, "state": req.state,
                 "result": req.result, "simulated": bool((req.result or {}).get("simulated"))}
        existing = inc.setdefault("approvals", [])
        updated = False
        for i, a in enumerate(existing):
            if a.get("id") == req.id:
                existing[i] = entry
                updated = True
                break
        if not updated:
            existing.append(entry)
        self._save_incident(inc)
        if hasattr(self.store, "append_audit"): self.store.append_audit(user, "remediation_execution", entry)
        return req

    def verify_and_close(self, iid, samples, synthetic):
        inc = self._get_incident(iid)
        inc["verification"] = verify_recovery(samples, self.criteria, synthetic)
        inc["state"] = "resolved" if inc["verification"]["resolved"] else "unresolved"
        pm = generate(inc); inc["postmortem"] = pm; self._save_incident(inc)
        retained = None
        if pm["retain_to_hindsight"]:
            retained = self.memory.retain_post_mortem(iid, _pm_text(inc, pm), verified=True)
        return pm, retained


def _pm_text(inc, pm):
    acts = "; ".join(a["action"] for a in pm["resolution"]) or "none"
    return (f"Incident {inc['id']} {inc['severity']} services={','.join(inc['services'])}. "
            f"Confirmed root cause: {pm['root_cause']['text']}. Resolution executed: {acts}. "
            f"Recovery verified: {pm['verification']['reason']}. Synthetic={pm['synthetic_data']}.")
