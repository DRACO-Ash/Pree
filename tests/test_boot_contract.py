"""Deploy-contract assertions about the shipped files.

Every negative assertion here is classified per environment. None may be guaranteed-false on
the platform runner that gates the deploy, because the platform commits its own generated
.gitlab-ci.yml into the checkout and runs the suite there, not here.
"""

from __future__ import annotations

import importlib
import itertools
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from pree.app import STORAGE_PROBE_PATH
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
_TOKEN_TERMS = (
    "token",
    "pree_team_token",
    "credential",
    "secret",
    "passphrase",
    "password",
)
_RETIRED_TOKEN_RULES = (
    "distinct character",
    "character variety",
    "character-variety rule",
    "mix upper case",
    "mixed case",
    "upper case, lower case",
)


def _build_number_words() -> dict[str, int]:
    """Every spelled-out integer from zero to a thousand, hyphenated and spaced.

    Constructed, not listed. Five consecutive rounds defeated this guard with a number word
    that was not in a hand-written table: "ten", "thirty", "eighteen", "twenty-eight". A table
    someone has to remember to extend is a table that will be short by one entry again.
    """
    ones = [
        "zero",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
    ]
    tens = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
    words = {name: value for value, name in enumerate(ones)}
    for ten in range(2, 10):
        words[tens[ten]] = ten * 10
        for one in range(1, 10):
            for joiner in ("-", " "):
                words[f"{tens[ten]}{joiner}{ones[one]}"] = ten * 10 + one
    for one in range(1, 10):
        words[f"{ones[one]} hundred"] = one * 100
    words["a hundred"] = 100
    words["a thousand"] = 1000
    words["one thousand"] = 1000
    return words


_NUMBER_WORDS = _build_number_words()


@dataclass(frozen=True)
class _Instruction:
    """One resolved Dockerfile instruction, with comments and continuations removed."""

    stage: int
    stage_name: str
    keyword: str
    argument: str
    index: int
    # The WORKDIR in force at this instruction. Needed because a COPY destination may be
    # relative, and `WORKDIR /usr/bin` with `COPY --from=build /bin/true find` writes
    # /usr/bin/find while the destination token is just "find".
    workdir: str = "/"


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
        if not raw.strip().startswith("#"):
            break
        # BuildKit's own directive pattern tolerates whitespace on both sides of the `=`
        # (`^#[ \t]*escape[ \t]*=[ \t]*(?P<escapechar>.).*$`). The first version of this check
        # skipped any fragment with a space before the `=`, so `# escape = ` with one space was
        # a comment to this parser and a directive to docker: it stopped `\` continuing a line,
        # split a HEALTHCHECK this parser had swallowed whole, and left the resolved user root
        # with the whole suite green. A single space reopened the hole the check was added to
        # close, so the pattern is now BuildKit's, not an approximation of it.
        found = re.match(r"^#\s*([A-Za-z][A-Za-z0-9_.-]*)\s*=", raw)
        if found is not None:
            assert found.group(1).lower() == "syntax", (
                f"unrecognised parser directive {found.group(1)!r}; a directive changes how "
                "docker reads this file and is invisible to a line-based parser"
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
    workdir = "/"
    for index, line in enumerate(joined):
        parts = line.split(None, 1)
        keyword = parts[0].upper()
        argument = parts[1] if len(parts) > 1 else ""
        # SHELL is refused, not merely parsed. It changes HOW every later RUN is executed
        # without changing a character of the RUN itself, so `SHELL ["/bin/true"]` above the
        # suid sweep turned the exact-match allowlist below into a statement about a string
        # docker never runs: the whole suite stayed green while nothing was swept. This
        # Dockerfile needs no SHELL, the same reasoning already applied to heredocs and to
        # unknown parser directives.
        assert keyword != "SHELL", (
            "SHELL changes how every later RUN is executed while leaving its text untouched, "
            "which defeats every assertion about a RUN's command; it is refused outright"
        )
        assert keyword in _DOCKERFILE_KEYWORDS, (
            f"unrecognised Dockerfile keyword {keyword!r} in {line[:60]!r}; an unknown line "
            "may be a heredoc body or a typo, and either way it must not be classified silently"
        )
        if keyword == "FROM":
            stage += 1
            workdir = "/"
            pieces = argument.split()
            stage_name = (
                pieces[-1].lower() if len(pieces) >= 3 and pieces[-2].upper() == "AS" else ""
            )
        elif keyword == "WORKDIR":
            candidate = argument.strip().strip("\"'")
            workdir = (
                candidate if candidate.startswith("/") else f"{workdir.rstrip('/')}/{candidate}"
            )
        out.append(_Instruction(stage, stage_name, keyword, argument, index, workdir))
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


def _resolved_launch_command() -> str:
    """The single CMD of the shipped stage, as docker would resolve it."""
    commands = [i.argument for i in _final_stage() if i.keyword == "CMD"]
    assert len(commands) == 1, f"expected exactly one CMD in the final stage, found {commands}"
    return commands[0]


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
    command = _resolved_launch_command()
    assert "0.0.0.0:" in command, f"the launch command does not bind every interface: {command}"
    assert "127.0.0.1" not in command, f"the launch command binds loopback: {command}"
    assert "exec gunicorn" in command, (
        f"without exec, SIGTERM never reaches the server and shutdown hangs: {command}"
    )
    exposed = [i.argument.strip() for i in final if i.keyword == "EXPOSE"]
    assert exposed == ["8080"], f"the final stage exposes {exposed}, not the platform port"


def test_every_base_image_is_pinned_by_digest() -> None:
    """ "The base digest is pinned" was a comment, asserted nowhere.

    Replacing the digest reference with the bare `python:3.12-slim` tag in both stages left the
    suite green, and so did swapping the base at build time with `--build-arg BASE_DIGEST=`. A
    floating tag means the image scanned in CI and the image that ships can differ.
    """
    instructions = _instructions()
    # An ARG default is not a pin. `--build-arg BASE_DIGEST=sha256:<other>` replaced the base of
    # both stages while this test resolved the default and saw nothing wrong, so a digest that
    # lives in an ARG referenced by a FROM is refused outright.
    defaults = {
        name: value
        for name, _, value in (
            i.argument.partition("=") for i in instructions if i.keyword == "ARG"
        )
    }
    for instruction in instructions:
        if instruction.keyword != "FROM":
            continue
        for name in defaults:
            assert f"${name}" not in instruction.argument and f"${{{name}}}" not in (
                instruction.argument
            ), (
                f"FROM substitutes the build argument {name!r}, which --build-arg can replace "
                f"at build time: {instruction.argument}"
            )
    unpinned: list[str] = []
    for instruction in instructions:
        if instruction.keyword != "FROM":
            continue
        reference = instruction.argument.split()[0]
        if reference.lower() == "scratch":
            continue
        resolved = re.sub(
            r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?",
            lambda match: defaults.get(match.group(1), ""),
            reference,
        )
        if not re.search(r"@sha256:[0-9a-f]{64}$", resolved):
            unpinned.append(f"{reference} resolves to {resolved!r}")
    assert not unpinned, f"a base image is not pinned to a digest: {unpinned}"


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


SUID_SWEEP = (
    "/usr/bin/find / -xdev -perm /6000 \\( -type f -o -type d \\) -exec /bin/chmod a-s {} +"
)
# Paths a mutation could plant a no-op binary on to neuter a command whose text is pinned.
# `COPY --from=build /bin/true /usr/bin/find` was one line and left 22 of 22 tests green.
_EXECUTABLE_DIRECTORIES = ("/bin/", "/sbin/", "/usr/bin/", "/usr/sbin/", "/usr/local/bin/")


def test_the_suid_sweep_is_exactly_the_command_that_clears_every_bit() -> None:
    """An ALLOWLIST of one. `find`'s predicate grammar is open-ended, so a denylist loses.

    The previous version refused eight neutering tokens and the reviewer found four more that
    each left every boot-contract test green while the sweep cleared nothing: `-not -perm /6000`
    (always false), `-newer /etc/hostname`, `-regex ".*/no-match"` and `-uid 4242`. Two of those
    were confirmed against a real fixture carrying 4755 and 2755 files: the bits survived, where
    the shipped command clears them. Chasing predicates is unwinnable, and with no docker daemon
    in the loop this text is the ONLY verification of a hard rule, so the command must be the
    command. Changing the sweep now means changing this literal too, deliberately.
    """
    sweep = next(i for i in _instructions() if "-perm /6000" in i.argument)
    assert " ".join(sweep.argument.split()) == SUID_SWEEP, (
        f"the sweep is not the exact vetted command.\n  shipped: {sweep.argument}\n  vetted:  "
        f"{SUID_SWEEP}"
    )
    # And exactly one of them, so a second, narrower find cannot sit beside it.
    sweeps = [i for i in _instructions() if "-perm /6000" in i.argument]
    assert len(sweeps) == 1, f"{len(sweeps)} instructions mention the suid mask; expected one"


def test_the_launch_command_refuses_to_trust_a_forwarded_client_address() -> None:
    """Both rate-limit tiers key on scope["client"], which a proxy header can rewrite.

    uvicorn installs ProxyHeadersMiddleware unconditionally and gunicorn's default trust list
    is os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1,::1"), so as shipped the middleware
    replaced the peer address with a caller-supplied X-Forwarded-For before the app ran.
    Measured against the running server: 0 of 300 requests refused with a rotating header,
    against 60 of 300 refused once the flag is pinned, and FORWARDED_ALLOW_IPS="*" in the
    environment could not reopen it because an explicit flag beats the environment default.

    255.255.255.255 is the limited broadcast address and can never be the source of a TCP
    connection. It is asserted rather than merely present because "*" in this flag hands the
    limiter's whole key space to the caller.
    """
    command = _resolved_launch_command()
    flags = [part for part in command.split() if part.startswith("--forwarded-allow-ips")]
    assert flags, (
        "the launch command does not pin --forwarded-allow-ips, so the trust list falls back to "
        "gunicorn's default of loopback plus whatever FORWARDED_ALLOW_IPS is set to, and both "
        "rate-limit tiers become caller-controlled"
    )
    assert len(flags) == 1, f"the flag is given more than once, and the last wins: {flags}"
    value = flags[0].partition("=")[2]
    assert value == "255.255.255.255", (
        f"the forwarded trust list is {value!r}; only the limited broadcast address is vetted "
        "here, and '*' or a loopback value trusts a header the caller writes"
    )


def test_nothing_writes_over_a_binary_the_hardening_steps_depend_on() -> None:
    """Pinning a command's text is worthless if its binaries can be replaced.

    `COPY --from=build /bin/true /usr/bin/find` above the sweep is one line, changes not a
    character of the pinned command, and leaves every other assertion green while the sweep
    clears nothing. Absolute paths in the sweep close the PATH route; this closes the other one.
    """
    offenders: list[str] = []
    for instruction in _instructions():
        if instruction.keyword not in {"COPY", "ADD"}:
            continue
        # Resolved against the WORKDIR in force, and stripped of quoting and JSON punctuation.
        # Testing the raw last token missed two forms: `WORKDIR /usr/bin` then
        # `COPY --from=build /bin/true find` gives the token "find", and the JSON form
        # `COPY --from=build ["/bin/true", "/usr/bin/find"]` gives `"/usr/bin/find"]`. Both
        # replaced /usr/bin/find with a no-op and left the whole suite green.
        raw = instruction.argument.split()[-1].strip("[]\"',")
        target = raw if raw.startswith("/") else f"{instruction.workdir.rstrip('/')}/{raw}"
        if any(target.startswith(directory) for directory in _EXECUTABLE_DIRECTORIES):
            offenders.append(f"{instruction.keyword} {instruction.argument[:80]} -> {target}")
    assert not offenders, (
        f"an instruction writes into a system executable directory, so the binaries the "
        f"hardening steps name may not be the binaries that run: {offenders}"
    )


def test_no_instruction_fetches_from_the_network_or_rewrites_the_shipped_path() -> None:
    """Two more ways to change what runs without changing what the assertions read.

    `ADD https://example.invalid/app.py /app/src/pree/app.py` replaces the application source
    at build time from a host nothing here controls, and the build is otherwise identical. And
    an `ENV PATH=` in the shipped stage hijacks the `sh` and the `gunicorn` the pinned CMD
    resolves, which is the same lesson as the sweep: pinning a command's text says nothing
    about which binaries its names reach. Both left the boot contract green.
    """
    remote = [
        f"{i.keyword} {i.argument[:80]}"
        for i in _instructions()
        if i.keyword == "ADD" and re.search(r"\bhttps?://|\bgit@", i.argument)
    ]
    assert not remote, f"an instruction fetches from the network at build time: {remote}"

    shipped_paths = [
        i.argument
        for i in _final_stage()
        if i.keyword == "ENV" and re.search(r"\bPATH\s*=", i.argument)
    ]
    assert len(shipped_paths) == 1, (
        f"the shipped stage sets PATH {len(shipped_paths)} times; the launch command resolves "
        f"sh and gunicorn through it: {shipped_paths}"
    )
    assert '"/opt/venv/bin:' in shipped_paths[0], (
        f"the shipped PATH does not begin with the venv, so the pinned CMD may resolve a "
        f"different gunicorn: {shipped_paths[0][:120]}"
    )


def test_the_shipped_stage_declares_no_instruction_that_undoes_a_control() -> None:
    """HEALTHCHECK NONE, VOLUME and STOPSIGNAL all passed silently.

    `HEALTHCHECK NONE` disables the storage proof that three earlier rounds treated as the
    control keeping a pod with a broken mount out of service, and the security policy cites it
    repeatedly. VOLUME on the data directory changes the mount semantics the store relies on.
    STOPSIGNAL SIGKILL removes the graceful shutdown the exec form exists to preserve.
    """
    final = _final_stage()
    checks = [i.argument for i in final if i.keyword == "HEALTHCHECK"]
    assert len(checks) == 1, f"the shipped stage declares {len(checks)} HEALTHCHECKs: {checks}"
    assert checks[0].strip().upper() != "NONE", "the shipped stage disables its HEALTHCHECK"
    assert STORAGE_PROBE_PATH in checks[0], (
        f"the HEALTHCHECK does not probe {STORAGE_PROBE_PATH}, so it proves nothing about "
        f"storage: {checks[0][:120]}"
    )
    for unwanted in ("VOLUME", "STOPSIGNAL"):
        found = [i.argument for i in final if i.keyword == unwanted]
        assert not found, f"the shipped stage declares {unwanted} {found}"


def test_the_pip_removal_targets_the_venv_the_build_actually_creates() -> None:
    """A removal aimed at a path that does not exist is a no-op the substring check accepted.

    Repointing the removal at `/nowhere/site-packages/pip*` left the suite green, and pip then
    ships in the image, which the Dockerfile's own comment says carries CVEs the policy scan
    stops on. The removal is tied to the venv the build stage creates.
    """
    instructions = _instructions()
    venv = next(i for i in instructions if i.keyword == "RUN" and "-m venv" in i.argument)
    target = venv.argument.split()[-1]
    assert target.startswith("/"), f"the venv is created at a relative path: {venv.argument!r}"
    removal = next(i for i in instructions if "site-packages/pip" in i.argument)
    assert f"{target}/lib/python3.12/site-packages/pip" in removal.argument, (
        f"the pip removal does not target the venv at {target!r}: {removal.argument[:120]!r}"
    )
    assert f"{target}/bin/pip" in removal.argument, (
        f"the pip entry points under {target!r} are not removed: {removal.argument[:120]!r}"
    )


def test_the_numeric_user_is_created_unprivileged() -> None:
    """`--uid 0` satisfied "a useradd exists in the shipped stage" and creates root."""
    creation = next(i for i in _instructions() if "--uid 10001" in i.argument)
    assert "--uid 10001" in creation.argument
    for privileged in ("--uid 0", "--gid 0", "--groups root", "-o "):
        assert privileged not in creation.argument, (
            f"the runtime user is created with {privileged!r}: {creation.argument[:120]!r}"
        )


def test_the_shipped_stage_is_exactly_one_copied_layer() -> None:
    """The image-policy scan reads layer history, so one clean layer is the point.

    Counting only `COPY --from=prep / /` let a second COPY into the scratch stage pass.
    """
    final = _final_stage()
    copies = [i.argument for i in final if i.keyword in {"COPY", "ADD"}]
    runs = [i.argument for i in final if i.keyword == "RUN"]
    assert len(copies) == 1, f"the shipped stage copies {len(copies)} times: {copies}"
    assert copies[0].startswith("--from="), f"the single copy is not from a stage: {copies[0]}"
    # Every TOKEN of the copy, not just its first. Asserting only that it starts with --from=
    # left its flags unguarded, and `COPY --from=prep --chmod=0777 --chown=0:0 / /` ships the
    # whole filesystem world-writable and root-owned with the suite green: --chmod undoes the
    # suid sweep's whole purpose and --chown undoes the numeric user.
    tokens = copies[0].split()
    assert len(tokens) == 3 and tokens[1:] == ["/", "/"], (
        f"the shipped copy carries flags or paths beyond --from=<stage> / /: {copies[0]!r}"
    )
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


def _claim_units(path: Path) -> list[str]:
    """One unit per line, plus each ADJACENT PAIR of table rows.

    Three shapes had to be handled and the first two attempts each broke on the third. Treating
    every line separately missed a floor split over two rows of a table. Joining every
    contiguous run of rows into one unit swallowed the whole 70-row control table, so any
    integer anywhere in it read as a claim about the token and the guard became unusable noise.
    Pairs of adjacent rows cover the split-row case and can never grow past two rows.
    """
    lines = [
        re.sub(r"\s+", " ", raw).strip() for raw in path.read_text(encoding="utf-8").splitlines()
    ]
    # A leading ordered-list marker is stripped: "8." at the start of a line is the item's
    # index, not a claim about anything, and the inverted rule below would read it as one.
    units = [re.sub(r"^\d+\.\s+", "", line.strip(" ●■")) for line in lines if line]
    # EVERY adjacent pair, not only table rows. Both these documents wrap at about 100 columns,
    # so a sentence stating the floor can split into a line with the subject and no number and a
    # line with the number and no subject, and neither half trips the rule. That is why
    # DEPLOYMENT.md kept its floor sentence as one long unwrapped line, which no guard enforced:
    # a rewrap would have silently disabled the check.
    units += [
        f"{first} {second}" for first, second in itertools.pairwise(lines) if first and second
    ]
    return units


_NUMBER_WORD_PATTERN = re.compile(
    r"\b(?:" + "|".join(sorted(map(re.escape, _NUMBER_WORDS), key=len, reverse=True)) + r")\b"
)


def _numbers_in(unit: str, size: re.Pattern[str], *, words: bool) -> set[str]:
    """Digits anywhere in the unit; spelled-out numbers only next to a size word.

    The two need different rules, because English uses number words as pronouns and articles
    and does not use digits that way. "or one built entirely from a repeated sequence" and
    "all three of the operator-set values" are not claims about a length, and treating every
    number word as a figure flagged four true sentences in this repository at once. A digit in
    a line about the token and its size is a claim; "one" might be a pronoun.

    Longest-first alternation, so "twenty-eight" matches as one number rather than as "twenty"
    followed by "eight", which would report 20 and 8 for a unit that states 28.
    """
    lowered = unit.lower()
    found = set(re.findall(r"\b\d[\d,]*\b", lowered))
    if not words:
        return found
    # The size word may be ANYWHERE in the unit, matching the digit branch. A 40-character
    # window was tried and beaten: a fabricated "Use sixteen, produced with a secure random
    # generator" sat a line away from its size word and passed, which would have had an operator
    # set a 16-character token and watch the deploy fail. The false positives that follow are
    # the price, and they are visible immediately rather than latent.
    if _NUMBER_WORD_PATTERN.search(lowered):
        found |= set(_NUMBER_WORD_PATTERN.findall(lowered))
    return found


def test_no_document_states_a_token_size_but_the_enforced_one() -> None:
    """Inverted, after nineteen attempts at matching forms.

    Every previous version asked "does this look like a floor?" and lost, because the answer
    depends on a complete table of number words, a complete table of size words, a complete
    table of token synonyms and a complete notion of adjacency, and each round the reviewer
    found the entry that was missing: "ten" and "thirty" were not in the number table,
    "octets" was not in the size table, "secret" was not in the token table, and an adjective
    between the figure and the word broke adjacency.

    The rule is now the other way round. In any unit that mentions the token AND mentions a
    size, the enforced constant must appear, and no OTHER number may. That needs no complete
    table of anything: a wrong floor is wrong because it is a number that is not 32, whatever
    words surround it. The cost is that a legitimate sentence pairing the token with any other
    figure now fails, which is a cost worth paying for a rule that stops needing repairs.
    """
    floor = str(importlib.import_module("pree.config").MIN_PRODUCTION_TOKEN_LENGTH)
    size = re.compile(
        r"char|byte|octet|bit\b|digit|letter|glyph|symbol|code ?point|length|long|floor|"
        r"minimum|maximum|shorter|longer|fewer|under|below|beneath|less than|more than|over\b|"
        r"at least|no fewer|refused below|rejects any",
        re.IGNORECASE,
    )
    # The INSTRUCTION-bearing files only, and this scope is a decision with a reason rather
    # than a convenience. docs/SECURITY.md and docs/CHANGELOG.md are records of engineering
    # history, and that history is measurements: "five 5,000-character field names", "a
    # 15,000-character request line", "a 30,074-byte record", each in a sentence that also says
    # "token". No text rule can separate a measurement from a floor claim without understanding
    # the sentence, and every version of this guard that tried flagged those true statements.
    #
    # What the scope costs: a wrong floor asserted inside the policy or the changelog would
    # pass. What it protects: every file an operator reads and copies from. The sheet is where
    # a wrong number becomes a wrong deployment, and the sheet is checked completely, digits
    # and spelled-out words alike.
    scanned = [
        REPO_ROOT / "docs" / "DEPLOYMENT.md",
        REPO_ROOT / "README.md",
        REPO_ROOT / "CLAUDE.md",
        REPO_ROOT / ".env.example",
    ]
    checked = 0
    wrong: list[str] = []
    retired: list[str] = []
    for path in scanned:
        if not path.is_file():
            continue
        for unit in _claim_units(path):
            lowered = unit.lower()
            if not any(term in lowered for term in _TOKEN_TERMS):
                continue
            if path.name == "DEPLOYMENT.md" and any(
                phrase in lowered for phrase in _RETIRED_TOKEN_RULES
            ):
                retired.append(f"{path.name}: {unit[:160]}")
            if not size.search(unit):
                continue
            # The early-out stays. Requiring the floor to be PRESENT in every unit that pairs
            # the token with a size was tried and is unworkable at this document scale: prose
            # legitimately says "the token length is reported as a boolean and a length" with no
            # figure at all, and fourteen such lines in these documents flagged at once. A guard
            # that flags fourteen true statements to catch one false one gets relaxed.
            #
            # The defeat those two fabrications used was an unrecognised number WORD, so the
            # word table is now complete BY CONSTRUCTION for every value up to a thousand
            # rather than hand-listed. "eighteen" and "twenty-eight" were the missing entries;
            # there are no missing entries now.
            numbers = _numbers_in(unit, size, words=True)
            if not numbers:
                continue
            checked += 1
            others = {n for n in numbers if n != floor and _NUMBER_WORDS.get(n) != int(floor)}
            if floor not in numbers or others:
                wrong.append(f"{path.name}: {unit[:160]} (numbers {sorted(numbers)})")

    assert checked, (
        "no document pairs the team token with a size at all; the operator has no way to know "
        "what production will refuse"
    )
    assert not wrong, (
        f"production refuses a token shorter than {floor} characters. Every line that mentions "
        f"the token and a size must state {floor} and no other number: {wrong}"
    )
    assert not retired, (
        f"a rule the code no longer has is still documented for the operator: {retired}"
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
    # The platform pipeline runs this suite from the EXTRACTED archive: .gitignore is present,
    # .git is not, and `git check-ignore` outside a work tree fails for every path. That
    # surfaced only when the pipeline simulation was re-run after this test was rebuilt from a
    # text check into a behavioural one, so the behavioural version had never run anywhere but
    # a developer checkout.
    #
    # The first attempt at this skip asserted `not ON_PLATFORM_RUNNER` on the grounds that the
    # platform commits its own generated .gitlab-ci.yml into a checkout, so a work tree would
    # always exist there. The simulation, which sets GITLAB_CI=true and runs from the extracted
    # archive, failed that assertion immediately: the belief was mine, not evidence, and it was
    # wrong. Recorded rather than quietly deleted, because it was a guess dressed as a control.
    #
    # What the ignore rules actually protect is the environment where someone COMMITS, and that
    # is a work tree by definition. The archive has no history to add to, so there is nothing
    # for this to be true or false about there. The local loop always runs in a work tree, so
    # the assertion below always runs somewhere that matters; the claim is exactly that, and
    # not that the platform verifies it.
    inside = subprocess.run(  # noqa: S603 - resolved git path, fixed literal arguments
        [git, "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    in_work_tree = inside.stdout.strip() == "true"
    if not in_work_tree:
        pytest.skip("no git work tree: nothing can be committed here, so nothing to ignore")
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
    # EVERY .env* file in the tree, not only the root one. The packaging exemption used to
    # match the basename anywhere, so `docs/.env.example` carrying a live-looking token shipped
    # in the archive while this test, reading the root path only, stayed green. The exemption is
    # anchored now; this reads the whole tree so the two cannot disagree again.
    candidates = sorted(
        path
        for path in REPO_ROOT.rglob(".env*")
        if path.is_file() and ".venv" not in path.parts and ".git" not in path.parts
    )
    assert candidates == [REPO_ROOT / ".env.example"], (
        f"the tree carries environment files beyond the root example: {candidates}"
    )
    for path in candidates:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, _, value = stripped.partition("=")
            if value.strip().strip("\"'") not in allowed_placeholders:
                offending.append(f"{path.name}: {name.strip()}={value.strip()}")
    assert not offending, (
        f"an example environment file carries values that are not placeholders: {offending}"
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
    """Every test the suite defines, sync and async alike.

    The pattern used to be `^def (test_...)`, which cannot see an `async def`. Every async test
    in the suite was therefore invisible to the register, so a row citing one failed as though
    the test did not exist. That failed closed rather than open, but it pushed the register
    towards citing file paths instead of test names, which is weaker evidence for no reason.
    """
    names: set[str] = set()
    for path in (REPO_ROOT / "tests").glob("test_*.py"):
        names |= set(
            re.findall(r"^(?:async )?def (test_[a-z0-9_]+)", path.read_text(encoding="utf-8"), re.M)
        )
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
