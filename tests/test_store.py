"""The store's contract: atomic writes, forward migration, and never shrinking on merge."""

from __future__ import annotations

import errno
import json
import os
import threading
from pathlib import Path

import pytest

from pree.store import SCHEMA_VERSION, JsonStore, StoreError, merge_without_shrinking


def test_seed_is_idempotent(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "data")
    first = store.seed()
    store.upsert("asset:candidate", {"score": 42.0})
    second = store.seed()
    assert first["schema_version"] == SCHEMA_VERSION
    assert second["assessments"]["asset:candidate"]["score"] == 42.0


def test_a_missing_snapshot_reads_as_empty_rather_than_failing(tmp_path: Path) -> None:
    snapshot = JsonStore(tmp_path / "absent").read()
    assert snapshot == {"schema_version": SCHEMA_VERSION, "assessments": {}}


def test_write_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    store = JsonStore(data_dir)
    store.seed()
    store.upsert("a:b", {"score": 1.0})
    assert not list(data_dir.glob("*.tmp"))


def test_write_backs_up_the_previous_snapshot_before_replacing_it(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "data")
    store.seed()
    store.upsert("a:b", {"score": 1.0})
    store.upsert("a:b", {"score": 2.0})
    backups = list((tmp_path / "data").glob("*.bak"))
    assert backups, "expected a backup of the prior snapshot"


def test_an_unreadable_snapshot_fails_closed(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "assessments.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(StoreError):
        JsonStore(data_dir).read()


def test_a_snapshot_that_is_not_an_object_fails_closed(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "assessments.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(StoreError):
        JsonStore(data_dir).read()


def test_an_older_snapshot_migrates_forward_without_dropping_unknown_keys(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "assessments.json").write_text(
        json.dumps({"assessments": {"a:b": {"score": 3.0}}, "written_by_a_newer_build": True}),
        encoding="utf-8",
    )
    snapshot = JsonStore(data_dir).read()
    assert snapshot["schema_version"] == SCHEMA_VERSION
    assert snapshot["written_by_a_newer_build"] is True
    assert snapshot["assessments"]["a:b"]["score"] == 3.0


def test_merge_never_deletes_a_key_the_update_omitted() -> None:
    stored = {"score": 10.0, "confidence": "high", "contributions": {"rf": 1.0}}
    merged = merge_without_shrinking(stored, {"score": 20.0})
    assert merged == {"score": 20.0, "confidence": "high", "contributions": {"rf": 1.0}}


def test_merge_recurses_into_nested_mappings() -> None:
    stored = {"contributions": {"rf": 1.0, "geometry": 0.5}}
    merged = merge_without_shrinking(stored, {"contributions": {"rf": 0.0}})
    assert merged["contributions"] == {"rf": 0.0, "geometry": 0.5}


def test_upsert_merges_rather_than_replacing_the_record(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "data")
    store.seed()
    store.upsert("a:b", {"score": 1.0, "confidence": "low"})
    snapshot = store.upsert("a:b", {"score": 9.0})
    assert snapshot["assessments"]["a:b"] == {"score": 9.0, "confidence": "low"}


def test_a_corrupt_schema_version_is_repaired_rather_than_trusted(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "assessments.json").write_text(
        json.dumps({"schema_version": "one", "assessments": "not a mapping"}), encoding="utf-8"
    )
    snapshot = JsonStore(data_dir).read()
    assert snapshot["schema_version"] == SCHEMA_VERSION
    assert snapshot["assessments"] == {}


def test_the_store_exposes_its_resolved_path_for_diagnosis(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "data")
    assert store.path == tmp_path / "data" / "assessments.json"


def test_a_failed_write_fails_closed_and_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-written snapshot must never become the snapshot."""
    data_dir = tmp_path / "data"
    store = JsonStore(data_dir)
    store.seed()

    def refuse(_: int) -> None:
        raise OSError(errno.EIO, "device refused the flush")

    monkeypatch.setattr(os, "fsync", refuse)
    with pytest.raises(StoreError, match="could not write snapshot"):
        store.write({"schema_version": SCHEMA_VERSION, "assessments": {"a:b": {}}})
    assert not list(data_dir.glob("*.tmp"))


def test_a_refused_write_leaves_every_prior_record_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The durability property the earlier no-temporary-file assertion did not cover.

    The previous implementation moved the live snapshot aside before writing its replacement,
    so a refused write left no snapshot at all: the dataset then read as empty, the API
    answered 404 for records that still existed, and the next write destroyed the backup too.
    """
    store = JsonStore(tmp_path / "data")
    store.seed()
    store.upsert("keep:one", {"score": 1.0})
    store.upsert("keep:two", {"score": 2.0})

    def refuse(_: int) -> None:
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(os, "fsync", refuse)
    with pytest.raises(StoreError):
        store.upsert("new:three", {"score": 3.0})
    monkeypatch.undo()

    surviving = store.read()["assessments"]
    assert sorted(surviving) == ["keep:one", "keep:two"]
    assert surviving["keep:one"]["score"] == 1.0
    # And the dataset must not shrink on the next successful write.
    after = store.upsert("new:four", {"score": 4.0})
    assert sorted(after["assessments"]) == ["keep:one", "keep:two", "new:four"]


def test_a_missing_primary_snapshot_recovers_from_the_backup(tmp_path: Path) -> None:
    """An interrupted write must not present a populated store as an empty one."""
    data_dir = tmp_path / "data"
    store = JsonStore(data_dir)
    store.seed()
    store.upsert("keep:one", {"score": 1.0})
    store.upsert("keep:two", {"score": 2.0})
    (data_dir / "assessments.json").unlink()
    recovered = store.read()["assessments"]
    assert "keep:one" in recovered


def test_two_concurrent_upserts_both_survive(tmp_path: Path) -> None:
    """The image runs more than one worker over one whole-file snapshot.

    Without a lock across the read, the merge and the write, two workers interleave and one
    assessment is silently lost under ordinary load, not under attack.
    """
    data_dir = tmp_path / "data"
    JsonStore(data_dir).seed()
    errors: list[BaseException] = []

    def upsert(name: str) -> None:
        try:
            for index in range(15):
                JsonStore(data_dir).upsert(f"{name}:cand-{index}", {"score": float(index)})
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=upsert, args=("worker-a",)),
        threading.Thread(target=upsert, args=("worker-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    keys = JsonStore(data_dir).read()["assessments"]
    assert len([k for k in keys if k.startswith("worker-a:")]) == 15
    assert len([k for k in keys if k.startswith("worker-b:")]) == 15
