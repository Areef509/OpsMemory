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


from .core.audit_chain import AuditChain, GENESIS, build_entry
from .tenant_context import current_tenant
AUDIT_LOCK_KEY = 7_420_011  # advisory-lock id for the audit chain

class InMemoryStore:
    def __init__(self):
        self.seen: set[str] = set()
        self.incidents: dict[str, dict] = {}
        self.approvals: dict[str, object] = {}
        self.audit: list[dict] = []
        self.chain = AuditChain()
        self._events: dict[str, list[dict]] = {}
        self._persisted: dict[str, dict] = {}
        self._experiments: list[dict] = []

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
    def save_investigation_experiment(self, record):
        """Test-only experiment persistence; runtime uses PostgresStore."""
        item = dict(record)
        self._experiments.append(item)
        return item

    def get_investigation_experiment(self, experiment_id):
        for item in self._experiments:
            if item.get("id") == experiment_id:
                return item
        raise KeyError(experiment_id)

    def list_investigation_experiments(self, incident_id):
        return [x for x in self._experiments if x.get("incident_id") == incident_id]

    def append_audit(self, actor, event, detail, resource_id=None, correlation_id=None):
        d = detail if isinstance(detail, dict) else {"value": detail}
        self.chain.append(actor, event, resource_id or d.get("incident_id") or d.get("request_id") or d.get("id"), correlation_id, d)
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
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise RuntimeError("psycopg[binary,pool] is required for PostgreSQL persistence") from exc
        min_size = int(os.environ.get("POSTGRES_POOL_MIN_SIZE", "1"))
        max_size = int(os.environ.get("POSTGRES_POOL_MAX_SIZE", "10"))
        if min_size < 1 or max_size < min_size:
            raise ValueError("Invalid PostgreSQL pool limits: require 1 <= min_size <= max_size")
        self._pool_min_size = min_size
        self._pool_max_size = max_size
        self._pool = ConnectionPool(
            conninfo=self.dsn,
            kwargs={"row_factory": dict_row},
            min_size=min_size,
            max_size=max_size,
            open=True,
            name="opsmemory-postgres",
        )
        self._closed = False
        try:
            self._pool.wait(timeout=10)
            with self._pool.connection() as conn:
                conn.execute("SELECT 1")
        except Exception:
            self.close()
            raise
        self._connect = self._pool.connection

    def close(self):
        """Close the pool and its connections; safe to call more than once."""
        if not self._closed:
            self._closed = True
            self._pool.close()

    def pool_stats(self) -> dict[str, int]:
        """Return a snapshot of psycopg_pool counters without acquiring a connection."""
        if self._closed:
            raise RuntimeError("PostgreSQL pool is closed")
        return dict(self._pool.get_stats())

    def resize_pool(self, min_size: int, max_size: int) -> dict[str, int]:
        """Resize this store's connection pool; callers must choose capacity deliberately."""
        if self._closed:
            raise RuntimeError("PostgreSQL pool is closed")
        if (isinstance(min_size, bool) or isinstance(max_size, bool)
                or not isinstance(min_size, int) or not isinstance(max_size, int)
                or min_size < 1 or max_size < min_size):
            raise ValueError("Invalid PostgreSQL pool limits: require integer 1 <= min_size <= max_size")
        self._pool.resize(min_size=min_size, max_size=max_size)
        self._pool_min_size = min_size
        self._pool_max_size = max_size
        return self.pool_stats()

    def seen_ids(self) -> set[str]:
        with self._connect() as conn:
            return {r["event_id"] for r in conn.execute("SELECT event_id FROM events WHERE tenant_id=%s", (current_tenant(),)).fetchall()}

    def save_events(self, events, incident_id=None):
        if incident_id:
            ids = [e.event_id for e in events]
            if ids:
                with self._connect() as conn:
                    conn.execute("UPDATE events SET incident_id=%s WHERE event_id = ANY(%s) AND incident_id IS NULL AND tenant_id=%s", (incident_id, ids, current_tenant()))
            return set(ids)
        inserted = set()
        with self._connect() as conn:
            for e in events:
                d = _event_dict(e)
                row = conn.execute("""INSERT INTO events(event_id,ts,service,environment,severity,source,message,
                    metadata,synthetic,raw,tenant_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s)
                    ON CONFLICT(event_id) DO NOTHING RETURNING event_id""",
                    (d["event_id"], d["timestamp"], d["service"], d["environment"], d["severity"],
                     d["source"], d["message"], json.dumps(d.get("metadata", {}), default=_json_default),
                     bool(d.get("synthetic", False)), json.dumps(d.get("raw", {}), default=_json_default), current_tenant())).fetchone()
                if row: inserted.add(row["event_id"])
        return inserted

    def events_for_incident(self, iid):
        with self._connect() as conn:
            rows = conn.execute("SELECT event_id,ts,service,environment,severity,source,message,metadata,synthetic,raw FROM events WHERE incident_id=%s AND tenant_id=%s ORDER BY ts", (iid, current_tenant())).fetchall()
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
                        metadata,synthetic,raw,tenant_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s)
                        ON CONFLICT(event_id) DO NOTHING RETURNING event_id""",
                        (d["event_id"], d["timestamp"], d["service"], d["environment"], d["severity"],
                         d["source"], d["message"], json.dumps(d.get("metadata", {}), default=_json_default),
                         bool(d.get("synthetic", False)), json.dumps(d.get("raw", {}), default=_json_default), current_tenant())).fetchone()
                    if not row: raise _DuplicateRace()
                data = json.dumps(inc, default=_json_default)
                conn.execute("""INSERT INTO incidents(id,severity,state,services,synthetic,data,tenant_id)
                    VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)""",
                    (inc["id"], inc["severity"], inc["state"], inc["services"], inc["synthetic"], data, current_tenant()))
                conn.execute("UPDATE events SET incident_id=%s WHERE event_id = ANY(%s) AND tenant_id=%s",
                             (inc["id"], [e.event_id for e in events], current_tenant()))
            return True
        except _DuplicateRace:
            return False

    def save_incident(self, inc):
        data = json.dumps(inc, default=_json_default)
        with self._connect() as conn:
            conn.execute("""INSERT INTO incidents(id,severity,state,services,synthetic,data,tenant_id)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT(id) DO UPDATE SET
                severity=EXCLUDED.severity,state=EXCLUDED.state,services=EXCLUDED.services,
                synthetic=EXCLUDED.synthetic,data=EXCLUDED.data WHERE incidents.tenant_id=EXCLUDED.tenant_id""",
                (inc["id"], inc["severity"], inc["state"], inc["services"], inc["synthetic"], data, current_tenant()))

    def get_incident(self, iid):
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM incidents WHERE id=%s AND tenant_id=%s", (iid, current_tenant())).fetchone()
        if not row: raise KeyError(iid)
        return row["data"]

    def list_incidents(self):
        with self._connect() as conn:
            rows = conn.execute("SELECT data FROM incidents WHERE tenant_id=%s ORDER BY created_at DESC", (current_tenant(),)).fetchall()
        return [r["data"] for r in rows]

    def save_approval(self, req):
        data = json.dumps(asdict(req), default=_json_default)
        with self._connect() as conn:
            conn.execute("""INSERT INTO approvals(id,incident_id,action,adapter,state,result,data,tenant_id)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s) ON CONFLICT(id) DO UPDATE SET
                state=EXCLUDED.state,result=EXCLUDED.result,data=EXCLUDED.data WHERE approvals.tenant_id=EXCLUDED.tenant_id""",
                (req.id, req.incident_id, req.action, req.adapter, req.state,
                 json.dumps(req.result, default=_json_default), data, current_tenant()))

    def get_approval(self, rid):
        from .core.remediation import ApprovalRequest
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM approvals WHERE id=%s AND tenant_id=%s", (rid, current_tenant())).fetchone()
        if not row: raise KeyError(rid)
        d = row["data"]
        return ApprovalRequest(**d)

    def approvals_for_incident(self, iid):
        with self._connect() as conn:
            rows = conn.execute("SELECT data FROM approvals WHERE incident_id=%s AND tenant_id=%s ORDER BY id", (iid, current_tenant())).fetchall()
        return [r["data"] for r in rows]

    def save_investigation_experiment(self, record):
        """Persist immutable memory-off/on investigation snapshots."""
        required = {"id", "incident_id", "actor", "evidence_class",
                    "incident_synthetic", "memory_off", "memory_on"}
        missing = required - set(record)
        if missing:
            raise ValueError("missing experiment fields: " + ", ".join(sorted(missing)))
        if record["evidence_class"] not in {"controlled_local_hackathon_test", "production"}:
            raise ValueError("invalid experiment evidence class")
        if not isinstance(record["memory_off"], dict) or not isinstance(record["memory_on"], dict):
            raise ValueError("both experiment snapshots must be JSON objects")

        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM incidents WHERE id=%s AND tenant_id=%s",
                (record["incident_id"], current_tenant())
            ).fetchone()
            if not exists:
                raise KeyError(record["incident_id"])
            row = conn.execute(
                """INSERT INTO investigation_experiments
                   (id, incident_id, actor, evidence_class, incident_synthetic,
                    memory_off, memory_on, metadata, tenant_id)
                   VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s)
                   RETURNING id, incident_id, actor, evidence_class,
                             incident_synthetic, memory_off, memory_on, metadata, created_at""",
                (record["id"], record["incident_id"], record["actor"],
                 record["evidence_class"], bool(record["incident_synthetic"]),
                 json.dumps(record["memory_off"], default=_json_default),
                 json.dumps(record["memory_on"], default=_json_default),
                 json.dumps(record.get("metadata", {}), default=_json_default), current_tenant())
            ).fetchone()
        return dict(row)

    def get_investigation_experiment(self, experiment_id):
        with self._connect() as conn:
            row = conn.execute(
                """SELECT id, incident_id, actor, evidence_class,
                          incident_synthetic, memory_off, memory_on, metadata, created_at
                   FROM investigation_experiments WHERE id=%s AND tenant_id=%s""",
                (experiment_id, current_tenant())
            ).fetchone()
        if not row:
            raise KeyError(experiment_id)
        return dict(row)

    def list_investigation_experiments(self, incident_id):
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id, incident_id, actor, evidence_class,
                          incident_synthetic, memory_off, memory_on, metadata, created_at
                   FROM investigation_experiments
                   WHERE incident_id=%s AND tenant_id=%s ORDER BY created_at DESC""",
                (incident_id, current_tenant())
            ).fetchall()
        return [dict(row) for row in rows]

    def append_audit(self, actor, event, detail, resource_id=None, correlation_id=None):
        """Hash-chained append. Requires migration 003_audit_chain.sql. A transaction-scoped advisory lock serialises
        writers so seq/prev_hash cannot fork. (Written against the schema; not yet executed against PostgreSQL.)"""
        d = detail if isinstance(detail, dict) else {"value": detail}
        d = json.loads(json.dumps(d, default=_json_default))
        rid = resource_id or d.get("incident_id") or d.get("request_id") or d.get("id")
        with self._connect() as conn:
            if not getattr(self, "_chain_columns", False):
                has = conn.execute("SELECT 1 FROM information_schema.columns WHERE table_name='audit_log' AND column_name='hash'").fetchone()
                if not has:  # migration 003 not applied yet: keep the existing (unchained) behaviour instead of breaking approvals
                    conn.execute("INSERT INTO audit_log(actor,event,detail,tenant_id) VALUES (%s,%s,%s::jsonb,%s)", (actor, event, json.dumps(d), current_tenant()))
                    return
                self._chain_columns = True
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (AUDIT_LOCK_KEY,))
            last = conn.execute("SELECT seq, hash FROM audit_log WHERE seq IS NOT NULL ORDER BY seq DESC LIMIT 1").fetchone()
            e = build_entry((last["seq"] + 1) if last else 1, last["hash"] if last else GENESIS, actor, event, rid, correlation_id, d)
            conn.execute("""INSERT INTO audit_log(actor,event,detail,seq,ts_signed,prev_hash,hash,canonical,resource_id,correlation_id,tenant_id)
                VALUES (%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (actor, event, json.dumps(d), e["seq"], e["ts"], e["prev_hash"], e["hash"], e["canonical"], e["resource_id"], e["correlation_id"], current_tenant()))

    def audit_chain_entries(self):
        """Read-only export of the chained rows, in sequence order, for verification."""
        with self._connect() as conn:
            rows = conn.execute("""SELECT seq, ts_signed AS ts, actor, event AS action, resource_id, correlation_id,
                detail, prev_hash, hash, canonical FROM audit_log WHERE seq IS NOT NULL ORDER BY seq""").fetchall()
        return [dict(r) for r in rows]

    def audit_log_entries(self, limit: int = 50):
        with self._connect() as conn:
            rows = conn.execute("SELECT id, ts, actor, event, detail FROM audit_log WHERE tenant_id=%s ORDER BY ts DESC LIMIT %s", (current_tenant(), limit)).fetchall()
        return [{**r, "ts": r["ts"].isoformat()} for r in rows]
