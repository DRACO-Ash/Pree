"""The store's contract: atomic writes, forward migration, and never shrinking on merge."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

from pree import store as store_module
from pree.store import (
    SCHEMA_VERSION,
    JsonStore,
    StoreError,
    merge_without_shrinking,
)


def test_seed_is_idempotent(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "data")
    first = store.seed()
    store.upsert("asset:candidate", {"score": 42.0})
    second = store.seed()
    assert first["schema_version"] == SCHEMA_VERSION
    assert second["assessments"]["asset:candidate"]["score"] == 42.0


def test_a_missing_snapshot_reads_as_empty_rather_than_failing(tmp_path: Path) -> None:
    snapshot = JsonStore(tmp_path / "absent").read()
    assert snapshot == {"schema_version": SCHEMA_VERSION, "assessments": {}, "write_order": []}


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
    # Match the release message specifically. Asserting only StoreError pins nothing the
    # sibling test does not already cover, and leaves an inversion of the masking green.
    with pytest.raises(StoreError, match="could not release"):
        store.upsert("a:b", {"score": 1.0})


def test_the_collection_is_capped_and_the_newest_record_always_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An uncapped whole-file snapshot fills the volume and takes the pod out of service.

    Every upsert rewrites the whole document, so size sets the cost of every write as well as
    the storage ceiling. The data-layer standard requires a cap that never silently loses a
    fresh entry, so the record just written must be the last thing dropped, never the first.
    """
    monkeypatch.setattr(store_module, "MAX_ASSESSMENTS", 5)
    store = JsonStore(tmp_path / "data")
    store.seed()
    for index in range(12):
        snapshot = store.upsert(f"asset-01:cand-{index}", {"score": float(index)})
        assert len(snapshot["assessments"]) <= 5
        # The write that just happened is always present, whatever the cap dropped.
        assert f"asset-01:cand-{index}" in snapshot["assessments"]
    surviving = set(store.read()["assessments"])
    # The five newest by WRITE order, which is not the same as the five highest by name.
    assert surviving == {f"asset-01:cand-{i}" for i in range(7, 12)}, sorted(surviving)
    assert store.read()["write_order"] == [f"asset-01:cand-{i}" for i in range(7, 12)]


def test_re_writing_an_existing_key_does_not_grow_the_collection(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "data")
    store.seed()
    for _ in range(5):
        snapshot = store.upsert("asset-01:cand-01", {"score": 1.0})
    assert len(snapshot["assessments"]) == 1


def test_re_writing_an_existing_key_still_merges_without_shrinking(tmp_path: Path) -> None:
    """The re-insertion that keeps age order must not lose the record's existing fields."""
    store = JsonStore(tmp_path / "data")
    store.seed()
    store.upsert("a:b", {"score": 1.0, "confidence": "low"})
    snapshot = store.upsert("a:b", {"score": 9.0})
    assert snapshot["assessments"]["a:b"] == {"score": 9.0, "confidence": "low"}


def test_an_older_snapshot_without_a_write_order_gains_a_stable_one(tmp_path: Path) -> None:
    """Migration must not invent an age order it cannot know, only give a stable one."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "assessments.json").write_text(
        json.dumps({"schema_version": 1, "assessments": {"b:2": {}, "a:1": {}}}),
        encoding="utf-8",
    )
    snapshot = JsonStore(data_dir).read()
    assert snapshot["write_order"] == ["a:1", "b:2"]


def test_a_write_order_naming_absent_records_is_pruned(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "assessments.json").write_text(
        json.dumps(
            {"schema_version": 1, "assessments": {"a:1": {}}, "write_order": ["gone:9", "a:1"]}
        ),
        encoding="utf-8",
    )
    assert JsonStore(data_dir).read()["write_order"] == ["a:1"]


def test_the_cap_never_drops_the_protected_key_even_if_it_is_the_oldest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct test of the guard, because upsert always appends the written key last.

    The guard is unreachable through upsert by construction, so it is only defence for a
    future caller that does not. Asserting it here is what stops it being removed as dead
    code and the invariant being lost with it.
    """
    monkeypatch.setattr(store_module, "MAX_ASSESSMENTS", 3)
    assessments: dict[str, Any] = {f"a:{i}": {} for i in range(6)}
    order = list(assessments)
    trimmed, kept = store_module._capped(assessments, order, protected="a:0")
    assert "a:0" in trimmed, "the protected key was dropped despite being the oldest"
    assert "a:0" in kept
    assert len(trimmed) == 3
    # The three kept are the protected one plus the two newest, and the order list agrees.
    assert set(trimmed) == {"a:0", "a:4", "a:5"}
    assert kept == ["a:0", "a:4", "a:5"]


def test_a_partial_write_order_still_trims_to_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap's bound holds only while the order covers every stored key exactly once.

    Pruning names that no longer exist enforced one half. Without the other half, a snapshot
    whose order omitted keys retained far more than the cap, and every later write then evicted
    the PREVIOUS write rather than the oldest record, so the store kept stale mass and lost the
    recent watch picture.
    """
    monkeypatch.setattr(store_module, "MAX_ASSESSMENTS", 5)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    untracked = {f"a:{i}": {"score": float(i)} for i in range(40)}
    (data_dir / "assessments.json").write_text(
        json.dumps({"schema_version": 1, "assessments": untracked, "write_order": []}),
        encoding="utf-8",
    )
    store = JsonStore(data_dir)
    snapshot = store.upsert("a:new", {"score": 99.0})
    assert len(snapshot["assessments"]) == 5, len(snapshot["assessments"])
    assert "a:new" in snapshot["assessments"]
    assert len(snapshot["write_order"]) == 5


def test_a_duplicated_write_order_entry_does_not_under_trim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing one duplicate from the order frees nothing from the collection."""
    monkeypatch.setattr(store_module, "MAX_ASSESSMENTS", 3)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "assessments.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "assessments": {f"a:{i}": {} for i in range(6)},
                "write_order": ["a:0"] * 6,
            }
        ),
        encoding="utf-8",
    )
    snapshot = JsonStore(data_dir).upsert("a:new", {"score": 1.0})
    assert len(snapshot["assessments"]) == 3
    assert "a:new" in snapshot["assessments"]


def test_the_write_order_always_covers_every_stored_key_exactly_once(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "assessments.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "assessments": {"a:1": {}, "a:2": {}, "a:3": {}},
                "write_order": ["a:2", "a:2", "gone:9"],
            }
        ),
        encoding="utf-8",
    )
    snapshot = JsonStore(data_dir).read()
    order = snapshot["write_order"]
    assert sorted(order) == ["a:1", "a:2", "a:3"]
    assert len(order) == len(set(order))
    assert order[0] == "a:2", "a tracked key keeps its position ahead of untracked ones"


def test_a_non_object_assessment_value_is_dropped_rather_than_crashing(
    tmp_path: Path,
) -> None:
    """A malformed value raised ValueError, which the store's error contract does not cover.

    The result was a framework 500 with no audit line, where every other storage fault is a
    handled 503. Validating the container but not its values left the fail-closed claim one
    level short.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "assessments.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "assessments": {"good:one": {"score": 1.0}, "bad:two": "not an object"},
            }
        ),
        encoding="utf-8",
    )
    store = JsonStore(data_dir)
    snapshot = store.read()
    assert "good:one" in snapshot["assessments"]
    assert "bad:two" not in snapshot["assessments"]
    # And the merge path no longer raises on it.
    assert store.upsert("bad:two", {"score": 2.0})["assessments"]["bad:two"] == {"score": 2.0}


def test_a_deeply_nested_snapshot_fails_closed_rather_than_crashing(tmp_path: Path) -> None:
    """RecursionError is a RuntimeError, so it escaped the store's declared error contract.

    A snapshot deep enough to exhaust the parser produced a framework 500 with no hardening
    headers and no audit line, where every other storage fault is a handled 503. That
    falsified three register rows at once.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    depth = 20_000
    (data_dir / "assessments.json").write_text("[" * depth + "]" * depth, encoding="utf-8")
    with pytest.raises(StoreError, match="unreadable"):
        JsonStore(data_dir).read()


def test_the_shipped_collection_cap_matches_the_sheet_and_the_write_budget() -> None:
    """Every other cap test monkeypatches the constant, so the SHIPPED value was unpinned.

    Setting MAX_ASSESSMENTS to 10**9 left the whole suite green. The mechanism was thoroughly
    tested at 3 and at 5 records and the number that actually ships was tested nowhere, which
    is the same class of gap as a guard asserting its own constant.
    """
    sheet = (Path(__file__).resolve().parent.parent / "docs" / "DEPLOYMENT.md").read_text(
        encoding="utf-8"
    )
    stated = re.search(r"capped at (\d+)\s*\n?records", sheet) or re.search(
        r"capped at (\d+)", sheet
    )
    assert stated is not None, "the deployment sheet states no record cap for the operator"
    assert int(stated.group(1)) == store_module.MAX_ASSESSMENTS, (
        f"the sheet documents a cap of {stated.group(1)} records but the code ships "
        f"{store_module.MAX_ASSESSMENTS}"
    )

    # And the cap has to fit the volume. The sheet publishes 1258 bytes per record measured on
    # this build; the snapshot is rewritten whole on every upsert, and a backup copy sits
    # beside it, so the steady-state floor is twice the collection plus the temporary file.
    measured_bytes_per_record = 1258
    peak_bytes = store_module.MAX_ASSESSMENTS * measured_bytes_per_record * 3
    assert peak_bytes <= 64 * 1024 * 1024, (
        f"at the shipped cap the volume holds up to {peak_bytes / 1024 / 1024:.0f} MiB across "
        "the snapshot, its backup and the temporary file, which is not a bounded footprint"
    )
