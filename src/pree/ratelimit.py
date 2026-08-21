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

    def _evict_if_needed(self, now: float) -> None:
        """Bound memory without handing an attacker a way to reset a throttled bucket.

        Insertion-order eviction let a spray of fresh keys pop a key that was currently over
        its limit, which reopened its window: the very outcome the limiter exists to prevent.
        Expired buckets go first, then the least recently active, and a key still over its
        limit is never evicted.
        """
        if len(self._hits) <= self._max_keys:
            return
        cutoff = now - self._window
        for key in [k for k, bucket in self._hits.items() if not bucket or bucket[-1] <= cutoff]:
            del self._hits[key]
            if len(self._hits) <= self._max_keys:
                return
        evictable = [
            (bucket[-1], key) for key, bucket in self._hits.items() if len(bucket) < self._limit
        ]
        evictable.sort()
        for _, key in evictable:
            del self._hits[key]
            if len(self._hits) <= self._max_keys:
                return

    def allow(self, key: str) -> bool:
        """Record a hit and report whether it is within the limit."""
        now = self._clock()
        bucket = self._prune(key, now)
        if len(bucket) >= self._limit:
            return False
        bucket.append(now)
        self._evict_if_needed(now)
        return True

    def retry_after_seconds(self, key: str) -> int:
        """Seconds until the oldest hit in the window expires, for the Retry-After header."""
        bucket = self._hits.get(key)
        if not bucket:
            return 0
        remaining = self._window - (self._clock() - bucket[0])
        return max(1, int(remaining) + 1)
