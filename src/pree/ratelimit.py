"""Two-tier rate limiting.

A coarse global limit protects the process from any caller. A finer per-actor limit protects
the expensive scoring path specifically. Exceeding either returns 429.

The window is a fixed monotonic bucket, which is cheap and cannot be skewed by a wall-clock
change. Time is injected so the behaviour is testable without sleeping.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

GLOBAL_LIMIT = 240
GLOBAL_WINDOW_SECONDS = 60.0
ACTOR_LIMIT = 20
ACTOR_WINDOW_SECONDS = 60.0
MAX_TRACKED_ACTORS = 1024


class RateLimiter:
    """A sliding-window limiter over one key space."""

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = MAX_TRACKED_ACTORS,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float]:
        bucket = self._hits.setdefault(key, deque())
        cutoff = now - self._window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        return bucket

    def _evict_if_needed(self) -> None:
        """Bound memory so a spray of distinct actor keys cannot grow the map without limit."""
        while len(self._hits) > self._max_keys:
            self._hits.pop(next(iter(self._hits)))

    def allow(self, key: str) -> bool:
        """Record a hit and report whether it is within the limit."""
        now = self._clock()
        bucket = self._prune(key, now)
        if len(bucket) >= self._limit:
            return False
        bucket.append(now)
        self._evict_if_needed()
        return True

    def retry_after_seconds(self, key: str) -> int:
        """Seconds until the oldest hit in the window expires, for the Retry-After header."""
        bucket = self._hits.get(key)
        if not bucket:
            return 0
        remaining = self._window - (self._clock() - bucket[0])
        return max(1, int(remaining) + 1)
