"""Authentication and log sanitisation.

The shared team token is the only security boundary. It is compared in constant time so a
timing signal cannot leak it, and it never reaches a log line, an audit record, a health
response, or a client error body.
"""

from __future__ import annotations

import hmac
import re

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


# The path field takes the RAW request target, in bytes, and is the only field that does. Three
# rounds of this control were built on the decoded path and each one collided, because decoding is
# lossy in ways no scrub downstream of it can undo:
#
#   ● Deleting a refused character is not injective, and the collisions landed on LEGITIMATE
#     routes. `GET /v1/,assess` was audited as `path:"/v1/assess"` and `GET /v1/assessments/a:b,c`
#     as `path:"/v1/assessments/a:bc"`.
#   ● Escaping fixed that and left a subtler one: space was the single whitespace character the
#     charset permitted, so it survived the escape and was then removed by a `.strip()` two
#     functions away. `GET /v1/assessments/a:b%20` was audited byte-identically to
#     `GET /v1/assessments/a:b`.
#   ● And decoding itself aliases, whatever the scrub does afterwards. `urllib.parse.unquote`
#     leaves an invalid escape intact, so `/v1/%assess` and `/v1/%25assess` arrive identical; and
#     `/v1/assessments/a%2Fb:c` decodes to `/v1/assessments/a/b:c`, which reads as a route with a
#     different shape entirely. A comment here once claimed a literal `%` "can only have arrived
#     as `%25`", and that was false.
#
# Every one of those is an unauthenticated caller putting a route they never requested into the
# audit trail, which is the one thing this field exists to get right. Taking `raw_path` removes the
# whole class rather than the instance: the wire bytes are what the client asked for, and escaping
# every byte outside the permitted set is injective by construction, so distinct requests cannot
# share a record. There is no `.strip()` here, deliberately.
_PERMITTED_PATH_BYTES = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./@:-"
)


def sanitise_log_path(raw_path: bytes, limit: int) -> str:
    """Escape the raw request target to ASCII, at the path's own length bound.

    `%` is deliberately NOT permitted, so it appears in the output only as an escape introducer and
    the mapping stays unambiguous: a literal `%` on the wire is written `%25`.

    A separate cap from the actor's because the two differ and the difference matters: the longest
    LEGITIMATE path this app serves is `/v1/assessments/` plus a 129-character store key, so
    capping a path at the actor's 64 would truncate a real key out of every rejection record and
    destroy the diagnosis those records exist to give.

    Injective UP TO the cap, and only up to it. Truncation cannot be injective, and pretending
    otherwise would be the same class of claim this function exists to correct: two targets
    agreeing on their first `limit` characters after escaping still produce one record. What this
    buys is that a target SHORTER than the cap cannot be made to read as a different one, which is
    the case an unauthenticated caller controls.
    """
    escaped = "".join(
        chr(byte) if byte in _PERMITTED_PATH_BYTES else f"%{byte:02X}" for byte in raw_path
    )
    return escaped[:limit] or UNPRINTABLE_MARKER


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


def _scrub(value: str, *, empty: str, limit: int, unsafe: re.Pattern[str] | None = None) -> str:
    # DELETION, which stays right for the two label fields this function now serves: an operator's
    # name and a rejected field name are read by a human, and `O%27Brien` costs that reader more
    # than the aliasing costs anyone. Both alias, and the residual is recorded rather than implied
    # away: `{"a ": 1}` and `{"a": 1}` log one field name, as do two actor labels differing only in
    # refused punctuation. Neither names a route, which is what made the path's aliasing a finding
    # and leaves these two a documented limit.
    cleaned = (unsafe or _UNSAFE_LOG_CHARS).sub("", value).strip()
    if not cleaned:
        return empty
    return cleaned[:limit]
