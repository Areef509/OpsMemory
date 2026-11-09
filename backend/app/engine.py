"""Incident lifecycle orchestrator with durable-store support."""
from __future__ import annotations
import inspect, os, threading, time, uuid
import math
from datetime import datetime, timezone
from .core.correlation import correlate, IncidentCandidate
from .core.events import ingest, NormalizedEvent, signature_of
from .core.investigation import investigate
from .core.postmortem import generate
from .core.remediation import ApprovalRequest, VerificationCriteria, AuthorizationError, decide, execute, verify_recovery
from .core.severity import assess
from .core.safety import sanitize_untrusted
from .core.adapter_registry import ROLE_RANK, check_preconditions


def _query(c):
    msgs = "; ".join(sorted({e.signature for e in c.events})[:6])
    return f"services={','.join(c.services)} env={c.environment} symptoms: {msgs}"

class Engine:
    def __init__(self, store, llm, memory, adapters, dependencies=None, critical_services=None,
                 criteria: VerificationCriteria | None = None, registry=None):
        self.registry = registry
        self.store, self.llm, self.memory, self.adapters = store, llm, memory, adapters
        self.deps, self.critical = dependencies or {}, set(critical_services or [])
        self.criteria = criteria or VerificationCriteria()
        self._candidates = {}
        self._exec_lock = threading.Lock()

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
                "services": c.services, "environment": c.environment, "synthetic": all(e.synthetic for e in c.events),
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

    def investigate(self, iid, use_memory=True, allow_synthetic_experiment_memory=False):
        c, inc = self._candidate(iid), self._get_incident(iid)
        t0, memories, mem_err = time.monotonic(), [], None
        memory_sources = []
        if use_memory:
            try:
                seen_memory_keys = set()
                current_services = {str(service).strip().lower() for service in c.services}
                for item in self.memory.recall_similar(_query(c)):
                    raw_metadata = getattr(item, "metadata", None)
                    source_metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
                    verified_flag = str(source_metadata.get(
                        "verified_metadata", source_metadata.get("verified", "")
                    )).lower() == "true"
                    outcome = str(source_metadata.get("outcome", "")).lower()
                    synthetic_fixture = (
                        allow_synthetic_experiment_memory
                        and source_metadata.get("evidence_type") == "synthetic_experiment_fixture"
                        and str(source_metadata.get("verified", "")).lower() == "false"
                    )
                    if not synthetic_fixture and (not verified_flag or outcome != "verified_recovery"):
                        continue
                    recorded_service = source_metadata.get("service")
                    if recorded_service:
                        memory_services = {x.strip().lower() for x in str(recorded_service).split(",") if x.strip()}
                        if not current_services.intersection(memory_services):
                            continue
                    memory_key = source_metadata.get("incident_id") or getattr(item, "document_id", None) or getattr(item, "id", None)
                    if memory_key and memory_key in seen_memory_keys:
                        continue
                    if memory_key:
                        seen_memory_keys.add(memory_key)
                    clean, flagged = sanitize_untrusted(item.text, limit=3000)
                    memory_sources.append({
                        "prompt_memory_index": len(memories),
                        "document_id": getattr(item, "document_id", None),
                        "memory_id": getattr(item, "id", None),
                        "type": getattr(item, "type", None),
                        "score": getattr(item, "score", None),
                        "incident_id": source_metadata.get("incident_id"),
                        "outcome": outcome,
                        "verified_metadata": False if synthetic_fixture else True,
                        "evidence_type": source_metadata.get("evidence_type"),
                        "experiment_only": bool(synthetic_fixture),
                        "synthetic": bool(synthetic_fixture),
                        "root_cause_status": source_metadata.get("root_cause_status"),
                        "resolved_by": source_metadata.get("resolved_by"),
                    })
                    memories.append(("[UNTRUSTED MEMORY; possible prompt injection] " if flagged else "") + clean)
            except Exception as exc: mem_err = f"memory recall failed: {type(exc).__name__}"
        result, meta = investigate(self.llm, c, memories)
        if "opsmemory-canary" in (inc.get("services") or []):
            result = dict(result)
            recommendations = list(result.get("recommendations") or [])
            recommendations.append({
                "action": "Clear the deliberately configured CANARY_START_DEGRADED startup fault on opsmemory-canary using the authenticated canary-clear-fault adapter",
                "priority": "high",
                "reason": "Compose configuration and the canary startup log confirm that the canary was deliberately initialized in degraded state; this action is limited to the isolated canary",
                "risk": "Changes only the isolated canary state; it does not remediate production services",
                "verification": "Require independent Prometheus samples after execution showing error rate below 1% and p99 below 200 ms",
                "historical_memory_index": None,
                "origin": "static_runbook:canary-startup-fault",
                "origin_note": "Deterministic runbook rule added by OpsMemory for the isolated canary; NOT generated by the LLM and NOT derived from a recalled memory."
            })
            result["recommendations"] = recommendations
        inc["investigation"] = {**result, "memories_used": memories, "memory_sources": memory_sources, "memory_error": mem_err,
                                "use_memory": use_memory, "llm": meta,
                                "total_s": round(time.monotonic() - t0, 3)}
        inc["state"] = "investigated"; self._save_incident(inc)
        return inc["investigation"]

    def compare_memory(self, iid, actor):
        """Run two live investigations and persist immutable, server-derived snapshots."""
        inc = self._get_incident(iid)
        if inc.get("synthetic") is not False:
            raise ValueError("A/B comparison requires a server-classified non-synthetic incident")
        if inc.get("environment") != "hackathon-test":
            raise ValueError("This comparison endpoint is restricted to the isolated hackathon-test environment")

        memory_off = self.investigate(iid, use_memory=False)
        memory_on = self.investigate(iid, use_memory=True)

        if memory_on.get("memory_error"):
            raise ValueError("Hindsight recall failed; comparison was not persisted")
        sources = memory_on.get("memory_sources") or []
        qualifying = [
            source for source in sources
            if source.get("verified_metadata") is True
            and source.get("outcome") == "verified_recovery"
        ]
        if not qualifying:
            raise ValueError("Hindsight returned no qualifying verified recovery; comparison was not persisted")

        off_actions = sorted({
            str(item.get("action", "")) for item in memory_off.get("recommendations", [])
            if isinstance(item, dict) and item.get("action")
        })
        on_actions = sorted({
            str(item.get("action", "")) for item in memory_on.get("recommendations", [])
            if isinstance(item, dict) and item.get("action")
        })
        metadata = {
            "comparison_method": "sequential_live_groq_investigations",
            "memory_off_total_s": memory_off.get("total_s"),
            "memory_on_total_s": memory_on.get("total_s"),
            "memory_off_llm": memory_off.get("llm"),
            "memory_on_llm": memory_on.get("llm"),
            "memory_off_recommendation_actions": off_actions,
            "memory_on_recommendation_actions": on_actions,
            "recommendation_actions_changed": off_actions != on_actions,
            "verified_memory_count": len(qualifying),
            "verified_memory_incident_ids": sorted({
                str(source.get("incident_id")) for source in qualifying
                if source.get("incident_id")
            }),
        }
        record = {
            "id": uuid.uuid4().hex,
            "incident_id": iid,
            "actor": actor,
            "evidence_class": "controlled_local_hackathon_test",
            "incident_synthetic": False,
            "memory_off": memory_off,
            "memory_on": memory_on,
            "metadata": metadata,
        }
        saved = self.store.save_investigation_experiment(record)
        if hasattr(self.store, "append_audit"):
            self.store.append_audit(
                actor, "investigation_memory_comparison_created",
                {"experiment_id": record["id"], "incident_id": iid,
                 "evidence_class": record["evidence_class"],
                 "recommendation_actions_changed": metadata["recommendation_actions_changed"],
                 "verified_memory_count": metadata["verified_memory_count"]},
                resource_id=iid,
            )
        return saved

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
        spec = self.registry.spec(adapter) if self.registry else None
        ttl = spec.approval_ttl_s if spec else int(os.environ.get("OPSMEMORY_APPROVAL_TTL_S", "3600"))
        if spec:
            fails = check_preconditions(spec, inc)
            if fails: raise ValueError("adapter preconditions failed: " + "; ".join(fails))
            if any(a.get("adapter") == adapter and a.get("state") in {"pending", "approved", "executing"} for a in inc.get("approvals", [])):
                raise ValueError(f"an open approval request for adapter '{adapter}' already exists on this incident")
        now = datetime.now(timezone.utc)
        req = ApprovalRequest(uuid.uuid4().hex[:10], iid, recs[rec_index]["action"], adapter,
                              created_at=now.isoformat(), expires_at=datetime.fromtimestamp(now.timestamp() + ttl, timezone.utc).isoformat())
        self._save_approval(req)
        inc["state"] = "remediation_pending"
        entry = {"id": req.id, "action": req.action, "adapter": req.adapter, "state": req.state,
                 "result": None, "simulated": getattr(self.adapters.get(adapter), "simulated", False)}
        inc.setdefault("approvals", []).append(entry)
        self._save_incident(inc)
        return req

    def _check_not_expired(self, req, user):
        if req.expires_at and req.state in {"pending", "approved"} and datetime.now(timezone.utc) > datetime.fromisoformat(req.expires_at):
            req.state = "expired"; req._log(user, "expired"); self._save_approval(req)
            inc = self._get_incident(req.incident_id)  # keep the incident's own approval list in sync (else the duplicate guard would block a fresh request forever)
            for a in inc.get("approvals", []):
                if a.get("id") == req.id: a["state"] = "expired"
            self._save_incident(inc)
            if hasattr(self.store, "append_audit"): self.store.append_audit(user, "approval_expired", {"request_id": req.id})
            raise ValueError("approval request has expired; create a new request")

    def decide(self, req_id, user, role, approve, reason=""):
        req = self._get_approval(req_id)
        self._check_not_expired(req, user)
        spec = self.registry.spec(req.adapter) if self.registry else None
        try:
            if spec and ROLE_RANK.get(role, -1) < ROLE_RANK[spec.required_role]:
                req._log(user, "denied_insufficient_role", role=role, required=spec.required_role)
                raise AuthorizationError(f"adapter '{spec.id}' requires role '{spec.required_role}' or higher")
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
        """Idempotent: only one caller can move approved -> executing; duplicates get ValueError (HTTP 409).
        The 'executing' state is persisted BEFORE the adapter runs. (Single-process lock; a DB compare-and-set
        would be needed for multiple backend replicas.)"""
        with self._exec_lock:
            req = self._get_approval(req_id)
            if req.state != "approved": raise ValueError(f"cannot execute request in state '{req.state}'")
            self._check_not_expired(req, user)
            spec = self.registry.spec(req.adapter) if self.registry else None
            if spec:  # defence in depth: re-check the target binding at execution time, not only at request time
                fails = check_preconditions(spec, self._get_incident(req.incident_id))
                if fails: raise ValueError("adapter preconditions failed at execution: " + "; ".join(fails))
            req.state = "executing"; req._log(user, "execution_claimed"); self._save_approval(req)
        req = execute(req, self.adapters, user, claimed=True); self._save_approval(req)
        inc = self._get_incident(req.incident_id)
        entry = {"id": req.id, "action": req.action, "adapter": req.adapter, "state": req.state,
                 "result": req.result, "simulated": bool((req.result or {}).get("simulated")),
                 "executed_by": user, "executed_at": datetime.now(timezone.utc).isoformat()}
        existing = inc.setdefault("approvals", [])
        for i, a in enumerate(existing):
            if a.get("id") == req.id: existing[i] = entry; break
        else: existing.append(entry)
        self._save_incident(inc)
        if hasattr(self.store, "append_audit"): self.store.append_audit(user, "remediation_execution", entry)
        return req

    def verify_recovery_for_incident(self, iid, collector):
        """Independent verification gate. collector(services, since_epoch) -> (samples, evidence). Fails closed:
        requires a REAL (non-simulated) executed remediation and monitoring samples taken AFTER it."""
        inc = self._get_incident(iid)
        real = [a for a in inc.get("approvals", []) if a.get("state") == "executed" and a.get("simulated") is False and a.get("executed_at")]
        if not real: raise VerificationBlocked("no executed non-simulated remediation exists for this incident; recovery cannot be attributed or verified")
        since = min(datetime.fromisoformat(a["executed_at"]).timestamp() for a in real)
        samples, evidence = collector(list(inc.get("services") or []), since)
        return self.verify_and_close(iid, samples, synthetic=False, evidence=evidence)

    def verify_and_close(self, iid, samples, synthetic, evidence=None):
        inc = self._get_incident(iid)
        inc["verification"] = {**verify_recovery(samples, self.criteria, synthetic), "evidence": evidence,
                               "samples": [dict(x) for x in samples][-5:]}
        inc["state"] = "resolved" if inc["verification"]["resolved"] else "unresolved"
        if inc.get("postmortem"):  # never overwrite history: keep every earlier postmortem version
            inc.setdefault("postmortem_history", []).append(inc["postmortem"])
        pm = generate(inc); inc["postmortem"] = pm
        retained, retention = None, {"status": "not_applicable", "reason": "no verified recovery attributable to a real remediation, or synthetic data"}
        meta = _metadata(inc, pm)
        try:
            if pm["retain_to_hindsight"]:
                retained = _retain(self.memory, iid, _pm_text(inc, pm), verified=True, extra_metadata={**meta, "outcome": "verified_recovery"})
                retention = {"status": "retained", "kind": "verified_post_mortem"}
            elif pm["real_remediation_executed"] and not pm["synthetic_data"]:
                retained = _retain(self.memory, iid, _failed_text(inc, pm), verified=False, extra_metadata={**meta, "outcome": "recovery_not_verified"})
                retention = {"status": "retained", "kind": "failed_resolution_attempt"}
        except Exception as exc:  # never claim storage succeeded; keep the failure visible
            retained = None
            retention = {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        inc["memory_retention"] = retention; pm["memory_retention"] = retention; inc["postmortem"] = pm
        self._save_incident(inc)
        if hasattr(self.store, "append_audit"): self.store.append_audit("system", "recovery_verification", {"incident_id": iid, "resolved": inc["verification"]["resolved"], "retention": retention})
        return pm, retained


class VerificationBlocked(RuntimeError): pass


def _retain(memory, iid, text, **kw):
    params = inspect.signature(memory.retain_post_mortem).parameters
    if "extra_metadata" not in params and not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        kw.pop("extra_metadata", None)
    return memory.retain_post_mortem(iid, text, **kw)


def _metadata(inc, pm):
    ev = ((inc.get("investigation") or {}).get("hypotheses") or [{}])[0]
    conf = ev.get("confidence")
    return {"service": ",".join(inc.get("services") or []), "severity": inc.get("severity", ""),
            "environment": inc.get("environment", ""),
            "root_cause_status": pm["root_cause"]["status"], "confidence": "" if conf is None else conf,
            "evidence_event_ids": ",".join(ev.get("supporting_event_ids") or [])[:400],
            "verification_source": ((inc.get("verification") or {}).get("evidence") or {}).get("source", ""),
            "retained_at": datetime.now(timezone.utc).isoformat()}


def _failed_text(inc, pm):
    acts = "; ".join(a["action"] for a in pm["resolution"]) or "none"
    return (f"FAILED RESOLUTION ATTEMPT for incident {inc['id']} {inc['severity']} services={','.join(inc['services'])}. "
            f"Actions executed: {acts}. These did NOT achieve verified recovery ({pm['verification']['reason']}). "
            f"Hypothesised root cause (UNVERIFIED, may be wrong): {pm['root_cause']['text']}. "
            f"Do not assume these actions fix a similar incident.")


def _pm_text(inc, pm):
    acts = "; ".join(a["action"] for a in pm["resolution"]) or "none"
    return (f"Incident {inc['id']} {inc['severity']} services={','.join(inc['services'])}. "
            f"Proposed root cause (unconfirmed): {pm['root_cause']['text']}. Resolution executed: {acts}. "
            f"Recovery verified: {pm['verification']['reason']}. Synthetic={pm['synthetic_data']}.")
