"""Deploy-contract assertions about the shipped files.

Every negative assertion here is classified per environment. None may be guaranteed-false on
the platform runner that gates the deploy, because the platform commits its own generated
.gitlab-ci.yml into the checkout and runs the suite there, not here.
"""

from __future__ import annotations

import importlib
import itertools
import os
import posixpath
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


# What counts as a SENTENCE about the team token, and the retired rules that must not return. The
# unit is a sentence, not a line: `_claim_units` was rebuilt to split on terminators after line
# windows lost twice. This table has been one entry short three times, "secret", "passphrase" and
# "key" each added after a fabrication used it, which is a residual recorded in the token-floor
# test rather than a claim of completeness.
_TOKEN_TERMS = (
    "token",
    "pree_team_token",
    "credential",
    "secret",
    "passphrase",
    "password",
    "key",
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


def _instructions(text: str | None = None) -> list[_Instruction]:
    """Parse the Dockerfile into resolved instructions, per build stage.

    Every assertion below used to be a substring grep over the raw text, and every one of them
    was defeatable: `USER root` appended after the numeric user, a `chmod u+s` after the suid
    sweep, `ENV PORT 9999` in the legacy space-separated form, a bind to loopback with the
    0.0.0.0 string left behind in a comment, a non-exec CMD with "exec gunicorn" left in a
    comment, and a second COPY into the supposedly single-layer stage. Comments are stripped
    and the resolved final-stage state is what gets asserted.
    """
    # The text is a parameter so the PARSER can be tested on synthetic input, not only
    # exercised through the shipped Dockerfile. Every previous defect in this file was a defect
    # in the parser, and four were found by a reviewer because nothing asserted what it does.
    text = _dockerfile() if text is None else text
    # A heredoc body is text to `docker build` and instructions to any line-based parser, so
    # `COPY <<DECOY` followed by a fabricated FROM/USER/CMD created a phantom final stage that
    # satisfied every assertion below while the real stage ran as root on loopback. This
    # project's Dockerfile has no need of heredocs, so their presence is refused rather than
    # interpreted.
    assert "<<" not in text, "heredocs are refused: a line-based parser cannot read them safely"
    # Parser directives are comments to every line-based reader and instructions to docker. An
    # `# escape=` directive stops `\` continuing a line, so docker split a HEALTHCHECK that this
    # parser had swallowed whole and read the `USER root` hidden inside it as its own
    # instruction. EVERY directive is refused, `syntax` included; see the reasoning below.
    for raw in text.split("\n"):
        if not raw.strip().startswith("#"):
            break
        # BuildKit's own directive pattern tolerates whitespace on both sides of the `=`
        # (`^#[ \t]*escape[ \t]*=[ \t]*(?P<escapechar>.).*$`). The first version of this check
        # skipped any fragment with a space before the `=`, so `# escape = ` with one space was
        # a comment to this parser and a directive to docker: it stopped `\` continuing a line,
        # split a HEALTHCHECK this parser had swallowed whole, and left the resolved user root
        # with the whole suite green. A single space reopened the hole the check was added to
        # close, so the pattern is now BuildKit's, not an approximation of it.
        #
        # EVERY directive is refused, `syntax` included. The previous version checked the
        # directive's NAME and never looked at its VALUE, so `# syntax=attacker.example/
        # evil-frontend:latest` passed 292 of 292 tests. A syntax value is a build frontend
        # image: BuildKit pulls it and hands it this file and the whole build context, and it
        # may emit any image it likes, which makes every assertion in this file a statement
        # about a document nothing executes. Pinning the value by digest would close that, but
        # this build needs no BuildKit-only feature, so the directive is gone from the
        # Dockerfile and refused here instead. That is one fewer network pull in the build and
        # one fewer thing to keep pinned.
        found = re.match(r"^#\s*([A-Za-z][A-Za-z0-9_.-]*)\s*=", raw)
        if found is not None:
            raise AssertionError(
                f"parser directive {found.group(1)!r} present; a directive changes how docker "
                "reads this file, or which frontend reads it at all, and is invisible to a "
                "line-based parser. This build needs none."
            )
    joined: list[str] = []
    buffer = ""
    # BuildKit's line model, not Python's. `scanLines` splits on "\n" ALONE and `trimNewline`
    # trims "\r\n" ALONE, so those are the only characters that may come off a line end before
    # the continuation rule is applied. This parser used `str.splitlines()` and a blanket
    # `rstrip()`, and both are wider: splitlines breaks at VT, FF, 0x1c-0x1e, NEL and U+2028,
    # and rstrip strips those plus NBSP and every Unicode space. Either width made a line
    # ending `\` + VT a continuation here and a COMPLETE instruction to docker, so
    # `LABEL org.opencontainers.image.title=pree\<0x0b>` above `USER root` swallowed the USER
    # into a LABEL argument: 292 tests green, shipped user root, and a 0x0b invisible in an
    # editor and in a diff. That is the round-twenty blocker reopened one byte to the side.
    for raw in text.split("\n"):
        line = raw.rstrip("\r\n")
        if line.lstrip().startswith("#") or not line.strip():
            continue
        # BuildKit's own rule, taken from its parser rather than approximated:
        # lineContinuationRegex = `([^\\])\\[ \t]*$|^\\[ \t]*$`. An ESCAPED backslash at the
        # end of a line is not a continuation, and treating it as one swallowed the next
        # instruction whole. `LABEL org.opencontainers.image.title=pree\\` followed by
        # `USER root` therefore vanished from every assertion in this file while docker
        # resolved the shipped stage's user to root, with the boot contract green.
        continues = re.search(r"(?:^|[^\\])\\[ \t]*$", line) is not None
        # Joined with NOTHING, which is what BuildKit does (`buf.Write(bytesRead)`), not with a
        # space. Inserting one meant a path split mid-token across a continuation resolved to
        # something harmless here and to the real target for docker: `/usr/bi` + `\\` + newline
        # + `n/find` read as `/app/n/find` and passed, while docker wrote `/usr/bin/find`.
        # Cut at the backslash rather than at the last character: with only "\r\n" trimmed
        # above, a continuation may carry trailing spaces or tabs after the `\`, exactly as
        # BuildKit's own `trimContinuationCharacter` allows.
        buffer += line[: line.rindex("\\")] if continues else line
        if not continues:
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


def test_the_parser_follows_buildkit_continuation_semantics() -> None:
    """The PARSER is tested here, not only exercised through the shipped Dockerfile.

    Every previous defect in this file was a defect in the parser, and four of them were found by
    a reviewer rather than by the suite, because nothing asserted what the parser does. Mutating
    the continuation rule, the joiner, the opaque-path refusal and the abbreviation shielding all
    left 289 tests green: the guards were load-bearing against the Dockerfile and the parser
    underneath them was load-bearing against nothing.

    BuildKit's rule is `lineContinuationRegex = ([^\\])\\[ \t]*$|^\\[ \t]*$`, and its joiner
    writes the continuation with no separator.
    """
    # An ESCAPED trailing backslash ends the instruction. Treating it as a continuation swallowed
    # the next instruction whole: a LABEL carrying one hid a USER root from every assertion.
    escaped = _instructions("FROM scratch\nLABEL title=pree\\\\\nUSER root\n")
    keywords = [i.keyword for i in escaped]
    assert keywords == ["FROM", "LABEL", "USER"], (
        f"an escaped trailing backslash was read as a continuation: {keywords}"
    )
    assert escaped[-1].argument == "root"

    # A REAL continuation joins with NOTHING. Inserting a space made a path split mid-token
    # resolve to something harmless here and to the real target for docker.
    joined = _instructions("FROM scratch\nCOPY a /usr/bi\\\nn/find\n")
    assert [i.keyword for i in joined] == ["FROM", "COPY"], [i.keyword for i in joined]
    assert joined[-1].argument == "a /usr/bin/find", (
        f"continuation lines were joined with a separator: {joined[-1].argument!r}"
    )

    # And a bare backslash on its own line is a continuation, per the second half of the rule.
    bare = _instructions("FROM scratch\nRUN echo one \\\n && echo two\n")
    assert [i.keyword for i in bare] == ["FROM", "RUN"], [i.keyword for i in bare]

    # A backslash followed by a space or a tab IS a continuation, because BuildKit's rule
    # tolerates `[ \t]*` after it. Trimming only "\r\n" leaves that whitespace on the line, so
    # the joiner has to cut at the backslash rather than at the last character.
    for tail in (" ", "\t", "  \t "):
        padded = _instructions(f"FROM scratch\nCOPY a /usr/bi\\{tail}\nn/find\n")
        assert [i.keyword for i in padded] == ["FROM", "COPY"], [i.keyword for i in padded]
        assert padded[-1].argument == "a /usr/bin/find", (
            f"a continuation padded with {tail!r} was mis-joined: {padded[-1].argument!r}"
        )

    # And a backslash followed by anything ELSE is NOT a continuation. The reasoning is at the
    # line model in `_instructions`; the carriers are, in order: VT, FF, the C1 line separators,
    # NEL, NBSP, EN QUAD, IDEOGRAPHIC SPACE, and LINE SEPARATOR, each of which is whitespace to
    # `str.rstrip()` or a line break to `str.splitlines()`.
    for carrier in (
        "\x0b",
        "\x0c",
        "\x1c",
        "\x1d",
        "\x1e",
        "\x85",
        "\xa0",
        "\u2000",
        "\u3000",
        "\u2028",
    ):
        text = f"FROM scratch\nLABEL title=pree\\{carrier}\nUSER root\n"
        hidden = _instructions(text)
        keywords = [i.keyword for i in hidden]
        assert keywords == ["FROM", "LABEL", "USER"], (
            f"a trailing backslash followed by {carrier!r} was read as a continuation, so the "
            f"next instruction vanished: {keywords}"
        )
        assert hidden[-1].argument == "root", hidden[-1].argument

    # CRLF line endings throughout, which is the one trailing character docker does trim.
    crlf = _instructions("FROM scratch\r\nCOPY a /usr/bi\\\r\nn/find\r\nUSER 10001:10001\r\n")
    assert [i.keyword for i in crlf] == ["FROM", "COPY", "USER"], [i.keyword for i in crlf]
    assert crlf[1].argument == "a /usr/bin/find", crlf[1].argument


def test_the_claim_unit_splitter_pairs_a_colon_sentence_with_what_follows(
    tmp_path: Path,
) -> None:
    """The prose-to-claim splitter, exercised directly, because it had two defects nobody saw.

    It raised UnboundLocalError on any document opening with a table row, and its colon pairing
    reached a table row and nothing else, so a wrong floor written as a bullet, a fenced block or
    a heading-then-row passed. The house style bullets with `●`, which made the bullet shape the
    likeliest of the four.
    """
    # No crash on a document whose first non-blank line is a table row.
    row_first = tmp_path / "row-first.md"
    row_first.write_text("| rule | value |\n| --- | --- |\n| minimum | 32 |\n", encoding="utf-8")
    assert _claim_units(row_first), "a document opening with a table row produced no units"

    for name, body in (
        ("bullet", "The floor is:\n\n● 16 characters, minimum.\n"),
        ("fence", "The floor is:\n\n```\n16 characters\n```\n"),
        ("heading", "The floor is:\n\n### Floor\n\n| minimum | 16 characters |\n"),
        ("row", "The floor is:\n\n| minimum | 16 characters |\n"),
    ):
        document = tmp_path / f"{name}.md"
        document.write_text(body, encoding="utf-8")
        units = _claim_units(document)
        assert any("The floor is:" in unit and "16" in unit for unit in units), (
            f"the colon sentence did not reach the {name} carrying the number: {units}"
        )

    # And the reach is BOUNDED, or a colon sentence runs together with the next section and the
    # guard starts flagging true statements two paragraphs away.
    far = tmp_path / "far.md"
    far.write_text(
        "The floor is:\n\n● one\n\n● two\n\n● three\n\n● four\n\nA later 16 appears here.\n",
        encoding="utf-8",
    )
    assert not any("The floor is:" in unit and "16" in unit for unit in _claim_units(far)), (
        "the colon sentence reached past its block and would flag an unrelated statement"
    )


def test_no_parser_directive_survives_the_first_line() -> None:
    """A directive is a comment to every line-based reader and an instruction to docker.

    The reasoning is at the refusal in `_instructions`; this asserts the outcome on the shipped
    file and on six directive forms.
    """
    assert not _dockerfile().startswith("# syntax"), (
        "the Dockerfile opens with a syntax directive again; it names a build frontend image "
        "that receives the whole build context, and this build needs no BuildKit-only feature"
    )
    for directive in (
        "# syntax=docker/dockerfile:1",
        "# syntax = docker/dockerfile:1",
        "# syntax=attacker.example/evil-frontend:latest",
        "# escape=`",
        "# escape = `",
        "#check=skip=all",
    ):
        with pytest.raises(AssertionError, match="parser directive"):
            _instructions(f"{directive}\nFROM scratch\nUSER 10001:10001\n")

    # The two shapes BuildKit's `DetectSyntax` honours BEYOND a leading `#name=` comment: a
    # byte-order mark before the comment, and the C-style `// syntax=` form. Both select an
    # attacker's frontend in the real parser. Both are refused here, but by the
    # unrecognised-keyword assert rather than by the directive guard, so nothing pinned them and
    # a change to the keyword handling could reopen them silently. Pinned now, by outcome rather
    # than by which assert fires, because either refusal is fail-closed.
    for exotic in (
        "\ufeff# syntax=attacker.example/evil-frontend:latest",
        "// syntax=attacker.example/evil-frontend:latest",
        "#!/bin/sh\n# syntax=attacker.example/evil-frontend:latest",
    ):
        with pytest.raises(AssertionError):
            _instructions(f"{exotic}\nFROM scratch\nUSER 10001:10001\n")

    # A comment that contains an equals sign somewhere OTHER than the directive position is
    # ordinary prose, and the shipped file's leading block is full of it. The refusal is
    # deliberately wider than BuildKit's own directive table: BuildKit treats an unknown
    # `# name=value` as a plain comment, but this parser cannot tell a comment from a directive
    # docker learns to honour in a later release, so the shape is refused and the leading block
    # is written without it. Fail closed, at the cost of one comment style.
    ordinary = _instructions("# the venv PATH is set with ENV PATH=... below\nFROM scratch\n")
    assert [i.keyword for i in ordinary] == ["FROM"], [i.keyword for i in ordinary]


def test_the_sentence_splitter_does_not_break_at_an_abbreviation() -> None:
    """`e.g. 16 characters` lost its subject to the previous half and a wrong floor passed."""
    assert _sentences("Use a long token, e.g. 16 characters. Then deploy.") == [
        "Use a long token, e.g. 16 characters.",
        "Then deploy.",
    ]
    assert _sentences("One. Two.") == ["One.", "Two."]
    assert _sentences("A floor applies, i.e. 32 characters.") == [
        "A floor applies, i.e. 32 characters."
    ]


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


# flag variables take exactly "1". PATH is checked separately against the guarded directories,
# because its value is a list rather than a constant.
_ALLOWED_ENV_VALUES = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PIP_NO_CACHE_DIR": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PYTHONUNBUFFERED": "1",
}
_ALLOWED_ENV_NAMES = frozenset({*_ALLOWED_ENV_VALUES, "PATH"})


def test_no_stage_sets_an_environment_variable_outside_the_allowlist() -> None:
    """FAIL CLOSED on an unknown name, rather than recognising a growing list of bad ones.

    This replaced two denylists, one of platform-injected names and one of credential terms, and it
    strictly subsumes both: no name on the allowlist contains a credential term or a
    platform-injected name, so neither denylist could ever fire first. Proved by mutation, where a
    single planted credential failed all three tests, so two of them were asserting nothing new.

    The history is why the shape changed. Those term tables were one entry short seven times
    running: `keys?` as a delimited word missed `keyring`; "key" was missing after "secret" and
    "passphrase" had been added for the same reason; `passwd` and `pwd` were absent while the
    pre-write hook had known them from the start; and finally `ENV PREE_AUTH=<value>` matched no
    term in any list. Adding "auth" would have been the eighth iteration. Naming what is PERMITTED
    ends the loop: this image needs four flag variables and a PATH, so a fifth name is a decision
    somebody makes and a reviewer sees, and a credential cannot be baked under any name because no
    new name is permitted at all. PREE_ENV matters most of the four platform names it replaced,
    because the loader defaults it to production, so baking `development` would permit no token,
    serve the documentation paths unauthenticated and admit a cleartext origin.
    """
    unexpected: list[str] = []
    for instruction in _instructions():
        if instruction.keyword not in {"ENV", "ARG"}:
            continue
        for name, value in _env_assignments(instruction.argument):
            if name not in _ALLOWED_ENV_NAMES:
                unexpected.append(f"{instruction.keyword} {name}")
            elif name in _ALLOWED_ENV_VALUES and value != _ALLOWED_ENV_VALUES[name]:
                unexpected.append(f"{instruction.keyword} {name}=[REDACTED:value]")
            elif name == "PATH":
                # EVERY stage, not only the shipped one. A neutered PATH in the prep stage is not
                # exploitable today, because the sweep names absolute binaries and every RUN and
                # COPY is allowlisted, but nothing read it at all and the guard that reads the
                # shipped stage says nothing about the stage that does the hardening.
                guarded = {directory.rstrip("/") for directory in _EXECUTABLE_DIRECTORIES}
                stray = [
                    entry
                    for entry in value.split(":")
                    if entry and entry != "$PATH" and entry.rstrip("/") not in guarded
                ]
                if stray:
                    unexpected.append(f"{instruction.keyword} PATH carries {stray}")
    assert not unexpected, (
        f"these environment names are set in the image and are not on the allowlist: "
        f"{unexpected}. An image-level value beats the platform's injection and can freeze a "
        f"credential into a layer, so a new name is a deliberate decision: add it to "
        f"_ALLOWED_ENV_NAMES with a reason"
    )


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


# The digest that decides what actually ships, pinned as a literal the way SUID_SWEEP and the
# forwarded trust list are. Requiring only that A digest exists let one character change swap the
# base filesystem, including for one carrying a pre-neutered /usr/bin/find, with the whole suite
# green. With the containerize leg exiting 2 for want of a daemon, this text is all there is.
BASE_DIGEST = "sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a"


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

    # And pinned to THE vetted digest, the same one in both stages. A digest that merely exists
    # is not a pin: changing one character ships a different filesystem.
    digests = [
        match.group(1)
        for instruction in instructions
        if instruction.keyword == "FROM"
        for match in [re.search(r"@(sha256:[0-9a-f]{64})", instruction.argument)]
        if match
    ]
    assert digests, "no FROM carries a digest at all"
    assert set(digests) == {BASE_DIGEST}, (
        f"a base image is pinned to a digest other than the vetted one. Changing the base is a "
        f"deliberate act: update BASE_DIGEST in this file too.\n  found: {sorted(set(digests))}"
        f"\n  vetted: {BASE_DIGEST}"
    )
    assert len(digests) == 2, (
        f"expected both build stages to carry the digest, found {len(digests)}"
    )


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


# The interpreter version, in ONE place. It feeds the vetted pip-removal command, the guarded
# importable trees and the pip-removal assertion, and it is checked against the base image tag by
# `test_the_guarded_python_version_is_the_one_the_base_image_ships`. It used to be written out
# four times, so a base bump needed four coordinated edits for one fact.
# The suid sweep. Written out twice ON PURPOSE, here and as the last entry of _VETTED_RUNS: two
# independent copies mean a one-line edit to the Dockerfile's sweep cannot satisfy both, so a
# neutered sweep turns the file red. That redundancy is the control, not an oversight.
SUID_SWEEP = (
    "/usr/bin/find / -xdev -perm /6000 \\( -type f -o -type d \\) -exec /bin/chmod a-s {} +"
)
_PYTHON_VERSION = "3.12"
_PYTHON_ZIP_VERSION = _PYTHON_VERSION.replace(".", "")

# The RUN commands that are allowed to mention an executable directory, pinned by exact text
# the way the sweep is. Anything else naming one of those directories is an offence, so there is
# no verb list to be one entry short. Changing any of these means changing this literal, which
# is the point: a reviewer sees the diff.
_VETTED_RUNS = (
    "python -m venv /opt/venv",
    "pip install --require-hashes --no-deps -r requirements.txt",
    "apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*",
    f"rm -rf /opt/venv/lib/python{_PYTHON_VERSION}/site-packages/pip* /opt/venv/bin/pip* "
    f"/usr/local/lib/python{_PYTHON_VERSION}/site-packages/pip* "
    f"/usr/local/lib/python{_PYTHON_VERSION}/site-packages/setuptools* "
    f"/usr/local/lib/python{_PYTHON_VERSION}/ensurepip /usr/local/bin/pip /usr/local/bin/pip3 "
    f"/usr/local/bin/pip{_PYTHON_VERSION} && rm -rf /var/lib/apt /var/cache/apt /etc/apt "
    "/usr/bin/apt "
    "/usr/bin/apt-* /usr/bin/dpkg /usr/bin/dpkg-* /usr/sbin/dpkg-* /var/lib/dpkg/info "
    "&& useradd --uid 10001 --user-group --system --no-create-home "
    "--shell /usr/sbin/nologin appuser && chown -R 10001:10001 /app",
    # SPELLED OUT AGAIN, not `SUID_SWEEP`. Folding the two into one literal was an engineering
    # tidy-up and it cost a fabrication: with one copy, planting `-uid 4242` in the Dockerfile's
    # sweep (an always-false narrowing that clears nothing) and updating the single constant left
    # the file green, where the same two-edit mutation against two independent copies turns three
    # tests red. With no Docker daemon in the loop this text is the only verification of a hard
    # rule, so the file keeps one statement of the command that a single edit cannot move.
    "/usr/bin/find / -xdev -perm /6000 \\( -type f -o -type d \\) -exec /bin/chmod a-s {} +",
)
# The COPY instructions allowed to write over a guarded tree, pinned by exact text the way the
# vetted RUNs are. Every COPY in this Dockerfile is listed, which is the point rather than a
# weakness: the burden is inverted, so ADDING a COPY that touches a guarded tree means adding it
# here, which is a decision somebody makes and a reviewer sees in the diff. The flattening COPY
# and the venv install write into directories a shim could hide in; the other two write a single
# file and a source tree into /app, which became guarded once `--pythonpath /app/src` was
# recognised as putting it on `sys.path`.
#
# `test_every_vetted_instruction_is_one_the_dockerfile_actually_has` stops an entry here from
# being a standing exemption for an instruction added later.
_VETTED_COPIES = (
    "--from=prep / /",
    "--from=build /opt/venv /opt/venv",
    "src ./src",
    "requirements.txt ./",
)
# Every place a shim could be planted over a binary or a module the pinned commands rely on: each
# directory on the shipped PATH, and each importable tree on a venv interpreter's sys.path.
#
# Four defeat classes are recorded rather than described, because each cost a round. A COPY over
# /usr/bin/find neutered the sweep. `/opt/venv/bin/` was missing while being FIRST on the PATH,
# holding the gunicorn the CMD execs. `{base_prefix}/lib/pythonXY.zip` sits ahead of the standard
# library on sys.path, so a COPY over the zip is startup code execution. And `--pythonpath
# /app/src` puts the source tree on sys.path, so a COPY over the auth module replaces it. The
# measurements are in docs/SECURITY.md; `test_the_guarded_directories_cover_every_entry_on_the_
# shipped_path` derives the PATH half from the Dockerfile's own ENV so this list cannot drift.
_EXECUTABLE_DIRECTORIES = (
    "/bin/",
    "/sbin/",
    "/usr/bin/",
    "/usr/sbin/",
    "/usr/local/bin/",
    "/usr/local/sbin/",
    "/opt/venv/bin/",
    f"/usr/local/lib/python{_PYTHON_VERSION}/",
    f"/usr/local/lib/python{_PYTHON_VERSION}.zip",
    f"/usr/local/lib/python{_PYTHON_ZIP_VERSION}.zip",
    "/opt/venv/lib/",
    "/app/src/",
)


# Every `key=value` in an ENV argument, quotes stripped. A key is matched by EQUALITY against
# this, never by substring: `PYTHONPATH` ends in `PATH`, and that one fact hid an unguarded
# directory at the front of the shipped search path with the whole suite green.
# REFUSE, do not parse. Three rounds were spent chasing BuildKit's lexer with a regex: the key may
# be quoted, and the quotes are stripped later, so `ENV "PREE_ENV"=development` sets PREE_ENV; then
# a doubled-quote key beat the tolerant pattern; then a token yielding two assignments paid for a
# token yielding none, so an aggregate count agreed while a credential went unparsed. Every one of
# those is a lexer trick, and a hand-rolled recogniser of adversarial input loses to lexer tricks
# indefinitely.
#
# So this does not recognise what docker accepts. It refuses everything that is not a PLAIN
# assignment, one word at a time, and this Dockerfile is written in that form throughout. A word
# docker would honour and this cannot read is a failure, not a skip.
# The inner content is CAPTURED, not post-stripped. `.strip("\"'")` removes both quote characters
# repeatedly from both ends, so `PREE_ENV="'development'"` was read as `development` where docker
# sets `'development'`, and a value ending in a quoted path lost its closing quote. A refusal that
# reads a value docker does not set is the failure it exists to prevent, one layer in.
_PLAIN_ASSIGNMENT = re.compile(r"""([A-Za-z_][A-Za-z0-9_]*)=(?:"([^"]*)"|'([^']*)'|([^\s"']*))$""")


def _split_outside_quotes(argument: str) -> list[str]:
    """Words, splitting only at whitespace that is not inside a quoted run.

    One rule, exactly stated, rather than a recogniser of quoting tricks: a quote character toggles
    the current run, and whitespace outside a run ends a word. Anything the rule cannot make sense
    of is handed to the refusal below as one word and refused there.
    """
    words: list[str] = []
    current: list[str] = []
    quote = ""
    for character in argument:
        if quote:
            current.append(character)
            if character == quote:
                quote = ""
            continue
        if character in "\"'":
            quote = character
            current.append(character)
            continue
        if character.isspace():
            if current:
                words.append("".join(current))
                current = []
            continue
        current.append(character)
    if current:
        words.append("".join(current))
    # An unterminated quote is refused rather than guessed at.
    assert not quote, f"an ENV or ARG argument has an unclosed {quote} quote: {argument[:80]}"
    return words


def _env_assignments(argument: str) -> list[tuple[str, str]]:
    """Parse an ENV or ARG argument, refusing anything that is not a plain `KEY=value` word.

    The legacy space-separated form carries no `=` at all and sets the variable just as surely, so
    it is refused rather than parsed: a parser that silently returns nothing for a form docker
    honours reports a pass for a line nobody read.
    """
    # Split at whitespace that is not inside quotes, so a quoted value containing a space is one
    # word. `str.split()` broke `ENV PREE_LABEL="Pree scorer"` in two and refused a form docker
    # honours, and `shlex.split(posix=False)` splits inside a mid-token quote for the same reason.
    # A false refusal is a real cost: the next person who needs the form deletes the guard.
    words = _split_outside_quotes(argument)
    assert words, "an ENV or ARG instruction has no argument at all"
    # A bare `ARG NAME` DECLARES a build argument with no default, which is the multi-arch idiom
    # (`ARG TARGETARCH`) and bakes nothing. It carries no `=` and is not the legacy form, so it was
    # refused with the wrong diagnosis.
    if len(words) == 1 and "=" not in words[0]:
        return []
    # The FIRST WORD decides the form, which is what docker inspects. Testing the whole argument
    # let `ENV PATH /opt/tools/exec=1:...` pass as an assignment while docker took the legacy form.
    assert "=" in words[0], (
        f"an ENV instruction uses the legacy space-separated form, which this parser does not read "
        f"and docker honours: {argument[:80]}"
    )
    parsed: list[tuple[str, str]] = []
    for word in words:
        found = _PLAIN_ASSIGNMENT.fullmatch(word)
        assert found is not None, (
            f"{word[:60]!r} is not a plain KEY=value assignment. Quoting, splicing and doubling a "
            f"key all change what docker sets while leaving a permissive parser agreeing with "
            f"itself, so anything but the plain form is refused: {argument[:80]}"
        )
        double, single, bare = found.group(2), found.group(3), found.group(4)
        value = double if double is not None else single if single is not None else (bare or "")
        parsed.append((found.group(1), value))
    return parsed


def test_the_guarded_python_version_is_the_one_the_base_image_ships() -> None:
    """The interpreter version is pinned in three places; this makes them one fact.

    `_PYTHON_VERSION` feeds the guarded importable tree, and the pip-removal assertion reads the
    same value. Moving the base image to 3.13 without updating it left the guard pointing at a
    directory that no longer exists, silently, because site-packages is not on the PATH the
    derivation test above reads.
    """
    bases = [i.argument.split()[0] for i in _instructions() if i.keyword == "FROM"]
    tagged = [base for base in bases if base.startswith("python:")]
    assert tagged, f"no stage builds on a python base image: {bases}"
    for base in tagged:
        version = base.removeprefix("python:").split("-")[0]
        assert version == _PYTHON_VERSION, (
            f"the base image is {base}, so the interpreter is {version} and the guarded "
            f"importable tree names {_PYTHON_VERSION}. A guard pointing at a directory the image "
            "does not have guards nothing"
        )


def test_the_guarded_directories_cover_every_entry_on_the_shipped_path() -> None:
    """Derived from the Dockerfile's own ENV PATH, so the guard list cannot drift from the image.

    A directory on the PATH and absent from the guard is a place a shim can be planted over a
    binary the pinned commands name. That is how `/opt/venv/bin/gunicorn` was open: the guard
    listed the system directories and the venv was first on the PATH.
    """
    # By KEY EQUALITY, over every assignment in every ENV instruction of the final stage. The
    # first version took the first ENV containing the SUBSTRING "PATH=" and read the value out
    # with `re.search(r'PATH="([^"]+)"')`, which matches inside any name ending in PATH. Two
    # lines beat it: `ENV PYTHONPATH="/opt/venv/bin:/usr/bin" PATH="/opt/tools/exec:/usr/bin"`
    # made this test read PYTHONPATH's value, find every entry guarded, and pass, while the
    # effective PATH began with an unguarded directory holding a planted `gunicorn` that the
    # shipped CMD resolves. A second `PATH=` appended to the same ENV instruction also passed,
    # because the other guard counts ENV instructions rather than assignments.
    assignments = [
        assignment
        for instruction in _final_stage()
        if instruction.keyword == "ENV"
        for assignment in _env_assignments(instruction.argument)
    ]
    paths = [value for key, value in assignments if key == "PATH"]
    assert len(paths) == 1, (
        f"the final stage assigns PATH {len(paths)} times; docker takes the last and this guard "
        f"cannot tell which one an operator meant: {assignments}"
    )
    guarded = {directory.rstrip("/") for directory in _EXECUTABLE_DIRECTORIES}
    unguarded = [entry for entry in paths[0].split(":") if entry.rstrip("/") not in guarded]
    assert not unguarded, (
        f"these directories are on the shipped PATH and not guarded, so a shim planted in one "
        f"replaces a binary the pinned commands name: {unguarded}"
    )


def test_every_vetted_instruction_is_one_the_dockerfile_actually_has() -> None:
    """An exemption for an instruction that is not there is an exemption waiting for one.

    Both vetted tuples invert the burden: naming an instruction exempts it, so a reviewer sees
    the diff. That only holds if an entry cannot be added ahead of the instruction it excuses.
    Each vetted string must match exactly one instruction in the file today.
    """
    for keyword, vetted in (("RUN", _VETTED_RUNS), ("COPY", _VETTED_COPIES)):
        present = [
            " ".join(instruction.argument.split())
            for instruction in _instructions()
            if instruction.keyword == keyword
        ]
        for text in vetted:
            collapsed = " ".join(text.split())
            count = present.count(collapsed)
            assert count == 1, (
                f"the vetted {keyword} {collapsed[:60]!r} matches {count} instructions in the "
                f"Dockerfile; a vetted entry with no instruction is a standing exemption for "
                f"whatever is added next. Present: {present}"
            )


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


def _binary_write_offences(instructions: list[_Instruction]) -> list[str]:
    """Every instruction that could replace a binary the hardening steps name.

    A CALLABLE, taking the instructions as an argument, because the test that used to hold this
    logic could only ever run it against the shipped Dockerfile. The reviewer deleted the
    opaque-path refusal below and all 292 tests stayed green: the test written to prove that
    refusal works asserted only that its own four string constants contained a refused
    character, which is a tautology over literals. A helper can be fed synthetic input, so the
    refusal is now exercised directly by `test_the_write_guard_refuses_a_path_built_opaquely`.

    Returns the offences it finds; RAISES for the categorical refusals (ADD, a variable in a
    COPY destination, an opaque character in a non-vetted RUN), because those are refusals of a
    construction rather than findings about a path.
    """
    offenders: list[str] = []
    for instruction in instructions:
        if instruction.keyword not in {"COPY", "ADD"}:
            continue
        # Resolved against the WORKDIR in force, and stripped of quoting and JSON punctuation.
        # Testing the raw last token missed two forms: `WORKDIR /usr/bin` then
        # `COPY --from=build /bin/true find` gives the token "find", and the JSON form
        # `COPY --from=build ["/bin/true", "/usr/bin/find"]` gives `"/usr/bin/find"]`. Both
        # replaced /usr/bin/find with a no-op and left the whole suite green.
        # ADD is refused outright, not inspected. docker detects an archive by CONTENT, so a tar
        # named `hardening.txt` extracts over whatever it likes and no reading of this file can
        # tell: the extension-matching version of this check caught `.tar` and missed exactly
        # that. This project uses COPY everywhere and has no legitimate ADD, so the whole
        # instruction goes rather than a list of extensions that will always be one short.
        assert instruction.keyword != "ADD", (
            f"ADD extracts a local archive over its destination and fetches remote URLs, and "
            f"docker detects the archive by content rather than by name, so what it writes "
            f"cannot be read from this file. Use COPY: {instruction.argument[:80]}"
        )
        raw = instruction.argument.split()[-1].strip("[]\"',")
        # A variable ANYWHERE in the resolved path, destination or WORKDIR. Checking only the
        # last token meant `ARG D=/usr/bin` with `WORKDIR $D` and a relative destination reached
        # the same place unseen, and chasing every substitution form is the losing game this
        # project keeps replaying.
        assert "$" not in raw and "$" not in instruction.workdir, (
            f"a COPY destination is built from a variable, so what it writes cannot be read "
            f"from this file: {instruction.keyword} {instruction.argument[:60]} "
            f"(workdir {instruction.workdir!r})"
        )
        joined = raw if raw.startswith("/") else f"{instruction.workdir.rstrip('/')}/{raw}"
        # NORMALISED. `//usr/bin/find`, `/usr//bin/find` and `WORKDIR /usr/./bin` with a
        # relative destination all name exactly the same file as `/usr/bin/find`, and all three
        # walked past a prefix test on the raw string. One redundant character was enough.
        # normpath alone is not enough: POSIX makes a LEADING double slash
        # implementation-defined, so posixpath.normpath("//usr/bin/find") returns it unchanged
        # and the prefix test still missed it. Collapse every run of slashes first.
        target = posixpath.normpath(re.sub(r"/{2,}", "/", joined))
        # The directory ITSELF, not only paths under it. normpath strips the trailing slash, so
        # `/usr/bin`, `/usr/bin/` and `/usr/bin/.` all normalise to `/usr/bin` and none of them
        # started with `/usr/bin/`: `COPY --from=build /tmp/find /usr/bin` writes
        # /usr/bin/find and passed. Two lines was enough to neuter the sweep.
        # The directory itself, anything under it, and anything ABOVE it. `COPY tree /usr` writes
        # /usr/bin/* and `COPY tree /opt/venv` writes /opt/venv/bin/*, and neither destination
        # was flagged: the same asymmetry this guard already fixed one level down, where
        # `/usr/bin` as a destination did not start with `/usr/bin/`. The two shipped COPYs that
        # legitimately write over a guarded tree are pinned by exact text, the way the vetted
        # RUNs are, so writing into one is a deliberate diff rather than a pattern to argue with.
        collapsed_copy = " ".join(instruction.argument.split())
        if collapsed_copy in _VETTED_COPIES:
            continue
        if any(
            target == directory.rstrip("/")
            or target.startswith(directory)
            or f"{directory.rstrip('/')}/".startswith(f"{target.rstrip('/')}/")
            for directory in _EXECUTABLE_DIRECTORIES
        ):
            offenders.append(f"{instruction.keyword} {instruction.argument[:80]} -> {target}")
    # And a RUN that writes into one of those directories. The guard only ever considered COPY
    # and ADD, so `RUN cp /bin/true /usr/bin/find` before the sweep was invisible: the simplest
    # form of the attack, and the one nobody had tried.
    # NO verb denylist. There was one, of six verbs, and three one-line mutations walked through
    # it: `RUN /bin/cat /bin/true > /usr/bin/find` needs no verb at all, `RUN tar -xf … -C
    # /usr/bin/` uses a verb that was not listed, and `RUN python -c "open('/usr/bin/find','w')…"`
    # names the target literally. Enumerating the ways a shell can write a file is the same
    # losing game as enumerating the ways a name can look like a credential.
    #
    # So ANY mention of an executable directory in a RUN is an offence, and the legitimate ones
    # are pinned by exact text rather than excused by a pattern. That inverts the burden: adding
    # a RUN that touches /usr/bin means adding it to _VETTED_RUNS deliberately, which is a
    # decision someone has to make and a reviewer can see.
    vetted = {" ".join(text.split()) for text in _VETTED_RUNS}
    for instruction in instructions:
        if instruction.keyword != "RUN":
            continue
        collapsed = " ".join(instruction.argument.split())
        if collapsed in vetted:
            continue
        # A non-vetted RUN may not build a path from anything this file cannot read. Every
        # spelling below reached /usr/bin/find in a round of its own: a `?` glob, a `*` glob, a
        # shell variable, a command substitution, a character class and a brace expansion. A
        # vetted RUN never reaches this loop, so refusing these costs nothing.
        for opaque in ("$", "`", "?", "*", "[", "]", "{", "}"):
            assert opaque not in collapsed, (
                f"a RUN builds a path from a substitution or a glob, so what it writes cannot be "
                f"read from this file ({opaque!r}): {collapsed[:80]}"
            )
        # Both spellings of each directory, and the WORKDIR in force, because `-C /usr/bin`
        # without the trailing slash and a `cd`-relative destination both matched nothing.
        haystack = f"{collapsed} {instruction.workdir}"
        for directory in _EXECUTABLE_DIRECTORIES:
            bare = directory.rstrip("/")
            if directory in haystack or f" {bare}" in f" {haystack}" or haystack.endswith(bare):
                offenders.append(f"RUN touches {bare}: {collapsed[:80]}")
                break

    return offenders


def test_nothing_writes_over_a_binary_the_hardening_steps_depend_on() -> None:
    """Pinning a command's text is worthless if its binaries can be replaced.

    `COPY --from=build /bin/true /usr/bin/find` above the sweep is one line, changes not a
    character of the pinned command, and leaves every other assertion green while the sweep
    clears nothing. Absolute paths in the sweep close the PATH route; this closes the other one.
    """
    offenders = _binary_write_offences(_instructions())
    assert not offenders, (
        f"an instruction writes into a system executable directory, so the binaries the "
        f"hardening steps name may not be the binaries that run: {offenders}"
    )


def test_the_write_guard_refuses_a_path_built_opaquely() -> None:
    """The GUARD is invoked here, on synthetic input, not asserted about.

    The test this replaces looped over four fabrications and asserted that each contained one of
    the refused characters: a statement about its own string constants, true whatever the guard
    does. Deleting the refusal loop outright left all 292 tests green, which is the same defect
    the fabrications were written to catch, one level up.
    """
    for fabrication in (
        "RUN cp /etc/hostname /usr/b?n/find",
        "RUN cp /usr/b*n/true /usr/b*n/find",
        "RUN D=/usr/b; cp /etc/hostname ${D}in/find",
        "RUN cp /etc/hostname `echo /usr/bin/find`",
        "RUN cp /etc/hostname /usr/[b]in/find",
        "RUN cp /etc/hostname /usr/{bin,sbin}/find",
        "RUN cp /etc/hostname \"$(printf '/usr/%s/find' bin)\"",
    ):
        with pytest.raises(AssertionError, match="builds a path from a substitution or a glob"):
            _binary_write_offences(_instructions(f"FROM scratch AS prep\n{fabrication}\n"))

    # A LITERAL write is a finding rather than a refusal, so it comes back in the list.
    for literal in (
        "RUN cp /bin/true /usr/bin/find",
        "RUN /bin/cat /bin/true > /usr/bin/find",
        "RUN tar -xf payload.txt -C /usr/bin/",
    ):
        found = _binary_write_offences(_instructions(f"FROM scratch AS prep\n{literal}\n"))
        assert found, f"a literal write into an executable directory was not seen: {literal}"

    # COPY and ADD, the two branches above the RUN branch.
    copy = _binary_write_offences(
        _instructions("FROM scratch AS prep\nCOPY --from=build /bin/true /usr/bin/find\n")
    )
    assert copy, "a COPY over /usr/bin/find was not seen"
    with pytest.raises(AssertionError, match="extracts a local archive"):
        _binary_write_offences(_instructions("FROM scratch AS prep\nADD hardening.txt /usr/bin/\n"))
    with pytest.raises(AssertionError, match="destination is built from a variable"):
        _binary_write_offences(
            _instructions("FROM scratch AS prep\nCOPY --from=build /bin/true ${D}/find\n")
        )

    # And a vetted RUN passes, which is what stops the refusal from being unusable.
    assert not _binary_write_offences(_instructions(f"FROM scratch AS prep\nRUN {SUID_SWEEP}\n")), (
        "the shipped suid sweep is refused by its own guard"
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
    assert f"{target}/lib/python{_PYTHON_VERSION}/site-packages/pip" in removal.argument, (
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


# Abbreviations that end in a period without ending a sentence. Splitting on every period plus
# whitespace cut "e.g. 16 random characters" in two, so the half carrying the number carried no
# token term and a wrong floor passed.
_ABBREVIATIONS = ("e.g.", "i.e.", "cf.", "etc.", "min.", "max.", "vs.", "approx.", "no.")


def _sentences(text: str) -> list[str]:
    """Split prose into sentences, without breaking at a known abbreviation."""
    shielded = text
    for index, abbreviation in enumerate(_ABBREVIATIONS):
        shielded = shielded.replace(abbreviation, f"\x00{index}\x00")
    restored = []
    for part in re.split(r"(?<=[.!?])\s+", shielded):
        unshielded = part
        for index, abbreviation in enumerate(_ABBREVIATIONS):
            unshielded = unshielded.replace(f"\x00{index}\x00", abbreviation)
        restored.append(unshielded)
    return restored


def _introduces_a_table(sentences: list[str]) -> str | None:
    """The last sentence, but only if it INTRODUCES what follows, i.e. it ends in a colon.

    A syntactic rule rather than a semantic one, and it is what makes the prose-to-row pairing
    usable. Pairing every trailing sentence with the next row flagged a true statement
    immediately: "…nothing else answers without the token." beside a row reading "200 or 503, see
    below" has the token, a size word and two numbers, and means nothing about a floor. A
    sentence that hands off to a table ends in a colon; one that finishes a thought does not.
    """
    if not sentences:
        return None
    last = sentences[-1].strip()
    return last if last.endswith(":") else None


# A colon sentence pairs with this many following units before it is dropped. Three covers a
# heading and its table row, or a bullet list's first three items, and stops short of running the
# sentence together with the next section. A wrong floor stated in the FOURTH unit after its
# colon sentence passes, which is a residual recorded in the token-floor test's docstring.
_PAIRING_REACH = 3


def _paired(pending: str | None, sentences: list[str]) -> list[str]:
    """The sentences themselves, plus each one joined to the colon sentence introducing them."""
    if not pending:
        return list(sentences)
    return list(sentences) + [f"{pending} {sentence}" for sentence in sentences]


def _decayed(pending: str | None, reach: int) -> tuple[str | None, int]:
    """The colon sentence one unit shorter, dropped once its reach runs out."""
    if pending is None:
        return None, 0
    remaining = reach - 1
    return (pending, remaining) if remaining > 0 else (None, 0)


def _next_pending(sentences: list[str], pending: str | None, reach: int) -> tuple[str | None, int]:
    """The colon sentence in force after this flush, and how much further it reaches.

    A new colon sentence replaces the old one. Otherwise the old one survives, one unit shorter,
    which is what lets it cross a `###` heading or a bullet to reach the row or the item that
    carries the number.
    """
    introduces = _introduces_a_table(sentences)
    if introduces is not None:
        return introduces, _PAIRING_REACH
    return _decayed(pending, reach)


def _claim_units(path: Path) -> list[str]:
    """SENTENCES, not line windows, plus each table row whole.

    Line windows lost twice. One line at a time missed a floor stated across two wrapped lines.
    Pairs of adjacent lines fixed that and missed a floor stated across THREE: the first pair
    carried the subject and the size word with no number, the second carried the size word and
    the number with no subject, and neither pair had all three. Widening to three lines would
    lose to four. A sentence is the unit a claim is actually written in, so the prose is joined
    and split on terminators instead, and the window disappears.

    Table rows are kept whole and separate, because a markdown row has no sentence terminator
    and joining rows into flowing prose would run the whole table together.
    """
    lines = [
        re.sub(r"\s+", " ", raw).strip() for raw in path.read_text(encoding="utf-8").splitlines()
    ]
    units: list[str] = []
    prose: list[str] = []
    # INITIALISED here, not at first assignment inside the loop. `pending` was read at the table
    # branch below and only ever written further down, so any document whose first non-blank
    # line was a table row raised UnboundLocalError: a crash in the guard rather than a silent
    # pass, but neither ruff nor mypy saw it and a crashing guard proves nothing.
    pending: str | None = None
    # How many further units a colon sentence keeps pairing with. A sentence ending in a colon
    # introduces a BLOCK, and the block is not always a table: the house style bullets with `●`,
    # so "The floor for the team token is:" / blank / "● 16 characters, minimum." was the most
    # likely shape of a wrong floor and the pairing did not survive to reach it. Nor did the
    # heading-then-table shape, nor a fenced code block. Pairing with the next few units instead
    # of the next row alone covers all four. Bounded, because pairing indefinitely runs a colon
    # sentence together with the next section and starts flagging true statements.
    reach = 0
    for line in lines:
        if line.startswith("|"):
            if prose:
                sentences = _sentences(" ".join(prose))
                units.extend(_paired(pending, sentences))
                # The LAST prose sentence pairs with this row. "The floor is the value in this
                # table:" followed by `| minimum | 16 characters |` split the subject from the
                # number, because prose was flushed at the first row and rows paired only with
                # rows.
                pending, reach = _next_pending(sentences, pending, reach)
                prose = []
            units.append(line)
            # Separator rows carry no claim, so pairing with one says nothing and the pending
            # sentence must survive to reach the row that does.
            if pending and not re.fullmatch(r"\|[\s|:-]+\|", line):
                units.append(f"{pending} {line}")
                # The SAME decay as the prose branch, through the same helper. It was written
                # out twice, so a change to the reach rule had to be made in two places.
                pending, reach = _decayed(pending, reach)
            continue
        # Blank lines do NOT reset the pairing. Markdown puts one between a paragraph and the
        # table it introduces, so resetting there meant "the floor is the value in this table:"
        # never reached the row carrying the number. Only real prose ends the pairing, and only
        # once the colon sentence has reached the units it introduces.
        if not line:
            if prose:
                sentences = _sentences(" ".join(prose))
                units.extend(_paired(pending, sentences))
                pending, reach = _next_pending(sentences, pending, reach)
                prose = []
            continue
        prose.append(re.sub(r"^\d+\.\s+", "", line.strip(" ●■")))
    if prose:
        sentences = _sentences(" ".join(prose))
        units.extend(_paired(pending, sentences))
    # Adjacent table rows too, for a floor split across a header row and its value row.
    units += [
        f"{first} {second}"
        for first, second in itertools.pairwise(lines)
        if first.startswith("|") and second.startswith("|")
    ]
    # NO sentence lookback, and it was tried. A floor can be split so the sentence naming the
    # token carries no number and the next sentence carries the number without naming the token
    # ("…how long the shared team token must be. The rule is that it must be at least 16 in
    # size."), and sentence units localise a claim correctly enough that neither half trips the
    # rule. Pairing adjacent sentences catches that and joined unrelated ones: three true
    # statements in this repository flagged immediately, among them "a verdict is cached for two
    # seconds". A guard that cries wolf gets relaxed rather than obeyed, so the residual is
    # recorded in the test's docstring instead. That is the third documented boundary on this
    # guard, and widening it a seventh time has now cost more than it bought twice running.
    return [unit for unit in units if unit.strip()]


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
    """INVERTED, after nineteen attempts at matching forms.

    Every earlier version asked "does this look like a floor?" and lost, because the answer needs
    complete tables of number words, size words and token synonyms plus a notion of adjacency, and
    each round found the missing entry. The rule is the other way round now: in any unit that
    mentions the token AND a size, the enforced constant must appear and no other number may. That
    buys a NUMBER side needing no table, which is where five consecutive defeats came from. The
    round-by-round history is in docs/SECURITY.md.

    FOUR residuals, recorded rather than implied away.

    1. The pronoun split. A floor across two sentences, one naming the token without a number and
       one carrying the number without the token, passes. Pairing adjacent sentences catches it and
       flags unrelated prose, so it is not done.
    2. The pairing reach. A colon sentence pairs with the next three units, so a wrong floor in the
       fourth bullet or row after it passes. Measured, and asserted in the splitter's own test.
    3. The size-word table. `size.search` gates the whole check, so "16 random units" states a
       floor with no size word in it and passes. An earlier docstring claimed this rule needed no
       table; it needs this one.
    4. The token-synonym table. "the shared access key as 16 random characters" passed until "key"
       was added, after "secret" and "passphrase" had been added for the same reason.

    The consequence of 3 or 4 is a misled operator and a fail-closed boot refusal, not a weak token
    in production. The cost of the inversion is real too: a legitimate sentence pairing the token
    with any other figure fails, and two in this repository did.
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


def _control_rows() -> list[tuple[str, str, str]]:
    """Every control-table row as (control, where, verified-by), parsed once.

    Both control-table tests parsed the separator and split the cells themselves, so a change to
    the table's shape had to be made in two places.
    """
    lines = _controls_section()
    separator = next(
        i for i, line in enumerate(lines) if set(line.strip()) <= set("|- ") and "|" in line
    )
    rows: list[tuple[str, str, str]] = []
    malformed: list[str] = []
    for line in lines[separator + 1 :]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) != 3:
            malformed.append(stripped[:80])
            continue
        rows.append((cells[0], cells[1], cells[2]))
    assert not malformed, f"control-table rows that do not have three cells: {malformed}"
    assert len(rows) > 20, f"only {len(rows)} rows parsed; the parser has drifted"
    return rows


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
    # The stray-row guard stays with this test, because a pipe-leading line above the separator
    # renders as literal text and still reads as a control claim.
    lines = _controls_section()
    separator = next(
        i for i, line in enumerate(lines) if set(line.strip()) <= set("|- ") and "|" in line
    )
    stray = [line.strip()[:80] for line in lines[: separator - 1] if line.strip().startswith("|")]
    assert not stray, f"lines that look like control rows before the table header: {stray}"

    defined = _defined_test_names()
    rows = [(control, evidence) for control, _where, evidence in _control_rows()]

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
    missing = [
        f"{control} -> {token}"
        for control, where, _evidence in _control_rows()
        for token in re.findall(r"`([^`]+)`", where)
        if not (REPO_ROOT / token).exists()
    ]
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
