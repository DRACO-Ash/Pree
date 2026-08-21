"""The application factory.

create_app(deps) wires routes, middleware, and injected dependencies and returns the app
without listening. main.py owns the listener. This split lets the whole HTTP surface be
tested in-process with isolated state and a fixed clock.

The request pipeline is, in order: rate limit, then authentication on cost-incurring and
state-changing routes, then boundary validation of the body, then the handler, then a
generic error response with the detail logged server-side.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .api_models import AssessRequest, AssessResponse, ContributionOut
from .audit import audit, build_logger
from .config import Config
from .health import diagnostics, probe_storage
from .ratelimit import (
    ACTOR_LIMIT,
    ACTOR_WINDOW_SECONDS,
    GLOBAL_LIMIT,
    GLOBAL_WINDOW_SECONDS,
    RateLimiter,
)
from .scoring import ThreatIndicators, assess
from .security import AuthError, authorise, sanitise_actor
from .store import SCHEMA_VERSION, JsonStore

LIVENESS_PATHS = ("/", "/healthz", "/readyz", "/livez", "/ping")
GENERIC_CLIENT_ERROR = "request rejected"
# Header NAMES, not credentials. The value they carry is never held in the source.
_TOKEN_HEADER = "x-pree-token"  # noqa: S105
_ACTOR_HEADER = "x-pree-actor"


def _etag_for(payload: dict[str, Any]) -> str:
    """A strong ETag over the response body. SHA-256 is cryptographic, not a fast digest."""
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f'"{hashlib.sha256(encoded).hexdigest()}"'


def _assessment_key(protected_asset_id: str, candidate_id: str) -> str:
    return f"{protected_asset_id}:{candidate_id}"


def create_app(
    config: Config,
    store: JsonStore,
    *,
    logger: logging.Logger | None = None,
    executor: ThreadPoolExecutor | None = None,
    global_limiter: RateLimiter | None = None,
    actor_limiter: RateLimiter | None = None,
) -> FastAPI:
    """Build the app from injected dependencies. Does not listen."""
    audit_log = logger or build_logger()
    probe_executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="probe")
    coarse = global_limiter or RateLimiter(GLOBAL_LIMIT, GLOBAL_WINDOW_SECONDS)
    fine = actor_limiter or RateLimiter(ACTOR_LIMIT, ACTOR_WINDOW_SECONDS)

    app = FastAPI(
        title="Pree",
        version=__version__,
        description="Confidence-tiered threat scoring for Protect and Defend operators.",
    )

    # CORS is fail-closed: only the configured origin, and never a wildcard alongside a
    # token. config.load_config refuses to start on the unsafe pairings, so by the time the
    # app is built the origin is either absent or safe.
    if config.allowed_origin:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[config.allowed_origin],
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=[_TOKEN_HEADER, _ACTOR_HEADER, "content-type"],
        )

    @app.middleware("http")
    async def coarse_rate_limit(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Tier one: a coarse global limit that protects the process from any caller.

        Liveness paths are exempt. Rate-limiting the platform's own probe would present an
        infrastructure fault as an application failure.
        """
        if request.url.path in LIVENESS_PATHS:
            return await call_next(request)
        if not coarse.allow("global"):
            return JSONResponse(
                {"error": "rate limited"},
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": str(coarse.retry_after_seconds("global"))},
            )
        return await call_next(request)

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

    def require_token(
        x_pree_token: str | None = Header(default=None, alias=_TOKEN_HEADER),
    ) -> None:
        """Tier two of the pipeline: the token gate on every gated route."""
        authorise(config, x_pree_token)

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

    @app.get("/healthz/storage")
    def storage_health(response: Response) -> dict[str, Any]:
        """Prove storage with a real write, racing a hard timeout.

        This is the container HEALTHCHECK target. On failure the 503 body names the resolved
        directory and the exact errno, so a screenshot of it is a full diagnosis.
        """
        probe = probe_storage(config.data_dir, probe_executor)
        if not probe.writable:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return probe.as_body()

    @app.get("/diagnostics")
    def read_diagnostics() -> dict[str, Any]:
        """A secret-free read-out. Each critical input is a boolean and a length only."""
        return diagnostics(config, probe_storage(config.data_dir, probe_executor))

    @app.post("/v1/assess", response_model=AssessResponse, dependencies=[Depends(require_token)])
    def create_assessment(
        payload: AssessRequest,
        response: Response,
        x_pree_actor: str | None = Header(default=None, alias=_ACTOR_HEADER),
    ) -> AssessResponse:
        """Score one candidate against one protected asset, then persist and audit it."""
        actor = sanitise_actor(x_pree_actor)
        if not fine.allow(actor):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate limited",
                headers={"Retry-After": str(fine.retry_after_seconds(actor))},
            )

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
        store.upsert(key, body.model_dump())
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
        if if_none_match == etag:
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
        return record

    return app
