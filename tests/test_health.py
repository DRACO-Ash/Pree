"""The storage proof. Its failure paths matter more than its success path, because they are
what a deploy failure actually shows the operator.
"""

from __future__ import annotations

import errno
import threading
import time
from concurrent.futures import Future
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
    assert all(p.writable is False for p in verdicts)
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


def test_a_concurrent_caller_joins_the_probe_rather_than_guessing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single-flight: a joiner reports what the real probe reports, so it cannot be wrong.

    The two heuristics this replaced each failed in a different direction. Guessing busy from
    slots still inside their timeout called a broken volume ready, because a flooder takes each
    slot the moment one frees. Guessing busy from the absence of a recent success called a
    healthy volume unready, and at the shipped parameters the cache expired one instant before
    the grace did, so an unauthenticated flood could hold that window open and restart the pod.
    """
    started = threading.Event()
    writes = {"n": 0}

    def slow_but_fine(path: Path) -> None:
        writes["n"] += 1
        started.set()
        time.sleep(STORAGE_PROBE_TIMEOUT_SECONDS * 0.5)
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(health, "_write_probe", slow_but_fine)
    # Caching disabled, so a second write is the only thing that could serve a second caller.
    pool = StorageProber(cache_seconds=0.0)
    results: list[bool] = []
    worker = threading.Thread(target=lambda: results.append(pool.probe(tmp_path / "data").writable))
    worker.start()
    try:
        assert started.wait(2.0)
        joiner = pool.probe(tmp_path / "data")
    finally:
        worker.join()
        pool.shutdown()
    assert joiner.writable is True, "the joiner reported a healthy volume as broken"
    assert results == [True]
    # The property under test is single-flight itself, not merely that the answer was right.
    # Asserting only the verdict passes just as well when the joiner runs its own write, which
    # is exactly what let a mutation disabling the join survive the entire suite.
    assert writes["n"] == 1, f"the joiner started its own write; {writes['n']} writes ran"


def test_a_write_that_fails_instantly_does_not_deadlock_the_prober(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A self-deadlock on the health path, made deterministic.

    Registering the completion callback while holding the prober's guard deadlocks whenever
    the future is ALREADY complete, because add_done_callback then runs the callback inline on
    the calling thread and the callback takes the same non-reentrant lock. A write that raises
    immediately does exactly that, which is the refused-mount case the probe exists to report.
    Naturally it is a race, so the executor is stubbed to return a completed future and make
    the inline path certain.
    """

    class AlreadyDone:
        def submit(self, fn: object, *args: object) -> Future[float]:
            future: Future[float] = Future()
            future.set_exception(OSError(errno.EACCES, "permission denied"))
            return future

        def shutdown(self, wait: bool = True) -> None:
            return None

    pool = StorageProber(cache_seconds=0.0)
    monkeypatch.setattr(pool, "_executor", AlreadyDone())
    probe = pool.probe(tmp_path / "data")
    assert probe.writable is False
    assert probe.errno_name == "EACCES"
    # The guard must be free afterwards, and a second probe must not block either.
    assert pool.probe(tmp_path / "data").errno_name == "EACCES"


def test_a_write_that_succeeds_instantly_does_not_deadlock_the_prober(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same inline-callback path, on the success side."""

    class AlreadyDone:
        def submit(self, fn: object, *args: object) -> Future[float]:
            future: Future[float] = Future()
            future.set_result(0.0)
            return future

        def shutdown(self, wait: bool = True) -> None:
            return None

    pool = StorageProber(cache_seconds=0.0)
    monkeypatch.setattr(pool, "_executor", AlreadyDone())
    assert pool.probe(tmp_path / "data").writable is True
    assert pool.probe(tmp_path / "data").writable is True


def test_a_write_that_took_longer_than_its_budget_is_reported_unready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The over-budget guard, judged on the write's own duration.

    A joiner arriving just after a slow write lands sees a completed future, so without this
    guard the success is reported and cached and an over-budget mount reads healthy to some
    callers and unready to others. Measured at 19 of 24 ready before the guard existed. The
    worker reports its own duration, so a stub can state one and the branch is reached without
    racing. An earlier version of this test asserted only that the second call came back
    unready, which an implementation with no guard at all satisfies identically.
    """

    class OverBudget:
        def submit(self, fn: object, *args: object) -> Future[float]:
            future: Future[float] = Future()
            future.set_result(STORAGE_PROBE_TIMEOUT_SECONDS + 0.5)
            return future

        def shutdown(self, wait: bool = True) -> None:
            return None

    pool = StorageProber(cache_seconds=0.0)
    monkeypatch.setattr(pool, "_executor", OverBudget())
    probe = pool.probe(tmp_path / "data")
    assert probe.writable is False, "an over-budget write was reported as ready"
    assert probe.errno_name == "ETIMEDOUT"
    assert probe.status == "unready"


def test_an_observers_own_delay_is_not_charged_to_the_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The budget is a statement about the write, not about how promptly it was observed.

    Measuring the observer's clock reading meant a request descheduled after taking the future
    reported a perfectly healthy volume as timed out: fail-closed by luck rather than by
    construction, and in the same direction as the false-unready major.
    """

    class FastWrite:
        def submit(self, fn: object, *args: object) -> Future[float]:
            future: Future[float] = Future()
            future.set_result(0.001)
            return future

        def shutdown(self, wait: bool = True) -> None:
            return None

    # The clock jumps far past the budget between starting the probe and reading the result.
    readings = iter([0.0, 0.0, STORAGE_PROBE_TIMEOUT_SECONDS * 10])
    pool = StorageProber(cache_seconds=0.0, clock=lambda: next(readings))
    monkeypatch.setattr(pool, "_executor", FastWrite())
    probe = pool.probe(tmp_path / "data")
    assert probe.writable is True, "the observer's own delay was charged to the mount"


def test_a_cached_verdict_ages_from_the_probe_start_not_from_when_it_was_observed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worst-case staleness must be the cache window, not the window plus the write latency.

    Stamping the cache at observation time makes a slow write's verdict live for the window
    PLUS however long that write took, which is a different and larger number than the one the
    deployment sheet publishes. The two implementations are told apart by whether the second
    call re-probes: stamped from the start it does, stamped from observation it serves a stale
    ready. Mutation testing found this fix had nothing distinguishing it.
    """
    submits = {"n": 0}

    class SlowButInBudget:
        def submit(self, fn: object, *args: object) -> Future[float]:
            submits["n"] += 1
            future: Future[float] = Future()
            future.set_result(1.0)
            return future

        def shutdown(self, wait: bool = True) -> None:
            return None

    # The probe starts at 0.0 and its write takes 1.0s. The second call happens at 1.5s, which
    # is beyond a 1.0s window measured from the START and inside one measured from observation.
    readings = [0.0, 0.0, 1.5]

    def clock() -> float:
        return readings.pop(0) if len(readings) > 1 else readings[0]

    pool = StorageProber(cache_seconds=1.0, clock=clock)
    monkeypatch.setattr(pool, "_executor", SlowButInBudget())
    assert pool.probe(tmp_path / "data").writable is True
    pool.probe(tmp_path / "data")
    assert submits["n"] == 2, (
        "the second call was served a cached verdict older than the published window"
    )
