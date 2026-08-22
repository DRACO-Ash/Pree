"""Shared fixtures. Every test gets isolated state and an explicit configuration."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pree.app import create_app
from pree.config import Config, load_config
from pree.health import StorageProber
from pree.store import JsonStore

# At least MIN_PRODUCTION_TOKEN_LENGTH characters, so the production configurations the
# suite builds are ones the app would actually accept.
TEST_TOKEN = "test-token-0123456789-abcdefghij"
# A credential shaped like one the generation command produces: past the length floor and well
# past the variety floor. Tests that satisfied the length floor with a repeated character were
# rejected the moment the variety floor shipped, which is the floor doing its job.
PRODUCTION_TOKEN = "Ab3-Cd6_Ef9.Gh2~Ij5Kl8Mn1Op4Qr7St"
AUTH = {"x-pree-token": TEST_TOKEN}


def make_config(tmp_path: Path, **overrides: object) -> Config:
    """Build a config through the real loader so validation is exercised, not bypassed."""
    env = {
        "PREE_ENV": "development",
        "PREE_DATA_DIR": str(tmp_path / "data"),
        "PREE_BUILD_ID": "test-build",
    }
    env.update({k: str(v) for k, v in overrides.items()})
    return load_config(env)


@pytest.fixture
def quiet_logger() -> logging.Logger:
    """An audit logger that does not write to the captured test output."""
    logger = logging.getLogger("pree.audit.test")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


@pytest.fixture
def prober() -> Iterator[StorageProber]:
    pool = StorageProber(cache_seconds=0.0)
    yield pool
    pool.shutdown()


def build_client(
    config: Config,
    logger: logging.Logger,
    pool: StorageProber,
    **deps: object,
) -> TestClient:
    """Mount the app in-process through the factory, with the store seeded."""
    store = JsonStore(config.data_dir)
    store.seed()
    app = create_app(config, store, logger=logger, prober=pool, **deps)  # type: ignore[arg-type]
    return TestClient(app)


@pytest.fixture
def client(
    tmp_path: Path, quiet_logger: logging.Logger, prober: StorageProber
) -> Iterator[TestClient]:
    """The app with a token configured, so the gate is on."""
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)
    with build_client(config, quiet_logger, prober) as test_client:
        yield test_client


@pytest.fixture
def open_client(
    tmp_path: Path, quiet_logger: logging.Logger, prober: StorageProber
) -> Iterator[TestClient]:
    """The app with no token configured: single-user local mode, auth off by design."""
    config = make_config(tmp_path)
    with build_client(config, quiet_logger, prober) as test_client:
        yield test_client


@pytest.fixture
def anyio_backend() -> str:
    """The async tests target asyncio only; anyio would otherwise also try trio."""
    return "asyncio"
