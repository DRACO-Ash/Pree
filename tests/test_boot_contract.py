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
from tests.test_api import EXPECTED_LIVENESS_PATHS

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


def test_the_security_policy_has_exactly_one_controls_section() -> None:
    """A second controls section is an unchecked place to put a claim.

    A fabricated "Additional controls" section asserting encryption at rest, token rotation and
    SIEM reporting passed the loop, and its encryption claim contradicted accepted risk 2 nine
    lines further down the same document.
    """
    headings = [
        line.strip()
        for line in (REPO_ROOT / "docs" / "SECURITY.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("#") and "control" in line.lower()
    ]
    assert headings == ["## Controls"], f"expected exactly one controls section, found {headings}"


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
    # Nothing may precede the header row but the heading and blank lines. A pipe-leading line
    # above the separator renders as literal text, but it still reads as a control claim.
    stray = [line.strip()[:80] for line in lines[: separator - 1] if line.strip().startswith("|")]
    assert not stray, f"lines that look like control rows before the table header: {stray}"
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


# Words that legitimately appear as backticked or quoted single tokens in a probe sentence.
# A whitelist, not a denylist: a denylist only ever catches the spellings already thought of,
# which is how "wedged", "busy", "BUSY", "not-ready" and "degraded" each got through in turn.
_PROBE_VOCABULARY = frozenset(
    {
        "status",
        "storage_writable",
        "errno",
        "errno_name",
        "data_dir",
        "probe_duration_ms",
        "probe_timeout_ms",
        "ETIMEDOUT",
        "EACCES",
        "ENOSPC",
        "true",
        "false",
        "null",
    }
)
# Sentences that name an HTTP code for a reason other than a probe verdict.
_CODE_EXEMPT = ("header", "carries", "redirect", "router probes the root")
_PROBE_TERMS = ("/healthz/storage", "probe", "write proof", "readiness", "health", "status")


def _probe_sentences(text: str) -> list[str]:
    """Every sentence anywhere in the sheet that makes a claim about the probe.

    Scoped to the whole document deliberately. Scoping to the `## Health paths` section left
    every probe claim elsewhere unchecked, and a fabricated `## Probe behaviour under load`
    section documenting a 204, a "degraded" status and an EBUSY errno passed untouched.
    """
    flat = re.sub(r"\s+", " ", text)
    return [
        sentence
        for sentence in re.split(r"(?<=[.!?])\s+", flat)
        if any(term in sentence.lower() for term in _PROBE_TERMS)
    ]


def test_the_deployment_sheet_documents_only_probe_behaviour_the_code_can_produce() -> None:
    """The sheet must not describe a probe state, code, errno or header the app cannot emit.

    Four earlier versions of this guard were defeated, each by a spelling or a location it did
    not consider: a three-token denylist, a lowercase-quoted-status-only check, a scan bounded
    to one section, and a status regex that only looked within six characters of the word
    "status" so `wedged` and `busy` survived. This version scans the whole document, treats any
    sentence mentioning the probe as a claim, and checks tokens against a derived allowlist.
    """
    emittable_status = {
        StorageProbe(True, "/data", None, None, 1).status,
        StorageProbe(False, "/data", 13, "EACCES", 1).status,
    }
    sheet = (REPO_ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    claims = _probe_sentences(sheet)
    assert claims, "the sheet makes no statement about the probe at all"

    allowed_tokens = emittable_status | _PROBE_VOCABULARY

    unknown_words: list[str] = []
    unexpected_codes: list[int] = []
    for sentence in claims:
        for token in re.findall(r"[`\"']([A-Za-z][A-Za-z_-]*)[`\"']", sentence):
            if token not in allowed_tokens and token.lower() not in allowed_tokens:
                unknown_words.append(token)
        if any(exempt in sentence.lower() for exempt in _CODE_EXEMPT):
            continue
        for code in re.findall(r"\b([1-5][0-9]{2})\b", sentence):
            if int(code) not in {200, 503}:
                unexpected_codes.append(int(code))

    assert not unknown_words, (
        f"the sheet presents probe values the code cannot produce: {sorted(set(unknown_words))}; "
        f"allowed: {sorted(allowed_tokens)}"
    )
    assert not unexpected_codes, (
        f"the sheet claims HTTP codes the probe never returns: {sorted(set(unexpected_codes))}"
    )

    # Any header named anywhere in the sheet must be one the app actually sets. Matched without
    # requiring an X- prefix, which a fabricated `Pree-Probe-State:` header slipped past.
    app_source = (REPO_ROOT / "src" / "pree" / "app.py").read_text(encoding="utf-8")
    named = set(re.findall(r"\b([A-Z][A-Za-z]+(?:-[A-Za-z]+)+)\s*:", sheet))
    invented = sorted(h for h in named if h not in app_source)
    assert not invented, f"the sheet names headers the app does not set: {invented}"

    # And the retired synthesised states must not return by any spelling or in any section.
    health_source = (REPO_ROOT / "src" / "pree" / "health.py").read_text(encoding="utf-8")
    for retired in ("EBUSY", "indeterminate", "degraded"):
        assert retired not in health_source, f"{retired} is back in the source; update the docs"
        assert retired.lower() not in sheet.lower(), f"the sheet still documents {retired}"


def test_the_documented_liveness_paths_match_the_paths_the_code_pins() -> None:
    """Close the code-doc-pin triangle.

    The pinned literals could not be defeated by editing the docs, but nothing tied the two
    together, so the sheet could quietly disagree with the platform contract.
    """
    sheet = (REPO_ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    row = next(line for line in sheet.splitlines() if "`/healthz`" in line and "`/ping`" in line)
    documented = set(re.findall(r"`(/[a-z]*)`", row))
    assert documented == set(EXPECTED_LIVENESS_PATHS), (
        f"the sheet documents {sorted(documented)} but the code pins "
        f"{sorted(EXPECTED_LIVENESS_PATHS)}"
    )
