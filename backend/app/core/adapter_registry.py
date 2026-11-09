"""Declarative, bounded remediation adapters.

An adapter is a NAMED, pre-reviewed operation bound to ONE target service. There is no field for a shell command,
URL or user-supplied string: the callable is supplied by code at registration time, never by a request or by LLM text.
Enforcement (service match, environment scope, role, preconditions, expiry, duplicates) happens in the Engine."""
from __future__ import annotations

import re, threading
from dataclasses import dataclass
from .remediation import Adapter

ROLE_RANK = {"viewer": 0, "sre": 1, "admin": 2}
RISK = {"low", "medium", "high"}
PRECONDITIONS = {"incident_service_matches_target", "incident_not_synthetic", "environment_in_scope", "incident_investigated"}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")


class SpecError(ValueError): pass


@dataclass(frozen=True)
class AdapterSpec:
    id: str
    target_service: str
    permitted_action: str
    required_role: str
    risk_level: str
    timeout_s: float
    idempotent: bool
    preconditions: tuple[str, ...]
    environment_scope: tuple[str, ...]
    rollback: str
    audit_fields: tuple[str, ...]
    verification: str                     # what independent evidence is required afterwards
    approval_ttl_s: int = 3600
    dry_run: bool = False                 # dry-run adapters must be labelled simulated and never count as remediation
    simulated: bool = False


def validate_spec(s: AdapterSpec) -> None:
    if not _ID.match(s.id): raise SpecError("adapter id must be 3-64 chars of [a-z0-9-]")
    if not s.target_service or "*" in s.target_service or "," in s.target_service: raise SpecError("exactly one concrete target_service is required")
    if not s.permitted_action.strip(): raise SpecError("permitted_action required")
    if s.required_role not in {"sre", "admin"}: raise SpecError("required_role must be sre or admin")
    if s.risk_level not in RISK: raise SpecError("risk_level must be low|medium|high")
    if not 0 < s.timeout_s <= 300: raise SpecError("timeout_s must be in (0, 300]")
    if not s.environment_scope or "*" in s.environment_scope: raise SpecError("explicit environment_scope required (no wildcard)")
    if not set(s.preconditions) <= PRECONDITIONS: raise SpecError("unknown precondition")
    for needed in ("incident_service_matches_target", "environment_in_scope"):
        if needed not in s.preconditions: raise SpecError(f"precondition '{needed}' is mandatory")
    if not s.rollback.strip(): raise SpecError("rollback / recovery procedure must be documented")
    if not s.verification.strip(): raise SpecError("independent verification requirement must be documented")
    if not s.audit_fields: raise SpecError("audit_fields required")
    if s.risk_level == "high" and s.required_role != "admin": raise SpecError("high-risk adapters require admin")
    if s.dry_run and not s.simulated: raise SpecError("dry-run adapters must be labelled simulated")
    if not 30 <= s.approval_ttl_s <= 86400: raise SpecError("approval_ttl_s must be 30..86400")


def check_preconditions(s: AdapterSpec, incident: dict) -> list[str]:
    fails = []
    if s.dry_run is False and "incident_service_matches_target" in s.preconditions and list(incident.get("services") or []) != [s.target_service]:
        fails.append(f"incident services {incident.get('services')} do not equal the adapter target [{s.target_service!r}] (cross-service invocation refused)")
    if "incident_not_synthetic" in s.preconditions and incident.get("synthetic") is not False:
        fails.append("incident is synthetic or unclassified")
    if "environment_in_scope" in s.preconditions and incident.get("environment") not in s.environment_scope:
        fails.append(f"incident environment {incident.get('environment')!r} not in adapter scope {list(s.environment_scope)}")
    if "incident_investigated" in s.preconditions and incident.get("state") not in {"investigated", "remediation_pending"}:
        fails.append("incident has not been investigated")
    return fails


def _with_timeout(run, timeout_s):
    def wrapped():
        box = {}
        def target():
            try: box["r"] = run()
            except BaseException as exc: box["e"] = exc
        t = threading.Thread(target=target, daemon=True); t.start(); t.join(timeout_s)
        if t.is_alive(): raise TimeoutError(f"adapter exceeded its {timeout_s}s timeout (operation may still be running; verify target state)")
        if "e" in box: raise box["e"]
        return box["r"]
    return wrapped


class AdapterRegistry:
    def __init__(self):
        self._specs: dict[str, AdapterSpec] = {}
        self._adapters: dict[str, Adapter] = {}

    def register(self, spec: AdapterSpec, run) -> None:
        validate_spec(spec)
        if spec.id in self._specs: raise SpecError(f"adapter '{spec.id}' already registered")
        if not callable(run): raise SpecError("run must be a callable supplied by code")
        self._specs[spec.id] = spec
        self._adapters[spec.id] = Adapter(spec.id, _with_timeout(run, spec.timeout_s), simulated=spec.simulated or spec.dry_run)

    def spec(self, name: str) -> AdapterSpec | None: return self._specs.get(name)
    def adapters(self) -> dict[str, Adapter]: return dict(self._adapters)
    def describe(self) -> list[dict]:
        return [{"id": s.id, "name": s.id, "target_service": s.target_service, "permitted_action": s.permitted_action, "required_role": s.required_role,
                 "risk_level": s.risk_level, "timeout_s": s.timeout_s, "idempotent": s.idempotent, "environment_scope": list(s.environment_scope),
                 "preconditions": list(s.preconditions), "rollback": s.rollback, "verification": s.verification, "dry_run": s.dry_run,
                 "simulated": s.simulated or s.dry_run} for s in self._specs.values()]
