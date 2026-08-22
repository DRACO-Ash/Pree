"""Every security property has its own test. A control without one is treated as failed."""

from __future__ import annotations

import ast
import hmac
from pathlib import Path

import pytest

from pree.app import MAX_LOGGED_PATH, STORE_KEY_MAX_LENGTH
from pree.security import (
    _PERMITTED_PATH_BYTES,
    MAX_ACTOR_LENGTH,
    UNPRINTABLE_MARKER,
    AuthError,
    authorise,
    sanitise_actor,
    sanitise_log_path,
    token_matches,
)
from tests.conftest import TEST_TOKEN, make_config

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_token_compare_accepts_the_exact_token_only() -> None:
    assert token_matches(TEST_TOKEN, TEST_TOKEN) is True
    assert token_matches(TEST_TOKEN + "x", TEST_TOKEN) is False
    assert token_matches(TEST_TOKEN[:-1], TEST_TOKEN) is False
    assert token_matches("", TEST_TOKEN) is False
    assert token_matches(None, TEST_TOKEN) is False


def test_token_compare_invokes_the_constant_time_primitive() -> None:
    """Assert the CALL, not the text.

    The previous version grepped the source for "hmac.compare_digest" and for the absence of
    "presented == expected". Both were satisfied by a function whose body had been replaced with
    `return expected == presented` and whose `import hmac` had been deleted, because the phrase
    still appeared in the docstring explaining why the primitive is used, and the denylist
    string never matched the reversed spelling. The whole loop stayed green with the project's
    own hard rule broken, which makes this the control most worth pinning properly.
    """
    source = (REPO_ROOT / "src" / "pree" / "security.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "token_matches"
    )
    calls = [
        node.func.attr
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "compare_digest" in calls, (
        f"token_matches does not call a constant-time primitive; it calls {calls}"
    )
    equality = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Compare)
        and any(isinstance(op, ast.Eq | ast.NotEq) for op in node.ops)
    ]
    assert not equality, (
        "token_matches compares with == or !=, which short-circuits on the first differing "
        "byte and leaks how much of the token was correct"
    )


def test_the_constant_time_primitive_is_actually_reached_at_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spy, so the assertion survives any refactor the AST check cannot see through."""
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return real(left, right)

    monkeypatch.setattr("pree.security.hmac.compare_digest", spy)
    assert token_matches(TEST_TOKEN, TEST_TOKEN) is True
    assert calls, "token_matches returned without reaching the constant-time primitive"


def test_auth_gate_is_open_when_no_token_is_configured(tmp_path: Path) -> None:
    """Single-user local mode: no token configured means the gate is open by design."""
    authorise(make_config(tmp_path), None)


def test_auth_gate_rejects_a_wrong_or_missing_token(tmp_path: Path) -> None:
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)
    authorise(config, TEST_TOKEN)
    for presented in (None, "", "wrong"):
        with pytest.raises(AuthError):
            authorise(config, presented)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "anonymous"),
        ("", "anonymous"),
        ("   ", "anonymous"),
        ("!!!", "anonymous"),
        ("ops.lead@example.test", "ops.lead@example.test"),
        ("watch-floor 2", "watch-floor 2"),
    ],
)
def test_actor_sanitisation_normalises_and_falls_back(raw: str | None, expected: str) -> None:
    assert sanitise_actor(raw) == expected


def test_actor_sanitisation_strips_log_forging_characters() -> None:
    forged = 'ops\n{"kind":"audit","actor":"admin"}'
    cleaned = sanitise_actor(forged)
    assert "\n" not in cleaned
    assert "{" not in cleaned
    assert '"' not in cleaned


def test_actor_sanitisation_caps_the_length() -> None:
    assert len(sanitise_actor("a" * 500)) == MAX_ACTOR_LENGTH


def test_the_path_scrub_is_injective_over_every_single_byte() -> None:
    """GENERATED, not hand-picked, because the hand-picked list is what let the last one through.

    The previous version listed the characters I happened to think of and claimed in its docstring
    that "every refused ASCII character in the charset appears at least once". That was false: it
    omitted eighteen of them and every control character, and one of the omissions was the defect.
    Space was the single whitespace character the charset permitted, so it survived the escape and
    was then removed by a `.strip()` two functions away, and `GET /v1/assessments/a:b%20` was
    audited byte-identically to `GET /v1/assessments/a:b`. A generated probe finds that in one line.

    Every byte, in all three positions, because the defect was positional: `.strip()` removed a
    leading or trailing space and left an embedded one alone, so an embedded-only probe would have
    passed.
    """
    generated = [b"/v1/assessments/a:b"]
    for byte in range(256):
        char = bytes([byte])
        generated.append(char + b"/v1/assessments/a:b")
        generated.append(b"/v1/assessments/a:b" + char)
        generated.append(b"/v1/assessments/a:" + char + b"b")
    # DEDUPED by input, because injectivity is a claim about distinct inputs and this generator
    # produces some target twice: appending `b` and embedding `b` both give `.../a:bb`. Comparing
    # without deduping reports that as a collision, which would be the test lying in the safe
    # direction and would still have to be explained away by the next reader.
    probes = sorted(set(generated))
    scrubbed = [sanitise_log_path(probe, 160) for probe in probes]
    collisions = sorted({out for out in scrubbed if scrubbed.count(out) > 1})
    assert not collisions, (
        f"distinct request targets produced one audit record, so a caller can name a route they "
        f"never requested: {collisions}"
    )
    for probe, out in zip(probes, scrubbed, strict=True):
        assert out.isascii() and out.isprintable(), f"{probe!r} scrubbed to {out!r}"
        assert len(out) <= 160


def test_no_permitted_path_byte_is_whitespace_so_no_trim_can_delete_one() -> None:
    """The INVARIANT behind the last collision, rather than the absence of the code that used it.

    A `.strip()` two functions away from the escape removed a trailing space that the escape had
    permitted through, and `GET /v1/assessments/a:b%20` was audited byte-identically to
    `GET /v1/assessments/a:b`. Deleting the strip is not what closed it: with space escaped to
    `%20` the strip has nothing to find, so reintroducing one is a behaviour-preserving no-op and
    no test can see it.

    What is load-bearing is that NO permitted byte is whitespace, and this asserts that. Permit
    space again and this turns red for the reason the collision existed, whether or not a trim is
    anywhere in the file. A test named for the absent code rather than the invariant would have
    claimed a property it could not check, which is the defect class this range keeps finding.
    """
    whitespace = sorted(byte for byte in _PERMITTED_PATH_BYTES if chr(byte).isspace())
    assert not whitespace, (
        f"a permitted path byte is whitespace, so any trim downstream of the escape deletes it and "
        f"two distinct targets share one record: {whitespace}"
    )
    assert sanitise_log_path(b"/v1/,assess", 160) == "/v1/%2Cassess"
    assert sanitise_log_path(b"/v1/assess ", 160) == "/v1/assess%20"
    assert sanitise_log_path(b" /v1/assess", 160) == "%20/v1/assess"
    assert sanitise_log_path(b"\t/v1/assess\n", 160) == "%09/v1/assess%0A"
    # And the escape introducer cannot pass through, or the mapping is ambiguous again.
    assert sanitise_log_path(b"/v1/%assess", 160) == "/v1/%25assess"
    assert sanitise_log_path(b"/v1/%25assess", 160) == "/v1/%2525assess"


def test_the_path_scrub_takes_the_raw_target_so_decoding_cannot_alias() -> None:
    """Decoding is lossy, and no scrub downstream of it can undo that.

    Three collisions that survive any charset applied to the DECODED path, which is why the field
    takes the wire bytes instead: `urllib.parse.unquote` leaves an invalid escape intact, so
    `/v1/%assess` and `/v1/%25assess` decode identically; and `/v1/assessments/a%2Fb:c` decodes to
    `/v1/assessments/a/b:c`, which reads as a route of a different shape. A comment in this project
    once claimed a literal `%` "can only have arrived as `%25`", and that was false.
    """
    for first, second in (
        (b"/v1/%assess", b"/v1/%25assess"),
        (b"/v1/assessments/a%2Fb:c", b"/v1/assessments/a/b:c"),
        (b"/v1/assessments/a:b%20", b"/v1/assessments/a:b"),
    ):
        assert sanitise_log_path(first, 160) != sanitise_log_path(second, 160), (
            f"{first!r} and {second!r} share one audit record"
        )


def test_the_path_scrub_is_not_claimed_injective_above_its_cap() -> None:
    """The honest limit, asserted so it is not rediscovered as a surprise.

    Truncation cannot be injective. Two targets agreeing on their first `limit` characters after
    escaping still produce one record, and the docstring says so rather than implying a property
    the function does not have.

    BOTH units, because the 3:1 expansion is the part that was stated wrongly. This test used to
    probe with permitted bytes only, which truncate 1:1, so the docstring could be reverted to
    "a target shorter than the cap" and nothing turned red. An all-escaping target truncates at 54
    RAW bytes, not 160.
    """
    first = b"/v1/" + b"a" * 200 + b"one"
    second = b"/v1/" + b"a" * 200 + b"two"
    assert sanitise_log_path(first, 160) == sanitise_log_path(second, 160)
    # 54 raw bytes, all escaping: 53 gives 159 characters and 54 gives 162, so this pair is the
    # first collision an all-escaping target can produce and it is nowhere near the cap.
    escaping = b"/" + b"\xff" * 53
    assert sanitise_log_path(escaping + b"\x01", 160) == sanitise_log_path(escaping + b"\x02", 160)
    assert len(sanitise_log_path(b"\xff" * 53, 160)) == 159
    assert len(sanitise_log_path(b"\xff" * 54, 160)) == 160


def test_no_truncated_path_record_can_read_as_a_route_this_app_serves() -> None:
    """Why the truncation aliasing is a diagnosis cost and not a forgery.

    A truncated record is exactly `limit` characters. The longest path this application serves is
    `/v1/assessments/` plus a 129-character store key, which is 145, so no truncated record can
    equal a real route's length and the collisions above cannot be aimed at one. That reasoning
    holds only while the cap exceeds the longest legitimate path, which is a relation between two
    constants that nothing asserted: raising MAX_LOGGED_PATH is a change to the code, but LOWERING
    the gap until they meet is the one that turns a diagnosis cost into a forgery.
    """
    longest_legitimate = len("/v1/assessments/") + STORE_KEY_MAX_LENGTH
    assert longest_legitimate < MAX_LOGGED_PATH, (
        f"the audit cap ({MAX_LOGGED_PATH}) is not above the longest legitimate path "
        f"({longest_legitimate}), so a truncated record can be made to read as a real route"
    )
    truncated = sanitise_log_path(b"/" + b"\xff" * 200, MAX_LOGGED_PATH)
    assert len(truncated) == MAX_LOGGED_PATH != longest_legitimate


def test_the_path_scrub_marks_an_empty_target_rather_than_logging_nothing() -> None:
    """An empty `path` field reads as an absent one, which is a different fact."""
    assert sanitise_log_path(b"", 160) == UNPRINTABLE_MARKER
