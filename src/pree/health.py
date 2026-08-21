"""Health, storage proof, and the diagnostics read-out.

Three probe shapes, deliberately distinct:

* Liveness. The conventional paths return 200 and touch nothing. Nothing they do can hang,
  because a liveness probe that hangs turns an infrastructure fault into a silent pod kill
  with no narrative to diagnose.
* Storage proof. A separate path performs a real WRITE, not an existence check, because an
  existence check passes on a read-only or root-owned mount. It races a hard timeout
  strictly shorter than the platform probe, and its 503 body names the resolved directory
  and the exact errno so a screenshot is a full diagnosis. A saturated probe pool is itself
  reported, as EBUSY, rather than being mistaken for a timeout.
* Diagnostics. A secret-free read-out with every plausible field present at once, reporting
  each critical input as a boolean and a length, never a value.
"""

from __future__ import annotations

import errno
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config

# Strictly shorter than the platform's readiness probe timeout, so this path always answers
# with a diagnosis rather than being cut off mid-write by the platform.
STORAGE_PROBE_TIMEOUT_SECONDS = 1.5
_PROBE_FILENAME = ".pree-write-probe"


@dataclass(frozen=True, slots=True)
class StorageProbe:
    """The outcome of a real write against the resolved data directory."""

    writable: bool
    data_dir: str
    errno_code: int | None
    errno_name: str | None
    duration_ms: int
    # True when the probe could not be taken at all, as opposed to taken and refused. A
    # saturated pool is not evidence that storage is broken, so it must not be reported as
    # though it were: doing so let three concurrent sockets turn the container HEALTHCHECK red.
    indeterminate: bool = False

    @property
    def status(self) -> str:
        if self.writable:
            return "ready"
        return "unknown" if self.indeterminate else "unready"

    def as_body(self) -> dict[str, Any]:
        """The response body. Carries the directory and the errno, never file contents."""
        return {
            "status": self.status,
            "storage_writable": self.writable,
            "data_dir": self.data_dir,
            "errno": self.errno_code,
            "errno_name": self.errno_name,
            "probe_duration_ms": self.duration_ms,
            "probe_timeout_ms": int(STORAGE_PROBE_TIMEOUT_SECONDS * 1000),
        }


def _write_probe(data_dir: Path) -> None:
    """Write and remove a probe file. Raises OSError if the mount refuses the write."""
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / _PROBE_FILENAME
    target.write_text(str(os.getpid()), encoding="utf-8")
    target.unlink(missing_ok=True)


class StorageProber:
    """Runs the storage probe on a bounded pool, with a short result cache.

    Two properties matter more than the write itself:

    * A probe that overruns its timeout must still free its slot when the worker eventually
      returns. Releasing only on the success and error paths meant a slow-but-successful
      volume pinned the pool at capacity forever, so a healthy mount reported EBUSY on every
      later probe and the container restarted in a loop.
    * Concurrent callers must not each cost a write. The probe path is unauthenticated by
      contract, so without a cache a handful of sockets could saturate the pool at will. One
      write serves every caller inside the cache window.
    """

    def __init__(
        self,
        max_workers: int = 2,
        cache_seconds: float = 2.0,
        success_grace_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_workers = max_workers
        self._cache_seconds = cache_seconds
        # A busy verdict needs POSITIVE evidence that storage works: a probe that completed
        # inside its budget, recently. Inferring "busy" from the absence of evidence let an
        # unauthenticated flood of this unmetered path hold every slot with a probe that was
        # always freshly started, so a mount whose writes overran the budget reported ready
        # indefinitely and the container's three-strike check never fired.
        self._success_grace = (
            cache_seconds if success_grace_seconds is None else success_grace_seconds
        )
        self._clock = clock
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="storage-probe"
        )
        self._guard = threading.Lock()
        # Start time per in-flight probe, so a slot held by a probe that has already overrun
        # its timeout can be told apart from one held by a probe still within it. Without that
        # distinction a permanently wedged mount looked merely busy, so the endpoint whose
        # whole purpose is catching a refused mount answered 200 forever.
        self._in_flight: dict[int, float] = {}
        self._next_id = 0
        self._cached: StorageProbe | None = None
        self._cached_at = 0.0
        self._last_success_at: float | None = None

    def _claim(self) -> int | None:
        with self._guard:
            if len(self._in_flight) >= self._max_workers:
                return None
            self._next_id += 1
            token = self._next_id
            self._in_flight[token] = self._clock()
            return token

    def _release(self, token: int) -> None:
        with self._guard:
            self._in_flight.pop(token, None)

    def _recently_succeeded(self) -> bool:
        """True when a probe completed inside its budget within the grace window.

        This is the only thing that justifies calling a saturated pool merely busy. Without
        it, a caller that keeps every slot occupied with freshly started probes makes the
        pool look healthy no matter how badly storage is behaving.
        """
        with self._guard:
            if self._last_success_at is None:
                return False
            return self._clock() - self._last_success_at <= self._success_grace

    def _all_in_flight_overran(self) -> bool:
        """True when every held slot belongs to a probe that has already timed out."""
        now = self._clock()
        with self._guard:
            if not self._in_flight:
                return False
            return all(
                now - started >= STORAGE_PROBE_TIMEOUT_SECONDS
                for started in self._in_flight.values()
            )

    def _fresh_enough(self) -> StorageProbe | None:
        with self._guard:
            if self._cached is None:
                return None
            if self._clock() - self._cached_at > self._cache_seconds:
                return None
            return self._cached

    def _remember(self, probe: StorageProbe) -> StorageProbe:
        with self._guard:
            self._cached = probe
            self._cached_at = self._clock()
        return probe

    def probe(self, data_dir: Path) -> StorageProbe:
        """Prove storage with a real write, racing a hard timeout."""
        cached = self._fresh_enough()
        if cached is not None:
            return cached
        directory = str(data_dir)
        token = self._claim()
        if token is None:
            if self._all_in_flight_overran() or not self._recently_succeeded():
                # Either every worker is stuck past its timeout, or nothing has completed
                # inside its budget recently. Both are unready, and reporting them as merely
                # busy is a fail-open an unauthenticated caller can trigger at will.
                return StorageProbe(
                    False, directory, errno.ETIMEDOUT, "ETIMEDOUT", 0, indeterminate=False
                )
            # Genuinely busy: storage demonstrably worked a moment ago and the slots are held
            # by probes still inside their timeout. Not evidence about the volume either way.
            return StorageProbe(False, directory, errno.EBUSY, "EBUSY", 0, indeterminate=True)
        started = self._clock()
        future = self._executor.submit(_write_probe, data_dir)
        # The slot is freed by the worker itself, whenever it finishes, including long after
        # this call has already given up and returned a timeout.
        future.add_done_callback(lambda _: self._release(token))
        try:
            future.result(timeout=STORAGE_PROBE_TIMEOUT_SECONDS)
        except FutureTimeout:
            elapsed = int((self._clock() - started) * 1000)
            return StorageProbe(False, directory, errno.ETIMEDOUT, "ETIMEDOUT", elapsed)
        except OSError as exc:
            elapsed = int((self._clock() - started) * 1000)
            code = exc.errno
            name = errno.errorcode.get(code) if code is not None else None
            return self._remember(StorageProbe(False, directory, code, name, elapsed))
        elapsed = int((self._clock() - started) * 1000)
        with self._guard:
            self._last_success_at = self._clock()
        return self._remember(StorageProbe(True, directory, None, None, elapsed))

    def shutdown(self) -> None:
        """Release the pool. A blocked worker is not waited on; it would never return."""
        self._executor.shutdown(wait=False)


def diagnostics(config: Config, probe: StorageProbe) -> dict[str, Any]:
    """A secret-free read-out. Every critical input is a boolean and a length, never a value.

    Every plausible field is present at once, so one read-out answers the whole question
    rather than prompting a second round trip during a deploy failure.
    """
    token = config.team_token
    origin = config.allowed_origin
    return {
        "build_id": config.build_id,
        "environment": config.environment,
        "port": config.port,
        "auth_enabled": config.auth_enabled,
        "team_token_present": token is not None,
        "team_token_length": len(token) if token else 0,
        "allowed_origin_present": origin is not None,
        "allowed_origin_length": len(origin) if origin else 0,
        "allowed_origin_is_wildcard": origin == "*",
        "data_dir": str(config.data_dir),
        # The resolved path is always absolute, so reporting that says nothing. What the
        # operator needs is whether the resolved directory is the one they configured.
        "data_dir_is_absolute": config.data_dir.is_absolute(),
        "data_dir_was_configured": config.data_dir_was_configured,
        "storage_writable": probe.writable,
        "storage_errno": probe.errno_code,
        "storage_errno_name": probe.errno_name,
        "identity_uid": os.getuid(),
        "identity_gid": os.getgid(),
        "identity_is_root": os.getuid() == 0,
    }
