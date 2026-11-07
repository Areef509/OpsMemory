"""Stage 2: group events into incident candidates (union-find).

Two events are linked when they are in the same environment, within `window_s`
of each other, and (same service OR adjacent in the dependency graph OR share
a correlation/trace id in metadata).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .events import NormalizedEvent


@dataclass
class IncidentCandidate:
    events: list[NormalizedEvent]

    @property
    def services(self) -> list[str]:
        return sorted({e.service for e in self.events})

    @property
    def start(self):
        return self.events[0].timestamp

    @property
    def end(self):
        return self.events[-1].timestamp

    @property
    def environment(self) -> str:
        return self.events[0].environment

    @property
    def repeated(self) -> dict[str, int]:
        c = Counter(e.signature for e in self.events)
        return {sig: n for sig, n in c.items() if n > 1}

    def timeline(self) -> list[dict]:
        return [{"t": e.timestamp.isoformat(), "service": e.service, "severity": e.severity,
                 "source": e.source, "message": e.message, "event_id": e.event_id}
                for e in self.events]


def _ids(e: NormalizedEvent) -> set[str]:
    return {str(e.metadata[k]) for k in ("correlation_id", "trace_id", "request_id") if e.metadata.get(k)}


def correlate(events: list[NormalizedEvent], dependencies: dict[str, list[str]] | None = None,
              window_s: int = 300) -> list[IncidentCandidate]:
    deps = dependencies or {}
    adj = {s: set(ds) for s, ds in deps.items()}
    for s, ds in deps.items():
        for d in ds:
            adj.setdefault(d, set()).add(s)

    evs = sorted(events, key=lambda e: e.timestamp)
    parent = list(range(len(evs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(evs):
        for j in range(i + 1, len(evs)):
            b = evs[j]
            if (b.timestamp - a.timestamp).total_seconds() > window_s:
                break
            if a.environment != b.environment:
                continue
            if a.service == b.service or b.service in adj.get(a.service, ()) or (_ids(a) & _ids(b)):
                parent[find(j)] = find(i)

    groups: dict[int, list[NormalizedEvent]] = {}
    for i, e in enumerate(evs):
        groups.setdefault(find(i), []).append(e)
    return [IncidentCandidate(g) for g in sorted(groups.values(), key=lambda g: g[0].timestamp)]
