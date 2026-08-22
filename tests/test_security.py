"""Every security property has its own test. A control without one is treated as failed."""

from __future__ import annotations

import ast
import hmac
from pathlib import Path

import pytest

from pree.security import (
    MAX_ACTOR_LENGTH,
    AuthError,
    authorise,
    sanitise_actor,
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
