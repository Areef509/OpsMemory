"""Small in-process sliding-window rate limiter (per process; behind a proxy all clients share one socket IP)."""
from __future__ import annotations
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit: int, window_s: float = 60.0, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window_s, clock
        self._hits: dict[str, deque] = defaultdict(deque)

    def _trim(self, key):
        now, q = self.clock(), self._hits[key]
        while q and now - q[0] > self.window: q.popleft()
        return q

    def allow(self, key: str) -> bool:
        q = self._trim(key)
        if len(q) >= self.limit: return False
        q.append(self.clock()); return True

    def hit(self, key: str) -> None: self._trim(key).append(self.clock())

    def blocked(self, key: str) -> bool: return len(self._trim(key)) >= self.limit
