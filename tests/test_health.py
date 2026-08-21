"""The storage proof. Its failure paths matter more than its success path, because they are
what a deploy failure actually shows the operator.
"""

from __future__ import annotations

import errno
import threading
import time
from pathlib import Path

import pytest

from pree import health
from pree.health import STORAGE_PROBE_TIMEOUT_SECONDS, StorageProber
from tests.conftest import make_config


def test_a_writable_directory_probes_clean(tmp_path: Path, prober: StorageProber) -> None:
    probe = prober.probe(tmp_path / "data")
    assert probe.writable is True
    assert probe.errno_code is None
    assert probe.as_body()["status"] == "ready"


def test_the_probe_leaves_no_file_behind(tmp_path: Path, prober: StorageProber) -> None:
    data_dir = tmp_path / "data"
    prober.probe(data_dir)
    assert list(data_dir.iterdir()) == []


def test_an_unusable_path_reports_the_directory_and_the_exact_errno(
    tmp_path: Path, prober: StorageProber
) -> None:
    """A file where a directory belongs is the cheapest reproduction of a refused mount."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    probe = prober.probe(blocker / "data")
    body = probe.as_body()
    assert probe.writable is False
    assert body["status"] == "unready"
    assert body["data_dir"] == str(blocker / "data")
    assert body["errno_name"] in {"ENOTDIR", "EEXIST"}
    assert body["errno"] is not None


def test_a_hung_mount_times_out_as_etimedout_rather_than_blocking(
    tmp_path: Path, prober: StorageProber, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe that can hang turns an infrastructure fault into an undiagnosable pod kill."""

    def hang(_: Path) -> None:
        time.sleep(STORAGE_PROBE_TIMEOUT_SECONDS * 3)

    monkeypatch.setattr(health, "_write_probe", hang)
    started = time.monotonic()
    probe = prober.probe(tmp_path / "data")
    elapsed = time.monotonic() - started
    assert probe.writable is False
    assert probe.errno_code == errno.ETIMEDOUT
    assert probe.errno_name == "ETIMEDOUT"
    assert elapsed < STORAGE_PROBE_TIMEOUT_SECONDS * 2


def test_the_probe_timeout_is_shorter_than_a_platform_probe_would_allow() -> None:
    assert 0 < STORAGE_PROBE_TIMEOUT_SECONDS < 5.0


def test_diagnostics_reports_identity_and_never_a_secret_value(
    tmp_path: Path, prober: StorageProber
) -> None:
    config = make_config(tmp_path, PREE_TEAM_TOKEN="a-real-looking-token")
    body = health.diagnostics(config, prober.probe(config.data_dir))
    assert "a-real-looking-token" not in str(body)
    assert body["team_token_length"] == len("a-real-looking-token")
    assert body["identity_is_root"] == (body["identity_uid"] == 0)
    assert body["data_dir_is_absolute"] is True


def test_an_eacces_mount_is_reported_with_its_errno_regardless_of_uid(
    tmp_path: Path, prober: StorageProber, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fsGroup diagnosis must be asserted on a root runner too.

    A permission-mode reproduction is skipped when the tests run as root, which is exactly
    the runner the platform uses, so the EACCES path went unasserted where it matters most.
    Injecting the errno covers it independently of the filesystem.
    """

    def refuse(_: Path) -> None:
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(health, "_write_probe", refuse)
    probe = prober.probe(tmp_path / "data")
    assert probe.writable is False
    assert probe.errno_name == "EACCES"
    assert probe.as_body()["data_dir"] == str(tmp_path / "data")


def test_a_slow_but_successful_probe_frees_its_slot_when_the_worker_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression that mattered most: a healthy volume must not pin the pod unready.

    The slot was released on the success and error paths only, so a write slower than the
    timeout but which eventually SUCCEEDED never freed its worker. Every later probe then
    reported EBUSY on completely healthy storage, and the container restarted in a loop.
    """
    real = health._write_probe

    def slow(path: Path) -> None:
        time.sleep(STORAGE_PROBE_TIMEOUT_SECONDS + 0.5)
        real(path)

    monkeypatch.setattr(health, "_write_probe", slow)
    pool = StorageProber(max_workers=1, cache_seconds=0.0)
    try:
        assert pool.probe(tmp_path / "data").errno_name == "ETIMEDOUT"
        monkeypatch.setattr(health, "_write_probe", real)
        time.sleep(STORAGE_PROBE_TIMEOUT_SECONDS + 1.0)
        recovered = pool.probe(tmp_path / "data")
    finally:
        pool.shutdown()
    assert recovered.writable is True, "the slot was never freed by the finished worker"


def test_concurrent_callers_cost_one_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe path is unauthenticated by contract, so it must not be a write amplifier."""
    calls = {"n": 0}
    real = health._write_probe

    def counted(path: Path) -> None:
        calls["n"] += 1
        real(path)

    monkeypatch.setattr(health, "_write_probe", counted)
    pool = StorageProber(cache_seconds=5.0)
    try:
        results = [pool.probe(tmp_path / "data") for _ in range(10)]
    finally:
        pool.shutdown()
    assert all(p.writable for p in results)
    assert calls["n"] == 1, f"expected one write for ten callers, got {calls['n']}"


def test_a_pool_busy_with_probes_still_inside_their_timeout_is_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slot held by a healthy in-flight probe says nothing about the volume either way.

    A busy verdict now requires positive evidence that storage works, so this test records a
    real success first. Without that evidence the honest answer is unready, which is what
    stops an unauthenticated flood from holding every slot and reporting ready regardless.
    """
    pool = StorageProber(max_workers=1, cache_seconds=0.0, success_grace_seconds=30.0)
    assert pool.probe(tmp_path / "data").writable is True

    running = threading.Event()

    def slow(_: Path) -> None:
        running.set()
        time.sleep(STORAGE_PROBE_TIMEOUT_SECONDS * 0.5)

    monkeypatch.setattr(health, "_write_probe", slow)
    worker = threading.Thread(target=pool.probe, args=(tmp_path / "data",))
    worker.start()
    try:
        assert running.wait(2.0)
        busy = pool.probe(tmp_path / "data")
    finally:
        worker.join()
        pool.shutdown()
    assert busy.errno_name == "EBUSY"
    assert busy.indeterminate is True
    assert busy.status == "unknown"


def test_a_busy_pool_with_no_recent_success_reports_unready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attacker-triggerable fail-open, closed.

    An unauthenticated flood of this unmetered path kept every slot occupied with freshly
    started probes, so a mount whose writes overran the probe budget never had all slots
    overrun at once and the pool reported merely busy. The container's three-strike check
    therefore never fired and a pod that could not complete a write stayed in service.
    """
    running = threading.Event()

    def overruns(_: Path) -> None:
        running.set()
        time.sleep(STORAGE_PROBE_TIMEOUT_SECONDS + 1.0)

    monkeypatch.setattr(health, "_write_probe", overruns)
    pool = StorageProber(max_workers=1, cache_seconds=0.0, success_grace_seconds=30.0)
    worker = threading.Thread(target=pool.probe, args=(tmp_path / "data",))
    worker.start()
    try:
        assert running.wait(2.0)
        verdict = pool.probe(tmp_path / "data")
    finally:
        worker.join()
        pool.shutdown()
    assert verdict.errno_name == "ETIMEDOUT"
    assert verdict.indeterminate is False
    assert verdict.status == "unready"


def test_a_wedged_mount_keeps_answering_unready_rather_than_going_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fail-open that mattered.

    Once every worker was stuck, a saturated pool reported EBUSY, which mapped to 200 and
    "unknown". The container HEALTHCHECK therefore never saw three consecutive failures, so a
    pod with completely unavailable storage stayed in service indefinitely. A pool whose every
    slot has already overrun its timeout is a wedged mount, not a busy pool.
    """
    release = threading.Event()
    monkeypatch.setattr(health, "_write_probe", lambda _: release.wait(30))
    pool = StorageProber(max_workers=2, cache_seconds=0.0)
    try:
        verdicts = [pool.probe(tmp_path / "data") for _ in range(5)]
    finally:
        release.set()
        pool.shutdown()
    assert all(p.errno_name == "ETIMEDOUT" for p in verdicts), [p.errno_name for p in verdicts]
    assert all(p.indeterminate is False for p in verdicts)
    assert all(p.status == "unready" for p in verdicts)


def test_the_cached_result_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The only bound on staleness of the readiness signal, and it was asserted nowhere.

    Deleting the expiry check left the suite green, which meant the endpoint could freeze at
    its first observed value for the life of the worker while storage broke underneath it.
    """
    calls = {"n": 0}
    real = health._write_probe

    def counted(path: Path) -> None:
        calls["n"] += 1
        real(path)

    monkeypatch.setattr(health, "_write_probe", counted)
    now = [1000.0]
    pool = StorageProber(cache_seconds=2.0, clock=lambda: now[0])
    try:
        assert pool.probe(tmp_path / "data").writable is True
        pool.probe(tmp_path / "data")
        assert calls["n"] == 1, "the second call inside the window should have been cached"
        now[0] += 5.0
        pool.probe(tmp_path / "data")
    finally:
        pool.shutdown()
    assert calls["n"] == 2, "the cache never expired, so the readiness signal was frozen"


def test_a_stale_ready_result_is_not_served_after_the_mount_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    pool = StorageProber(cache_seconds=2.0, clock=lambda: now[0])
    try:
        assert pool.probe(tmp_path / "data").writable is True

        def refuse(_: Path) -> None:
            raise OSError(errno.EACCES, "permission denied")

        monkeypatch.setattr(health, "_write_probe", refuse)
        now[0] += 5.0
        after = pool.probe(tmp_path / "data")
    finally:
        pool.shutdown()
    assert after.writable is False
    assert after.errno_name == "EACCES"


def test_an_idle_pool_is_not_reported_as_having_overrun() -> None:
    """`all()` over an empty mapping is True, so without the emptiness guard a pool that
    drained between a failed claim and this check would report a wedged mount rather than a
    busy one. It fails safe, but the guard is load-bearing and was asserted nowhere."""
    pool = StorageProber(cache_seconds=0.0)
    try:
        assert pool._all_in_flight_overran() is False
    finally:
        pool.shutdown()
