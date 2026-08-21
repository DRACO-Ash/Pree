"""Shared fixtures. Every test gets isolated state and an explicit configuration."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pree.app import create_app
from pree.config import Config, load_config
from pree.store import JsonStore

TEST_TOKEN = "test-token-0123456789"


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
def executor() -> Iterator[ThreadPoolExecutor]:
    pool = ThreadPoolExecutor(max_workers=2)
    yield pool
    pool.shutdown(wait=True)


@pytest.fixture
def client(
    tmp_path: Path, quiet_logger: logging.Logger, executor: ThreadPoolExecutor
) -> Iterator[TestClient]:
    """The app mounted in-process via the factory, with isolated state and auth on."""
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)
    store = JsonStore(config.data_dir)
    store.seed()
    app = create_app(config, store, logger=quiet_logger, executor=executor)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def open_client(
    tmp_path: Path, quiet_logger: logging.Logger, executor: ThreadPoolExecutor
) -> Iterator[TestClient]:
    """The app with no token configured: single-user local mode, auth off by design."""
    config = make_config(tmp_path)
    store = JsonStore(config.data_dir)
    store.seed()
    app = create_app(config, store, logger=quiet_logger, executor=executor)
    with TestClient(app) as test_client:
        yield test_client
