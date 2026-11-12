"""Small in-process sliding-window rate limiter.

Good enough for a single backend process. If you run several workers or
replicas, each keeps its own counters: move this to a shared store (e.g. Redis)
before relying on it for abuse protection at scale.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Callable, Deque, Dict


class SlidingWindowLimiter:
    """Allow at most ``limit`` hits per ``window_s`` seconds per key.

    ``limit <= 0`` disables the limiter.
    """

    MAX_KEYS = 10_000  # prune idle keys beyond this so memory stays bounded

    def __init__(self, limit: int, window_s: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: Dict[str, Deque[float]] = {}

    def hit(self, key: str) -> float:
        """Record a hit. Returns 0 if allowed, else seconds until a slot frees."""
        if self.limit <= 0:
            return 0.0
        now = self._clock()
        q = self._hits.get(key)
        if q is None:
            if len(self._hits) >= self.MAX_KEYS:
                self._prune(now)
            q = self._hits[key] = deque()
        while q and now - q[0] >= self.window_s:
            q.popleft()
        if len(q) >= self.limit:
            return max(self.window_s - (now - q[0]), 0.001)
        q.append(now)
        return 0.0

    def _prune(self, now: float) -> None:
        for k in [k for k, q in self._hits.items() if not q or now - q[-1] >= self.window_s]:
            del self._hits[k]
