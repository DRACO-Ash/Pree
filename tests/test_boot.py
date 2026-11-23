"""The boot path. It must fail closed on a bad environment and bind a serving app.

The mount tests are gone with the store: there is no mount to refuse. See
`docs/APPSTORE-REDESIGN.md`.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

import pree.main
from pree.config import ConfigError
from pree.main import build
from tests.conftest import PRODUCTION_TOKEN


def test_boot_wires_a_serving_app(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PREE_ENV", "development")
    monkeypatch.delenv("PREE_DATA_DIR", raising=False)
    monkeypatch.setenv("PREE_BUILD_ID", "boot-test")
    monkeypatch.setenv("PREE_TEAM_TOKEN", PRODUCTION_TOKEN)
    app = build()
    boot_line = capsys.readouterr().out
    # EXACTLY, and the whole line. Three substring checks with no negative assertion left the
    # channel the platform aggregates unpinned: appending `token={config.team_token}` to the boot
    # line printed the shared credential into the pod log on every start with the suite green.
    # The line is fully derivable from the configuration under test, so there is no reason to
    # assert it loosely.
    assert boot_line.splitlines()[0] == (
        f"pree boot: build=boot-test env=development port=8080 "
        f"auth=on token_len={len(PRODUCTION_TOKEN)} stateless=true"
    ), boot_line
    assert PRODUCTION_TOKEN not in boot_line, "the boot line printed the team token"
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


def test_boot_fails_closed_on_an_unsafe_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PREE_ENV", "production")
    monkeypatch.setenv("PREE_TEAM_TOKEN", PRODUCTION_TOKEN)
    monkeypatch.delenv("PREE_ALLOWED_ORIGIN", raising=False)
    with pytest.raises(ConfigError, match="no PREE_ALLOWED_ORIGIN"):
        build()


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
