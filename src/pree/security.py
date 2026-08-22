"""Authentication and log sanitisation.

The shared team token is the only security boundary. It is compared in constant time so a
timing signal cannot leak it, and it never reaches a log line, an audit record, a health
response, or a client error body.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable

from .config import Config

MAX_ACTOR_LENGTH = 64
# What a log field carries when its whole value scrubs away. A caller cannot forge it, because the
# brackets are characters the scrub itself strips, and it is deliberately NOT "anonymous": that is
# the sentinel for "no actor supplied", and reusing it made a rejected field name of `{"*": 1}`
# indistinguishable from an anonymous caller in the field that exists for diagnosis.
UNPRINTABLE_MARKER = "[unprintable]"
# The ACTOR label stays Unicode-aware. `\w` in a str pattern keeps every letter in every script,
# which is deliberate: an operator's name may legitimately be non-Latin, and forcing ASCII here
# would mangle it into initials. The cost is bounded, because the actor is one field capped at
# MAX_ACTOR_LENGTH, so its worst case is that cap times the twelve bytes json.dumps spends on a
# surrogate escape.
_UNSAFE_LOG_CHARS = re.compile(r"[^\w.@:\- ]")
# CALLER-SUPPLIED data is ASCII-only, and the difference matters. `\w` keeps astral letters, so
# `%F0%9D%90%80` (U+1D400, category Lu) survived the scrub and each one cost twelve bytes as a
# surrogate escape: a 160-character path wrote 1,802 bytes where the test asserted 416, and twelve
# astral field names wrote 6,684 against 548 shipped. The amplification this scrub was said to
# remove was still there for every letter outside ASCII. Nothing legitimate is lost: a store key is
# `[A-Za-z0-9._-]`, a route path is ASCII by construction, and a rejected field name is only ever
# echoed for diagnosis.
#
# The field-name half of that was UNASSERTED for a whole round. The 4,096-byte ceiling this comment
# once cited belongs to a test whose payload carried no astral name at all, so reverting this line
# alone left all 316 tests green. Both halves are now asserted, and the assertion is the PROPERTY
# rather than a byte count: `part.isascii() and part.isprintable()` per logged field name in
# tests/test_api.py::test_a_rejected_body_cannot_write_an_unbounded_audit_line, which fires on the
# first non-ASCII part regardless of how many of the ten logged slots the payload fills.
_UNSAFE_ASCII_CHARS = re.compile(r"[^\w.@:\- ]", re.ASCII)


class AuthError(Exception):
    """Raised when a caller fails the token gate. The message is never returned verbatim."""


def token_matches(presented: str | None, expected: str) -> bool:
    """Compare a presented token against the expected one in constant time.

    hmac.compare_digest does not short-circuit on the first differing byte, so the
    comparison time does not reveal how much of the token was correct.
    """
    if presented is None:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def authorise(config: Config, presented: str | None) -> None:
    """Gate a cost-incurring or state-changing route. Raises AuthError on failure.

    With no token configured the app is in single-user local mode and the gate is open by
    design. With a token configured every gated route requires it.
    """
    if not config.auth_enabled:
        return
    expected = config.team_token
    if expected is None or not token_matches(presented, expected):
        raise AuthError("token rejected")


def sanitise_actor(value: str | None) -> str:
    """Reduce a caller-supplied actor label to something safe and bounded for a log line.

    User-supplied log content is stripped of control and separator characters so it cannot
    forge a second log entry, and capped so it cannot flood the log.
    """
    if not value:
        return "anonymous"
    return _scrub(value, empty="anonymous", limit=MAX_ACTOR_LENGTH)


# A path's separator is not a log-injection risk and IS its meaning. The actor charset was written
# for a label and deletes `/`, so applied to a path it turned `/v1/assess` into `v1assess` and made
# two different requests produce an identical audit record: the field stopped identifying its
# subject, in the records that exist for diagnosis.
#
# `%` is NOT in this set, and that is the whole point of the escape below. Deleting a refused
# character is not injective, and the collisions land on LEGITIMATE routes: `GET /v1/,assess` was
# audited as `path:"/v1/assess"` and `GET /v1/assessments/a:b,c` as `path:"/v1/assessments/a:bc"`,
# so an unauthenticated caller could write records naming a route or a store key they never
# requested, and an analyst reading the stream would be misled by design rather than by accident.
# Escaping instead of deleting restores injectivity: `%` never appears in the output except as an
# introducer, and Starlette hands over an already-decoded path, so a literal `%` in it can only
# have arrived as `%25` and is escaped back to exactly that. No path this app serves contains one:
# every route is `[A-Za-z0-9./_-]` and a store key is `[A-Za-z0-9._-]` either side of a colon.
_UNSAFE_PATH_CHARS = re.compile(r"[^\w./@:\- ]", re.ASCII)


def _percent_escape(match: re.Match[str]) -> str:
    """Render one refused character as its UTF-8 bytes in percent-hex, so the scrub is injective.

    Uppercase hex and one escape per BYTE, which is what a percent-encoded path looks like
    everywhere else, so an analyst reads `%2C` without needing to know this function exists. The
    output is ASCII by construction, so the byte-amplification the charset closes stays closed: an
    escape costs three ASCII characters, all of which json.dumps spends one byte on.
    """
    return "".join(f"%{byte:02X}" for byte in match.group().encode("utf-8"))


def sanitise_log_path(value: str, limit: int) -> str:
    """The same scrub for a request path, at the path's own length bound, and ESCAPED not deleted.

    A separate cap because the two differ and the difference matters: the longest LEGITIMATE path
    this app serves is `/v1/assessments/` plus a 129-character store key, so capping a path at the
    actor's 64 would truncate a real key out of every rejection record and destroy the diagnosis
    those records exist to give.

    Injective UP TO the cap, and only up to it. Truncation cannot be injective, and pretending
    otherwise would be the same class of claim this function exists to correct: two paths agreeing
    on their first `limit` characters after escaping still produce one record. What the escape buys
    is that a path SHORTER than the cap can no longer be made to read as a different one, which is
    the case an unauthenticated caller controls.
    """
    return _scrub(
        value,
        empty=UNPRINTABLE_MARKER,
        limit=limit,
        unsafe=_UNSAFE_PATH_CHARS,
        replacement=_percent_escape,
    )


def sanitise_log_part(value: str) -> str:
    """The same scrub for a field name echoed back in a rejection record.

    A DISTINCT empty marker, because `anonymous` is the sentinel for "no actor supplied": a rejected
    field name that scrubs to nothing, such as `{"*": 1}` or `{"": 1}`, was logged as `anonymous`
    and became indistinguishable from an anonymous caller in the one field that exists for
    diagnosis.
    """
    return _scrub(
        value, empty=UNPRINTABLE_MARKER, limit=MAX_ACTOR_LENGTH, unsafe=_UNSAFE_ASCII_CHARS
    )


def _scrub(
    value: str,
    *,
    empty: str,
    limit: int,
    unsafe: re.Pattern[str] | None = None,
    replacement: Callable[[re.Match[str]], str] | str = "",
) -> str:
    # The default is DELETION, which stays right for the two label fields: an operator's name and a
    # rejected field name are read by a human, and `O%27Brien` costs that reader more than the
    # aliasing costs anyone. Only the path claims to name a route, so only the path is escaped.
    cleaned = (unsafe or _UNSAFE_LOG_CHARS).sub(replacement, value).strip()
    if not cleaned:
        return empty
    return cleaned[:limit]
