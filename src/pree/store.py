"""Atomic JSON store for the assessment snapshot.

Writes go to a temporary file in the same directory and are renamed over the target, so a
crash never leaves a half-written file. The snapshot carries a schema version and is
migrated forward additively on read. Merges never shrink the dataset.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
_SNAPSHOT_NAME = "assessments.json"
_BACKUP_SUFFIX = ".bak"


class StoreError(RuntimeError):
    """Raised when the store cannot be read or written safely."""


def _empty_snapshot() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "assessments": {}}


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
    return migrated


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

    def seed(self) -> dict[str, Any]:
        """Create the snapshot if it is absent. Idempotent on re-run."""
        if not self._path.exists():
            self.write(_empty_snapshot())
        return self.read()

    def read(self) -> dict[str, Any]:
        """Read and migrate the snapshot. A missing file reads as an empty snapshot."""
        if not self._path.exists():
            return _empty_snapshot()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StoreError(f"snapshot at {self._path} is unreadable") from exc
        if not isinstance(raw, dict):
            raise StoreError(f"snapshot at {self._path} is not a JSON object")
        return migrate_forward(raw)

    def write(self, snapshot: dict[str, Any]) -> None:
        """Write the snapshot atomically, backing up any existing file first."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            self._path.replace(self._path.with_suffix(self._path.suffix + _BACKUP_SUFFIX))
        payload = json.dumps(snapshot, indent=2, sort_keys=True)
        handle, tmp_name = tempfile.mkstemp(dir=self._data_dir, suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            tmp_path.replace(self._path)
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            raise StoreError(f"could not write snapshot to {self._path}") from exc

    def upsert(self, key: str, record: dict[str, Any]) -> dict[str, Any]:
        """Merge one record into the snapshot without shrinking it, then persist."""
        snapshot = self.read()
        assessments = dict(snapshot["assessments"])
        assessments[key] = merge_without_shrinking(assessments.get(key, {}), record)
        snapshot["assessments"] = assessments
        self.write(snapshot)
        return snapshot
