"""Stage 9: post-mortem from recorded data only. Root cause is labelled CONFIRMED only when recovery was verified."""
from __future__ import annotations


def generate(incident: dict) -> dict:
    synthetic_data = bool(incident.get("synthetic") or incident.get("verification", {}).get("synthetic"))
    verified = bool(incident.get("verification", {}).get("resolved")) and not synthetic_data
    top = (incident.get("investigation", {}).get("hypotheses") or [{}])[0]
    return {
        "incident_id": incident["id"],
        "timeline": incident["timeline"],
        "severity": incident["severity"],
        "root_cause": {"text": top.get("cause", "undetermined"),
                       "status": "confirmed" if verified and top else "unverified_hypothesis"},
        "resolution": [a for a in incident.get("approvals", []) if a["state"] == "executed"],
        "failed_attempts": [a for a in incident.get("approvals", []) if a["state"] in ("execution_failed", "rejected")],
        "verification": incident.get("verification", {"resolved": False, "reason": "not run"}),
        "synthetic_data": synthetic_data,
        "retain_to_hindsight": verified,
    }
