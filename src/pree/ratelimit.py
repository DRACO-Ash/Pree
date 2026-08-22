"""Two-tier rate limiting.

A coarse limit protects the process from any caller. A finer limit protects the expensive
scoring path specifically. Exceeding either returns 429.

Both tiers key on the PEER ADDRESS. The names below still say "actor", which is the keying this
project deliberately abandoned: the fine tier once keyed on a caller-supplied actor header, so a
fresh label per request bypassed it entirely. The constants keep their names because renaming a
public constant is a change with no security value, and the history is recorded here so nobody
reads the name as a description.

The window is a fixed monotonic bucket, which is cheap and cannot be skewed by a wall-clock
change. Time is injected so the behaviour is testable without sleeping.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

GLOBAL_LIMIT = 240
GLOBAL_WINDOW_SECONDS = 60.0
# Named for history, keyed on the peer address. See the module docstring.
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
        self._guard = threading.Lock()

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
            # pop, not del: the lock rules out a concurrent delete, but a key can also appear
            # in both candidate lists, and a KeyError here is a 500 in place of a 429.
            self._hits.pop(key, None)
            if len(self._hits) <= self._max_keys:
                return True
        evictable = sorted(
            (b[-1], k) for k, b in self._hits.items() if k != protected and len(b) < self._limit
        )
        for _, key in evictable:
            self._hits.pop(key, None)
            if len(self._hits) <= self._max_keys:
                return True
        # Every remaining bucket is at its limit and belongs to someone else. Refuse.
        self._hits.pop(protected, None)
        return False

    def allow(self, key: str) -> bool:
        """Record a hit and report whether it is within the limit.

        Serialised, because the fine limiter is genuinely concurrent. `create_assessment` is a
        sync handler, so Starlette runs it in the anyio threadpool and several threads call this
        at once. Two failures live in the eviction pass: `sorted(...)` iterates the key table
        while another thread's `setdefault` inserts into it, which raises "dictionary changed
        size during iteration"; and two threads can select the same candidate, so the second
        `del` raises KeyError. Each became a 500 with no audit line and none of the hardening
        headers, in place of the 429 the limiter exists to return.

        The lock is held only for the bookkeeping: a few deque operations and, in the saturated
        case, one pass over a table capped at MAX_TRACKED_ACTORS.
        """
        with self._guard:
            now = self._clock()
            bucket = self._prune(key, now)
            if len(bucket) >= self._limit:
                return False
            bucket.append(now)
            return self._evict_if_needed(now, key)

    def retry_after_seconds(self, key: str) -> int:
        """Seconds until the oldest hit in the window expires, for the Retry-After header.

        Under the same lock as `allow`. This is called from the thread pool immediately after
        `allow` returns False, on the same key, and it reads the same shared deque: between the
        emptiness test and `bucket[0]` another thread's prune could empty it, giving IndexError
        where a 429 with a Retry-After belongs. I could not reproduce it (8 threads, 160,000
        calls: zero exceptions) because it needs a whole window to elapse inside a two-bytecode
        gap, but it is the same class as the bug fixed one function above, and "I could not hit
        it" is not a reason to leave a read-modify-write unguarded.
        """
        with self._guard:
            return self._retry_after_locked(key)

    def _retry_after_locked(self, key: str) -> int:
        bucket = self._hits.get(key)
        if not bucket:
            # A refused key whose bucket was evicted by the fail-closed branch has no history,
            # and answering 0 tells a compliant client to retry immediately, in a tight loop.
            return 1
        remaining = self._window - (self._clock() - bucket[0])
        return max(1, int(remaining) + 1)
