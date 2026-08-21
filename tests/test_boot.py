"""The boot path. It must fail closed on a bad environment and stay diagnosable on a bad mount."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pree.config import ConfigError
from pree.main import build


def test_boot_wires_a_serving_app_and_seeds_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("PREE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PREE_BUILD_ID", "boot-test")
    monkeypatch.delenv("PREE_TEAM_TOKEN", raising=False)
    app = build()
    boot_line = capsys.readouterr().out
    assert "pree boot:" in boot_line
    assert "storage=accepted" in boot_line
    assert "build=boot-test" in boot_line
    assert (data_dir / "assessments.json").is_file()
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


def test_boot_records_a_refused_mount_but_still_serves_liveness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A storage fault must leave a narrative and a live pod, not a silent kill."""
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("PREE_DATA_DIR", str(blocker / "data"))
    app = build()
    assert "storage=refused" in capsys.readouterr().out
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/healthz/storage").status_code == 503


def test_boot_fails_closed_on_an_unsafe_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PREE_ENV", "production")
    monkeypatch.setenv("PREE_TEAM_TOKEN", "a-token")
    monkeypatch.delenv("PREE_ALLOWED_ORIGIN", raising=False)
    with pytest.raises(ConfigError, match="no PREE_ALLOWED_ORIGIN"):
        build()
