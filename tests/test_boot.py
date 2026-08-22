"""The boot path. It must fail closed on a bad environment and stay diagnosable on a bad mount."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import pree.main
from pree.config import ConfigError
from pree.main import build
from tests.conftest import PRODUCTION_TOKEN
from tests.conftest import PRODUCTION_TOKEN as BOOT_TOKEN


def test_boot_wires_a_serving_app_and_seeds_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("PREE_ENV", "development")
    monkeypatch.setenv("PREE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PREE_BUILD_ID", "boot-test")
    monkeypatch.delenv("PREE_TEAM_TOKEN", raising=False)
    monkeypatch.setenv("PREE_TEAM_TOKEN", BOOT_TOKEN)
    app = build()
    boot_line = capsys.readouterr().out
    # EXACTLY, and the whole line. Three substring checks with no negative assertion left the
    # channel the platform aggregates unpinned: appending `token={config.team_token}` to the boot
    # line printed the shared credential into the pod log on every start with the suite green.
    # The line is fully derivable from the configuration under test, so there is no reason to
    # assert it loosely.
    assert boot_line.splitlines()[0] == (
        f"pree boot: build=boot-test env=development port=8080 data_dir={data_dir} "
        f"data_dir_configured=True auth=on token_len={len(BOOT_TOKEN)} storage=accepted"
    ), boot_line
    assert BOOT_TOKEN not in boot_line, "the boot line printed the team token"
    assert (data_dir / "assessments.json").is_file()
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


def test_boot_records_a_refused_mount_but_still_serves_liveness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A storage fault must leave a narrative and a live pod, not a silent kill."""
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("PREE_ENV", "development")
    monkeypatch.setenv("PREE_DATA_DIR", str(blocker / "data"))
    app = build()
    assert "storage=refused" in capsys.readouterr().out
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/healthz/storage").status_code == 503


def test_boot_fails_closed_on_an_unsafe_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PREE_ENV", "production")
    monkeypatch.setenv("PREE_TEAM_TOKEN", PRODUCTION_TOKEN)
    monkeypatch.delenv("PREE_ALLOWED_ORIGIN", raising=False)
    with pytest.raises(ConfigError, match="no PREE_ALLOWED_ORIGIN"):
        build()


def test_a_corrupt_snapshot_still_leaves_a_live_diagnosable_pod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The state a crash mid-write or a rollback leaves behind must not kill the pod.

    The store raises its own StoreError for a corrupt snapshot, which an OSError-only handler
    did not catch. It escaped during module import, so gunicorn could not import the app and
    the pod never bound: no liveness path, no diagnostics, just CrashLoopBackOff.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "assessments.json").write_text("{not json at all", encoding="utf-8")
    monkeypatch.setenv("PREE_ENV", "development")
    monkeypatch.setenv("PREE_DATA_DIR", str(data_dir))
    monkeypatch.delenv("PREE_TEAM_TOKEN", raising=False)
    app = build()
    assert "storage=refused" in capsys.readouterr().out
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/").status_code == 200
        # The read-out is reachable, which is what makes the fault diagnosable at all.
        assert client.get("/diagnostics").status_code == 200


def test_importing_the_module_does_not_boot_the_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boot must not be an import side effect.

    A module-level `app = build()` read the real environment at import, so a fail-closed
    configuration error surfaced as an ImportError: unimportable to a test collector, and to
    gunicorn a worker that dies before it can log the reason. The launch command calls the
    factory explicitly instead.
    """
    monkeypatch.setenv("PREE_ENV", "production")
    monkeypatch.delenv("PREE_TEAM_TOKEN", raising=False)
    reloaded = importlib.reload(pree.main)
    assert not hasattr(reloaded, "app")
    assert callable(reloaded.build)
