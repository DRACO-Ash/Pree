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

    def _evict_if_needed(self, now: float, protected: str) -> bool:
        """Reclaim space without ever letting the limiter stop limiting.

        Two rules make this fail closed. The key currently being counted is never a
        candidate: it was the only bucket under its limit once the table filled with
        saturated ones, so it evicted itself on every request and the caller was admitted
        without bound. And when nothing is evictable the request is refused rather than
        admitted, because a limiter that runs out of bookkeeping must deny, not wave through.
        """
        if len(self._hits) <= self._max_keys:
            return True
        cutoff = now - self._window
        expired = [
            k for k, b in self._hits.items() if k != protected and (not b or b[-1] <= cutoff)
        ]
        for key in expired:
            del self._hits[key]
            if len(self._hits) <= self._max_keys:
                return True
        evictable = sorted(
            (b[-1], k) for k, b in self._hits.items() if k != protected and len(b) < self._limit
        )
        for _, key in evictable:
            del self._hits[key]
            if len(self._hits) <= self._max_keys:
                return True
        # Every remaining bucket is at its limit and belongs to someone else. Refuse.
        del self._hits[protected]
        return False

    def allow(self, key: str) -> bool:
        """Record a hit and report whether it is within the limit."""
        now = self._clock()
        bucket = self._prune(key, now)
        if len(bucket) >= self._limit:
            return False
        bucket.append(now)
        return self._evict_if_needed(now, key)

    def retry_after_seconds(self, key: str) -> int:
        """Seconds until the oldest hit in the window expires, for the Retry-After header."""
        bucket = self._hits.get(key)
        if not bucket:
            # A refused key whose bucket was evicted by the fail-closed branch has no history,
            # and answering 0 tells a compliant client to retry immediately, in a tight loop.
            return 1
        remaining = self._window - (self._clock() - bucket[0])
        return max(1, int(remaining) + 1)
