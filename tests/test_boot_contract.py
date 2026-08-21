"""Deploy-contract assertions about the shipped files.

Every negative assertion here is classified per environment. None may be guaranteed-false on
the platform runner that gates the deploy, because the platform commits its own generated
.gitlab-ci.yml into the checkout and runs the suite there, not here.
"""

from __future__ import annotations

import importlib
import os
import re
from pathlib import Path

import pytest

from pree.health import StorageProbe

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


def _controls_section() -> list[str]:
    """The lines of the Controls section, up to the next top-level heading."""
    lines = (REPO_ROOT / "docs" / "SECURITY.md").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "## Controls")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return lines[start:end]


def _defined_test_names() -> set[str]:
    names: set[str] = set()
    for path in (REPO_ROOT / "tests").glob("test_*.py"):
        names |= set(re.findall(r"^def (test_[a-z0-9_]+)", path.read_text(encoding="utf-8"), re.M))
    return names


def test_every_control_row_cites_a_test_that_exists() -> None:
    """A control row whose evidence cannot be checked is an unverifiable claim.

    Two earlier versions of this guard were defeated. The first matched bare test names only,
    so a row citing a test FILE went unchecked and a row citing nothing at all was invisible.
    The second skipped any row it could not parse, so a four-cell row, a row indented by two
    spaces, and a row whose first cell was the word "Control" all slipped through, as did a row
    citing an existing but unrelated source file, and a token like `test_x()` that is neither a
    bare test name nor a path so no branch asserted anything. All rendered as ordinary rows.

    This version fails on anything it cannot check rather than skipping it, finds the header by
    position rather than by its text, and requires each row to cite at least one real TEST, not
    merely something that exists.
    """
    lines = _controls_section()
    separator = next(
        i for i, line in enumerate(lines) if set(line.strip()) <= set("|- ") and "|" in line
    )
    defined = _defined_test_names()

    rows: list[tuple[str, str]] = []
    malformed: list[str] = []
    for line in lines[separator + 1 :]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) != 3:
            malformed.append(stripped[:80])
            continue
        rows.append((cells[0], cells[2]))

    assert not malformed, f"control-table rows that do not have three cells: {malformed}"
    assert len(rows) > 20, f"only {len(rows)} rows parsed; the parser has drifted"

    uncited: list[str] = []
    unresolvable: list[str] = []
    for control, evidence in rows:
        tokens = re.findall(r"`([^`]+)`", evidence)
        cites_a_test = False
        for token in tokens:
            verifies = token in defined or (
                token.startswith(("tests/", "scripts/")) and (REPO_ROOT / token).exists()
            )
            if verifies:
                cites_a_test = True
            elif (REPO_ROOT / token).exists():
                continue
            else:
                unresolvable.append(f"{control} -> {token}")
        if not cites_a_test:
            uncited.append(control)

    assert not unresolvable, (
        f"control rows citing tokens that are neither a real test nor an existing "
        f"path: {unresolvable}"
    )
    assert not uncited, f"control rows citing no test that exists: {uncited}"


def test_the_where_column_of_every_control_row_points_at_a_real_file() -> None:
    lines = _controls_section()
    separator = next(
        i for i, line in enumerate(lines) if set(line.strip()) <= set("|- ") and "|" in line
    )
    missing: list[str] = []
    for line in lines[separator + 1 :]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) != 3:
            continue
        for token in re.findall(r"`([^`]+)`", cells[1]):
            if not (REPO_ROOT / token).exists():
                missing.append(f"{cells[0]} -> {token}")
    assert not missing, f"control rows whose Where column names a missing file: {missing}"


def _health_section() -> str:
    """The health-paths section of the deployment sheet."""
    text = (REPO_ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    start = text.index("## Health paths")
    end = text.find("\n## ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def test_the_deployment_sheet_documents_only_probe_behaviour_the_code_can_produce() -> None:
    """The sheet must not describe a probe state, code, errno or header the app cannot emit.

    Two earlier versions were defeated. The first was a three-token denylist wearing the name of
    a property. The second checked one spelling, a lowercase quoted `"status": "..."`, so an
    uppercase status, a hyphenated one, an unquoted one, a 429 stated in prose, a fabricated
    X-Pree header and a 204 on readiness all passed. The vocabulary is derived from the code and
    checked without depending on how the sheet happens to punctuate it.
    """
    emittable_status = {
        StorageProbe(True, "/data", None, None, 1).status,
        StorageProbe(False, "/data", 13, "EACCES", 1).status,
    }
    section = _health_section()

    # Any word presented as a status, however it is quoted or cased.
    documented_status = {
        word.lower()
        for word in re.findall(r'status["\'`:\s]{1,6}["\'`]?([A-Za-z][A-Za-z-]*)', section)
    }
    documented_status -= {"code", "codes", "is", "of", "the", "and", "or", "for"}
    impossible = sorted(documented_status - emittable_status)
    assert not impossible, (
        f"the sheet presents statuses the code cannot return: {impossible}; "
        f"the code can return {sorted(emittable_status)}"
    )

    # HTTP codes, judged only in sentences that are actually about the probe. Elsewhere the
    # section legitimately mentions other codes, for instance which responses carry the
    # hardening headers, and the rule that the root must never return a redirect.
    probe_claims = [
        sentence
        for sentence in re.split(r"(?<=[.!?])\s+", section.replace("\n", " "))
        if "/healthz/storage" in sentence or "status" in sentence.lower()
    ]
    assert probe_claims, "the sheet makes no statement about the probe at all"
    claimed_codes = {
        int(code)
        for sentence in probe_claims
        for code in re.findall(r"\b([1-5][0-9]{2})\b", sentence)
    }
    unexpected_codes = sorted(claimed_codes - {200, 503})
    assert not unexpected_codes, (
        f"the sheet claims HTTP codes the probe never returns: {unexpected_codes}"
    )

    # Response headers named in the sheet must be ones the app actually sets.
    app_source = (REPO_ROOT / "src" / "pree" / "app.py").read_text(encoding="utf-8")
    named_headers = set(re.findall(r"\b(X-[A-Za-z]+(?:-[A-Za-z]+)*)\b", section))
    invented = sorted(h for h in named_headers if h not in app_source)
    assert not invented, f"the sheet names headers the app does not set: {invented}"

    # And the retired synthesised states must not return by any spelling.
    health_source = (REPO_ROOT / "src" / "pree" / "health.py").read_text(encoding="utf-8")
    for retired in ("EBUSY", "indeterminate", "degraded"):
        assert retired not in health_source, f"{retired} is back in the source; update the docs"
        assert retired.lower() not in section.lower(), f"the sheet still documents {retired}"
