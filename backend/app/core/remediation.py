"""Stage 7/8: human approval workflow, audit trail, recovery verification.

Actions run only through a registered adapter AFTER approval by a user whose
role permits it. Adapters flagged `simulated=True` are reported as such.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
import math

ROLE_CAN_APPROVE = {"sre", "admin"}


class AuthorizationError(PermissionError):
    pass


@dataclass
class Adapter:
    name: str
    run: Callable[[], dict]
    simulated: bool


@dataclass
class ApprovalRequest:
    id: str
    incident_id: str
    action: str
    adapter: str
    state: str = "pending"  # pending|approved|rejected|executed|execution_failed
    audit: list = field(default_factory=list)
    result: dict | None = None

    def _log(self, actor, event, **kw):
        self.audit.append({"t": datetime.now(timezone.utc).isoformat(), "actor": actor, "event": event, **kw})


def decide(req: ApprovalRequest, user: str, role: str, approve: bool, reason: str = ""):
    if role not in ROLE_CAN_APPROVE:
        req._log(user, "denied_unauthorized", role=role)
        raise AuthorizationError(f"role '{role}' may not approve remediation")
    if req.state != "pending":
        raise ValueError(f"request already {req.state}")
    req.state = "approved" if approve else "rejected"
    req._log(user, req.state, reason=reason)


def execute(req: ApprovalRequest, adapters: dict[str, Adapter], user: str):
    if req.state != "approved":
        raise ValueError(f"cannot execute request in state '{req.state}'")
    ad = adapters.get(req.adapter)
    if ad is None:
        req.state = "execution_failed"
        req._log(user, "no_adapter", adapter=req.adapter)
        return req
    try:
        req.result = {**ad.run(), "simulated": ad.simulated}
        req.state = "executed"
        req._log(user, "executed", adapter=ad.name, simulated=ad.simulated)
    except Exception as exc:  # outcome recorded, never reported as success
        req.state = "execution_failed"
        req._log(user, "execution_failed", error=str(exc))
    return req


@dataclass
class VerificationCriteria:
    max_error_rate: float = 0.01
    max_p99_ms: float = 800.0
    consecutive_samples: int = 3


def verify_recovery(samples: list[dict], crit: VerificationCriteria, synthetic: bool) -> dict:
    """samples: [{'error_rate':float,'p99_ms':float}, ...] oldest->newest. Resolved only if the
    last N samples ALL satisfy the criteria."""
    n = crit.consecutive_samples
    if n < 1: return {"resolved": False, "reason": "consecutive_samples must be positive", "synthetic": synthetic}
    if len(samples) < n:
        return {"resolved": False, "reason": f"need {n} samples, have {len(samples)}", "synthetic": synthetic}
    tail = samples[-n:]
    normalized = []
    for sample in tail:
        try: er, p99 = float(sample["error_rate"]), float(sample["p99_ms"])
        except (KeyError, TypeError, ValueError):
            return {"resolved": False, "reason": "invalid recovery metric sample", "synthetic": synthetic}
        if not math.isfinite(er) or not math.isfinite(p99) or not 0 <= er <= 1 or p99 < 0:
            return {"resolved": False, "reason": "non-finite or out-of-range recovery metric sample", "synthetic": synthetic}
        normalized.append({"error_rate": er, "p99_ms": p99})
    bad = [s for s in normalized if s["error_rate"] > crit.max_error_rate or s["p99_ms"] > crit.max_p99_ms]
    return {"resolved": not bad, "reason": "criteria met" if not bad else f"{len(bad)}/{n} samples violate criteria",
            "synthetic": synthetic}
