"""Stage 1: event validation, normalization, redaction, idempotency.

Pure-stdlib so it is unit-testable without any service. The FastAPI layer
(not yet written) will wrap these functions; PostgreSQL persistence is also
not yet written -- `ingest()` takes a `seen` set only as a stand-in.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SEVERITIES = {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}
_SEV_ALIASES = {"warn": "warning", "fatal": "critical", "err": "error", "sev1": "critical"}
REQUIRED = ("timestamp", "service", "environment", "severity", "message", "source")

_REDACTIONS = [
    (re.compile(r"(?i)bearer\s+[a-z0-9._\-]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)(password|passwd|secret|api[_-]?key|token)\s*[=:]\s*\S+"), r"\1=[REDACTED]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[REDACTED_EMAIL]"),
]


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class NormalizedEvent:
    event_id: str          # idempotency key
    signature: str         # message template, used to count repeated alerts
    timestamp: datetime
    service: str
    environment: str
    severity: str
    message: str
    source: str
    metadata: dict[str, Any] = field(default_factory=dict)
    synthetic: bool = False
    raw: dict[str, Any] = field(default_factory=dict)  # original evidence, redacted


def redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def _parse_ts(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValidationError("timestamp must be an ISO-8601 string")
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"invalid timestamp: {value!r}") from exc
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)


def signature_of(message: str) -> str:
    """Template of a message: numbers/hex ids collapsed, so repeats match."""
    s = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", message.lower())
    s = re.sub(r"\d+(\.\d+)?", "<n>", s)
    return re.sub(r"\s+", " ", s).strip()


def _redact_value(value: Any) -> Any:
    """Recursively redact strings in nested payloads before they reach storage/LLM."""
    if isinstance(value, str): return redact(value)
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            name = str(key).lower().replace("-", "_")
            if any(part in name for part in ("password", "passwd", "secret", "api_key", "token", "authorization")):
                cleaned[str(key)] = "[REDACTED]"
            else:
                cleaned[str(key)] = _redact_value(item)
        return cleaned
    if isinstance(value, list): return [_redact_value(v) for v in value]
    return value


def normalize(raw: dict[str, Any]) -> NormalizedEvent:
    if not isinstance(raw, dict):
        raise ValidationError("event must be an object")
    missing = [k for k in REQUIRED if not raw.get(k)]
    if missing:
        raise ValidationError(f"missing required fields: {', '.join(missing)}")
    sev = str(raw["severity"]).lower()
    sev = _SEV_ALIASES.get(sev, sev)
    if sev not in SEVERITIES:
        raise ValidationError(f"unknown severity: {raw['severity']!r}")
    ts = _parse_ts(raw["timestamp"])
    service = str(raw["service"]).strip().lower()
    env = str(raw["environment"]).strip().lower()
    source = str(raw["source"]).strip().lower()
    message = redact(str(raw["message"]).strip())
    meta = raw.get("metadata") or {}
    if not isinstance(meta, dict):
        raise ValidationError("metadata must be an object")
    meta = _redact_value(meta)
    key = raw.get("idempotency_key") or f"{service}|{env}|{source}|{ts.isoformat()}|{message}"
    event_id = hashlib.sha256(key.encode()).hexdigest()[:32]
    redacted_raw = _redact_value(raw)
    return NormalizedEvent(
        event_id=event_id, signature=signature_of(message), timestamp=ts, service=service,
        environment=env, severity=sev, message=message, source=source, metadata=meta,
        synthetic=bool(raw.get("synthetic", False)), raw=redacted_raw,
    )


def ingest(batch: list[dict[str, Any]], seen: set[str]) -> tuple[list[NormalizedEvent], list[dict]]:
    """Returns (accepted, rejected). Duplicates (same event_id) are silently idempotent."""
    accepted, rejected = [], []
    for i, raw in enumerate(batch):
        try:
            ev = normalize(raw)
        except ValidationError as exc:
            rejected.append({"index": i, "error": str(exc)})
            continue
        if ev.event_id in seen:
            continue
        seen.add(ev.event_id)
        accepted.append(ev)
    return accepted, rejected
