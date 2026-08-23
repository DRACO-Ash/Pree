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

    One of FOUR permitted readers of `config.team_token` in the package, the others being
    `token_verifier` below and `Config.auth_enabled` and `Config.for_service`, which derive the
    token-free facts the service layer receives. That set is asserted across every module by
    `test_the_credential_has_exactly_one_set_of_readers_across_the_whole_package`. An earlier
    version of this docstring said "this function and `token_verifier` are the ONLY readers" and
    cited a test name that does not exist; both were wrong, and no guard covers a source docstring.
    """
    if not config.auth_enabled:
        return
    expected = config.team_token
    if expected is None or not token_matches(presented, expected):
        raise AuthError("token rejected")


def token_verifier(config: Config) -> Callable[[str | None], None]:
    """Close over the credential once, and hand the caller a callable instead of a secret.

    The BOUNDARY. A security review defeated the previous protection twice in nine lines, because
    it was a static rule about how the name `config` was spelt inside an audit expression, and the
    attacker picks the names: a helper called as `helper(config, exc)` in another module, and a
    helper whose parameter was named `cfg`, each recovered the deployed token verbatim from the pod
    log on an unauthenticated 401. So the HTTP layer is handed this closure and a `ServiceConfig`
    that has no token field, and no ATTRIBUTE the module can name reaches the credential.

    What that does NOT do, stated here because the previous version of this docstring claimed an
    absolute it did not have: the cell is still nameable from `app.py` as
    `verify_token.__closure__[0].cell_contents`, and `os.environ` is readable from the same scope.
    The next review took both in four lines each and put the whole credential in the pod log with
    the suite green. Introspection and a direct environment read are refused by
    `test_no_module_reaches_the_credential_by_introspection_or_the_environment`, not by this
    function, and that division is the honest statement of what each part buys.

    Two things narrow the cell itself. It closes over the EXPECTED STRING rather than the whole
    `Config`, so introspecting it yields one value instead of every setting beside it; and
    `Config.team_token` is `repr=False`, so no `repr`, f-string or format of a `Config` anywhere can
    print the credential, which closes that whole class in one keyword.
    """
    expected = config.team_token

    def verify(presented: str | None) -> None:
        if expected is None:
            return
        if not token_matches(presented, expected):
            raise AuthError("token rejected")

    return verify


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
# whole class rather than the instance: the wire bytes are the PATH the client asked for, and
# escaping every byte outside the permitted set is injective by construction, so two requests whose
# PATHS differ cannot share a record. There is no `.strip()` here, deliberately.
#
# Two requests differing only in their QUERY STRING do share one record, and that is deliberate.
# uvicorn's h11 implementation partitions the target on `?` before it ever reaches the scope, so
# `raw_path` is the path and never the full target; a comment here once said "the request target as
# the client sent it", and that was false. The query is not recovered, because `audit.py` records
# why: `GET /diagnostics?x-pree-token=<the real token>` was refused for authentication and then
# written verbatim into the pod log store, the one channel in this application that ever held the
# credential in cleartext. Putting the query back into an audited field would re-open that in the
# forensic channel, which is a worse trade than the aliasing. The distinction that IS recoverable
# without the value - whether a query was present at all - is recorded as a boolean beside the
# path. Two DIFFERENT queries still share a record; the boolean does not claim otherwise.
_PERMITTED_PATH_BYTES = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./@:-"
)


def sanitise_log_path(raw_path: bytes, limit: int) -> str:
    """Escape the raw request PATH to ASCII, at the path's own length bound.

    `%` is deliberately NOT permitted, so it appears in the output only as an escape introducer and
    the mapping stays unambiguous: a literal `%` on the wire is written `%25`.

    A separate cap from the actor's because the two differ and the difference matters: the longest
    LEGITIMATE path this app serves is `/v1/assessments/` plus a 129-character store key, so
    capping a path at the actor's 64 would truncate a real key out of every rejection record and
    destroy the diagnosis those records exist to give.

    Injective in the ESCAPED form up to `limit` characters, and the unit matters, because the
    escape expands 3:1. Truncation begins at 54 raw bytes when every byte needs escaping, not at
    160: `b"/" + b"\xff" * 53 + b"\x01"` and the same with `\x02` produce one record, and an
    earlier version of this docstring said "a target SHORTER than the cap cannot be made to read as
    a different one", which is false in wire bytes. No truncated record can be made to read as a
    real route, because a truncated one is exactly `limit` characters and the longest legitimate
    path is 145, so the forgery this function exists to stop stays stopped either way.
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
