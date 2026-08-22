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
from .security import (
    MAX_ACTOR_LENGTH,
    AuthError,
    authorise,
    sanitise_actor,
    sanitise_log_part,
    sanitise_log_path,
)
from .store import SCHEMA_VERSION, JsonStore, StoreError

LIVENESS_PATHS = ("/", "/healthz", "/readyz", "/livez", "/ping")
STORAGE_PROBE_PATH = "/healthz/storage"
# Exempt from the coarse limiter. The liveness paths touch nothing and the storage probe is
# bounded by its own hard timeout, so neither can be the expensive path the limiter protects.
# Leaving the storage probe subject to the limiter let unauthenticated traffic drive the
# container HEALTHCHECK to 429 and restart the pod, which is a cheaper denial of service than
# attacking the application itself.
UNMETERED_PATHS = (*LIVENESS_PATHS, STORAGE_PROBE_PATH)
# The methods a platform probe actually uses. The exemption is for the probe, not for the path:
# any other verb on one of these paths is ordinary traffic and is metered like any other.
# Per PATH, because the exemption is for the probe and a path that does not serve a method is
# not being probed with it. HEAD was exempt on /healthz/storage, which serves only GET, so a
# 405 that can never be a platform probe was both unmetered and unaudited: 600 of 600 admitted,
# 33,000 bytes of access log in 0.62 seconds. It grants no capability beyond flooding
# GET /healthz, but it contradicts the reasoning used to meter preflights on these same paths.
_PROBE_METHODS: dict[str, frozenset[str]] = {
    **{path: frozenset({"GET", "HEAD"}) for path in LIVENESS_PATHS},
    STORAGE_PROBE_PATH: frozenset({"GET"}),
}
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
MAX_LOGGED_PATH = 160
# The store key's shape, enforced at the boundary rather than assumed. Two identifiers of at
# most 64 characters and the colon between them.
STORE_KEY_MAX_LENGTH = 129
STORE_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}:[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
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


def _limit_keys(request: Request) -> tuple[str, ...]:
    """Every bucket this request must fit inside. Refused if ANY of them is over.

    ONE key space, and the token plays no part in choosing it: a saturated peer is refused
    identically whether its token is right or wrong, which is what removes the guessing oracle
    and restores the premise the token-length floor is calculated from. The socket key is
    unconditional and the forwarded fold is ADDITIONAL, so a forwarding header can only ever add
    a constraint, never grant a fresh bucket.

    Three earlier designs are recorded in docs/SECURITY.md with their measurements, and the
    regression test for each is
    `test_a_forwarding_header_cannot_widen_the_rate_limit_key_space`. The residual, that operators
    behind a shared ingress share a bucket, is a recorded accepted risk.
    """
    client = request.client
    keys = [f"socket:{client.host if client else 'unknown'}"]
    if _peer_key(request) == "forwarded":
        keys.append("forwarded")
    return tuple(keys)


def _unaudited_rejection(request: Request, code: int) -> bool:
    """Is this a rejection whose audit line would be pure noise an attacker can size?

    Exactly one shape qualifies: a method-not-allowed on one of the six paths the platform
    probes. Those paths exist to be hit constantly by infrastructure, the refusal reveals
    nothing, and writing a record for each was measured at 8.1 MB a minute per worker from an
    unauthenticated caller. Deliberately narrow: a 405 on a real route is still audited, because
    there the method a caller tried is worth knowing.
    """
    return code == status.HTTP_405_METHOD_NOT_ALLOWED and request.url.path in UNMETERED_PATHS


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
                    "path": sanitise_log_path(request.url.path, MAX_LOGGED_PATH),
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
                    "path": sanitise_log_path(request.url.path, MAX_LOGGED_PATH),
                    "errors": [
                        {
                            # SCRUBBED, not merely capped: each part is a caller-supplied field
                            # name, so a control sequence in a key reaches the log through it. One
                            # line survives today only because json.dumps escapes it, which stops
                            # holding the moment a value is unwrapped by a log viewer or `jq -r`.
                            # The actor label has been scrubbed for this reason since round eleven.
                            "loc": [sanitise_log_part(str(part)) for part in item.get("loc", ())],
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
        # A wrong method on a probe path gets the contract but no audit line. Auditing it was
        # the other half of an 8.1 MB-a-minute amplification: the record says nothing an
        # operator needs, because a DELETE to /healthz is refused whoever sends it, and the
        # request is metered now so the flood is bounded either way. Every other rejection,
        # including a 405 on a real route, is still audited.
        if _unaudited_rejection(request, exc.status_code):
            # exc.headers is LOAD-BEARING here, and a previous version of this comment said it
            # was not. Starlette raises the 405 with Allow on the EXCEPTION; nothing sets it on
            # the response. Remove this argument and the six probe paths answer 405 with no
            # Allow, which RFC 9110 makes a MUST, and the test that covers it turns red.
            #
            # The comment claimed a non-reproduction because the measurement removed the
            # argument from the OTHER branch below, which serves /diagnostics, and then measured
            # the probe paths, which this branch serves. Same class of error as the contaminated
            # directory one round earlier: the mutation was adjacent to the control, not on it.
            return JSONResponse({"error": detail}, status_code=exc.status_code, headers=exc.headers)
        audit_log.warning(
            json.dumps(
                {
                    "kind": "http_reject",
                    "path": sanitise_log_path(request.url.path, MAX_LOGGED_PATH),
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
                    "path": sanitise_log_path(request.url.path, MAX_LOGGED_PATH),
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
        # ONE route carrying both methods, and out of the schema. NO operation id is passed,
        # and two earlier versions of this comment claimed one was: the claim was left behind
        # when the fix moved from pinning an id to leaving the schema, and a comment asserting a
        # control that is not in the code is worse than no comment.
        #
        # FastAPI does not add HEAD for a GET route, so `HEAD /healthz` was a 405: wrong for a
        # liveness path a probe may be configured to HEAD, and the cheapest way into the
        # unmetered-405 amplification. Registering both on one route without an explicit id made
        # FastAPI derive one id from an arbitrary member of the method set, so the development
        # OpenAPI document gave GET and HEAD the same id and the verify loop carried a
        # duplicate-operation-id warning on every run. Splitting them into two routes fixed the
        # warning and broke something quieter: Starlette builds a 405's Allow header from the
        # matched route's own methods, so `DELETE /healthz` advertised `GET` alone while the
        # resource also serves HEAD. RFC 9110 wants the methods the RESOURCE supports. One route
        # kept out of the schema gives a correct Allow and a valid document at once; see the
        # include_in_schema argument below for why no choice of id does.
        app.add_api_route(
            path,
            liveness,
            methods=["GET", "HEAD"],
            status_code=status.HTTP_200_OK,
            # Out of the schema entirely, and that is a trade rather than an oversight. FastAPI
            # generates one operation per METHOD from one route and gives them all the route's
            # single operation id, so a two-method route in the schema is a duplicate id however
            # the id is chosen: explicitly here, or derived. The alternatives were two routes,
            # which made Allow advertise GET alone on a resource that serves HEAD, or leaving the
            # document invalid. A correct Allow beats a dev-only schema entry for a path whose
            # whole contract is "200, touches nothing", and the deployment sheet documents these
            # five paths with a test pinning them to the code.
            include_in_schema=False,
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
        refused = _first_refused(fine, _limit_keys(request))
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
    meter_preflight: Callable[[Request], Response | None],
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
            # A GENUINE preflight only: Origin and Access-Control-Request-Method both present
            # is exactly the shape CORSMiddleware answers itself without calling down, so it is
            # exactly the shape the coarse limiter below never sees. A bare OPTIONS with an
            # Origin and no requested method falls through to the routes and IS metered there;
            # metering it here as well halved its allowance, 119 of 200 admitted against a
            # documented 240.
            if request.method == "OPTIONS" and {
                "origin",
                "access-control-request-method",
            } <= {name.lower() for name in request.headers}:
                # EVERY preflight, on every path. `refuse_over_limit` returns None for the
                # liveness paths and the storage probe before it meters, so preflights on those
                # six paths stayed uncounted while the audit line below was written anyway:
                # measured, 1,200 refused preflights across /healthz, / and /healthz/storage
                # returned 400 each with nothing counting them, and eight threads grew the log
                # by 435,200 bytes in 3.18 seconds, about 8.2 MB a minute per worker. The
                # exemption exists so the platform's own probes are never throttled, and the
                # platform probes with GET, never with a cross-origin preflight, so metering
                # OPTIONS regardless of path costs the platform nothing.
                over = meter_preflight(request)
                if over is not None:
                    return over
            response = await call_next(request)
            if response.status_code != status.HTTP_400_BAD_REQUEST:
                return response
            if request.method != "OPTIONS" or "origin" not in request.headers:
                return response
            if request.url.path in UNMETERED_PATHS:
                # A preflight against a path that touches nothing gets the contract but not the
                # audit line. Auditing it was 45% of the bytes that flood wrote, on the channel
                # the forensic trail lives in, for an event that reveals nothing: a cross-origin
                # preflight to /healthz is refused whoever sends it.
                return JSONResponse(
                    {"error": GENERIC_CLIENT_ERROR},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
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
                        "path": sanitise_log_path(request.url.path, MAX_LOGGED_PATH),
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
) -> FastAPI:
    """Build the app from injected dependencies. Does not listen."""
    audit_log = logger or build_logger()
    storage = prober or StorageProber()
    # Bound the access log before anything can be served through it.
    bound_access_log()
    coarse = global_limiter or RateLimiter(GLOBAL_LIMIT, GLOBAL_WINDOW_SECONDS)
    fine = actor_limiter or RateLimiter(ACTOR_LIMIT, ACTOR_WINDOW_SECONDS)
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

    def _charge_coarse(request: Request) -> Response | None:
        """Charge the coarse limiter, and refuse if any of the request's buckets is over."""
        refused = _first_refused(coarse, _limit_keys(request))
        if refused is None:
            return None
        return JSONResponse(
            {"error": RATE_LIMITED_ERROR},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(coarse.retry_after_seconds(refused))},
        )

    def _refuse_over_limit(request: Request) -> Response | None:
        """The metered-path entry point: exempt the platform's PROBES, then charge.

        A probe is a GET or a HEAD. The exemption used to cover the path whatever the method,
        so any other verb on one of the six got a router 405 and a full audit line with nothing
        counting it: measured, 4,000 requests across eight threads, none refused, 624,000 bytes
        of log in 4.60 seconds, about 8.1 MB a minute per worker on the channel the forensic
        trail lives in. That is the same amplification, at the same measured rate, that this
        project closed for preflights one commit earlier while claiming to have closed the last
        uncounted unauthenticated path.
        """
        if request.method in _PROBE_METHODS.get(request.url.path, frozenset()):
            return None
        return _charge_coarse(request)

    def _meter_preflight(request: Request) -> Response | None:
        """A preflight is charged on EVERY path, exemption or not.

        The exemption exists so the platform's own probes are never throttled, and the platform
        probes with GET. A cross-origin OPTIONS to /healthz is not a platform probe and is
        refused whoever sends it, so counting it costs the platform nothing and closes the last
        uncounted unauthenticated path.
        """
        return _charge_coarse(request)

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

    # --- third-outermost: CORS, so it wraps every rejection the layers below emit. Registered
    # before the framing guard and the hardening headers, both of which therefore wrap it: with
    # Starlette middleware, later-registered is further out. This comment said
    # "second-outermost", and so did the framing guard's; only one layer can be. ---
    # Fail-closed by construction: only the configured origin, and load_config refuses to
    # start on a wildcard origin with a token, so by here the origin is absent or safe.
    register_cors(app, config, audit_log, _meter_preflight)

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
