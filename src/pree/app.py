"""The application factory.

create_app(deps) wires routes, middleware, and injected dependencies and returns the app
without listening. main.py owns the listener. This split lets the whole HTTP surface be
tested in-process with isolated state and a fixed clock.

The request pipeline is, in order: rate limit, then reject an oversize body, then authenticate
on cost-incurring and state-changing routes, then validate the body at the boundary, then the
handler, then a generic error response with the detail logged server-side.

Middleware nesting is deliberate. Outermost first: the hardening headers, then CORS, then
the coarse rate limiter, then the body cap, then the routes. The headers go outermost so every
response carries them, including one CORS short-circuits. CORS sits above the limiter or a 429
or 413 reaches a browser client with no origin header and cannot be read by it. The body cap
must sit above the routes because the framework buffers the whole body before it resolves the
token dependency, so an unauthenticated caller can otherwise make the process hold an
arbitrary payload.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .api_models import AssessRequest, AssessResponse, ContributionOut
from .audit import audit, bound_access_log, build_logger
from .config import Config
from .health import StorageProbe, StorageProber, diagnostics
from .ratelimit import (
    ACTOR_LIMIT,
    ACTOR_WINDOW_SECONDS,
    GLOBAL_LIMIT,
    GLOBAL_WINDOW_SECONDS,
    RateLimiter,
)
from .scoring import ThreatIndicators, assess
from .security import MAX_ACTOR_LENGTH, AuthError, authorise, sanitise_actor, token_matches
from .store import SCHEMA_VERSION, JsonStore, StoreError

LIVENESS_PATHS = ("/", "/healthz", "/readyz", "/livez", "/ping")
STORAGE_PROBE_PATH = "/healthz/storage"
# Exempt from the coarse limiter. The liveness paths touch nothing and the storage probe is
# bounded by its own hard timeout, so neither can be the expensive path the limiter protects.
# Leaving the storage probe subject to the limiter let unauthenticated traffic drive the
# container HEALTHCHECK to 429 and restart the pod, which is a cheaper denial of service than
# attacking the application itself.
UNMETERED_PATHS = (*LIVENESS_PATHS, STORAGE_PROBE_PATH)
# Any header by which a proxy claims to speak for someone else. Their PRESENCE collapses the
# rate-limit key rather than being trusted for its content.
_FORWARD_HEADERS = frozenset({"x-forwarded-for", "forwarded", "x-real-ip", "x-client-ip"})
GENERIC_CLIENT_ERROR = "request rejected"
STORE_UNAVAILABLE_ERROR = "could not store the assessment"
RATE_LIMITED_ERROR = "rate limited"
# The only detail strings the HTTP handler will echo. Everything else, framework messages
# included, becomes the generic error: a handler that passes through an arbitrary detail is a
# reflection primitive, and one of those details already differed between two tiers of the
# same control.
# Statuses that must never carry a body. h11 rejects a Content-Length on these, so returning
# JSON for one turns the intended status into a 500.
BODILESS_STATUSES = frozenset({204, 304})
OWN_ERROR_DETAILS = frozenset({GENERIC_CLIENT_ERROR, STORE_UNAVAILABLE_ERROR, RATE_LIMITED_ERROR})
# Generous for this schema, which is a handful of numbers and two short identifiers, and small
# enough that an unauthenticated caller cannot exhaust memory before the token gate runs.
MAX_BODY_BYTES = 32 * 1024
# The audit line for a rejected body is bounded, because the field names inside it are caller
# controlled. A 20,000-character key produced a 20,104-byte log record, so a rejected request
# was a cheaper way to fill the log volume than an accepted one.
MAX_VALIDATION_ERRORS_LOGGED = 10
# The request PATH is caller controlled too, and bounding only the body missed the cheaper
# attack: a rejected body needs an upload, while a long path needs neither a body nor a valid
# token. h11 admits roughly 16 KiB of request line, and JSON escaping doubled that on the way
# into the log, so one unauthenticated 401 wrote about 30 KB.
#
# 160 characters, not the 96 first chosen. The longest LEGITIMATE path this app serves is
# /v1/assessments/ plus a 129-character store key (two 64-character identifiers and the colon
# between them), which is 145 characters, so 96 truncated a real key out of every 401 and 503
# record and destroyed the diagnosis it exists to give.
#
# The bound is on CHARACTERS and the cost is in BYTES, and the two are not the same: the path
# arrives percent-decoded, and json.dumps renders one astral code point as a 12-byte surrogate
# escape. The worst case is therefore about 12x this number, roughly 2 KB per record, which the
# test asserts against that exact input rather than against an ASCII one.
# The store key's shape, enforced at the boundary rather than assumed. Two identifiers of at
# most 64 characters and the colon between them.
STORE_KEY_MAX_LENGTH = 129
STORE_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}:[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
MAX_LOGGED_PATH = 160
# Wrong tokens a single peer may present per window before every token-bearing request from it
# is refused, right or wrong. Twenty is generous for a human pasting a value and mistyping it,
# and it caps a guessing run at twenty attempts a minute per address rather than the tens of
# thousands the validity-keyed buckets allowed. AUTH_FAILURE_COST spends the budget in one
# step per failure; it exists as a named constant so the arithmetic is visible rather than
# implied by a limit of one. Only FAILURES are charged: an operator with the right token can
# make as many requests as the ordinary limiters allow.
AUTH_FAILURE_LIMIT = 20
AUTH_FAILURE_WINDOW_SECONDS = 60.0
# The reason string is composed server-side in both handlers that log one: AuthError carries a
# fixed literal, and StoreError embeds a configured path, never caller input. Truncating it to
# the actor length cut "could not acquire the store lock at /proc/.../.assessments.json" off
# mid-path and lost the errno, so the bound is generous and exists only as a backstop.
MAX_LOGGED_REASON = 512
# The interactive documentation paths. FastAPI serves all three by default, which made the
# whole route table, every field range and the token header name readable by an unauthenticated
# caller, and made /docs load a floating-tag CDN script onto the app origin: the same origin
# CORS trusts with credentials. They are served in development only.
OPENAPI_PATH = "/openapi.json"
DOCS_PATH = "/docs"
REDOC_PATH = "/redoc"
DOC_PATHS = (OPENAPI_PATH, DOCS_PATH, REDOC_PATH)
# Nothing this API returns needs a script, a style, a frame, or a form. Locked by default and
# tightened only, never loosened.
STRICT_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
SECURITY_HEADERS = {
    "Content-Security-Policy": STRICT_CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}
_TOKEN_HEADER = "x-pree-token"  # noqa: S105 - a header NAME, not a credential
_ACTOR_HEADER = "x-pree-actor"


class FrameGuard:
    """Refuse an ambiguously framed request, from the OUTERMOST layer.

    Position is the whole control here, and two rounds got it wrong. The check first lived
    inside the body-size middleware, after its bodyless-method early return, so it never ran
    for GET, HEAD, OPTIONS, DELETE or TRACE: precisely the method class smuggling uses, because
    a front end permits a GET with no body. Moving it above that return fixed those methods and
    left a preflight open, because Starlette answers a CORS preflight inside the CORS
    middleware without calling down, and CORS sits above the body-size layer. Measured: 200 OK
    and two responses on one connection, with a pipelined GET served.

    There is one position from which no other middleware can answer first, and this is it.
    Nothing may be registered outside this except the hardening headers, which only decorate a
    response on the way out.
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    @staticmethod
    def _ambiguously_framed(scope: Any) -> bool:
        """Refuse a request that declares BOTH a Transfer-Encoding and a Content-Length.

        RFC 9112 section 6.1 requires this to be rejected or the connection closed, and h11
        instead frames by Transfer-Encoding and leaves the Content-Length bytes in the buffer,
        where they are served as a pipelined request: one such request produced a 401 for the
        declared body followed by a 200 for a smuggled GET /healthz. It is only exploitable
        against a front end that frames by Content-Length where h11 frames by chunks, which a
        modern ingress rejects, so this is a primitive rather than a live path. It is also
        three lines in the one place that already walks the headers, which makes leaving it a
        choice rather than an oversight.
        """
        names = {name.lower() for name, _ in scope.get("headers", ())}
        return b"transfer-encoding" in names and b"content-length" in names

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and self._ambiguously_framed(scope):
            body = json.dumps({"error": GENERIC_CLIENT_ERROR}).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": status.HTTP_400_BAD_REQUEST,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                        # RFC 9112 section 6.1 says reject OR close. Both: the bytes after a
                        # frame two parsers would read differently must not be reusable as a
                        # pipelined request on this connection, whatever the front end made of
                        # them. A layer below this one dropped this header while rewriting the
                        # response, which is how the smuggled request got served.
                        (b"connection", b"close"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self._app(scope, receive, send)


class BodySizeLimit:
    """Reject an oversize request body before the application ever reads it.

    The body is drained here, counted as it arrives, and replayed to the application from a
    bounded buffer. Draining rather than wrapping the receive channel matters: raising from
    inside a wrapped channel is caught by the framework's own body-parsing guard and reported
    as a generic 400, so the cap could not state its own reason. Buffering answers both
    shapes honestly, a declared Content-Length above the cap and a chunked body with no
    declared length at all, and the buffer is bounded by the cap, which is the whole point.
    """

    _BODYLESS_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE", "TRACE"})

    def __init__(self, app: Any, max_bytes: int = MAX_BODY_BYTES) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        if scope.get("method") in self._BODYLESS_METHODS:
            await self._app(scope, receive, send)
            return

        if self._declared_over_cap(scope):
            await self._reject(send)
            return

        chunks: list[bytes] = []
        received = 0
        disconnected = False
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                disconnected = True
                break
            chunk = message.get("body") or b""
            received += len(chunk)
            if received > self._max_bytes:
                await self._reject(send)
                return
            chunks.append(chunk)
            if not message.get("more_body"):
                break

        body = b"".join(chunks)
        replayed = False

        async def replay() -> Any:
            nonlocal replayed
            if disconnected:
                return {"type": "http.disconnect"}
            if replayed:
                return {"type": "http.disconnect"}
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self._app(scope, replay, send)

    def _declared_over_cap(self, scope: Any) -> bool:
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    return int(value) > self._max_bytes
                except ValueError:
                    return False
        return False

    async def _reject(self, send: Any) -> None:
        """Answer from the middleware, without the application ever seeing the request.

        No `close` parameter any more. It existed for the ambiguous-frame refusal, which now
        lives in FrameGuard above every other layer, so keeping it here left an unreachable
        branch reading as a control.
        """
        body = json.dumps({"error": GENERIC_CLIENT_ERROR}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status.HTTP_413_CONTENT_TOO_LARGE,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _etag_for(payload: dict[str, Any]) -> str:
    """A strong ETag over the response body. SHA-256 is cryptographic, not a fast digest."""
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f'"{hashlib.sha256(encoded).hexdigest()}"'


def _etag_matches(header: str | None, etag: str) -> bool:
    """Honour If-None-Match properly: a list of validators, a weak prefix, or a wildcard.

    An exact string comparison against the whole header never matched a browser sending
    `W/"..."` or two validators, so every conditional request re-sent the full body.
    """
    if not header:
        return False
    for raw in header.split(","):
        candidate = raw.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


def _assessment_key(protected_asset_id: str, candidate_id: str) -> str:
    return f"{protected_asset_id}:{candidate_id}"


def _first_refused(limiter: RateLimiter, keys: tuple[str, ...]) -> str | None:
    """Charge every key and return the first that refused, or None if all admitted.

    Every key is charged even after one refuses. Short-circuiting on the first refusal would
    leave the others uncounted, so a caller who is over one limit would ride free on the rest.
    """
    refused: str | None = None
    for key in keys:
        if not limiter.allow(key) and refused is None:
            refused = key
    return refused


def _peer_key(request: Request) -> str:
    """The socket peer, folded to one literal when any forwarding header is present."""
    if _FORWARD_HEADERS & {name.lower() for name in request.headers}:
        return "forwarded"
    client = request.client
    return client.host if client else "unknown"


def _is_authenticated(request: Request, config: Config) -> bool:
    """Constant-time check of the presented token, for bucket selection only."""
    presented = request.headers.get(_TOKEN_HEADER)
    return bool(config.team_token and presented and token_matches(presented, config.team_token))


def _limit_keys(request: Request, config: Config) -> tuple[str, ...]:
    """Every bucket this request must fit inside. Refused if ANY of them is over.

    Returning a tuple rather than one key closes the half of this that adding a header could
    still exploit. Folding a forwarded request onto a single shared key stopped it minting
    fresh buckets, but the folded key was a DIFFERENT bucket from the peer's own, so a caller
    already at its limit escaped simply by adding X-Forwarded-For. Measured: a throttled
    caller went back to 404 by adding any of three headers. Charging both keys means a header
    can only ever reduce a caller's allowance, never increase it.

    The space is chosen by the token's VALIDITY, not its presence, so an unauthenticated caller
    cannot reach the operators' budget with `X-Pree-Token: anything`. That choice creates a
    guessing oracle on its own, which is why `_guessing_budget_spent` exists below and must be
    consulted BEFORE these keys are used.
    """
    space = "auth" if _is_authenticated(request, config) else "unauth"
    peer = _peer_key(request)
    keys = [f"{space}:{peer}"]
    if peer != "forwarded":
        return tuple(keys)
    # A forwarded request is charged to the fold AND to the socket peer, so a header can only
    # ever reduce an allowance. Both live in the same space, so switching space cannot escape
    # either: the guessing budget below is what stops that.
    client = request.client
    keys.append(f"{space}:socket:{client.host if client else 'unknown'}")
    return tuple(keys)


def _guessing_budget_spent(request: Request, config: Config, failures: RateLimiter) -> bool:
    """Has this peer spent its budget of wrong tokens? Charged before validity is used.

    Deciding the rate-limit bucket by the token's validity fixed one defect and created a
    worse one: refusal itself became a free oracle. Once a peer saturated its `unauth:` bucket
    with wrong guesses, every wrong guess landed in the saturated bucket and returned 429 while
    the RIGHT token landed in a fresh `auth:` bucket and returned 200, so the caller could keep
    guessing at full speed and read the answer off the status code. Measured: 2,666
    distinguishable guesses in three seconds, about 53,000 a minute, against the 240 a minute
    that config.py asserts and uses to justify the token length floor.

    So a peer gets a bounded number of WRONG tokens, counted separately, and once that budget
    is spent every token-bearing request from that peer is refused whether the token is right
    or wrong. Wrong and right become indistinguishable again, which is the property the floor
    calculation depends on.

    Charged on the socket peer, never on the fold: a caller must not be able to spend someone
    else's guessing budget by naming a forwarding header, and must not be able to escape its
    own by adding one.
    """
    if request.headers.get(_TOKEN_HEADER) is None:
        return False
    client = request.client
    peer = client.host if client else "unknown"
    key = f"guess:{peer}"
    # ASK first, without charging. Charging every token-bearing request to the failure budget
    # would lock out a legitimate operator who simply made more than AUTH_FAILURE_LIMIT ordinary
    # requests in a window, which is a denial of service dressed as a control. The question and
    # the charge have to be separable, which is why RateLimiter.spent exists.
    if failures.spent(key):
        return True
    if not _is_authenticated(request, config):
        failures.allow(key)
    return False


def register_error_handlers(app: FastAPI, audit_log: logging.Logger) -> None:
    """Register the three fail-closed error handlers.

    Every one of them returns a generic body to the client and keeps the cause
    server-side, so no handler can become an oracle or reflect untrusted input.
    """

    @app.exception_handler(AuthError)
    async def handle_auth_error(request: Request, exc: AuthError) -> JSONResponse:
        """Client sees a generic 401. The reason stays server-side."""
        audit_log.warning(
            json.dumps(
                {
                    "kind": "auth_reject",
                    "path": request.url.path[:MAX_LOGGED_PATH],
                    "reason": str(exc)[:MAX_LOGGED_REASON],
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return JSONResponse(
            {"error": GENERIC_CLIENT_ERROR}, status_code=status.HTTP_401_UNAUTHORIZED
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """A rejected body returns a generic 422; the detail is logged, never echoed.

        The framework default echoes the offending input back to the caller, which both
        reflects untrusted content and cannot serialise a non-finite float, turning a
        boundary rejection into a 500. This handler closes both.
        """
        audit_log.warning(
            json.dumps(
                {
                    "kind": "validation_reject",
                    "path": request.url.path[:MAX_LOGGED_PATH],
                    "errors": [
                        {
                            "loc": [str(part)[:MAX_ACTOR_LENGTH] for part in item.get("loc", ())],
                            "type": str(item.get("type"))[:MAX_ACTOR_LENGTH],
                        }
                        for item in exc.errors()[:MAX_VALIDATION_ERRORS_LOGGED]
                    ],
                    "error_count": len(exc.errors()),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return JSONResponse(
            {"error": GENERIC_CLIENT_ERROR},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> Response:
        """Bring every framework-raised HTTP error inside the app's own contract.

        Two rejections escaped it. A 5,000-digit integer exceeds CPython's int_max_str_digits,
        so json.loads raises a plain ValueError rather than a JSONDecodeError and FastAPI turns
        it into `{"detail": "There was an error parsing the body"}` with no audit line: a
        different response shape and no record, from a body every other rejection audits. And
        the per-actor limiter raised `{"detail": "rate limited"}` while the coarse limiter
        returned `{"error": "rate limited"}`, so the two tiers of one control disagreed.

        The detail is echoed only when it is one of this application's own literals. Anything
        else, including every framework message, becomes the generic error, so no handler can
        reflect a message the app did not write.
        """
        detail = exc.detail if exc.detail in OWN_ERROR_DETAILS else GENERIC_CLIENT_ERROR
        audit_log.warning(
            json.dumps(
                {
                    "kind": "http_reject",
                    "path": request.url.path[:MAX_LOGGED_PATH],
                    "status": exc.status_code,
                    "reason": str(exc.detail)[:MAX_LOGGED_REASON],
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        # 204 and 304 carry no body by definition, and h11 refuses a Content-Length on them, so
        # a JSONResponse would turn the intended status into a 500. Nothing raises those as
        # exceptions today, which makes this a trap set for the next handler rather than a live
        # fault, and the cost of closing it is two lines.
        if exc.status_code in BODILESS_STATUSES:
            return Response(status_code=exc.status_code, headers=exc.headers)
        # exc.headers carries Retry-After for a 429; dropping it tells a compliant client to
        # retry immediately, in a tight loop.
        return JSONResponse({"error": detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(StoreError)
    async def handle_store_error(request: Request, exc: StoreError) -> JSONResponse:
        """Storage refused. The client gets a generic 503; the cause is logged server-side."""
        audit_log.error(
            json.dumps(
                {
                    "kind": "store_error",
                    "path": request.url.path[:MAX_LOGGED_PATH],
                    "reason": str(exc)[:MAX_LOGGED_REASON],
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return JSONResponse(
            {"error": STORE_UNAVAILABLE_ERROR},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


def register_health_routes(
    app: FastAPI,
    config: Config,
    probe_now: Callable[[], StorageProbe],
    require_token: Callable[..., None],
) -> None:
    """Register the liveness paths, the storage proof, and the diagnostics read-out."""

    async def liveness() -> dict[str, str]:
        """Liveness only: 200, unauthenticated, touching nothing, so it cannot hang.

        Declared async deliberately. A sync handler runs in the shared request threadpool, so
        under a flood of the storage-probe path (each caller of which holds a worker for up to
        the probe budget) liveness queued behind it: measured at 1.46s p95 against a 0.045s
        baseline. Nothing here needs a thread.
        """
        return {"status": "ok", "service": "pree", "version": __version__}

    for path in LIVENESS_PATHS:
        # The storage proof lives on its own path below, where a hard timeout bounds it.
        app.add_api_route(
            path,
            liveness,
            methods=["GET"],
            status_code=status.HTTP_200_OK,
            include_in_schema=path == "/healthz",
        )

    @app.get(STORAGE_PROBE_PATH)
    def storage_health(response: Response) -> dict[str, Any]:
        """Prove storage with a real write, racing a hard timeout.

        This is the container HEALTHCHECK target. On failure the 503 body names the resolved
        directory and the exact errno, so a screenshot of it is a full diagnosis.
        """
        probe = probe_now()
        if not probe.writable:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return probe.as_body()

    @app.get("/diagnostics", dependencies=[Depends(require_token)])
    def read_diagnostics() -> dict[str, Any]:
        """A secret-free read-out: each critical input as a boolean and a length only.

        Gated whenever a token is configured. It holds no secret value, but publishing the
        exact token length narrows an attacker's search space for free. Before a token is set,
        which is when a first deploy needs it most, the gate is open by construction.
        """
        return diagnostics(config, probe_now())


def register_api_routes(
    app: FastAPI,
    *,
    config: Config,
    store: JsonStore,
    audit_log: logging.Logger,
    fine: RateLimiter,
    require_token: Callable[..., None],
) -> None:
    """Register the gated scoring and read routes.

    Both sit behind the token gate, the coarse limiter, and the body cap. The scoring
    route additionally carries the fine per-address limit, because it is the expensive
    path and the one that writes.
    """

    @app.post("/v1/assess", response_model=AssessResponse, dependencies=[Depends(require_token)])
    def create_assessment(
        payload: AssessRequest,
        request: Request,
        x_pree_actor: str | None = Header(default=None, alias=_ACTOR_HEADER),
    ) -> AssessResponse:
        """Score one candidate against one protected asset, then persist and audit it."""
        refused = _first_refused(fine, _limit_keys(request, config))
        if refused is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=RATE_LIMITED_ERROR,
                headers={"Retry-After": str(fine.retry_after_seconds(refused))},
            )

        # The actor label is for the audit trail only. It is caller-supplied, so it is never a
        # rate-limit key and never an identity claim.
        actor = sanitise_actor(x_pree_actor)
        started = time.monotonic()
        result = assess(ThreatIndicators(**payload.indicators.model_dump()))
        body = AssessResponse(
            protected_asset_id=payload.protected_asset_id,
            candidate_id=payload.candidate_id,
            score=result.score,
            confidence=str(result.confidence),
            evidence_coverage=result.evidence_coverage,
            missing_indicators=result.missing_indicators,
            contributions=[
                ContributionOut(
                    indicator=c.indicator,
                    weight=c.weight,
                    normalised=c.normalised,
                    rationale=c.rationale,
                )
                for c in result.contributions
            ],
            schema_version=SCHEMA_VERSION,
        )
        key = _assessment_key(payload.protected_asset_id, payload.candidate_id)
        try:
            store.upsert(key, body.model_dump())
        except StoreError:
            # A privileged action that failed is still a privileged action: it gets its own
            # audit line, or the trail records successes only and an operator who acted and
            # failed leaves no trace.
            audit(
                audit_log,
                action="assess",
                actor=actor,
                duration_ms=int((time.monotonic() - started) * 1000),
                outcome="error",
                key=key,
            )
            raise
        audit(
            audit_log,
            action="assess",
            actor=actor,
            duration_ms=int((time.monotonic() - started) * 1000),
            outcome="ok",
            key=key,
            score=result.score,
            confidence=str(result.confidence),
            evidence_coverage=result.evidence_coverage,
        )
        return body

    @app.get("/v1/assessments/{key}", dependencies=[Depends(require_token)])
    def read_assessment(
        key: Annotated[str, PathParam(max_length=STORE_KEY_MAX_LENGTH, pattern=STORE_KEY_PATTERN)],
        response: Response,
        x_pree_actor: str | None = Header(default=None, alias=_ACTOR_HEADER),
        if_none_match: str | None = Header(default=None, alias="if-none-match"),
    ) -> Any:
        """Return a stored assessment, honouring If-None-Match with 304.

        Audited, including the successes. A refused request was audited, a rejected body was
        audited, a store failure was audited, and a successful DISCLOSURE of a record was not.
        The security policy names this store as one of the two assets worth protecting, because
        it reveals what the operator is watching and what they judge dangerous; under the
        shared-token model "who read what" is the only forensic question the trail could answer
        about a stolen token, and it could not answer it.
        """
        started = time.monotonic()
        actor = sanitise_actor(x_pree_actor)
        record = store.read()["assessments"].get(key)
        elapsed = int((time.monotonic() - started) * 1000)
        # The path validator on the parameter above is what shape-checks this, and it exists
        # because this comment previously CLAIMED a validator that was not there. Truncated
        # anyway: a bound that depends on another layer's correctness is a bound that moves
        # when that layer does, and an astral-plane key still costs twelve JSON bytes each.
        audited_key = key[:MAX_LOGGED_PATH]
        if record is None:
            audit(audit_log, "read_assessment", actor, elapsed, "not_found", key=audited_key)
            response.status_code = status.HTTP_404_NOT_FOUND
            return {"error": "not found"}
        etag = _etag_for(record)
        if _etag_matches(if_none_match, etag):
            audit(audit_log, "read_assessment", actor, elapsed, "not_modified", key=audited_key)
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
        audit(audit_log, "read_assessment", actor, elapsed, "disclosed", key=audited_key)
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
        return record


def register_cors(
    app: FastAPI,
    config: Config,
    audit_log: logging.Logger,
    refuse_over_limit: Callable[[Request], Response | None],
) -> None:
    """Register CORS and the layer that normalises and meters what CORS answers itself.

    Extracted because create_app grew past its statement budget, and because the ordering here
    is a security property that deserves to be read in one place: the normaliser must sit
    OUTSIDE CORSMiddleware, since Starlette answers a preflight inside it without calling down,
    and that makes this the only layer that can meter a preflight or see its response.
    """
    if config.allowed_origin:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[config.allowed_origin],
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=[_TOKEN_HEADER, _ACTOR_HEADER, "content-type"],
        )

        @app.middleware("http")
        async def normalise_cors_rejection(
            request: Request, call_next: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            """Bring a refused preflight inside the one error contract, and audit it.

            Starlette answers a disallowed preflight itself, with a 400 whose body is the plain
            text "Disallowed CORS origin" and no audit line. The body carries no caller input,
            so this was contract drift rather than a reflection, but the control table claimed
            every rejection used one contract and was audited, and this one did neither. Sitting
            ABOVE the CORS middleware is the only place that can see its answer, and it is
            also the only place that can METER a preflight: CORS answers one itself without
            calling down, so the coarse limiter below never saw one. Unmetered, refused
            preflights wrote 902,000 bytes of log in 1.6 seconds, about 32.9 MB a minute per
            worker, from an unauthenticated caller: the exact class of amplification the
            bounded audit lines exist to close, on the one path nothing counted.
            """
            if request.method == "OPTIONS":
                over = refuse_over_limit(request)
                if over is not None:
                    return over
            response = await call_next(request)
            if response.status_code != status.HTTP_400_BAD_REQUEST:
                return response
            if request.method != "OPTIONS" or "origin" not in request.headers:
                return response
            # There is no "leave a Connection: close response alone" branch here, and there
            # was one for a while. It was unreachable, because FrameGuard sits outside this
            # layer and answers an ambiguously framed request before it arrives, and an
            # unreachable branch that reads as a control is the thing this project keeps
            # deleting. What replaces it is an assertion that FrameGuard IS outermost, which
            # is the property that makes the branch unnecessary.
            audit_log.warning(
                json.dumps(
                    {
                        "kind": "cors_reject",
                        "path": request.url.path[:MAX_LOGGED_PATH],
                        # The ACTUAL reason. This field said origin_allowed=false for every 400
                        # on a preflight, including one raised for the allowed origin by a
                        # different control, so the only record of the event misstated it.
                        "origin_allowed": request.headers.get("origin") == config.allowed_origin,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return JSONResponse(
                {"error": GENERIC_CLIENT_ERROR},
                status_code=status.HTTP_400_BAD_REQUEST,
            )


def create_app(
    config: Config,
    store: JsonStore,
    *,
    logger: logging.Logger | None = None,
    prober: StorageProber | None = None,
    global_limiter: RateLimiter | None = None,
    actor_limiter: RateLimiter | None = None,
    failure_limiter: RateLimiter | None = None,
) -> FastAPI:
    """Build the app from injected dependencies. Does not listen."""
    audit_log = logger or build_logger()
    storage = prober or StorageProber()
    # Bound the access log before anything can be served through it.
    bound_access_log()
    coarse = global_limiter or RateLimiter(GLOBAL_LIMIT, GLOBAL_WINDOW_SECONDS)
    fine = actor_limiter or RateLimiter(ACTOR_LIMIT, ACTOR_WINDOW_SECONDS)
    # Wrong tokens per peer, counted separately from traffic so a legitimate operator's normal
    # request rate can never exhaust it and a guessing run cannot hide inside it.
    auth_failures = failure_limiter or RateLimiter(AUTH_FAILURE_LIMIT, AUTH_FAILURE_WINDOW_SECONDS)
    # Holds the last observed storage state so a change of state can be logged once, rather
    # than every probe restating it. A pod the platform later kills still leaves a narrative.
    last_ready: dict[str, bool | None] = {"writable": None}

    serve_docs = not config.is_production
    app = FastAPI(
        title="Pree",
        version=__version__,
        description="Confidence-tiered threat scoring for Protect and Defend operators.",
        openapi_url=OPENAPI_PATH if serve_docs else None,
        docs_url=DOCS_PATH if serve_docs else None,
        redoc_url=REDOC_PATH if serve_docs else None,
        # Starlette redirects a trailing slash by default, and it does so BEFORE any dependency
        # runs. `POST /v1/assess/` answered 307 with an absolute Location built from the
        # caller's own Host header, unauthenticated, and a 307 preserves the method, the body
        # and the headers: an operator who typed a trailing slash and followed redirects would
        # re-send the team token to a host the caller named. Pinning the forwarded trust list
        # in the previous commit made it worse, not better, because the scheme is now
        # unconditionally http, so that resend would be in cleartext. There is no route here
        # that needs the convenience, so the redirect is off and a trailing slash is a 404.
        redirect_slashes=False,
    )

    def probe_now() -> StorageProbe:
        """Probe storage and log any transition between writable and unwritable."""
        probe = storage.probe(config.data_dir)
        if last_ready["writable"] != probe.writable:
            state = "ready" if probe.writable else "unready"
            print(
                f"pree storage {state}: data_dir={probe.data_dir} "
                f"errno={probe.errno_name or 'none'}",
                flush=True,
            )
            last_ready["writable"] = probe.writable
        return probe

    # --- innermost of the middleware stack, above the routes. Later registrations are
    # OUTERMOST, so the order below reads inside-out. Which layer sits where is a security
    # property, not a style choice, and getting it wrong is what the last round's framing
    # defect was: the check lived in the innermost layer and CORS answered above it. ---
    app.add_middleware(BodySizeLimit)

    def _refuse_over_limit(request: Request) -> Response | None:
        """Charge the coarse limiter, and refuse if any of the request's buckets is over."""
        if request.url.path in UNMETERED_PATHS:
            return None
        if _guessing_budget_spent(request, config, auth_failures):
            # Deliberately the same 429 a rate limit gives, with no hint that the reason was a
            # wrong token. Answering differently here would rebuild the oracle this closes.
            return JSONResponse(
                {"error": RATE_LIMITED_ERROR},
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": str(int(AUTH_FAILURE_WINDOW_SECONDS))},
            )
        refused = _first_refused(coarse, _limit_keys(request, config))
        if refused is None:
            return None
        return JSONResponse(
            {"error": RATE_LIMITED_ERROR},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(coarse.retry_after_seconds(refused))},
        )

    @app.middleware("http")
    async def coarse_rate_limit(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Tier one: a coarse per-address limit that protects the process.

        Keyed per peer address so one abusive client cannot consume the whole team's budget.
        The liveness paths and the storage probe are exempt; rate-limiting the platform's own
        probes would present an infrastructure fault as an application failure, and would let
        rejected traffic restart the pod.

        A CORS preflight never reaches this layer, because Starlette answers it inside the CORS
        middleware above. Preflights are metered separately, outside CORS: unmetered they wrote
        32.9 MB of log a minute per worker from an unauthenticated caller.
        """
        over = _refuse_over_limit(request)
        return over if over is not None else await call_next(request)

    # --- second-outermost: CORS, so it wraps every rejection the layers below emit. The
    # hardening headers are registered after this and are therefore outermost. ---
    # Fail-closed by construction: only the configured origin, and load_config refuses to
    # start on a wildcard origin with a token, so by here the origin is absent or safe.
    register_cors(app, config, audit_log, _refuse_over_limit)

    # --- second-outermost: the framing guard. It has to be above CORS, because Starlette
    # answers a preflight inside the CORS middleware without calling down, so a preflight
    # carrying both framings never reached the check while it lived in BodySizeLimit: measured,
    # 200 OK and two responses on one connection with a smuggled GET served. Registered before
    # security_headers only so the hardening headers still wrap it. ---
    app.add_middleware(FrameGuard)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Set the hardening headers on every response.

        The development documentation pages are exempt from the Content-Security-Policy only,
        because they legitimately load their own script and style. They do not exist in
        production, so the exemption cannot reach a deployed app.
        """
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            if name == "Content-Security-Policy" and serve_docs and request.url.path in DOC_PATHS:
                continue
            response.headers.setdefault(name, value)
        return response

    register_error_handlers(app, audit_log)

    def require_token(
        x_pree_token: str | None = Header(default=None, alias=_TOKEN_HEADER),
    ) -> None:
        """The token gate on every gated route."""
        authorise(config, x_pree_token)

    register_health_routes(app, config, probe_now, require_token)

    register_api_routes(
        app,
        config=config,
        store=store,
        audit_log=audit_log,
        fine=fine,
        require_token=require_token,
    )

    return app
