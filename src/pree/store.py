"""Atomic JSON store for the assessment snapshot.

Writes go to a temporary file in the same directory and are renamed over the target, so a
crash never leaves a half-written file. The snapshot carries a schema version and is
migrated forward additively on read. Merges never shrink the dataset.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
# The snapshot is one whole-file document rewritten on every write, so its size sets the cost
# of every upsert and the volume only ever grows. Left uncapped, a token holder writing
# distinct asset and candidate pairs at the per-address rate limit adds tens of megabytes a
# day until the volume fills, at which point writes return ENOSPC, the storage proof goes 503
# and the pod is out of service with no application-level way back. The data-layer standard
# requires a capped collection that never silently loses a fresh entry, so the cap keeps the
# newest and the record just written is never the one dropped.
MAX_ASSESSMENTS = 5000
_SNAPSHOT_NAME = "assessments.json"
_BACKUP_SUFFIX = ".bak"
_LOCK_NAME = ".assessments.lock"


class StoreError(RuntimeError):
    """Raised when the store cannot be read or written safely."""


def _empty_snapshot() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "assessments": {}, "write_order": []}


def migrate_forward(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Bring an older snapshot up to the current schema through additive steps.

    An unrecognised key is carried through untouched rather than dropped, so a rollback to a
    newer build does not lose data written by it.
    """
    migrated = dict(snapshot)
    version = migrated.get("schema_version")
    if not isinstance(version, int) or version < 1:
        migrated["schema_version"] = SCHEMA_VERSION
    if not isinstance(migrated.get("assessments"), dict):
        migrated["assessments"] = {}
    order = migrated.get("write_order")
    if not isinstance(order, list) or not all(isinstance(k, str) for k in order):
        # An older snapshot carries no write order. Seed it from the stored keys so the cap has
        # a defined age order from the next write onward; the relative age of pre-existing
        # records is genuinely unknown and is not invented, only given a stable order.
        order = sorted(migrated["assessments"])
    migrated["write_order"] = [k for k in order if k in migrated["assessments"]]
    return migrated


def _capped(
    assessments: dict[str, Any], order: list[str], protected: str
) -> tuple[dict[str, Any], list[str]]:
    """Bound the collection, dropping the oldest first and never the record just written.

    Age comes from an explicit write-order list, not from dict insertion order. The snapshot
    is serialised with sorted keys, so object order does NOT survive the round trip: relying
    on it dropped whichever key sorted first alphabetically rather than the oldest one.
    """
    if len(assessments) <= MAX_ASSESSMENTS:
        return assessments, order
    trimmed = dict(assessments)
    kept_order = list(order)
    surplus = len(trimmed) - MAX_ASSESSMENTS
    for key in list(kept_order):
        if surplus == 0:
            break
        if key == protected:
            continue
        kept_order.remove(key)
        trimmed.pop(key, None)
        surplus -= 1
    return trimmed, kept_order


def merge_without_shrinking(stored: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Merge an update into stored state without deleting keys the update omitted.

    A partial payload must never remove a field the caller did not send. Where both sides
    hold a mapping the merge recurses; otherwise the update wins for the keys it names.
    """
    merged = dict(stored)
    for key, value in update.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = merge_without_shrinking(existing, value)
        else:
            merged[key] = value
    return merged


class JsonStore:
    """A single-file JSON store on the platform volume."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._path = data_dir / _SNAPSHOT_NAME

    @property
    def path(self) -> Path:
        return self._path

    @property
    def _backup_path(self) -> Path:
        return self._path.with_suffix(self._path.suffix + _BACKUP_SUFFIX)

    def _readable_primary(self) -> Path | None:
        """The primary, but only if it parses. Otherwise there is nothing worth backing up.

        Copying an unparseable primary over a good backup destroyed the only remaining copy,
        so one corruption became unrecoverable on the very next write.
        """
        try:
            if not self._path.exists():
                return None
            self._load(self._path)
        except (OSError, StoreError):
            return None
        return self._path

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Hold an exclusive lock across a whole read-modify-write.

        The image runs more than one worker, and the snapshot is a single whole-file document,
        so two unsynchronised upserts interleave and one loses its record. An advisory lock on
        a sibling file serialises them. The lock is per-volume, so it holds across workers in
        the same pod, which is exactly the scope that has the problem.
        """
        lock_path = self._data_dir / _LOCK_NAME
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a+")
        except OSError as exc:
            raise StoreError(f"could not acquire the store lock at {lock_path}") from exc
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                raise StoreError(f"could not lock the store at {lock_path}") from exc
            try:
                yield
            finally:
                # A failed release must not escape as a bare OSError either: flock was the one
                # filesystem call in this method left outside the wrap.
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError as exc:
                    raise StoreError(f"could not release the store lock at {lock_path}") from exc
        finally:
            handle.close()

    def seed(self) -> dict[str, Any]:
        """Create the snapshot if it is absent. Idempotent on re-run, and safe under a race."""
        with self._exclusive():
            if not self._path.exists() and not self._backup_path.exists():
                self.write(_empty_snapshot())
            return self.read()

    def read(self) -> dict[str, Any]:
        """Read and migrate the snapshot.

        A missing primary snapshot falls back to the backup before it reads as empty. Without
        that fallback an interrupted write would present a populated store as an empty one,
        the API would answer 404 for records that still exist, and the next successful write
        would overwrite the backup and make the loss permanent.
        """
        try:
            primary_exists = self._path.exists()
            backup_exists = self._backup_path.exists()
        except OSError as exc:
            # Path.exists does not swallow EACCES, so an unreadable directory raises here
            # rather than reporting False.
            raise StoreError(f"could not stat the snapshot at {self._path}") from exc
        source = self._path
        if not primary_exists:
            if backup_exists:
                # Decisive line: a recovery is a fact the operator needs, not an internal detail.
                print(
                    f"pree store: primary snapshot absent, recovering from {self._backup_path}",
                    flush=True,
                )
                source = self._backup_path
            else:
                return _empty_snapshot()
        try:
            return self._load(source)
        except StoreError:
            if source == self._path and backup_exists:
                print(
                    f"pree store: primary snapshot unreadable, recovering from {self._backup_path}",
                    flush=True,
                )
                return self._load(self._backup_path)
            raise

    def _load(self, source: Path) -> dict[str, Any]:
        """Parse and migrate one snapshot file, or fail closed."""
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StoreError(f"snapshot at {source} is unreadable") from exc
        if not isinstance(raw, dict):
            raise StoreError(f"snapshot at {source} is not a JSON object")
        return migrate_forward(raw)

    def write(self, snapshot: dict[str, Any]) -> None:
        """Write the snapshot atomically, leaving the live file intact until the swap.

        The order matters and is the whole point. The replacement is written and flushed
        first, the current snapshot is COPIED to the backup second, and only then does the
        atomic rename take place. Moving the live file aside first, as an earlier version of
        this method did, means a refused write leaves no snapshot at all: the dataset then
        reads as empty and the next write destroys the backup too.
        """
        payload = json.dumps(snapshot, indent=2, sort_keys=True)
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            handle, tmp_name = tempfile.mkstemp(dir=self._data_dir, suffix=".tmp")
        except OSError as exc:
            raise StoreError(f"could not open a temporary file in {self._data_dir}") from exc
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            previous = self._readable_primary()
            if previous is not None:
                shutil.copy2(previous, self._backup_path)
            tmp_path.replace(self._path)
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            raise StoreError(f"could not write snapshot to {self._path}") from exc

    def upsert(self, key: str, record: dict[str, Any]) -> dict[str, Any]:
        """Merge one record into the snapshot without shrinking it, then persist.

        The read, the merge and the write are held under one exclusive lock, so a concurrent
        worker cannot read the same snapshot and overwrite this record.
        """
        with self._exclusive():
            snapshot = self.read()
            assessments = dict(snapshot["assessments"])
            assessments[key] = merge_without_shrinking(assessments.get(key, {}), record)
            # Age comes from an explicit list, because the snapshot is written with sorted
            # keys and object order does not survive the round trip. The written key goes to
            # the end, so it is the last thing the cap would ever drop.
            order = [k for k in snapshot["write_order"] if k != key]
            order.append(key)
            snapshot["assessments"], snapshot["write_order"] = _capped(assessments, order, key)
            self.write(snapshot)
        return snapshot
