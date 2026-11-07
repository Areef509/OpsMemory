"""Storage adapters for OpsMemory.

InMemoryStore is for unit tests only. PostgresStore is the durable runtime store.
The Postgres adapter imports psycopg lazily so core unit tests do not need a DB.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any


class InMemoryStore:
    def __init__(self):
        self.seen: set[str] = set()
        self.incidents: dict[str, dict] = {}
        self.approvals: dict[str, object] = {}
        self.audit: list[dict] = []
        self._events: dict[str, list[dict]] = {}
        self._persisted: dict[str, dict] = {}

    def seen_ids(self): return set(self.seen)
    def save_events(self, events, incident_id=None):
        inserted = set()
        for e in events:
            d = _event_dict(e)
            if incident_id:
                if e.event_id in self._persisted:
                    self._events.setdefault(incident_id, []).append(self._persisted[e.event_id])
            elif e.event_id not in self._persisted:
                self._persisted[e.event_id] = d
                inserted.add(e.event_id)
        self.seen.update(inserted)
        return inserted
    def events_for_incident(self, iid): return list(self._events.get(iid, []))
    def save_incident(self, inc): self.incidents[inc["id"]] = inc
    def save_incident_with_events(self, inc, events):
        inserted = self.save_events(events)
        if len(inserted) != len(events): return False
        self.save_events(events, inc["id"])
        self.save_incident(inc)
        return True
    def get_incident(self, iid): return self.incidents[iid]
    def list_incidents(self): return list(self.incidents.values())
    def save_approval(self, req): self.approvals[req.id] = req
    def get_approval(self, rid): return self.approvals[rid]
    def approvals_for_incident(self, iid):
        return [asdict(r) if is_dataclass(r) else (vars(r) if hasattr(r, "__dict__") else dict(r))
                for r in self.approvals.values() if getattr(r, "incident_id", None) == iid]
    def append_audit(self, actor, event, detail):
        self.audit.append({"actor": actor, "event": event, "detail": detail})
    def audit_log_entries(self, limit: int = 50):
        return list(reversed(self.audit))[:limit]


def _event_dict(e: Any) -> dict:
    d = asdict(e) if is_dataclass(e) else dict(e)
    for key in ("timestamp",):
        if isinstance(d.get(key), datetime): d[key] = d[key].isoformat()
    return d


def _json_default(value):
    if isinstance(value, datetime): return value.isoformat()
    if is_dataclass(value): return asdict(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


class PostgresStore:
    """Small synchronous repository. Each operation uses a short transaction.

    Requires psycopg 3 and a migrated PostgreSQL database. No in-memory fallback is
    performed: missing DB configuration or connectivity is a hard startup failure.
    """
    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or os.environ.get("DATABASE_URL")
        if not self.dsn:
            raise RuntimeError("DATABASE_URL is required for PostgresStore")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("psycopg[binary] is required for PostgreSQL persistence") from exc
        self._connect = lambda: psycopg.connect(self.dsn, row_factory=dict_row)
        with self._connect() as conn:
            conn.execute("SELECT 1")

    def seen_ids(self) -> set[str]:
        with self._connect() as conn:
            return {r["event_id"] for r in conn.execute("SELECT event_id FROM events").fetchall()}

    def save_events(self, events, incident_id=None):
        if incident_id:
            ids = [e.event_id for e in events]
            if ids:
                with self._connect() as conn:
                    conn.execute("UPDATE events SET incident_id=%s WHERE event_id = ANY(%s) AND incident_id IS NULL", (incident_id, ids))
            return set(ids)
        inserted = set()
        with self._connect() as conn:
            for e in events:
                d = _event_dict(e)
                row = conn.execute("""INSERT INTO events(event_id,ts,service,environment,severity,source,message,
                    metadata,synthetic,raw) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb)
                    ON CONFLICT(event_id) DO NOTHING RETURNING event_id""",
                    (d["event_id"], d["timestamp"], d["service"], d["environment"], d["severity"],
                     d["source"], d["message"], json.dumps(d.get("metadata", {}), default=_json_default),
                     bool(d.get("synthetic", False)), json.dumps(d.get("raw", {}), default=_json_default))).fetchone()
                if row: inserted.add(row["event_id"])
        return inserted

    def events_for_incident(self, iid):
        with self._connect() as conn:
            rows = conn.execute("SELECT event_id,ts,service,environment,severity,source,message,metadata,synthetic,raw FROM events WHERE incident_id=%s ORDER BY ts", (iid,)).fetchall()
        return [{**r, "ts": r["ts"].isoformat() if hasattr(r.get("ts"), "isoformat") else r.get("ts"),
                 "timestamp": r["ts"].isoformat() if hasattr(r.get("ts"), "isoformat") else r.get("ts")} for r in rows]

    def save_incident_with_events(self, inc, events):
        """Atomically claim new event IDs, link them, and persist the incident."""
        class _DuplicateRace(Exception): pass
        try:
            with self._connect() as conn:
                for e in events:
                    d = _event_dict(e)
                    row = conn.execute("""INSERT INTO events(event_id,ts,service,environment,severity,source,message,
                        metadata,synthetic,raw) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb)
                        ON CONFLICT(event_id) DO NOTHING RETURNING event_id""",
                        (d["event_id"], d["timestamp"], d["service"], d["environment"], d["severity"],
                         d["source"], d["message"], json.dumps(d.get("metadata", {}), default=_json_default),
                         bool(d.get("synthetic", False)), json.dumps(d.get("raw", {}), default=_json_default))).fetchone()
                    if not row: raise _DuplicateRace()
                data = json.dumps(inc, default=_json_default)
                conn.execute("""INSERT INTO incidents(id,severity,state,services,synthetic,data)
                    VALUES (%s,%s,%s,%s,%s,%s::jsonb)""",
                    (inc["id"], inc["severity"], inc["state"], inc["services"], inc["synthetic"], data))
                conn.execute("UPDATE events SET incident_id=%s WHERE event_id = ANY(%s)",
                             (inc["id"], [e.event_id for e in events]))
            return True
        except _DuplicateRace:
            return False

    def save_incident(self, inc):
        data = json.dumps(inc, default=_json_default)
        with self._connect() as conn:
            conn.execute("""INSERT INTO incidents(id,severity,state,services,synthetic,data)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT(id) DO UPDATE SET
                severity=EXCLUDED.severity,state=EXCLUDED.state,services=EXCLUDED.services,
                synthetic=EXCLUDED.synthetic,data=EXCLUDED.data""",
                (inc["id"], inc["severity"], inc["state"], inc["services"], inc["synthetic"], data))

    def get_incident(self, iid):
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM incidents WHERE id=%s", (iid,)).fetchone()
        if not row: raise KeyError(iid)
        return row["data"]

    def list_incidents(self):
        with self._connect() as conn:
            rows = conn.execute("SELECT data FROM incidents ORDER BY created_at DESC").fetchall()
        return [r["data"] for r in rows]

    def save_approval(self, req):
        data = json.dumps(asdict(req), default=_json_default)
        with self._connect() as conn:
            conn.execute("""INSERT INTO approvals(id,incident_id,action,adapter,state,result,data)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb) ON CONFLICT(id) DO UPDATE SET
                state=EXCLUDED.state,result=EXCLUDED.result,data=EXCLUDED.data""",
                (req.id, req.incident_id, req.action, req.adapter, req.state,
                 json.dumps(req.result, default=_json_default), data))

    def get_approval(self, rid):
        from .core.remediation import ApprovalRequest
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM approvals WHERE id=%s", (rid,)).fetchone()
        if not row: raise KeyError(rid)
        d = row["data"]
        return ApprovalRequest(**d)

    def approvals_for_incident(self, iid):
        with self._connect() as conn:
            rows = conn.execute("SELECT data FROM approvals WHERE incident_id=%s ORDER BY id", (iid,)).fetchall()
        return [r["data"] for r in rows]

    def append_audit(self, actor, event, detail):
        with self._connect() as conn:
            conn.execute("INSERT INTO audit_log(actor,event,detail) VALUES (%s,%s,%s::jsonb)",
                         (actor, event, json.dumps(detail, default=_json_default)))

    def audit_log_entries(self, limit: int = 50):
        with self._connect() as conn:
            rows = conn.execute("SELECT id, ts, actor, event, detail FROM audit_log ORDER BY ts DESC LIMIT %s", (limit,)).fetchall()
        return [{**r, "ts": r["ts"].isoformat()} for r in rows]
