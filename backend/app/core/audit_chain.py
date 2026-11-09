"""Tamper-evident audit chain (SHA-256 hash chaining over canonical JSON).

Each entry commits to its predecessor: hash = SHA256(prev_hash + "\n" + canonical_json(entry fields)).
Detects: modified entries, forged/invalid hashes, broken links, reordering, deleted entries in the middle.
Does NOT by itself detect deletion of the newest entries (tail truncation) or full re-computation of the chain by
someone with write access to every row: for that, record the head hash somewhere the database admin cannot edit
and pass it to verify_chain(expect_head=..., expect_count=...)."""
from __future__ import annotations

import hashlib, json, re, threading
from datetime import datetime, timezone

GENESIS = "0" * 64
_HEX = re.compile(r"^[0-9a-f]{64}$")
FIELDS = ("seq", "ts", "actor", "action", "resource_id", "correlation_id", "detail", "prev_hash")


def canonical(fields: dict) -> str:
    return json.dumps({k: fields[k] for k in FIELDS}, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def entry_hash(prev_hash: str, canonical_json: str) -> str:
    return hashlib.sha256((prev_hash + "\n" + canonical_json).encode("utf-8")).hexdigest()


def build_entry(seq: int, prev_hash: str, actor: str, action: str, resource_id=None, correlation_id=None,
                detail=None, ts: str | None = None) -> dict:
    fields = {"seq": seq, "ts": ts or datetime.now(timezone.utc).isoformat(), "actor": str(actor), "action": str(action),
              "resource_id": None if resource_id is None else str(resource_id),
              "correlation_id": None if correlation_id is None else str(correlation_id),
              # round-trip so the stored canonical form is exactly what a verifier will re-derive
              "detail": json.loads(json.dumps(detail if detail is not None else {}, sort_keys=True, default=str)),
              "prev_hash": prev_hash}
    c = canonical(fields)
    return {**fields, "canonical": c, "hash": entry_hash(prev_hash, c)}


class AuditChain:
    """In-process chain (used by the in-memory store and tests)."""
    def __init__(self):
        self.entries: list[dict] = []
        self._lock = threading.Lock()

    @property
    def head(self) -> str:
        return self.entries[-1]["hash"] if self.entries else GENESIS

    def append(self, actor, action, resource_id=None, correlation_id=None, detail=None) -> dict:
        with self._lock:
            e = build_entry(len(self.entries) + 1, self.head, actor, action, resource_id, correlation_id, detail)
            self.entries.append(e)
            return e


def verify_chain(entries: list[dict], expect_head: str | None = None, expect_count: int | None = None,
                 first_prev_hash: str = GENESIS, first_seq: int = 1) -> dict:
    """entries must be supplied in stored order. Returns {ok, checked, head, issues[]}."""
    issues, prev, expected_seq = [], first_prev_hash, first_seq
    for e in entries:
        seq = e.get("seq")
        def bad(kind, **kw): issues.append({"seq": seq, "type": kind, **kw})
        h = e.get("hash")
        if not isinstance(h, str) or not _HEX.match(h): bad("invalid_hash_format")
        if seq != expected_seq:
            bad("sequence_gap_or_reorder", expected=expected_seq, found=seq)
        if e.get("prev_hash") != prev: bad("chain_break", expected_prev=prev, found_prev=e.get("prev_hash"))
        try:
            parsed = json.loads(e.get("canonical", ""))
            if canonical({**{k: parsed.get(k) for k in FIELDS}}) != e.get("canonical"): bad("non_canonical_record")
            for k in FIELDS:
                if parsed.get(k) != e.get(k): bad("field_differs_from_signed_record", field=k)
            if entry_hash(parsed.get("prev_hash", ""), e["canonical"]) != h: bad("hash_mismatch")
        except (ValueError, KeyError, TypeError):
            bad("unreadable_record")
        prev = h if isinstance(h, str) else prev
        expected_seq = (seq + 1) if isinstance(seq, int) else expected_seq + 1
    if expect_count is not None and len(entries) != expect_count:
        issues.append({"seq": None, "type": "count_mismatch", "expected": expect_count, "found": len(entries)})
    if expect_head is not None and (entries[-1]["hash"] if entries else first_prev_hash) != expect_head:
        issues.append({"seq": None, "type": "head_mismatch", "expected": expect_head})
    return {"ok": not issues, "checked": len(entries), "head": entries[-1].get("hash") if entries else first_prev_hash, "issues": issues}
