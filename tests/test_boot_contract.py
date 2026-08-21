"""Deploy-contract assertions about the shipped files.

Every negative assertion here is classified per environment. None may be guaranteed-false on
the platform runner that gates the deploy, because the platform commits its own generated
.gitlab-ci.yml into the checkout and runs the suite there, not here.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

ON_PLATFORM_RUNNER = os.environ.get("GITLAB_CI") == "true"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _dockerfile() -> str:
    return (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_the_dockerfile_sits_at_the_repository_root() -> None:
    """The platform builds from the archive root, so a nested Dockerfile breaks the build."""
    assert (REPO_ROOT / "Dockerfile").is_file()


def test_the_dockerfile_never_bakes_the_port_or_the_data_directory() -> None:
    """An ENV default always beats a code fallback chain and defeats platform injection."""
    body = _dockerfile()
    assert "ENV PORT=" not in body
    assert "ENV PREE_DATA_DIR=" not in body
    assert "ENV STORAGE_MOUNT_PATH=" not in body


def test_the_container_binds_every_interface_on_the_platform_port() -> None:
    body = _dockerfile()
    assert "0.0.0.0:${PORT:-8080}" in body
    assert "EXPOSE 8080" in body


def test_the_container_runs_as_a_non_root_numeric_user() -> None:
    assert "USER 10001:10001" in _dockerfile()


def test_the_launch_command_execs_so_sigterm_reaches_the_server() -> None:
    assert "exec gunicorn" in _dockerfile()


def test_the_suid_sweep_is_the_last_mutation_in_the_prep_stage() -> None:
    """Later instructions can re-introduce the bits the sweep just cleared."""
    body = _dockerfile()
    sweep = body.index("-perm /6000")
    prep_end = body.index("\nFROM scratch\n")
    assert sweep < prep_end
    tail = body[sweep:prep_end]
    assert "useradd" not in tail
    assert "adduser" not in tail
    assert "COPY" not in tail


def test_the_shipped_stage_is_flattened_to_a_single_layer() -> None:
    """The image policy scan reads layer history, so one clean layer is the only construction
    with no history to flag."""
    body = _dockerfile()
    assert "\nFROM scratch\n" in body
    assert body.count("COPY --from=prep / /") == 1


def test_the_sonar_configuration_scopes_sources_to_src() -> None:
    body = (REPO_ROOT / "sonar-project.properties").read_text(encoding="utf-8")
    assert "sonar.sources=src" in body
    assert "sonar.python.coverage.reportPaths=coverage.xml" in body


def test_the_gitignore_blocks_every_secret_and_local_artefact() -> None:
    body = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".env", ".env.local", ".env.*.local", "data/", "coverage/", "coverage.xml"):
        assert pattern in body, f".gitignore is missing {pattern}"


def test_the_example_environment_file_carries_no_real_value() -> None:
    body = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "PREE_TEAM_TOKEN=\n" in body or "PREE_TEAM_TOKEN=" in body
    assert "PORT=" not in body.replace("# PORT is injected", "")


@pytest.mark.skipif(
    ON_PLATFORM_RUNNER,
    reason=(
        "Environment-gated negative assertion. The platform commits its own generated "
        ".gitlab-ci.yml into the checkout, so an assertion about untracked files is "
        "guaranteed-false there and would fail the stage that gates the deploy."
    ),
)
def test_no_environment_file_is_tracked_in_the_working_tree() -> None:
    assert not (REPO_ROOT / ".env").exists()


def test_the_launch_command_targets_the_factory_that_actually_exists() -> None:
    """The load-bearing coupling between the Dockerfile and the module, which was untested.

    Asserting only "exec gunicorn" meant reverting the target to `pree.main:app` kept the whole
    suite green while the container could not start at all: gunicorn exits 4 with
    "Failed to find attribute 'app'". Both halves are asserted here so they cannot drift.
    """
    body = _dockerfile()
    assert "pree.main:build()" in body
    assert "pree.main:app" not in body
    module = importlib.import_module("pree.main")
    assert callable(module.build)
    assert not hasattr(module, "app")
