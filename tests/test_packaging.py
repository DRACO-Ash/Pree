"""The upload archive's own checks, run against the real script.

`scripts/package-appstore.sh` has failed a security review six rounds running, and every
verification of it in this project's history has been a manual measurement typed at a shell.
That is the pattern this suite exists to break: a control nothing exercises automatically is a
control that regresses between rounds without anybody noticing.

The script is invoked as the operator invokes it. These tests are slow by the standards of the
rest of the suite (each one zips the tree), so there are three of them and no more.
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


@pytest.fixture
def planted(request: pytest.FixtureRequest) -> Iterator[Path]:
    """A file in the tree whose NAME reads like a credential, removed however the test ends.

    The content is a placeholder: the script's scan reads names, never bytes, and says so.
    """
    target = REPO_ROOT / "docs" / f"pree_packaging_probe_{request.node.name[:24]}_key.txt"
    target.write_text("placeholder for a packaging test, not a credential\n", encoding="utf-8")
    try:
        yield target
    finally:
        target.unlink(missing_ok=True)


@needs_zip
def test_a_clean_tree_packages_and_the_archive_lands_at_the_named_path(tmp_path: Path) -> None:
    """The happy path, so the refusal tests below cannot pass by the script being broken."""
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
