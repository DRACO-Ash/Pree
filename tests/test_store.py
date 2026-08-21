"""The store's contract: atomic writes, forward migration, and never shrinking on merge."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import tempfile
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

    # The PRIMARY must still be there. Asserting only that the records are readable passes
    # under the old ordering too, because the backup fallback recovers them.
    assert (tmp_path / "data" / "assessments.json").exists()
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


@pytest.mark.parametrize("failing_call", ["mkdir", "open", "mkstemp", "exists", "flock"])
def test_every_filesystem_refusal_surfaces_as_a_store_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_call: str
) -> None:
    """The store's contract is StoreError, and the app handles exactly that.

    Four call sites raised bare OSError, so on the documented root-owned mount the API returned
    a framework 500 with no audit line: the two controls the security policy claims for that
    exact state were false.
    """
    store = JsonStore(tmp_path / "data")
    store.seed()

    def refuse(*_: object, **__: object) -> object:
        raise PermissionError(errno.EACCES, "permission denied")

    targets: dict[str, tuple[object, str]] = {
        "mkdir": (Path, "mkdir"),
        "open": (Path, "open"),
        "mkstemp": (tempfile, "mkstemp"),
        "exists": (Path, "exists"),
        "flock": (fcntl, "flock"),
    }
    owner, attribute = targets[failing_call]
    monkeypatch.setattr(owner, attribute, refuse)
    with pytest.raises(StoreError):
        store.upsert("a:b", {"score": 1.0})


def test_a_corrupt_primary_recovers_from_the_backup(tmp_path: Path) -> None:
    """Fail-closed is not the same as unrecoverable when a good backup is sitting there."""
    data_dir = tmp_path / "data"
    store = JsonStore(data_dir)
    store.seed()
    store.upsert("keep:one", {"score": 1.0})
    store.upsert("keep:two", {"score": 2.0})
    (data_dir / "assessments.json").write_text("{truncated", encoding="utf-8")
    recovered = store.read()["assessments"]
    # The backup lags the primary by one write by construction, so keep:two is expected to be
    # absent. Stating the exact surviving set is the point: a membership check would pass even
    # if recovery silently returned a single stale record.
    assert sorted(recovered) == ["keep:one"]


def test_a_recovery_does_not_destroy_the_backup_it_recovered_from(tmp_path: Path) -> None:
    """The recovery used to poison its own safety net.

    After recovering from the backup, the next write copied the still-corrupt primary over the
    good backup, so a second corruption was unrecoverable and every later read raised. The
    backup is now refreshed only from a primary that parses.
    """
    data_dir = tmp_path / "data"
    store = JsonStore(data_dir)
    store.seed()
    store.upsert("keep:one", {"score": 1.0})
    store.upsert("keep:two", {"score": 2.0})
    (data_dir / "assessments.json").write_text("{truncated", encoding="utf-8")

    store.upsert("new:three", {"score": 3.0})

    backup = json.loads((data_dir / "assessments.json.bak").read_text(encoding="utf-8"))
    assert isinstance(backup, dict), "the backup was overwritten with the corrupt primary"
    assert "keep:one" in backup["assessments"]
    # And a second corruption is still survivable, which is the whole point of a backup.
    (data_dir / "assessments.json").write_text("{corrupt again", encoding="utf-8")
    assert "keep:one" in store.read()["assessments"]


def test_a_failed_lock_release_surfaces_as_a_store_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release half of the lock wrap, which the acquire test cannot reach.

    The parametrised flock case raises on LOCK_EX, so the context manager never enters its
    body and the LOCK_UN handler never runs. Deleting that handler therefore left the whole
    suite green, which means half of the "every filesystem refusal is a handled 503" control
    asserted nothing and was free to regress into a bare OSError.
    """
    store = JsonStore(tmp_path / "data")
    store.seed()
    real = fcntl.flock

    def refuse_release(fd: int, operation: int) -> None:
        if operation == fcntl.LOCK_UN:
            raise OSError(errno.EIO, "device refused the unlock")
        real(fd, operation)

    monkeypatch.setattr(fcntl, "flock", refuse_release)
    with pytest.raises(StoreError, match="could not release"):
        store.upsert("a:b", {"score": 1.0})


def test_a_failed_release_masks_the_body_error_but_keeps_the_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raising from inside `finally` replaces an in-flight exception from the body.

    Both are StoreError, so the caller's contract holds either way. Pinning it here is what
    stops the nesting being "simplified" into something that leaks a bare OSError.
    """
    store = JsonStore(tmp_path / "data")
    store.seed()
    real = fcntl.flock

    def refuse_release(fd: int, operation: int) -> None:
        if operation == fcntl.LOCK_UN:
            raise OSError(errno.EIO, "device refused the unlock")
        real(fd, operation)

    def refuse_write(*_: object, **__: object) -> object:
        raise PermissionError(errno.EACCES, "permission denied")

    monkeypatch.setattr(fcntl, "flock", refuse_release)
    monkeypatch.setattr(tempfile, "mkstemp", refuse_write)
    with pytest.raises(StoreError):
        store.upsert("a:b", {"score": 1.0})
