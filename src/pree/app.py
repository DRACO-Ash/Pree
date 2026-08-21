"""The application factory.

create_app(deps) wires routes, middleware, and injected dependencies and returns the app
without listening. main.py owns the listener. This split lets the whole HTTP surface be
tested in-process with isolated state and a fixed clock.

The request pipeline is, in order: reject an oversize body, then rate limit, then authenticate
on cost-incurring and state-changing routes, then validate the body at the boundary, then the
handler, then a generic error response with the detail logged server-side.

Middleware nesting is deliberate, outermost first: CORS, then the coarse rate limiter, then
the body cap, then the routes. CORS must be outermost or a 429 or 413 reaches a browser
client with no origin header and cannot be read by it. The body cap must sit above the routes
because the framework buffers the whole body before it resolves the token dependency, so an
unauthenticated caller can otherwise make the process hold an arbitrary payload.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .api_models import AssessRequest, AssessResponse, ContributionOut
from .audit import audit, build_logger
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
from .security import AuthError, authorise, sanitise_actor
from .store import SCHEMA_VERSION, JsonStore, StoreError

LIVENESS_PATHS = ("/", "/healthz", "/readyz", "/livez", "/ping")
STORAGE_PROBE_PATH = "/healthz/storage"
# Exempt from the coarse limiter. The liveness paths touch nothing and the storage probe is
# bounded by its own hard timeout, so neither can be the expensive path the limiter protects.
# Leaving the storage probe subject to the limiter let unauthenticated traffic drive the
# container HEALTHCHECK to 429 and restart the pod, which is a cheaper denial of service than
# attacking the application itself.
UNMETERED_PATHS = (*LIVENESS_PATHS, STORAGE_PROBE_PATH)
GENERIC_CLIENT_ERROR = "request rejected"
STORE_UNAVAILABLE_ERROR = "could not store the assessment"
# Generous for this schema, which is a handful of numbers and two short identifiers, and small
# enough that an unauthenticated caller cannot exhaust memory before the token gate runs.
MAX_BODY_BYTES = 32 * 1024
_TOKEN_HEADER = "x-pree-token"  # noqa: S105 - a header NAME, not a credential
_ACTOR_HEADER = "x-pree-actor"


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
        if scope.get("type") != "http" or scope.get("method") in self._BODYLESS_METHODS:
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


def _client_key(request: Request) -> str:
    """The rate-limit key: the peer address, never a caller-supplied header.

    Keying the fine tier on the actor header let a caller mint a fresh label per request and
    bypass the tier entirely. The peer address is not perfect behind a shared proxy, which is
    recorded in the security policy, but it is not chosen by the caller.
    """
    client = request.client
    return client.host if client else "unknown"


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
                {"kind": "auth_reject", "path": request.url.path, "reason": str(exc)},
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
                    "path": request.url.path,
                    "errors": [
                        {
                            "loc": [str(part) for part in item.get("loc", ())],
                            "type": str(item.get("type")),
                        }
                        for item in exc.errors()
                    ],
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return JSONResponse(
            {"error": GENERIC_CLIENT_ERROR},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    @app.exception_handler(StoreError)
    async def handle_store_error(request: Request, exc: StoreError) -> JSONResponse:
        """Storage refused. The client gets a generic 503; the cause is logged server-side."""
        audit_log.error(
            json.dumps(
                {"kind": "store_error", "path": request.url.path, "reason": str(exc)},
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

    def liveness() -> dict[str, str]:
        """Liveness only: 200, unauthenticated, touching nothing, so it cannot hang."""
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
        limit_key = _client_key(request)
        if not fine.allow(limit_key):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate limited",
                headers={"Retry-After": str(fine.retry_after_seconds(limit_key))},
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
        key: str,
        response: Response,
        if_none_match: str | None = Header(default=None, alias="if-none-match"),
    ) -> Any:
        """Return a stored assessment, honouring If-None-Match with 304."""
        record = store.read()["assessments"].get(key)
        if record is None:
            response.status_code = status.HTTP_404_NOT_FOUND
            return {"error": "not found"}
        etag = _etag_for(record)
        if _etag_matches(if_none_match, etag):
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
        return record


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
    coarse = global_limiter or RateLimiter(GLOBAL_LIMIT, GLOBAL_WINDOW_SECONDS)
    fine = actor_limiter or RateLimiter(ACTOR_LIMIT, ACTOR_WINDOW_SECONDS)
    # Holds the last observed storage state so a change of state can be logged once, rather
    # than every probe restating it. A pod the platform later kills still leaves a narrative.
    last_ready: dict[str, bool | None] = {"writable": None}

    app = FastAPI(
        title="Pree",
        version=__version__,
        description="Confidence-tiered threat scoring for Protect and Defend operators.",
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

    # --- innermost of the three middlewares: the body cap, above the routes ---
    app.add_middleware(BodySizeLimit)

    @app.middleware("http")
    async def coarse_rate_limit(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Tier one: a coarse per-address limit that protects the process.

        Keyed per peer address so one abusive client cannot consume the whole team's budget.
        The liveness paths and the storage probe are exempt; rate-limiting the platform's own
        probes would present an infrastructure fault as an application failure, and would let
        rejected traffic restart the pod.
        """
        if request.url.path in UNMETERED_PATHS:
            return await call_next(request)
        key = _client_key(request)
        if not coarse.allow(key):
            return JSONResponse(
                {"error": "rate limited"},
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": str(coarse.retry_after_seconds(key))},
            )
        return await call_next(request)

    # --- outermost: CORS, so it wraps every rejection the layers below emit ---
    # Fail-closed by construction: only the configured origin, and load_config refuses to
    # start on a wildcard origin with a token, so by here the origin is absent or safe.
    if config.allowed_origin:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[config.allowed_origin],
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=[_TOKEN_HEADER, _ACTOR_HEADER, "content-type"],
        )

    register_error_handlers(app, audit_log)

    def require_token(
        x_pree_token: str | None = Header(default=None, alias=_TOKEN_HEADER),
    ) -> None:
        """The token gate on every gated route."""
        authorise(config, x_pree_token)

    register_health_routes(app, config, probe_now, require_token)

    register_api_routes(app, store, audit_log, fine, require_token)

    return app
