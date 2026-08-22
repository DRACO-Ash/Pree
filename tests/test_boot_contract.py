"""Deploy-contract assertions about the shipped files.

Every negative assertion here is classified per environment. None may be guaranteed-false on
the platform runner that gates the deploy, because the platform commits its own generated
.gitlab-ci.yml into the checkout and runs the suite there, not here.
"""

from __future__ import annotations

import importlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pree.health import StorageProbe
from tests.test_api import EXPECTED_LIVENESS_PATHS

ON_PLATFORM_RUNNER = os.environ.get("GITLAB_CI") == "true"
REPO_ROOT = Path(__file__).resolve().parent.parent


# The complete OCI/BuildKit instruction set. Anything outside it is not an instruction, and a
# parser that shrugs at an unknown line is how a heredoc body became a phantom build stage.
_DOCKERFILE_KEYWORDS = frozenset(
    {
        "FROM",
        "RUN",
        "CMD",
        "LABEL",
        "MAINTAINER",
        "EXPOSE",
        "ENV",
        "ADD",
        "COPY",
        "ENTRYPOINT",
        "VOLUME",
        "USER",
        "WORKDIR",
        "ARG",
        "ONBUILD",
        "STOPSIGNAL",
        "HEALTHCHECK",
        "SHELL",
    }
)


# What counts as a sentence about the team token, and the retired rules that must not return.
# "credential" is here because a fabricated sentence naming only "the shared operator
# credential" stated a 16-character floor and was not checked at all.
_TOKEN_TERMS = ("token", "pree_team_token", "credential")
_RETIRED_TOKEN_RULES = (
    "distinct character",
    "character variety",
    "character-variety rule",
    "mix upper case",
    "mixed case",
    "upper case, lower case",
)
# A floor is always stated with a comparator or the word "floor" itself. Requiring one keeps
# the guard from reading an unrelated character count as a rule.
_FLOOR_PHRASES = (
    "floor",
    "shorter than",
    "fewer than",
    "longer than",
    "at least",
    "no less than",
    "minimum",
    "raised to",
    "must be",
)
_WORD_NUMBERS = {
    "eight": 8,
    "twelve": 12,
    "sixteen": 16,
    "twenty": 20,
    "twenty-four": 24,
    "thirty-two": 32,
    "sixty-four": 64,
}


@dataclass(frozen=True)
class _Instruction:
    """One resolved Dockerfile instruction, with comments and continuations removed."""

    stage: int
    stage_name: str
    keyword: str
    argument: str
    index: int


def _dockerfile() -> str:
    return (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")


def _instructions() -> list[_Instruction]:
    """Parse the Dockerfile into resolved instructions, per build stage.

    Every assertion below used to be a substring grep over the raw text, and every one of them
    was defeatable: `USER root` appended after the numeric user, a `chmod u+s` after the suid
    sweep, `ENV PORT 9999` in the legacy space-separated form, a bind to loopback with the
    0.0.0.0 string left behind in a comment, a non-exec CMD with "exec gunicorn" left in a
    comment, and a second COPY into the supposedly single-layer stage. Comments are stripped
    and the resolved final-stage state is what gets asserted.
    """
    text = _dockerfile()
    # A heredoc body is text to `docker build` and instructions to any line-based parser, so
    # `COPY <<DECOY` followed by a fabricated FROM/USER/CMD created a phantom final stage that
    # satisfied every assertion below while the real stage ran as root on loopback. This
    # project's Dockerfile has no need of heredocs, so their presence is refused rather than
    # interpreted.
    assert "<<" not in text, "heredocs are refused: a line-based parser cannot read them safely"
    # Parser directives are comments to every line-based reader and instructions to docker. An
    # `# escape=` directive stops `\` continuing a line, so docker split a HEALTHCHECK that this
    # parser had swallowed whole and read the `USER root` hidden inside it as its own
    # instruction. Only the syntax directive is permitted; anything else is refused.
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped.startswith("#"):
            break
        directive = stripped.lstrip("#").strip()
        if "=" in directive and " " not in directive.split("=")[0]:
            assert directive.split("=")[0].strip() == "syntax", (
                f"unrecognised parser directive {directive!r}; a directive changes how docker "
                "reads this file and is invisible to a line-based parser"
            )
    joined: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.split("#")[0].rstrip() if raw.lstrip().startswith("#") else raw.rstrip()
        if line.lstrip().startswith("#") or not line.strip():
            continue
        buffer += line[:-1] + " " if line.endswith("\\") else line
        if not line.endswith("\\"):
            joined.append(buffer.strip())
            buffer = ""
    if buffer:
        joined.append(buffer.strip())

    out: list[_Instruction] = []
    stage = -1
    stage_name = ""
    for index, line in enumerate(joined):
        parts = line.split(None, 1)
        keyword = parts[0].upper()
        argument = parts[1] if len(parts) > 1 else ""
        assert keyword in _DOCKERFILE_KEYWORDS, (
            f"unrecognised Dockerfile keyword {keyword!r} in {line[:60]!r}; an unknown line "
            "may be a heredoc body or a typo, and either way it must not be classified silently"
        )
        if keyword == "FROM":
            stage += 1
            pieces = argument.split()
            stage_name = (
                pieces[-1].lower() if len(pieces) >= 3 and pieces[-2].upper() == "AS" else ""
            )
        out.append(_Instruction(stage, stage_name, keyword, argument, index))
    return out


def _final_stage() -> list[_Instruction]:
    instructions = _instructions()
    last = max(i.stage for i in instructions)
    return [i for i in instructions if i.stage == last]


def _shipped_source_stage() -> str:
    """The name of the stage the shipped layer is actually copied FROM.

    Without this the hardening assertions floated free of the image. The sweep was asserted to
    exist in some stage and the shipped COPY to come from some stage, with nothing joining the
    two, so moving the sweep into the `build` stage shipped every setuid binary the base image
    carries with the whole suite green, and repointing the COPY at `build` shipped pip as well.
    """
    copies = [i for i in _final_stage() if i.keyword in {"COPY", "ADD"}]
    assert len(copies) == 1, f"the shipped stage copies {len(copies)} times"
    argument = copies[0].argument
    source = next(
        (part.split("=", 1)[1] for part in argument.split() if part.startswith("--from=")),
        None,
    )
    assert source is not None, f"the shipped layer is not copied from a stage: {argument!r}"
    assert not source.isdigit(), (
        f"the shipped layer is copied from stage index {source!r}; a positional reference "
        "silently follows any stage inserted above it, so name the stage"
    )
    return source.lower()


def test_the_dockerfile_sits_at_the_repository_root() -> None:
    """The platform builds from the archive root, so a nested Dockerfile breaks the build."""
    assert (REPO_ROOT / "Dockerfile").is_file()


def test_the_resolved_runtime_user_is_the_non_root_numeric_one() -> None:
    """The LAST USER in the final stage is what runs, not the first one present.

    Appending `USER root` left the old grep green while the container ran as root.
    """
    users = [i.argument.strip() for i in _final_stage() if i.keyword == "USER"]
    assert users, "the final stage sets no USER at all, so it runs as root"
    assert users[-1] == "10001:10001", f"the effective runtime user is {users[-1]!r}"


def test_no_stage_bakes_the_port_or_the_data_directory() -> None:
    """An ENV default beats the code fallback chain and defeats platform injection.

    Checked per resolved instruction and in both syntaxes: `ENV KEY=VALUE` and the legacy
    `ENV KEY VALUE`, the latter of which slipped past a grep for `ENV PORT=`.
    """
    baked: list[str] = []
    for instruction in _instructions():
        if instruction.keyword != "ENV":
            continue
        for assignment in instruction.argument.split():
            name = assignment.split("=")[0]
            if name in {"PORT", "PREE_DATA_DIR", "STORAGE_MOUNT_PATH"}:
                baked.append(f"{instruction.keyword} {instruction.argument}")
        first = instruction.argument.split(None, 1)[0] if instruction.argument else ""
        if "=" not in first and first in {"PORT", "PREE_DATA_DIR", "STORAGE_MOUNT_PATH"}:
            baked.append(f"{instruction.keyword} {instruction.argument}")
    assert not baked, f"platform-injected values baked into the image: {baked}"


def test_the_effective_launch_command_binds_every_interface_and_execs() -> None:
    """Asserted on the resolved CMD, not on the file.

    Binding loopback while leaving the 0.0.0.0 string in a comment, and dropping `exec` while
    leaving "exec gunicorn" in a comment, both passed the old greps.
    """
    final = _final_stage()
    entrypoints = [i.argument for i in final if i.keyword == "ENTRYPOINT"]
    assert not entrypoints, (
        f"an ENTRYPOINT overrides CMD, so the asserted launch command would never run and the "
        f"CMD array would become its arguments: {entrypoints}"
    )
    assert not [i for i in _instructions() if i.keyword == "ONBUILD"], (
        "ONBUILD defers an instruction to a downstream build, where none of these assertions apply"
    )
    commands = [i.argument for i in final if i.keyword == "CMD"]
    assert len(commands) == 1, f"expected exactly one CMD in the final stage, found {commands}"
    command = commands[0]
    assert "0.0.0.0:" in command, f"the launch command does not bind every interface: {command}"
    assert "127.0.0.1" not in command, f"the launch command binds loopback: {command}"
    assert "exec gunicorn" in command, (
        f"without exec, SIGTERM never reaches the server and shutdown hangs: {command}"
    )
    exposed = [i.argument.strip() for i in final if i.keyword == "EXPOSE"]
    assert exposed == ["8080"], f"the final stage exposes {exposed}, not the platform port"


def test_the_suid_sweep_is_the_last_mutating_instruction_of_its_stage() -> None:
    """Nothing may follow the sweep, because later instructions can re-introduce the bits.

    The old version forbade only useradd, adduser and COPY in the tail, so a `chmod u+s`
    after the sweep passed. This asserts the sweep's position against every mutating
    instruction in its stage instead of a denylist of three.
    """
    instructions = _instructions()
    sweep = next(i for i in instructions if "-perm /6000" in i.argument)
    mutating = {"RUN", "COPY", "ADD"}
    later = [
        f"{i.keyword} {i.argument[:60]}"
        for i in instructions
        if i.stage == sweep.stage and i.keyword in mutating and i.index > sweep.index
    ]
    assert not later, f"instructions follow the suid sweep in its own stage: {later}"


def test_every_hardening_step_runs_in_the_stage_that_actually_ships() -> None:
    """Position within a stage is worthless if the stage is not the one that ships.

    Both halves of this were asserted and neither was joined to the other. Moving the sweep
    from `prep` into `build` left all 238 tests green while the shipped filesystem kept every
    setuid binary python:3.12-slim carries, among them su, mount, passwd and newgrp; and
    repointing the shipped COPY at `build` shipped the unswept stage with pip in it. Each
    hardening step is now required to be in the stage the shipped layer is copied from.
    """
    shipped_from = _shipped_source_stage()
    instructions = _instructions()
    required = {
        "the suid and sgid sweep": "-perm /6000",
        "the pip removal": "site-packages/pip",
        "the numeric user creation": "--uid 10001",
    }
    misplaced: list[str] = []
    for label, needle in required.items():
        matches = [i for i in instructions if needle in i.argument and i.keyword == "RUN"]
        assert matches, f"{label} is absent from the Dockerfile entirely"
        for found in matches:
            if found.stage_name != shipped_from:
                misplaced.append(
                    f"{label} runs in stage {found.stage_name or '<unnamed>'!r}, "
                    f"but the shipped layer is copied from {shipped_from!r}"
                )
    assert not misplaced, misplaced


def test_the_shipped_stage_is_exactly_one_copied_layer() -> None:
    """The image-policy scan reads layer history, so one clean layer is the point.

    Counting only `COPY --from=prep / /` let a second COPY into the scratch stage pass.
    """
    final = _final_stage()
    copies = [i.argument for i in final if i.keyword in {"COPY", "ADD"}]
    runs = [i.argument for i in final if i.keyword == "RUN"]
    assert len(copies) == 1, f"the shipped stage copies {len(copies)} times: {copies}"
    assert copies[0].startswith("--from="), f"the single copy is not from a stage: {copies[0]}"
    assert not runs, f"the shipped stage runs commands, adding layers: {runs}"
    assert any(i.keyword == "FROM" and i.argument.strip().lower() == "scratch" for i in final), (
        "the shipped stage is not FROM scratch"
    )


def _properties(path: Path) -> dict[str, str]:
    """Resolved key-value pairs, last value winning, as a properties reader would see them."""
    resolved: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        resolved[key.strip()] = value.strip()
    return resolved


def test_the_sonar_configuration_scopes_sources_to_src() -> None:
    """Read the RESOLVED value, not the presence of a line.

    A substring check passes on a file where the right line is followed by a wrong one, and a
    properties reader takes the last value. `sonar.sources=src` on line one with
    `sonar.sources=.` on line two satisfied the old assertion and scanned the whole checkout,
    including the tests and the virtual environment.
    """
    resolved = _properties(REPO_ROOT / "sonar-project.properties")
    assert resolved.get("sonar.sources") == "src", (
        f"sonar.sources resolves to {resolved.get('sonar.sources')!r}, not 'src'"
    )
    assert resolved.get("sonar.python.coverage.reportPaths") == "coverage.xml", (
        f"the coverage report path resolves to "
        f"{resolved.get('sonar.python.coverage.reportPaths')!r}"
    )


def test_every_documented_token_floor_matches_the_number_the_code_enforces() -> None:
    """Tie the documented token rule to the constant, so it cannot drift again.

    It already had. The floor was raised from 24 to 32 and the character-variety rule was
    deleted, but two documents still stated the old numbers and the retired rule, and one of my
    own commit messages claimed all three had been updated when only one had. Prose drifts
    silently; a constant does not.
    """
    floor = importlib.import_module("pree.config").MIN_PRODUCTION_TOKEN_LENGTH
    wrong: list[str] = []
    retired: list[str] = []
    checked = 0
    for name in ("DEPLOYMENT.md", "SECURITY.md", "CHANGELOG.md"):
        # Fragments are built PER LINE, then split into sentences within the line. Flattening
        # the whole document first and splitting on the pipe put a table row's cells into
        # separate fragments, so `| Token floor | at least 24 characters |` had the subject in
        # one fragment and the number in another and neither fragment had both. A line keeps a
        # row, a bullet and an undotted heading whole.
        fragments: list[str] = []
        for line in (REPO_ROOT / "docs" / name).read_text(encoding="utf-8").splitlines():
            collapsed = re.sub(r"\s+", " ", line).strip(" ●■")
            if collapsed:
                fragments.extend(re.split(r"(?<=[.!?])\s+", collapsed))
        for fragment in fragments:
            lowered = fragment.lower()
            # Named by the environment variable as well as by the word, because a sentence
            # about "the shared operator credential" is about the token and said 16.
            if not any(term in lowered for term in _TOKEN_TERMS):
                continue
            # The retired-rule check runs BEFORE the floor gate. Putting it after meant a
            # reworded variety rule ("must mix upper case, lower case and digits") states no
            # number, failed the floor gate, and was never examined for the rule at all.
            #
            # It is scoped to the operator sheet on purpose. There, a sentence naming a rule is
            # an instruction the operator will follow, so a rule the code no longer has is a
            # defect. Elsewhere the same words appear in the record of the rule's REMOVAL, and
            # no text test can reliably separate "we enforce this" from "we deleted this"
            # without an exemption phrase that then becomes the escape hatch.
            if name == "DEPLOYMENT.md" and any(
                phrase in lowered for phrase in _RETIRED_TOKEN_RULES
            ):
                retired.append(f"{name}: {fragment.strip()}")
            # A number of characters is only a FLOOR claim when a comparator says so. Matching
            # every "N characters" in a token sentence flagged "five 5,000-character field
            # names", which is a fact about a log line, and a guard that cries wolf gets
            # relaxed rather than obeyed.
            if not any(phrase in lowered for phrase in _FLOOR_PHRASES):
                continue
            stated_numbers = [
                int(digits) for digits in re.findall(r"\b(\d+)[\s-]*characters?\b", fragment)
            ]
            stated_numbers += [
                _WORD_NUMBERS[word]
                for word in re.findall(r"\b([a-z]+(?:-[a-z]+)?)[\s-]*characters?\b", lowered)
                if word in _WORD_NUMBERS
            ]
            for stated in stated_numbers:
                checked += 1
                if stated != floor:
                    wrong.append(f"{name}: {fragment.strip()}")
    # A fenced block carries no prose the split above would recognise as a claim, so the
    # constant's own name is checked wherever it appears with a value attached.
    for name in ("DEPLOYMENT.md", "SECURITY.md", "CHANGELOG.md"):
        body = (REPO_ROOT / "docs" / name).read_text(encoding="utf-8")
        for value in re.findall(r"MIN_PRODUCTION_TOKEN_LENGTH\s*[=:]\s*(\d+)", body):
            checked += 1
            if int(value) != floor:
                wrong.append(f"{name}: MIN_PRODUCTION_TOKEN_LENGTH stated as {value}")

    assert checked, (
        "no document states a character floor for the team token; the operator has no way to "
        "know what production will refuse"
    )
    assert not wrong, (
        f"the code refuses a token shorter than {floor} characters, but the docs say otherwise: "
        f"{wrong}"
    )
    assert not retired, (
        f"the character-variety rule was deleted from the code but is still documented: {retired}"
    )


def test_git_actually_ignores_every_secret_and_local_artefact() -> None:
    """Assert the BEHAVIOUR, not the text of the file.

    The previous version grepped .gitignore for substrings. Appending "!.env" kept it green
    while `git check-ignore .env` stopped matching, so the file that would hold the production
    token became committable, and the only behavioural guard merely checked non-existence and
    is skipped on the platform runner.
    """
    candidates = [
        ".env",
        ".env.local",
        ".env.production.local",
        "data/assessments.json",
        "coverage.xml",
        ".coverage",
        "src/pree/__pycache__/app.cpython-312.pyc",
    ]
    git = shutil.which("git")
    assert git, "git is required to assert ignore behaviour rather than file contents"
    result = subprocess.run(  # noqa: S603 - resolved git path, fixed literal arguments
        [git, "check-ignore", "--stdin", "--no-index"],
        input="\n".join(candidates),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    ignored = set(result.stdout.split())
    missing = [path for path in candidates if path not in ignored]
    assert not missing, f"git does not ignore: {missing}"


def test_the_example_environment_file_carries_no_real_value() -> None:
    """Every value must be empty or an explicit placeholder.

    The previous version read `assert "PREE_TEAM_TOKEN=\n" in body or "PREE_TEAM_TOKEN=" in
    body`, and the second clause is true for any value at all, so the test named
    "carries_no_real_value" passed with a live-looking token in the file. That matters beyond
    hygiene: the packaging allowlist ships .env.example inside the App Store archive, so a
    pasted credential would be committed and delivered.
    """
    allowed_placeholders = {"", "development", "local"}
    offending: list[str] = []
    for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if value.strip().strip("\"'") not in allowed_placeholders:
            offending.append(f"{name.strip()}={value.strip()}")
    assert not offending, (
        f"the example environment file carries values that are not placeholders: {offending}"
    )


def test_the_launch_command_targets_the_factory_that_actually_exists() -> None:
    """The load-bearing coupling between the Dockerfile and the module, which was untested.

    Asserting only "exec gunicorn" meant reverting the target to `pree.main:app` kept the whole
    suite green while the container could not start at all: gunicorn exits 4 with
    "Failed to find attribute 'app'". Both halves are asserted here so they cannot drift.
    """
    commands = [i.argument for i in _final_stage() if i.keyword == "CMD"]
    assert len(commands) == 1
    command = commands[0]
    # Read from the resolved CMD, not the file: leaving the old target in a comment satisfied a
    # whole-file substring while the container exited 4 on start.
    assert "pree.main:build()" in command, f"the launch target is not the factory: {command}"
    assert "pree.main:app" not in command
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
