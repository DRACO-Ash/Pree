#!/usr/bin/env python3
"""App Store Python gate pre-flight check.

Standard library only. No network. Reads the package root and reports every
condition known to decide, or to have been wrongly blamed for, a Dependency
Scanning outcome.

Findings carry confidence markers, matching the house discipline:
  FACT      observed directly in the package under test
  INFERENCE reasoned from the analyser's documented behaviour
  UNKNOWN   not establishable from here

Exit status:
  0  no blocking findings
  1  at least one BLOCK finding
  2  usage or environment error

Usage:
  python3 preflight.py [PACKAGE_ROOT]
  python3 preflight.py --self-test
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

# Filenames the analyser scans directly. Sourced from GitLab's published
# supported-files table for the current dependency scanning analyser.
SCANNED_FILES = {
    "requirements.txt",
    "pipdeptree.json",
    "Pipfile.lock",
    "pipenv.graph.json",
    "poetry.lock",
    "uv.lock",
}

# Filenames that trigger dependency resolution but are never themselves scanned.
RESOLUTION_TRIGGERS = {
    "requirements.txt",
    "requirements.in",
    "requirements.pip",
    "requires.txt",
    "setup.py",
    "setup.cfg",
    "pyproject.toml",
}

# Directory names excluded from the scan by default, at any depth.
EXCLUDED_DIRS = {
    "spec", "test", "tests", "tmp", "node_modules", ".bundle", "vendor", ".git",
}

# Patterns in a requirements or manifest file that break or silently distort
# resolution.
RESOLVER_HAZARDS = (
    (re.compile(r"^\s*-e\s", re.M), "editable install (-e); stripped before resolution"),
    (re.compile(r"(?<![\w.])file:", re.M), "file: reference; stripped before resolution"),
    (re.compile(r"git\+(https?|ssh)://", re.M), "VCS URL; resolution fails for this file"),
    (re.compile(r"^\s*\.{1,2}/", re.M), "local path reference; stripped before resolution"),
)

BLOCK, WARN, INFO, GOOD = "BLOCK", "WARN", "INFO", "GOOD"


class Finding:
    def __init__(self, level: str, marker: str, text: str, fix: str = "") -> None:
        self.level = level
        self.marker = marker
        self.text = text
        self.fix = fix

    def render(self) -> str:
        head = f"[{self.level:<5}] {self.marker:<9} {self.text}"
        return head if not self.fix else f"{head}\n{'':>19}fix: {self.fix}"


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def check_pyproject(root: Path, out: list) -> None:
    p = root / "pyproject.toml"
    if not p.is_file():
        out.append(Finding(
            BLOCK, "INFERENCE",
            "No pyproject.toml at the package root.",
            "Add one with a [project] table. This is the only root-file "
            "difference between the two packages that pass and the one that failed.",
        ))
        return
    body = read(p)
    if not re.search(r"^\s*\[project\]\s*$", body, re.M):
        out.append(Finding(
            BLOCK, "FACT",
            "pyproject.toml has no [project] table.",
            "A pyproject.toml carrying only build-system configuration is "
            "skipped by the resolver with a warning, so it buys nothing.",
        ))
        return
    out.append(Finding(GOOD, "FACT", "pyproject.toml present with a [project] table."))
    if re.search(r"^\s*\[tool\.poetry\]", body, re.M):
        out.append(Finding(
            INFO, "FACT",
            "Poetry project detected; the resolver takes the poetry path.",
            "Commit poetry.lock. The pip-tools guidance in this skill does not apply.",
        ))
    if re.search(r"^\s*dependencies\s*=", body, re.M):
        out.append(Finding(
            WARN, "INFERENCE",
            "pyproject.toml declares [project] dependencies.",
            "Neither passing application does. Two declarations drift; keep "
            "dependencies in the .in files and their locked outputs.",
        ))
    if not re.search(r"^\s*requires-python\s*=", body, re.M):
        out.append(Finding(
            WARN, "INFERENCE",
            "pyproject.toml does not declare requires-python.",
            'Add requires-python = ">=3.12" so the resolver targets one interpreter.',
        ))


def check_scannable(root: Path, out: list) -> None:
    present = sorted(n for n in SCANNED_FILES if (root / n).is_file())
    if not present:
        triggers = sorted(n for n in RESOLUTION_TRIGGERS if (root / n).is_file())
        if triggers:
            out.append(Finding(
                WARN, "INFERENCE",
                "No directly scannable file at the root; relying on resolution "
                f"from {', '.join(triggers)}.",
                "Commit a lockfile so the scan does not depend on a resolution "
                "job that runs with allow_failure and fails silently.",
            ))
        else:
            out.append(Finding(
                BLOCK, "FACT",
                "No scannable file and no resolution trigger at the root.",
                "The analyser has nothing to read. Commit requirements.txt.",
            ))
        return
    out.append(Finding(GOOD, "FACT", f"Scannable file(s) at root: {', '.join(present)}."))


def check_placement(root: Path, out: list) -> None:
    for path in root.rglob("*"):
        if not path.is_file() or path.name not in SCANNED_FILES:
            continue
        rel = path.relative_to(root)
        parts = rel.parts[:-1]
        if not parts:
            continue
        if any(p in EXCLUDED_DIRS or p.startswith(".") for p in parts):
            out.append(Finding(
                WARN, "FACT",
                f"{rel} sits under an excluded or hidden directory; never scanned.",
                "Move it to the package root if it is meant to be seen.",
            ))
        elif len(parts) > 1:
            out.append(Finding(
                WARN, "FACT",
                f"{rel} is deeper than DS_MAX_DEPTH default of 2; likely skipped.",
                "Move it to the root or an immediate subdirectory.",
            ))


def check_hazards(root: Path, out: list) -> None:
    targets = [n for n in sorted(RESOLUTION_TRIGGERS | SCANNED_FILES)
               if (root / n).is_file() and n.endswith((".txt", ".in", ".py", ".cfg", ".toml", ".pip"))]
    targets += [p.name for p in root.glob("requirements-*.in")]
    targets += [p.name for p in root.glob("requirements-*.txt")]
    clean = True
    for name in sorted(set(targets)):
        body = read(root / name)
        for pattern, why in RESOLVER_HAZARDS:
            if pattern.search(body):
                clean = False
                out.append(Finding(
                    BLOCK, "FACT",
                    f"{name} contains a {why}.",
                    "Remove it, or expect the package set the analyser sees to "
                    "be incomplete or the resolution to fail outright.",
                ))
    if clean and targets:
        out.append(Finding(GOOD, "FACT", "No resolver hazards in any dependency file."))


def check_pins_and_hashes(root: Path, out: list) -> None:
    lock = root / "requirements.txt"
    if not lock.is_file():
        return
    body = read(lock)
    loose = re.findall(r"^\s*([A-Za-z0-9_.\-]+)\s*(>=|<=|~=|>|<|\^)", body, re.M)
    if loose:
        out.append(Finding(
            BLOCK, "FACT",
            f"requirements.txt has {len(loose)} unpinned or range-pinned entries "
            f"(first: {loose[0][0]}).",
            "Exact-pin everything. A range means the set you tested is not the "
            "set that installs.",
        ))
    else:
        out.append(Finding(GOOD, "FACT", "requirements.txt is exactly pinned throughout."))

    pins = set(re.findall(r"^\s*([A-Za-z0-9_.\-]+)==", body, re.M))
    hashed = set(re.findall(r"^\s*([A-Za-z0-9_.\-]+)==[^\n]*\\?\s*\n\s*--hash=", body, re.M))
    if hashed and pins - hashed:
        out.append(Finding(
            BLOCK, "FACT",
            f"{len(pins - hashed)} pinned entries carry no --hash while others do.",
            "pip enforces hash checking only when every requirement has a hash. "
            "One unhashed line disables verification. Regenerate with "
            "uv pip compile --generate-hashes.",
        ))
    elif hashed:
        out.append(Finding(GOOD, "FACT", "Every pinned entry carries a hash."))
    else:
        out.append(Finding(
            INFO, "FACT",
            "requirements.txt carries no hashes.",
            "Not a gate failure; PSIRENS passes without them. Hashes still "
            "protect against a re-uploaded or tampered artefact.",
        ))


def check_runtime_subset(root: Path, out: list) -> None:
    full, lean = root / "requirements.txt", root / "requirements-runtime.txt"
    if not (full.is_file() and lean.is_file()):
        return
    def pins(p: Path) -> dict:
        return dict(re.findall(r"^\s*([A-Za-z0-9_.\-]+)==([^\s;\\]+)", read(p), re.M))
    a, b = pins(full), pins(lean)
    drift = {k: (a[k], b[k]) for k in b if k in a and a[k] != b[k]}
    missing = sorted(k for k in b if k not in a)
    if drift or missing:
        detail = ", ".join(f"{k} {v[0]} vs {v[1]}" for k, v in list(drift.items())[:3])
        out.append(Finding(
            BLOCK, "INFERENCE",
            "The runtime lock file is not a version-identical subset of "
            f"requirements.txt ({detail or 'missing: ' + ', '.join(missing[:3])}).",
            "requirements-runtime.txt is not a scanned filename, so the image "
            "would ship a version no gate examined. Re-lock both from the same "
            "inputs, or ask for DS_PIPCOMPILE_LOCKFILE_FILE_NAME_PATTERN.",
        ))
    else:
        out.append(Finding(
            GOOD, "INFERENCE",
            "Runtime lock file is a version-identical subset of requirements.txt.",
        ))


def check_layout(root: Path, out: list) -> None:
    if (root / "Dockerfile").is_file():
        out.append(Finding(GOOD, "FACT", "Dockerfile at the package root."))
    else:
        nested = [p.relative_to(root) for p in root.rglob("Dockerfile")]
        if nested:
            out.append(Finding(
                BLOCK, "FACT",
                f"Dockerfile is nested at {nested[0]}, not at the root.",
                "Container template detection and the build context both break.",
            ))
    if (root / ".gitlab-ci.yml").is_file():
        out.append(Finding(
            WARN, "FACT",
            ".gitlab-ci.yml is inside the package.",
            "Neither passing application ships one. It lives in the deployment "
            "repository and a version upload does not update it.",
        ))
    if not (root / "tests").is_dir():
        out.append(Finding(
            WARN, "FACT",
            "No tests/ directory at the package root.",
            "The platform test stage runs pytest against the package root "
            "before it builds any image.",
        ))
    ignore = read(root / ".dockerignore")
    for want in ("tests", ".venv", ".git"):
        if ignore and want not in ignore:
            out.append(Finding(
                WARN, "FACT",
                f".dockerignore does not exclude {want}.",
                "Keep the build context lean and the test toolchain out of the image.",
            ))


def check_dockerfile(root: Path, out: list) -> None:
    df = root / "Dockerfile"
    if not df.is_file():
        return
    body = read(df)
    froms = re.findall(r"^\s*FROM\s+(\S+)", body, re.M)
    unpinned = [f for f in froms if "@sha256:" not in f and f != "scratch"]
    if unpinned:
        out.append(Finding(
            WARN, "INFERENCE",
            f"Base image pinned by tag, not digest: {unpinned[0]}.",
            "A moving tag means two builds of the identical archive can produce "
            "different images and different Container Scan results. Pin the "
            "digest and schedule a refresh job.",
        ))
    else:
        out.append(Finding(GOOD, "FACT", "Every base image is digest-pinned."))
    if re.search(r"apt-get\s+(-y\s+)?upgrade[^\n]*\|\|\s*true", body):
        out.append(Finding(
            WARN, "FACT",
            "The operating system patch step is fail-open (|| true).",
            "A build where the upgrade failed looks identical to one where it "
            "succeeded. Emit a distinguishable marker on the failure path.",
        ))
    if not re.search(r"^\s*USER\s+", body, re.M):
        out.append(Finding(
            WARN, "INFERENCE",
            "No USER instruction; the container likely runs as root.",
        ))


def check_loop_guards(root: Path, out: list) -> None:
    gating = ("pip-audit", "pytest", "mypy", "ruff check", "docker build")
    for script in sorted(root.glob("scripts/*.sh")):
        body = read(script)
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "|" not in stripped:
                continue
            if any(g in stripped for g in gating) and not re.search(r"\|\|", stripped):
                out.append(Finding(
                    BLOCK, "FACT",
                    f"{script.name} pipes a gating command into another command.",
                    "In POSIX sh a pipeline reports the last command's status, "
                    "so the gate always passes. Redirect to a file instead.",
                ))
                break


def run(root: Path) -> int:
    out: list = []
    check_pyproject(root, out)
    check_scannable(root, out)
    check_placement(root, out)
    check_hazards(root, out)
    check_pins_and_hashes(root, out)
    check_runtime_subset(root, out)
    check_layout(root, out)
    check_dockerfile(root, out)
    check_loop_guards(root, out)

    order = {BLOCK: 0, WARN: 1, INFO: 2, GOOD: 3}
    out.sort(key=lambda f: order[f.level])
    print(f"App Store gate pre-flight: {root}")
    print("=" * 72)
    for f in out:
        print(f.render())
    blocks = sum(1 for f in out if f.level == BLOCK)
    warns = sum(1 for f in out if f.level == WARN)
    print("=" * 72)
    print(f"{blocks} blocking, {warns} advisory.")
    if blocks:
        print("Do not upload. Clear the blocking findings first.")
    else:
        print("Contract satisfied. This predicts nothing about the gate's verdict;")
        print("it removes the failure modes that are visible from here.")
    print("UNKNOWN: the analyser's real error text. If you see it, record it.")
    return 1 if blocks else 0


def self_test() -> int:
    failures = []

    def expect(name: str, cond: bool) -> None:
        if not cond:
            failures.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        out: list = []
        check_pyproject(root, out)
        expect("missing pyproject blocks", any(f.level == BLOCK for f in out))

        (root / "pyproject.toml").write_text("[build-system]\nrequires = []\n")
        out = []
        check_pyproject(root, out)
        expect("build-only pyproject blocks", any(f.level == BLOCK for f in out))

        (root / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n')
        out = []
        check_pyproject(root, out)
        expect("valid pyproject passes", not any(f.level == BLOCK for f in out))

        (root / "requirements.txt").write_text("flask==3.1.1\nrequests>=2.28\n")
        out = []
        check_pins_and_hashes(root, out)
        expect("range pin blocks", any(f.level == BLOCK for f in out))

        (root / "requirements.txt").write_text("flask==3.1.1\n-e .\n")
        out = []
        check_hazards(root, out)
        expect("editable install blocks", any(f.level == BLOCK for f in out))

        (root / "requirements.txt").write_text("flask==3.1.1\nsgp4==2.27\n")
        (root / "requirements-runtime.txt").write_text("flask==3.1.0\n")
        out = []
        check_runtime_subset(root, out)
        expect("runtime drift blocks", any(f.level == BLOCK for f in out))

        (root / "requirements-runtime.txt").write_text("flask==3.1.1\n")
        out = []
        check_runtime_subset(root, out)
        expect("runtime subset passes", not any(f.level == BLOCK for f in out))

        (root / "Dockerfile").write_text("FROM python:3.12-slim\nRUN true\n")
        out = []
        check_dockerfile(root, out)
        expect("tag pin warns", any(f.level == WARN for f in out))

        (root / "scripts").mkdir()
        (root / "scripts" / "verify.sh").write_text("pip-audit -r requirements.txt | tee log\n")
        out = []
        check_loop_guards(root, out)
        expect("piped gate blocks", any(f.level == BLOCK for f in out))

        deep = root / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "requirements.txt").write_text("flask==3.1.1\n")
        out = []
        check_placement(root, out)
        expect("deep file warns", any(f.level == WARN for f in out))

    if failures:
        print("SELF-TEST FAILED:")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("SELF-TEST PASSED: 10 assertions.")
    return 0


def main(argv: list) -> int:
    args = [a for a in argv[1:] if a]
    if "--self-test" in args:
        return self_test()
    if "-h" in args or "--help" in args:
        print(__doc__)
        return 0
    root = Path(args[0]) if args else Path(os.getcwd())
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    return run(root.resolve())


if __name__ == "__main__":
    sys.exit(main(sys.argv))
