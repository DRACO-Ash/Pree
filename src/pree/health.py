"""Health, storage proof, and the diagnostics read-out.

Three probe shapes, deliberately distinct:

* Liveness. The conventional paths return 200 and touch nothing. Nothing they do can hang,
  because a liveness probe that hangs turns an infrastructure fault into a silent pod kill
  with no narrative to diagnose.
* Storage proof. A separate path performs a real WRITE, not an existence check, because an
  existence check passes on a read-only or root-owned mount. It races a hard timeout
  strictly shorter than the platform probe, and its 503 body names the resolved directory
  and the exact errno so a screenshot is a full diagnosis. Concurrent callers join the probe
  already in flight rather than each costing a write, so this unauthenticated path cannot be
  used to amplify writes or to move the verdict in either direction. It does still occupy a
  request worker for the length of the probe it joins, which is bounded by the probe budget.
* Diagnostics. A secret-free read-out with every plausible field present at once, reporting
  each critical input as a boolean and a length, never a value.
"""

from __future__ import annotations

import errno
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
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

    @property
    def status(self) -> str:
        return "ready" if self.writable else "unready"

    def as_body(self) -> dict[str, Any]:
        """The response body. Carries the errno, never file contents.

        The resolved absolute directory appears only on FAILURE. This path is unauthenticated
        by design, so on success it was publishing the container's filesystem layout to anyone
        who asked, while `/diagnostics` gated the very same field on the stated reasoning that
        configuration detail "narrows an attacker's search space for free". The deployment sheet
        described the directory as appearing in the 503 body, which was true of the sheet and
        not of the code.

        On failure the disclosure earns its place: the whole point of this path is that a
        screenshot of the 503 is a complete diagnosis, and the directory is half of it.
        """
        body: dict[str, Any] = {
            "status": self.status,
            "storage_writable": self.writable,
            "errno": self.errno_code,
            "errno_name": self.errno_name,
            "probe_duration_ms": self.duration_ms,
            "probe_timeout_ms": int(STORAGE_PROBE_TIMEOUT_SECONDS * 1000),
        }
        if not self.writable:
            body["data_dir"] = self.data_dir
        return body


def _write_probe(data_dir: Path) -> None:
    """Write and remove a probe file. Raises OSError if the mount refuses the write."""
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / _PROBE_FILENAME
    target.write_text(str(os.getpid()), encoding="utf-8")
    target.unlink(missing_ok=True)


class StorageProber:
    """Runs the storage probe single-flight, with a short result cache.

    Concurrent callers JOIN the probe already in flight rather than being handed a synthesised
    verdict. That is the whole design, and it replaces two heuristics that each failed in a
    different direction:

    * Guessing "busy" from slots still inside their timeout called a broken volume ready,
      because a flooder takes each slot the moment one frees, so the newest was always fresh.
    * Guessing "busy" from the absence of a recent success called a healthy volume unready.
      At the shipped parameters the result cache expired one instant before that grace did,
      and an unauthenticated flood of this deliberately unmetered path could hold the window
      open and restart the pod.

    A joiner cannot be wrong about the volume, because it reports what the real probe reports.
    A wedged mount still answers unready: a joiner waits only until the in-flight probe's own
    budget expires, which for a stuck probe is immediately.
    """

    def __init__(
        self,
        max_workers: int = 2,
        cache_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="storage-probe"
        )
        self._guard = threading.Lock()
        self._current: Future[float] | None = None
        self._current_started = 0.0
        self._cached: StorageProbe | None = None
        self._cached_at = 0.0

    def _fresh_enough(self) -> StorageProbe | None:
        with self._guard:
            if self._cached is None:
                return None
            if self._clock() - self._cached_at > self._cache_seconds:
                return None
            return self._cached

    def _remember(self, probe: StorageProbe, observed_at: float) -> StorageProbe:
        """Cache a verdict, stamped with when the probe STARTED.

        Stamping at observation time made the worst-case staleness the cache window plus the
        write latency, which is a different and larger number than the one documented.
        """
        with self._guard:
            self._cached = probe
            self._cached_at = observed_at
        return probe

    def _timed_write(self, data_dir: Path) -> float:
        """Perform the write and return ITS OWN duration.

        The budget is a statement about the write, not about how promptly a caller managed to
        observe it. Measuring the observer's clock reading charged an observer's own delay to
        the mount, so a descheduled request could report a healthy volume as timed out.
        """
        started = self._clock()
        _write_probe(data_dir)
        return self._clock() - started

    def _clear(self, future: Future[float]) -> None:
        with self._guard:
            if self._current is future:
                self._current = None

    def _join_or_start(self, data_dir: Path) -> tuple[Future[float], float]:
        """Return the in-flight probe, starting one only if none is running."""
        with self._guard:
            if self._current is not None:
                return self._current, self._current_started
            future = self._executor.submit(self._timed_write, data_dir)
            self._current = future
            self._current_started = self._clock()
            started = self._current_started
        # Registered OUTSIDE the guard, deliberately. add_done_callback runs the callback
        # inline on the calling thread when the future is ALREADY complete, which is exactly
        # what a write that fails instantly does, and the callback takes this same
        # non-reentrant guard. Holding it here was a self-deadlock on the health path, in the
        # refused-mount case the probe exists to report.
        future.add_done_callback(lambda _: self._clear(future))
        return future, started

    def probe(self, data_dir: Path) -> StorageProbe:
        """Prove storage with a real write, racing the in-flight probe's own hard timeout."""
        cached = self._fresh_enough()
        if cached is not None:
            return cached
        directory = str(data_dir)
        future, started = self._join_or_start(data_dir)
        remaining = max(0.0, started + STORAGE_PROBE_TIMEOUT_SECONDS - self._clock())
        try:
            duration = future.result(timeout=remaining)
        except FutureTimeout:
            elapsed = int((self._clock() - started) * 1000)
            return StorageProbe(False, directory, errno.ETIMEDOUT, "ETIMEDOUT", elapsed)
        except OSError as exc:
            elapsed = int((self._clock() - started) * 1000)
            code = exc.errno
            name = errno.errorcode.get(code) if code is not None else None
            return self._remember(StorageProbe(False, directory, code, name, elapsed), started)
        elapsed = int(duration * 1000)
        if duration > STORAGE_PROBE_TIMEOUT_SECONDS:
            # The write succeeded, but not inside the budget. A joiner arriving just after a
            # slow write finally lands would otherwise report ready and cache it, so an
            # over-budget mount read healthy to some callers and unready to others. The budget
            # is the contract: a write that misses it is not a pass, whoever observed it.
            return StorageProbe(False, directory, errno.ETIMEDOUT, "ETIMEDOUT", elapsed)
        return self._remember(StorageProbe(True, directory, None, None, elapsed), started)

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
