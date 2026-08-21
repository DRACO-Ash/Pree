"""The storage proof. Its failure paths matter more than its success path, because they are
what a deploy failure actually shows the operator.
"""

from __future__ import annotations

import errno
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


def test_a_saturated_probe_pool_is_reported_as_busy_not_as_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung mount must not make later probes lie about why they failed."""

    def hang(_: Path) -> None:
        time.sleep(STORAGE_PROBE_TIMEOUT_SECONDS * 20)

    monkeypatch.setattr(health, "_write_probe", hang)
    pool = StorageProber(max_workers=1)
    try:
        first = pool.probe(tmp_path / "data")
        second = pool.probe(tmp_path / "data")
    finally:
        pool.shutdown()
    assert first.errno_name == "ETIMEDOUT"
    # The slot stays claimed while the worker is still blocked, so the next answer is an
    # honest "no capacity to observe", not a second fabricated timeout.
    assert second.errno_name == "EBUSY"
    assert second.duration_ms == 0
