"""Stage 3: deterministic SEV-1..SEV-4 with a human-readable explanation.

Inputs that are not present in the evidence are treated as UNKNOWN and are
listed in the explanation rather than guessed.
"""
from __future__ import annotations

from dataclasses import dataclass

from .correlation import IncidentCandidate
from .events import SEVERITIES


@dataclass
class SeverityResult:
    level: str            # "SEV-1".."SEV-4"
    score: int
    reasons: list[str]
    unknowns: list[str]


def assess(c: IncidentCandidate, critical_services: set[str] | None = None) -> SeverityResult:
    critical_services = critical_services or set()
    score, reasons, unknowns = 0, [], []
    prod = c.environment in {"prod", "production"}
    if prod:
        score += 3; reasons.append("affects production (+3)")
    else:
        reasons.append(f"environment is '{c.environment}', not production (+0)")

    worst = max(c.events, key=lambda e: SEVERITIES[e.severity]).severity
    pts = {"critical": 3, "error": 2, "warning": 1}.get(worst, 0)
    score += pts; reasons.append(f"highest event severity '{worst}' (+{pts})")

    hit = [s for s in c.services if s in critical_services]
    if hit:
        score += 3; reasons.append(f"business-critical service(s) affected: {', '.join(hit)} (+3)")
    if len(c.services) > 1:
        score += 1; reasons.append(f"{len(c.services)} services affected (+1)")

    meta = {}
    for e in c.events:
        meta.update({k: v for k, v in e.metadata.items() if v is not None})
    users = meta.get("users_affected")
    if isinstance(users, (int, float)):
        p = 3 if users >= 10000 else 2 if users >= 1000 else 1 if users >= 100 else 0
        score += p; reasons.append(f"users_affected={users} (+{p})")
    else:
        unknowns.append("users_affected not reported")
    if meta.get("data_integrity_risk") is True:
        score += 4; reasons.append("data integrity risk reported (+4)")
    avail = meta.get("availability_pct")
    if isinstance(avail, (int, float)):
        p = 3 if avail < 50 else 2 if avail < 95 else 1 if avail < 99.9 else 0
        score += p; reasons.append(f"availability {avail}% (+{p})")
    else:
        unknowns.append("availability_pct not reported")
    mins = (c.end - c.start).total_seconds() / 60
    if mins >= 30:
        score += 2; reasons.append(f"duration {mins:.0f} min (+2)")
    elif mins >= 10:
        score += 1; reasons.append(f"duration {mins:.0f} min (+1)")

    level = "SEV-1" if score >= 11 else "SEV-2" if score >= 8 else "SEV-3" if score >= 4 else "SEV-4"
    return SeverityResult(level, score, reasons, unknowns)
