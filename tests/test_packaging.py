"""The upload archive's own checks, run against the real script.

`scripts/package-appstore.sh` has failed a security review six rounds running, and every
verification of it in this project's history has been a manual measurement typed at a shell.
That is the pattern this suite exists to break: a control nothing exercises automatically is a
control that regresses between rounds without anybody noticing.

The script is invoked as the operator invokes it. Each test zips the tree, measured at 0.08s
against a 31s suite, so the cost is not the reason there are three of them: three is what covers
the happy path, the refusal path and the archive shape.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "package-appstore.sh"
# Resolved from PATH once, so the subprocess calls below name an absolute executable rather
# than a bare word, the way the git checks in test_boot_contract.py do.
SHELL = shutil.which("sh")
UNZIP = shutil.which("unzip")

needs_zip = pytest.mark.skipif(
    shutil.which("zip") is None or UNZIP is None or SHELL is None,
    reason="the packaging script needs sh, zip and unzip",
)


def _package(out: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - resolved shell path, fixed literal arguments
        [str(SHELL), str(SCRIPT), str(out)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


PROBE_PREFIX = "pree_packaging_probe_"


def _sweep_stale_probes() -> list[str]:
    """Remove any probe file a killed run left behind, and name what was removed.

    Every directory this script scans is a tracked one, so the probe has to live in the working
    tree. `finally` covers a failing test and not a killed process, and a surviving probe makes
    every later `package-appstore.sh` run refuse for a file the suite itself created. Sweeping
    at fixture entry makes that self-healing instead of a puzzle.
    """
    stale = sorted((REPO_ROOT / "docs").glob(f"{PROBE_PREFIX}*"))
    for path in stale:
        path.unlink()
    return [path.name for path in stale]


@pytest.fixture
def planted(request: pytest.FixtureRequest) -> Iterator[Path]:
    """A file in the tree whose NAME reads like a credential, removed however the test ends.

    The content is a placeholder: the script's scan reads names, never bytes, and says so.
    """
    _sweep_stale_probes()
    target = REPO_ROOT / "docs" / f"{PROBE_PREFIX}{request.node.name[:24]}_key.txt"
    target.write_text("placeholder for a packaging test, not a credential\n", encoding="utf-8")
    try:
        yield target
    finally:
        target.unlink(missing_ok=True)
        assert not _sweep_stale_probes(), "a probe file survived the test that created it"


@needs_zip
def test_a_clean_tree_packages_and_the_archive_lands_at_the_named_path(tmp_path: Path) -> None:
    """The happy path, so the refusal tests below cannot pass by the script being broken."""
    assert not _sweep_stale_probes(), "a probe file from an earlier run was still in the tree"
    out = tmp_path / "upload.zip"
    result = _package(out)
    assert result.returncode == 0, result.stdout + result.stderr
    assert out.is_file(), "the script reported success and wrote no archive"
    assert not out.with_suffix(".zip.partial").exists(), "the staging file was left behind"


@needs_zip
def test_a_refused_tree_leaves_no_archive_at_the_upload_path(tmp_path: Path, planted: Path) -> None:
    """A failed run must leave nothing uploadable, and it used to leave exactly that.

    The archive was written straight to the output path before any check ran, so every refusal
    exited 1 with the rejected zip sitting at the path the script's own instructions tell a
    human to upload. Measured with a planted `docs/deploy_key.txt`: exit 1, and a zip on disk
    containing it.
    """
    out = tmp_path / "upload.zip"
    result = _package(out)
    assert result.returncode == 1, f"the planted {planted.name} was not refused: {result.stdout}"
    assert "reads like a credential" in result.stderr, result.stderr
    leftovers = sorted(path.name for path in tmp_path.iterdir())
    assert leftovers == [], f"a refused run left an uploadable artefact: {leftovers}"


@needs_zip
def test_the_archive_is_flat_and_carries_the_files_the_platform_builds_from(
    tmp_path: Path,
) -> None:
    """The platform detects the template from a ROOT-level Dockerfile and builds from the root.

    A nested archive fails the build with zero pipeline stages run, which is the most expensive
    way to learn this: the upload cycle is the feedback loop.
    """
    out = tmp_path / "upload.zip"
    assert _package(out).returncode == 0
    listing = subprocess.run(  # noqa: S603 - resolved unzip path, fixed literal arguments
        [str(UNZIP), "-Z1", str(out)], capture_output=True, text=True, check=True
    ).stdout.split()
    for required in ("Dockerfile", "requirements.txt", "pyproject.toml"):
        assert required in listing, f"{required} is not at the archive root: {listing[:20]}"
    assert any(name.startswith("src/pree/") for name in listing), "the source tree is missing"
    assert any(name.startswith("tests/") for name in listing), (
        "the platform runs the suite against the archive root, so the tests must ship"
    )


def test_the_two_version_stamps_agree_and_the_changelog_names_the_release() -> None:
    """CLAUDE.md says the two stamps "must agree", and until now nothing checked that they did.

    That is the defect class this whole range has been about: a rule asserted in prose, with no
    data asserting it. The stamps can drift in one edit, and the drift is invisible until the
    platform builds a wheel version that does not match what the archive name claims.

    The stamp is NOT bumped per pre-release round, and that reading is recorded here rather than
    left implicit, because a reviewer reasonably reads "bump on every change" as per-commit. V0.1
    is unreleased: every round so far hardens the same undelivered artefact, so `0.1.0` is still
    the version that will ship as V0.1, and a bump to `0.1.1` would assert a patch to a release
    that never happened. The stamp moves on DELIVERY. The changelog row moves every round.
    """
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = [
        line.split("=", 1)[1].strip().strip('"')
        for line in pyproject.splitlines()
        if line.startswith("version =")
    ]
    assert len(declared) == 1, f"pyproject.toml declares {len(declared)} versions: {declared}"
    package = (REPO_ROOT / "src" / "pree" / "__init__.py").read_text(encoding="utf-8")
    stamps = [
        line.split("=", 1)[1].strip().strip('"')
        for line in package.splitlines()
        if line.startswith("__version__")
    ]
    assert stamps == declared, (
        f"the two version stamps disagree: pyproject.toml says {declared}, "
        f"src/pree/__init__.py says {stamps}"
    )
    # And the release the changelog names has to be the one the stamp will ship as. `0.1.0` is
    # V0.1; a stamp of `0.2.0` with a changelog still headed V0.1 is a delivery-name defect that
    # only shows up in the archive filename, which is the rollback source.
    major, minor, _ = declared[0].split(".")
    changelog = (REPO_ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    expected = f"## V{major}.{minor}"
    assert expected in changelog, (
        f"the stamp {declared[0]} ships as {expected}, and the changelog has no such heading"
    )
