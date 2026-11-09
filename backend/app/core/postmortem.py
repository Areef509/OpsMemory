"""Stage 9: post-mortem from recorded data only. Root cause is labelled CONFIRMED only when recovery was verified."""
from __future__ import annotations
from datetime import datetime, timezone


def generate(incident: dict) -> dict:
    synthetic_data = bool(incident.get("synthetic") or incident.get("verification", {}).get("synthetic"))
    real_resolution = [a for a in incident.get("approvals", []) if a.get("state") == "executed" and a.get("simulated") is False]
    recovered = bool(incident.get("verification", {}).get("resolved")) and not synthetic_data
    verified = recovered and bool(real_resolution)  # recovery alone never confirms a cause
    top = (incident.get("investigation", {}).get("hypotheses") or [{}])[0]
    return {
        "incident_id": incident["id"],
        "timeline": incident["timeline"],
        "severity": incident["severity"],
        "root_cause": {"text": top.get("cause", "undetermined"),
                       "status": "confirmed" if (
                           verified
                           and bool(top.get("cause"))
                           and isinstance(incident.get("causal_evidence"), dict)
                           and incident["causal_evidence"].get("verified") is True
                           and bool(incident["causal_evidence"].get("evidence_ids"))
                       ) else "unverified_hypothesis"},
        "resolution": [a for a in incident.get("approvals", []) if a["state"] == "executed"],
        "failed_attempts": [a for a in incident.get("approvals", []) if a["state"] in ("execution_failed", "rejected")],
        "verification": incident.get("verification", {"resolved": False, "reason": "not run"}),
        "synthetic_data": synthetic_data,
        "recovery_observed": recovered,
        "real_remediation_executed": bool(real_resolution),
        "retain_to_hindsight": verified,
        # ---- structured postmortem (schema v2). Everything below is copied from recorded data; nothing is invented. ----
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "service": ",".join(incident.get("services") or []),
        "environment": incident.get("environment"),
        "detection_time": (incident.get("timeline") or [{}])[0].get("t"),
        "user_impact": {"as_reported_by_severity_inputs": incident.get("severity_reasons") or [],
                        "not_reported": incident.get("severity_unknowns") or [],
                        "note": "Impact is only what event metadata reported; it is not independently measured."},
        "evidence_collected": {"event_ids": [e.get("event_id") for e in incident.get("timeline", [])],
                               "sources": sorted({e.get("source") for e in incident.get("timeline", []) if e.get("source")}),
                               "causal_evidence": incident.get("causal_evidence")},
        "hypotheses": (incident.get("investigation") or {}).get("hypotheses") or [],
        "rejected_hypotheses": (incident.get("investigation") or {}).get("dropped_hypotheses") or [],
        "operator_attestations": incident.get("operator_attestations") or [],
        "remediation_and_approvals": [{k: a.get(k) for k in ("id", "adapter", "action", "state", "reason", "executed_by", "executed_at", "simulated")}
                                      for a in incident.get("approvals", [])],
        "recovery_evidence": {"verification": incident.get("verification"), "independent_source": (incident.get("verification") or {}).get("evidence")},
        "outcome": ("recovered_verified_after_real_remediation" if verified else
                    "recovery_observed_not_attributed_to_a_real_remediation" if recovered else
                    "not_recovered_or_unverified"),
        "lessons_learned": incident.get("lessons_learned") or [],       # operator-supplied only
        "preventive_actions": incident.get("preventive_actions") or [],  # operator-supplied only
        "suggested_next_actions_unreviewed": (incident.get("investigation") or {}).get("next_actions") or [],
        "memory_retention": incident.get("memory_retention"),
    }
