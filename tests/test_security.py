"""Every security property has its own test. A control without one is treated as failed."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.conftest import TEST_TOKEN, make_config

from pree.security import MAX_ACTOR_LENGTH, AuthError, authorise, sanitise_actor, token_matches


def test_token_compare_accepts_the_exact_token_only() -> None:
    assert token_matches(TEST_TOKEN, TEST_TOKEN) is True
    assert token_matches(TEST_TOKEN + "x", TEST_TOKEN) is False
    assert token_matches(TEST_TOKEN[:-1], TEST_TOKEN) is False
    assert token_matches("", TEST_TOKEN) is False
    assert token_matches(None, TEST_TOKEN) is False


def test_token_compare_uses_a_constant_time_primitive() -> None:
    """Guard the implementation, not just the result: a naive == would pass the test above.

    A byte-by-byte comparison leaks how much of the token was correct through its timing, so
    the source must call hmac.compare_digest rather than an equality operator.
    """
    source = Path("src/pree/security.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest" in source
    assert "presented == expected" not in source


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
