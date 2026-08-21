"""Health, storage proof, and the diagnostics read-out.

Three probe shapes, deliberately distinct:

* Liveness. The conventional paths return 200 and touch nothing. Nothing they do can hang,
  because a liveness probe that hangs turns an infrastructure fault into a silent pod kill
  with no narrative to diagnose.
* Storage proof. A separate path performs a real WRITE, not an existence check, because an
  existence check passes on a read-only or root-owned mount. It races a hard timeout
  strictly shorter than the platform probe, and its 503 body names the resolved directory
  and the exact errno so a screenshot is a full diagnosis.
* Diagnostics. A secret-free read-out with every plausible field present at once, reporting
  each critical input as a boolean and a length, never a value.
"""

from __future__ import annotations

import errno
import os
import time
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

    def as_body(self) -> dict[str, Any]:
        """The response body. Carries the directory and the errno, never file contents."""
        return {
            "status": "ready" if self.writable else "unready",
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


def probe_storage(data_dir: Path, executor: ThreadPoolExecutor) -> StorageProbe:
    """Prove storage with a real write, racing a hard timeout.

    The write runs on a worker thread so a hung mount cannot block the event loop. A timeout
    is reported as ETIMEDOUT, which is the honest reading: the mount neither accepted nor
    refused the write.
    """
    started = time.monotonic()
    directory = str(data_dir)
    try:
        executor.submit(_write_probe, data_dir).result(timeout=STORAGE_PROBE_TIMEOUT_SECONDS)
    except FutureTimeout:
        elapsed = int((time.monotonic() - started) * 1000)
        return StorageProbe(False, directory, errno.ETIMEDOUT, "ETIMEDOUT", elapsed)
    except OSError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        code = exc.errno
        name = errno.errorcode.get(code) if code is not None else None
        return StorageProbe(False, directory, code, name, elapsed)
    return StorageProbe(True, directory, None, None, int((time.monotonic() - started) * 1000))


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
        "data_dir_is_absolute": config.data_dir.is_absolute(),
        "storage_writable": probe.writable,
        "storage_errno": probe.errno_code,
        "storage_errno_name": probe.errno_name,
        "identity_uid": os.getuid(),
        "identity_gid": os.getgid(),
        "identity_is_root": os.getuid() == 0,
    }
