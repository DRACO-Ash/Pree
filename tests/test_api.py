"""The HTTP surface, mounted in-process through the factory with isolated state."""

from __future__ import annotations

import errno
import inspect
import io
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException, Request
from fastapi import applications as fastapi_applications
from fastapi.exceptions import RequestValidationError, WebSocketRequestValidationError
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Route

from pree import __version__
from pree import app as app_module
from pree.app import (
    DOC_PATHS,
    LIVENESS_PATHS,
    MAX_BODY_BYTES,
    MAX_LOGGED_PATH,
    MAX_VALIDATION_ERRORS_LOGGED,
    STORAGE_PROBE_PATH,
    STORE_KEY_PATTERN,
    UNMETERED_PATHS,
    _limit_keys,
    create_app,
)
from pree.audit import build_logger
from pree.health import StorageProber
from pree.main import build
from pree.ratelimit import GLOBAL_LIMIT, RateLimiter
from pree.security import MAX_ACTOR_LENGTH, AuthError, sanitise_actor
from pree.store import JsonStore, StoreError
from tests.conftest import AUTH, PRODUCTION_TOKEN, TEST_TOKEN, build_client, make_config

# Pinned literals, deliberately NOT derived from LIVENESS_PATHS. These five paths are the
# contract in CLAUDE.md and in the deployment sheet; a test that reads them from the constant
# it is checking cannot notice the constant shrinking.
EXPECTED_LIVENESS_PATHS = frozenset({"/", "/healthz", "/readyz", "/livez", "/ping"})
# PINNED literals, like EXPECTED_LIVENESS_PATHS above and for the identical reason. The first
# version of this was `frozenset(UNMETERED_PATHS) | frozenset(DOC_PATHS)`, which derives the
# exemption set from the constants it is policing: appending "/v1/dump" to UNMETERED_PATHS and
# adding an ungated route on it passed 300 of 300, and the same edit took the new path out of the
# coarse rate limiter too, so an unauthenticated dump of the store was unmetered as well. A test
# that reads the exemption from the constant cannot see the exemption widening.
#
# Everything else on the route table is gated, and the tests below walk the table rather than
# listing the routes, because a hand-written list cannot see a route somebody adds.
# `@app.post("/v1/debug")` returning the team token, unauthenticated, passed 292 of 292 tests and
# did not trip the coverage floor: one line, and the shared credential goes to any client on the
# internet with a green gate.
EXPECTED_UNMETERED_PATHS = frozenset(EXPECTED_LIVENESS_PATHS | {"/healthz/storage"})
# The development documentation, served only when PREE_ENV is development. FastAPI registers
# these as plain Starlette routes rather than APIRoutes, and `/docs/oauth2-redirect` is not in
# DOC_PATHS because the CSP exemption does not need it; the route table still carries it.
EXPECTED_DOC_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})
UNAUTHENTICATED_PATHS = EXPECTED_UNMETERED_PATHS | EXPECTED_DOC_PATHS


def test_the_unauthenticated_path_set_is_the_one_the_tests_below_police() -> None:
    """The pinned literals against the shipped constants, so neither can drift unnoticed.

    This is the assertion that makes the walk below meaningful: without it, widening
    UNMETERED_PATHS widens the exemption and every gate test still passes.
    """
    assert frozenset(UNMETERED_PATHS) == EXPECTED_UNMETERED_PATHS, (
        f"UNMETERED_PATHS is {sorted(UNMETERED_PATHS)}; every path there answers without a token "
        "and is exempt from the coarse rate limiter, so a new entry is a deliberate decision"
    )
    assert frozenset(DOC_PATHS) <= EXPECTED_DOC_PATHS, (
        f"DOC_PATHS is {sorted(DOC_PATHS)}, which is not a subset of the documentation paths "
        "this suite expects to answer unauthenticated"
    )


def _all_routes(app: Any) -> list[Any]:
    """EVERY entry in the route table, whatever its type. Used by every walk in this file.

    Every walk, deliberately: the rule this carries was honoured by convention at ten call sites
    and by this helper at six, which meant the helper enforced nothing.

    Not `[r for r in app.routes if isinstance(r, APIRoute)]`, which is what this was. That
    filter made the control written to make routes visible blind to every other registration
    mechanism, and each of these is one line: `app.add_route("/v1/debug", handler)` served the
    team token unauthenticated with 300 of 300 green, and so did `app.mount("/admin", admin)`
    with a gated-looking sub-application. A WebSocketRoute has a path and no methods at all and
    was invisible the same way. FastAPI's own /docs and /openapi.json are plain Starlette
    routes, which is the proof the class is reachable in a single call.
    """
    return list(app.routes)


@contextmanager
def _app_in(env: str) -> Iterator[Any]:
    """A built app for one environment, with throwaway storage, for reading its route table.

    Both environments matter: the documentation routes exist only in development, and asserting
    the table in one environment would leave the other unexamined.
    """
    with tempfile.TemporaryDirectory() as directory:
        overrides: dict[str, object] = {"PREE_ENV": env, "PREE_TEAM_TOKEN": PRODUCTION_TOKEN}
        if env == "production":
            overrides["PREE_ALLOWED_ORIGIN"] = "https://pree.apps.bluestaq.com"
        config = make_config(Path(directory), **overrides)
        prober = StorageProber(cache_seconds=0.0)
        try:
            yield create_app(config, JsonStore(config.data_dir), prober=prober)
        finally:
            prober.shutdown()


def _dependency_names(route: APIRoute) -> set[str]:
    """Every dependency callable in the route's dependant tree, by name.

    The tree, not the top level: a dependency added through a router or a nested Depends is as
    load-bearing as one written on the decorator, and reading only `route.dependencies` would
    miss it.
    """
    names: set[str] = set()
    pending = list(route.dependant.dependencies)
    while pending:
        dependant = pending.pop()
        call = getattr(dependant, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", ""))
        pending.extend(dependant.dependencies)
    return names


# A label that sanitisation visibly changes: newline, braces, quotes and over-length. Using
# an actor that survives sanitisation unchanged is what let the enforcement point go unpinned.
FORGING_ACTOR = 'ops\n{"kind":"audit","actor":"root"}' + "Z" * 500

FULL_BODY = {
    "protected_asset_id": "asset-01",
    "candidate_id": "cand-99",
    "indicators": {
        "closest_approach_km": 5.0,
        "relative_velocity_kms": 0.1,
        "manoeuvres_in_window": 6,
        "baseline_manoeuvres": 1.0,
        "photometric_sigma": 2.5,
        "rf_emissions_detected": True,
    },
}


@pytest.mark.parametrize("path", sorted(EXPECTED_LIVENESS_PATHS))
def test_every_conventional_health_path_returns_200_unauthenticated(
    client: TestClient, path: str
) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_root_is_200_and_not_a_redirect(client: TestClient) -> None:
    """The platform router probes the root, so a 302 there fails the deploy."""
    assert client.get("/", follow_redirects=False).status_code == 200


def test_storage_health_proves_a_real_write_unauthenticated(client: TestClient) -> None:
    body = client.get(STORAGE_PROBE_PATH).json()
    assert body["storage_writable"] is True
    assert body["errno"] is None
    assert body["probe_duration_ms"] < body["probe_timeout_ms"]


def test_diagnostics_is_gated_when_a_token_is_configured(client: TestClient) -> None:
    """The read-out holds no secret value, but the token length narrows a search space."""
    assert client.get("/diagnostics").status_code == 401
    assert client.get("/diagnostics", headers=AUTH).status_code == 200


def test_diagnostics_is_open_before_a_token_exists(open_client: TestClient) -> None:
    """A first deploy needs the read-out most, and has no token to present."""
    assert open_client.get("/diagnostics").status_code == 200


def test_diagnostics_reports_secrets_as_a_boolean_and_a_length_only(
    client: TestClient,
) -> None:
    response = client.get("/diagnostics", headers=AUTH)
    body = response.json()
    assert body["team_token_present"] is True
    assert body["team_token_length"] == len(TEST_TOKEN)
    assert TEST_TOKEN not in response.text
    # The EXACT key set, not a presence check per field. A presence loop cannot see a field that
    # is ADDED, so a new one shipped unasserted, and this route reports lengths and booleans about
    # the credential: exactly the shape where one extra field is the value itself. The impact is
    # bounded, since the caller already holds the token to get here, which is why this was a minor
    # rather than a blocker; the fix is the same either way.
    assert set(body) == {
        "build_id",
        "environment",
        "port",
        "auth_enabled",
        "team_token_present",
        "team_token_length",
        "allowed_origin_present",
        "allowed_origin_is_wildcard",
        "allowed_origin_length",
        "data_dir",
        "data_dir_is_absolute",
        "data_dir_was_configured",
        "storage_writable",
        "storage_errno",
        "storage_errno_name",
        "identity_uid",
        "identity_gid",
        "identity_is_root",
    }, f"diagnostics reports {sorted(body)}"


def test_assess_requires_the_token(client: TestClient) -> None:
    response = client.post("/v1/assess", json=FULL_BODY)
    assert response.status_code == 401
    assert response.json() == {"error": "request rejected"}


def test_a_wrong_token_gets_the_same_generic_error(client: TestClient) -> None:
    response = client.post("/v1/assess", json=FULL_BODY, headers={"x-pree-token": "wrong"})
    assert response.status_code == 401
    assert response.json() == {"error": "request rejected"}


def test_assess_returns_an_explainable_scored_assessment(client: TestClient) -> None:
    response = client.post("/v1/assess", json=FULL_BODY, headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["score"] > 80.0
    assert body["confidence"] == "high"
    assert body["missing_indicators"] == []
    assert len(body["contributions"]) == 5
    assert all(c["rationale"] for c in body["contributions"])


def test_assess_reports_missing_indicators_rather_than_inventing_them(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/assess",
        json={
            "protected_asset_id": "asset-01",
            "candidate_id": "cand-02",
            "indicators": {"closest_approach_km": 12.0},
        },
        headers=AUTH,
    )
    body = response.json()
    assert set(body["missing_indicators"]) == {
        "relative_velocity",
        "manoeuvre_cadence",
        "photometric_anomaly",
        "rf_activity",
    }
    assert body["confidence"] == "low"


def test_assess_persists_the_assessment(client: TestClient) -> None:
    client.post("/v1/assess", json=FULL_BODY, headers=AUTH)
    stored = client.get("/v1/assessments/asset-01:cand-99", headers=AUTH)
    assert stored.status_code == 200
    assert stored.json()["score"] > 80.0


@pytest.mark.parametrize("template", ["{etag}", "W/{etag}", '"other", {etag}', "*"])
def test_if_none_match_is_honoured_for_weak_and_listed_validators(
    client: TestClient, template: str
) -> None:
    """An exact whole-header comparison matched none of these, so every request re-sent."""
    client.post("/v1/assess", json=FULL_BODY, headers=AUTH)
    first = client.get("/v1/assessments/asset-01:cand-99", headers=AUTH)
    etag = first.headers["etag"]
    again = client.get(
        "/v1/assessments/asset-01:cand-99",
        headers={**AUTH, "if-none-match": template.format(etag=etag)},
    )
    assert again.status_code == 304


def test_a_stale_validator_still_returns_the_body(client: TestClient) -> None:
    client.post("/v1/assess", json=FULL_BODY, headers=AUTH)
    again = client.get(
        "/v1/assessments/asset-01:cand-99", headers={**AUTH, "if-none-match": '"stale"'}
    )
    assert again.status_code == 200


def test_an_unknown_assessment_is_a_404(client: TestClient) -> None:
    assert client.get("/v1/assessments/nope:nope", headers=AUTH).status_code == 404


def test_reading_an_assessment_requires_the_token(client: TestClient) -> None:
    assert client.get("/v1/assessments/asset-01:cand-99").status_code == 401


@pytest.mark.parametrize(
    "bad_indicators",
    [
        {"closest_approach_km": -1.0},
        {"relative_velocity_kms": 500.0},
        {"manoeuvres_in_window": -3},
        {"photometric_sigma": 1e9},
        {"unexpected_field": 1},
        {"closest_approach_km": "5"},
        {"manoeuvres_in_window": True},
    ],
)
def test_out_of_range_unknown_or_coercible_input_is_rejected_at_the_boundary(
    client: TestClient, bad_indicators: dict[str, object]
) -> None:
    response = client.post(
        "/v1/assess",
        json={
            "protected_asset_id": "asset-01",
            "candidate_id": "cand-02",
            "indicators": bad_indicators,
        },
        headers=AUTH,
    )
    assert response.status_code == 422
    assert response.json() == {"error": "request rejected"}


def test_an_unknown_top_level_field_is_rejected(client: TestClient) -> None:
    """Covers the outer model's extra="forbid". The earlier case only reached the inner one,
    so the outer control could be deleted with the whole suite staying green."""
    response = client.post(
        "/v1/assess",
        json={
            "protected_asset_id": "asset-01",
            "candidate_id": "cand-02",
            "indicators": {},
            "unexpected_top_level": 1,
        },
        headers=AUTH,
    )
    assert response.status_code == 422


@pytest.mark.parametrize("literal", ["Infinity", "-Infinity", "NaN", "1e400"])
def test_a_non_finite_number_is_rejected_and_never_crashes(
    client: TestClient, literal: str
) -> None:
    """These are non-standard JSON that Python accepts. Left permitted they reached the
    framework's default handler, which cannot serialise them, turning a rejection into a 500."""
    raw = (
        '{"protected_asset_id":"asset-01","candidate_id":"cand-02",'
        f'"indicators":{{"closest_approach_km":{literal}}}}}'
    )
    response = client.post(
        "/v1/assess", content=raw, headers={**AUTH, "content-type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json() == {"error": "request rejected"}


def test_the_validation_error_never_echoes_the_callers_input_back(client: TestClient) -> None:
    marker = "reflect me please"  # invalid: the pattern forbids a space
    response = client.post(
        "/v1/assess",
        json={"protected_asset_id": marker, "candidate_id": "c", "indicators": {}},
        headers=AUTH,
    )
    assert response.status_code == 422
    assert marker not in response.text


def test_a_rejected_body_cannot_write_an_unbounded_audit_line(tmp_path: Path) -> None:
    """The field NAMES in a validation error are caller controlled, so the log line was too.

    A single request carrying one 20,000-character key produced a 20,104-byte audit record, and
    a body under the size cap can carry hundreds of them. Filling the log volume that way is
    cheaper than filling the data volume, and it needs no token.
    """
    stream = io.StringIO()
    logger = build_logger(stream)
    config = make_config(tmp_path)
    with build_client(config, logger, StorageProber(cache_seconds=0.0)) as bounded:
        payload: dict[str, Any] = {"protected_asset_id": 1, "candidate_id": 2}
        # Two shapes at once, because each defeats a different bound. Five 5,000-character
        # keys sit under the 32 KiB body cap, so truncation and not the cap is what stops
        # them; and MANY tiny keys exceed MAX_VALIDATION_ERRORS_LOGGED, so the cap is what
        # stops those. The first version of this test sent only the five, which left the cap
        # unasserted: deleting it wrote a 142,290-byte record with the suite green.
        payload.update({f"k{index}{'x' * 5_000}": 1 for index in range(5)})
        payload.update({f"t{index}": 1 for index in range(MAX_VALIDATION_ERRORS_LOGGED * 20)})
        assert bounded.post("/v1/assess", json=payload, headers=AUTH).status_code == 422

    lines = [line for line in stream.getvalue().splitlines() if "validation_reject" in line]
    assert lines, "the rejection was not audited at all"
    for line in lines:
        assert len(line) < 4096, f"audit line is {len(line)} bytes, unbounded by caller input"
        record = json.loads(line)
        assert len(record["errors"]) == MAX_VALIDATION_ERRORS_LOGGED, (
            f"{len(record['errors'])} errors were logged; the cap is not load-bearing"
        )
        assert record["error_count"] > MAX_VALIDATION_ERRORS_LOGGED, (
            "the request did not produce more errors than the cap, so the cap is untested"
        )
        for item in record["errors"]:
            for part in item["loc"]:
                assert len(part) <= MAX_ACTOR_LENGTH


def test_a_long_request_path_cannot_write_an_unbounded_audit_line(tmp_path: Path) -> None:
    """The cheaper version of the same attack, and the one the first fix missed.

    Bounding the body left the path unbounded, and a path needs no body and no valid token: a
    15,000-character request line wrote a 30,074-byte 401 audit record, because JSON escaping
    of control characters doubles the bytes on the way in. The route is metered, but 240
    requests a window per worker is still megabytes of log a minute from a single address.
    """
    stream = io.StringIO()
    logger = build_logger(stream)
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)
    # Two encodings, because a control character and an astral one cost different numbers of
    # BYTES per character. %01 escapes to six JSON bytes; an emoji arrives percent-decoded and
    # json.dumps renders it as a 12-byte surrogate pair. The first version of this test tried
    # only %01 and asserted a 1,024-byte ceiling that the emoji input already exceeded, so the
    # bound held and the assertion about it did not.
    worst_case = 0
    with build_client(config, logger, StorageProber(cache_seconds=0.0)) as bounded:
        for escaped in ("%01", "%F0%9F%98%80"):
            path = "/v1/assessments/" + escaped * 4_000
            assert bounded.get(path, headers={"x-pree-token": "wrong"}).status_code == 401

    lines = [line for line in stream.getvalue().splitlines() if "auth_reject" in line]
    assert len(lines) == 2, f"expected one audit line per rejection, got {len(lines)}"
    for line in lines:
        worst_case = max(worst_case, len(line))
        # 12 bytes per character is the surrogate-escape ceiling, plus the fixed JSON envelope.
        assert len(line) <= MAX_LOGGED_PATH * 12 + 256, (
            f"audit line is {len(line)} bytes, above the bound the truncation implies"
        )
        assert len(json.loads(line)["path"]) <= MAX_LOGGED_PATH
    # And state the measured figure, so a later change that quietly worsens it is visible.
    assert worst_case > MAX_LOGGED_PATH, "the test never exercised the truncation at all"


def test_every_rejection_uses_one_error_contract_and_is_audited(tmp_path: Path) -> None:
    """One shape for every rejection, and a record of each.

    Two escaped. A 5,000-digit integer exceeds CPython's int_max_str_digits, so json.loads
    raises a plain ValueError rather than a JSONDecodeError and FastAPI answered with its own
    `{"detail": "There was an error parsing the body"}` and wrote no audit line, from a body
    every other malformed value audits. And the per-actor limiter answered `{"detail": ...}`
    while the coarse limiter answered `{"error": ...}`, so two tiers of one control disagreed.
    """
    stream = io.StringIO()
    logger = build_logger(stream)
    config = make_config(tmp_path)
    with build_client(config, logger, StorageProber(cache_seconds=0.0)) as strict:
        huge = (
            '{"protected_asset_id":"a","candidate_id":"b",'
            '"indicators":{"manoeuvres_in_window":' + "9" * 5_000 + "}}"
        )
        parsed = strict.post(
            "/v1/assess", content=huge, headers={**AUTH, "content-type": "application/json"}
        )
        missing = strict.get("/v1/no-such-route", headers=AUTH)

    for response in (parsed, missing):
        assert response.json() == {"error": "request rejected"}, response.text
        assert "detail" not in response.json()
        assert response.headers["content-security-policy"].startswith("default-src 'none'")
    assert parsed.status_code == 400
    assert missing.status_code == 404
    audited = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert len(audited) == 2, f"expected one audit line per rejection, got {audited}"


def test_a_bodiless_status_stays_bodiless(tmp_path: Path) -> None:
    """204 and 304 carry no body, and h11 refuses a Content-Length on them.

    Nothing in the app raises those as exceptions today, so returning JSON for one was a trap
    set for the next handler rather than a live fault: the intended status would have become a
    500. Asserted by raising one directly, because a control with no test is a claim.
    """
    config = make_config(tmp_path)
    logger = build_logger(io.StringIO())
    store = JsonStore(tmp_path / "data")
    store.seed()
    app = create_app(config, store, logger=logger, prober=StorageProber(cache_seconds=0.0))

    @app.get("/test-only/no-content")
    async def _no_content() -> None:
        raise HTTPException(status_code=204)

    with TestClient(app) as client:
        response = client.get("/test-only/no-content", headers=AUTH)

    assert response.status_code == 204
    assert response.content == b"", f"a 204 carried a body: {response.content!r}"
    assert "content-length" not in response.headers


def test_both_rate_limit_tiers_answer_identically_and_keep_retry_after(tmp_path: Path) -> None:
    """The fine tier raised an HTTPException and the coarse tier built a response by hand."""
    config = make_config(tmp_path)
    for kwargs in (
        {"actor_limiter": RateLimiter(1, 60.0)},
        {"global_limiter": RateLimiter(1, 60.0)},
    ):
        with build_client(
            config, build_logger(io.StringIO()), StorageProber(cache_seconds=0.0), **kwargs
        ) as limited:
            body = {"protected_asset_id": "a1", "candidate_id": "b1", "indicators": {}}
            assert limited.post("/v1/assess", json=body, headers=AUTH).status_code == 200
            refused = limited.post("/v1/assess", json=body, headers=AUTH)
        assert refused.status_code == 429, kwargs
        assert refused.json() == {"error": "rate limited"}, (kwargs, refused.text)
        assert int(refused.headers["retry-after"]) >= 1, kwargs


def test_the_factory_installs_the_access_log_filter(tmp_path: Path) -> None:
    """Tie the filter to the app, not just to itself.

    The three filter tests each called `bound_access_log()` first, so they verified the filter
    and never the wiring: replacing the factory's call with `pass` left all 250 tests green.
    That is the same "asserted to exist, never joined to what ships" shape as the suid sweep two
    rounds ago, and the hole it leaves is a 620 MB-a-minute access log.
    """
    access = logging.getLogger("uvicorn.access")
    gunicorn_access = logging.getLogger("gunicorn.access")
    access.filters = []
    gunicorn_access.filters = []

    build_client(
        make_config(tmp_path), build_logger(io.StringIO()), StorageProber(cache_seconds=0.0)
    )

    for logger in (access, gunicorn_access):
        installed = [f for f in logger.filters if type(f).__name__ == "_TruncateRequestPath"]
        assert len(installed) == 1, (
            f"building the app left {len(installed)} truncating filters on {logger.name}; the "
            "access log is unbounded in every worker that imports the factory"
        )


def test_a_trailing_slash_is_a_404_not_a_redirect(client: TestClient) -> None:
    """Starlette's slash redirect ran BEFORE the token gate and named the caller's own host.

    `POST /v1/assess/` answered 307 with `location: http://<caller's Host header>/v1/assess`,
    unauthenticated, and a 307 preserves the method, the body and the headers, so a client that
    follows it re-sends the team token. Measured live with `Host: attacker.test`, the Location
    was `http://attacker.test/v1/assess`. Pinning the forwarded trust list made it worse rather
    than better, because the scheme became unconditionally http, so an operator who typed a
    trailing slash and followed redirects would put the token on the wire in cleartext.
    """
    for path in ("/v1/assess/", "/diagnostics/", "/healthz/storage/"):
        response = client.get(path, headers=AUTH, follow_redirects=False)
        assert response.status_code != 307, (
            f"{path} still redirects to {response.headers.get('location')!r}, before the token "
            "gate and to a host the caller names"
        )
        assert "location" not in response.headers, (
            f"{path} answered {response.status_code} with a Location header"
        )


def _keys_for(peer: str, headers: dict[str, str]) -> tuple[str, ...]:
    """The rate-limit keys for a fabricated request, exercising _limit_keys directly.

    Direct, because the test client's peer address is a constant. An earlier version of the
    test below rotated X-Forwarded-For through the client and asserted a 429, which it got with
    the control removed as well: the peer never varied, so the limiter refused for the wrong
    reason and the assertion said nothing at all.
    """
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 12345),
    }
    return _limit_keys(Request(scope))


def test_a_forwarding_header_cannot_widen_the_rate_limit_key_space() -> None:
    """A forwarding header may only ever ADD a constraint, never move the caller to a new one.

    Three defects in three rounds. The peer address both tiers key on is rewritten above the
    application by uvicorn's proxy-header middleware from a header the caller sends, so rotating
    it minted a fresh bucket per request. Folding on the header's presence stopped that but put
    the request in a DIFFERENT bucket from the peer's own, so a throttled caller escaped by
    adding the header. Charging both keys fixed that, and then the socket key was spelled
    `socket:<ip>` only when a header was present and a bare `<ip>` when it was not: two
    different strings again, so the escape came straight back. Measured: 240 admitted, then 240
    more, and 20 authenticated writes then 20 more against a documented 20.

    The socket key is unconditional now, so the fold can only ever add.
    """
    bare = {_keys_for(f"10.0.0.{n}", {}) for n in range(8)}
    assert len(bare) == 8, f"distinct peers must get distinct buckets, got {bare}"

    for header in ("x-forwarded-for", "forwarded", "x-real-ip", "x-client-ip"):
        for index in range(8):
            without = _keys_for(f"10.0.0.{index}", {})
            with_header = _keys_for(f"10.0.0.{index}", {header: f"203.0.113.{index}"})
            # The SAME socket key both times. This is the assertion the previous version was
            # missing: it compared the folded key across peers and never compared a peer's own
            # key with and without the header.
            assert set(without) <= set(with_header), (
                f"adding {header} changed the peer's own bucket from {without} to "
                f"{with_header}, so the header buys a fresh allowance"
            )
            assert len(with_header) == len(without) + 1, (
                f"adding {header} did not add a constraint: {with_header}"
            )
        folded = {_keys_for(f"10.0.0.{n}", {header: f"203.0.113.{n}"})[-1] for n in range(8)}
        assert folded == {"forwarded"}, f"a rotating {header} varied the folded key: {folded}"


def test_the_storage_probe_publishes_the_data_directory_only_on_failure(
    client: TestClient, tmp_path: Path
) -> None:
    """This path is unauthenticated by design, so its 200 body is public.

    It carried the resolved absolute data directory, publishing the container's filesystem
    layout to anyone who asked, while `/diagnostics` gated the very same field on the reasoning
    that configuration detail narrows an attacker's search space for free. The deployment sheet
    described the directory as appearing in the 503 body, which was true of the sheet and not of
    the code. On failure it earns its place: a screenshot of the 503 is the whole diagnosis.
    """
    ready = client.get(STORAGE_PROBE_PATH)
    assert ready.status_code == 200
    assert "data_dir" not in ready.json(), (
        f"the successful probe publishes the data directory: {ready.json()}"
    )
    assert str(tmp_path) not in ready.text

    # The failing case is built directly from the probe, because a config pointing at an
    # unwritable directory cannot boot a store to hand the factory.
    unwritable = StorageProber(cache_seconds=0.0).probe(Path("/proc/nonexistent-pree"))
    assert unwritable.writable is False
    assert unwritable.as_body()["data_dir"] == "/proc/nonexistent-pree", (
        "the failing probe no longer names the directory, so a screenshot is not a diagnosis"
    )
    assert unwritable.as_body()["errno_name"], "the failing probe names no errno"


def test_every_read_of_the_store_is_audited(tmp_path: Path) -> None:
    """A successful disclosure was the one privileged action with no audit line.

    A refused request was audited, a rejected body was audited, a store failure was audited, and
    a successful read of a record was not. Under the shared-token model "who read what" is the
    only forensic question the trail could answer about a stolen token.
    """
    stream = io.StringIO()
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)
    with build_client(config, build_logger(stream), StorageProber(cache_seconds=0.0)) as audited:
        assert audited.post("/v1/assess", json=FULL_BODY, headers=AUTH).status_code == 200
        key = f"{FULL_BODY['protected_asset_id']}:{FULL_BODY['candidate_id']}"
        hit = audited.get(f"/v1/assessments/{key}", headers=AUTH)
        assert hit.status_code == 200
        audited.get(
            f"/v1/assessments/{key}", headers={**AUTH, "if-none-match": hit.headers["etag"]}
        )
        audited.get("/v1/assessments/nosuch:record", headers=AUTH)

    outcomes = [
        json.loads(line)["outcome"]
        for line in stream.getvalue().splitlines()
        if line.strip() and json.loads(line).get("action") == "read_assessment"
    ]
    assert outcomes == ["disclosed", "not_modified", "not_found"], (
        f"the read path audited {outcomes}; every read must leave a record of its outcome"
    )


def test_a_refused_cors_preflight_uses_the_same_contract_and_is_audited(tmp_path: Path) -> None:
    """Starlette answered a disallowed preflight itself, in plain text and unaudited.

    The body carried no caller input, so this was contract drift rather than a reflection, but
    the control table claimed every rejection used one contract and was audited, and this one
    did neither.
    """
    stream = io.StringIO()
    config = make_config(
        tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN, PREE_ALLOWED_ORIGIN="https://pree.example"
    )
    client = build_client(config, build_logger(stream), StorageProber(cache_seconds=0.0))
    refused = client.options(
        "/v1/assess",
        headers={"Origin": "https://evil.test", "Access-Control-Request-Method": "POST"},
    )
    assert refused.status_code == 400
    assert refused.json() == {"error": "request rejected"}, refused.text
    assert "Disallowed" not in refused.text
    assert any("cors_reject" in line for line in stream.getvalue().splitlines()), (
        "a refused preflight left no audit line"
    )
    # And the allowed origin still works, which is the whole point of the middleware.
    allowed = client.options(
        "/v1/assess",
        headers={"Origin": "https://pree.example", "Access-Control-Request-Method": "POST"},
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://pree.example"

    # A 400 that is NOT a refused preflight keeps its own handling. Rewriting every 400 would
    # have swallowed the parse-failure path, which has its own audit line and its own reason.
    huge = (
        '{"protected_asset_id":"a","candidate_id":"b",'
        '"indicators":{"manoeuvres_in_window":' + "9" * 5_000 + "}}"
    )
    parse_failure = client.post(
        "/v1/assess", content=huge, headers={**AUTH, "content-type": "application/json"}
    )
    assert parse_failure.status_code == 400
    assert parse_failure.json() == {"error": "request rejected"}
    lines = stream.getvalue().splitlines()
    assert any("http_reject" in line for line in lines), (
        "the parse failure lost its own audit line to the CORS normaliser"
    )


def test_a_guessing_run_is_bounded_by_the_ordinary_limiter(tmp_path: Path) -> None:
    """One bucket per peer, and the token plays no part in choosing it.

    Three rounds of splitting the key space made this worse each time. Splitting by the token
    header's PRESENCE let an unauthenticated caller into the operators' budget. Splitting by its
    VALIDITY made refusal an oracle: after saturating the unauthenticated bucket a wrong guess
    returned 429 and the right token 200, so a guessing run read the answer off the status code
    at about 53,000 attempts a minute. Bounding wrong guesses per peer closed that and handed an
    unauthenticated caller a denial of service against every operator, because behind the
    platform ingress they all present one address.

    With one bucket, a saturated peer is refused identically whichever token it holds, which is
    the property the token-length floor is calculated from, and no unauthenticated request can
    refuse an authenticated one that the ordinary limit would have admitted.
    """
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)
    client = build_client(
        config,
        build_logger(io.StringIO()),
        StorageProber(cache_seconds=0.0),
        global_limiter=RateLimiter(6, 60.0),
    )
    for attempt in range(6):
        client.get("/no-such-route", headers={"x-pree-token": f"wrong-{attempt}"})

    wrong = client.get("/no-such-route", headers={"x-pree-token": "wrong-again"}).status_code
    right = client.get("/no-such-route", headers=AUTH).status_code
    assert wrong == right == 429, (
        f"a saturated peer answered {wrong} to a wrong token and {right} to the right one: the "
        f"difference is a guessing oracle"
    )


def test_an_unauthenticated_flood_cannot_refuse_a_request_the_limit_would_admit(
    tmp_path: Path,
) -> None:
    """The attacker-driven denial of service the guessing budget created.

    Twenty wrong tokens from anywhere on the internet locked out every operator for the rest of
    the window, at about a third of a request a second, twelve times cheaper than the coarse
    limit beside it. The previous version of this test flooded ten times against a budget of
    twenty, so it passed while the defect shipped: this one floods well past any per-peer
    failure budget and asserts the authenticated caller is still served.
    """
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)
    client = build_client(
        config,
        build_logger(io.StringIO()),
        StorageProber(cache_seconds=0.0),
        global_limiter=RateLimiter(100_000, 60.0),
    )
    for attempt in range(40):
        client.get("/no-such-route", headers={"x-pree-token": f"guess-{attempt}"})

    assert client.post("/v1/assess", json=FULL_BODY, headers=AUTH).status_code == 200, (
        "40 wrong tokens from one address refused an authenticated request the ordinary limit "
        "would have admitted"
    )
    assert client.get("/diagnostics", headers=AUTH).status_code == 200


def test_a_refused_preflight_is_metered(tmp_path: Path) -> None:
    """CORS answers a preflight itself, so the coarse limiter below never saw one.

    Unmetered, refused preflights wrote 902,000 bytes of log in 1.6 seconds, about 32.9 MB a
    minute per worker, from an unauthenticated caller. That is the exact amplification class the
    bounded audit lines exist to close, on the one path nothing counted, and this round had just
    added a new audit line to it.
    """
    config = make_config(
        tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN, PREE_ALLOWED_ORIGIN="https://pree.example"
    )
    # EVERY path, including the six exempt from the ordinary limiter. Testing only a metered
    # path let the defect ship twice: the metering helper returned early for the exempt paths
    # while the audit line below was written anyway, and 1,200 refused preflights across
    # /healthz, / and /healthz/storage returned 400 each with nothing counting them.
    for path in ("/v1/assess", "/healthz", "/", STORAGE_PROBE_PATH):
        metered = build_client(
            config,
            build_logger(io.StringIO()),
            StorageProber(cache_seconds=0.0),
            global_limiter=RateLimiter(3, 60.0),
        )
        codes = [
            metered.options(
                path,
                headers={"Origin": "https://evil.test", "Access-Control-Request-Method": "POST"},
            ).status_code
            for _ in range(8)
        ]
        assert 429 in codes, f"refused preflights on {path} are not metered: {codes}"


def test_the_frame_guard_is_the_outermost_middleware(tmp_path: Path) -> None:
    """Position IS the control, and two rounds got it wrong in two different places.

    The framing check first lived inside the body-size middleware, after its bodyless-method
    early return, so it never ran for GET. Moved above that return, it still missed every CORS
    preflight, because Starlette answers a preflight inside the CORS middleware without calling
    down and CORS sits above the body-size layer: measured, 200 OK and two responses on one
    connection with a pipelined GET served.

    Only one position cannot be answered above, and this asserts the guard holds it. Everything
    outside it must be a layer that merely decorates a response on the way out, which is the
    hardening headers and nothing else.
    """
    config = make_config(
        tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN, PREE_ALLOWED_ORIGIN="https://pree.example"
    )
    store = JsonStore(tmp_path / "data")
    store.seed()
    app = create_app(
        config, store, logger=build_logger(io.StringIO()), prober=StorageProber(cache_seconds=0.0)
    )
    names = [
        middleware.cls.__name__
        if hasattr(middleware.cls, "__name__")
        else type(middleware.cls).__name__
        for middleware in app.user_middleware
    ]
    # user_middleware is listed OUTERMOST FIRST, and only BaseHTTPMiddleware wrapping
    # security_headers may precede FrameGuard.
    assert "FrameGuard" in names, f"the frame guard is not registered at all: {names}"
    position = names.index("FrameGuard")
    assert position <= 1, (
        f"FrameGuard is at position {position} of the middleware stack, so {names[:position]} "
        f"can answer a request before the framing check runs: {names}"
    )
    # And WHAT sits above it, not merely how many. `position <= 1` passed for any layer at all
    # at position 0, while the comment above claims only the hardening headers may precede it:
    # a middleware that answered before calling down would keep FrameGuard at position 1 and
    # this test green. The one permitted layer is the BaseHTTPMiddleware wrapping
    # security_headers, which only decorates a response on the way out.
    if position == 1:
        outermost = app.user_middleware[0]
        # By NAME, through getattr: Starlette types the middleware factory as a protocol, so
        # an identity check against the class reads as non-overlapping to mypy and made the next
        # assertion unreachable, and a direct __name__ access does not type-check either. The
        # first version of this passed the type checker and asserted nothing.
        assert getattr(outermost.cls, "__name__", "") == "BaseHTTPMiddleware", (
            f"{names[0]} sits outside FrameGuard and is not the hardening-header decorator, so "
            f"it can answer a request before the framing check runs"
        )
        dispatch = outermost.kwargs.get("dispatch") or (
            outermost.args[0] if outermost.args else None
        )
        assert getattr(dispatch, "__name__", "") == "security_headers", (
            f"the outermost layer dispatches {getattr(dispatch, '__name__', dispatch)!r}, not "
            f"security_headers"
        )

    # And behaviourally: an ambiguous frame is refused with the connection closed even on a
    # preflight for the ALLOWED origin, which is the case CORS would otherwise answer itself.
    with TestClient(app) as client:
        refused = client.request(
            "OPTIONS",
            "/v1/assess",
            headers={
                "Origin": "https://pree.example",
                "Access-Control-Request-Method": "POST",
                "transfer-encoding": "chunked",
                "content-length": "6",
            },
        )
    assert refused.status_code == 400, refused.text
    assert refused.headers.get("connection", "").lower() == "close"


@pytest.mark.parametrize(
    "bad_key",
    [
        "no-colon-at-all",
        "a:b:c",
        "a" * 65 + ":b",
        "a:" + "b" * 65,
        "-leading:b",
        "a b:c",
        ":b",
        "a:",
        "a" * 200,
    ],
)
def test_a_malformed_store_key_is_refused_at_the_boundary(client: TestClient, bad_key: str) -> None:
    """The read path's own comment claimed a validator that was not there.

    It said the key "reached here through the path validator, so it is already shape-checked",
    and the parameter carried no pattern and no length bound: the only limit was the truncation
    on the next line, which bounds the audit record and not the lookup. The validator exists
    now, and this asserts it, because a comment asserting a control is exactly what this project
    keeps finding to be false.
    """
    response = client.get(f"/v1/assessments/{bad_key}", headers=AUTH)
    assert response.status_code == 422, f"{bad_key!r} reached the store with {response.status_code}"
    assert response.json() == {"error": "request rejected"}


def test_a_key_containing_a_slash_cannot_reach_the_route_at_all(client: TestClient) -> None:
    """A slash makes it a different path, so the router refuses it before the validator runs.

    Asserted separately rather than folded into the case above: this refusal is a 404 from the
    router, not a 422 from the pattern, and a test that accepted either status would pass with
    the pattern deleted whenever the path merely happened not to match a route.
    """
    for traversal in ("a:/etc/passwd", "../../etc/passwd", "a/b", "a:..%2f..%2fetc"):
        response = client.get(f"/v1/assessments/{traversal}", headers=AUTH)
        assert response.status_code == 404, f"{traversal!r} gave {response.status_code}"
        assert response.json() == {"error": "request rejected"}


def test_a_well_formed_store_key_still_reaches_the_store(client: TestClient) -> None:
    """The pattern must not refuse the keys the write path actually mints."""
    assert client.post("/v1/assess", json=FULL_BODY, headers=AUTH).status_code == 200
    key = f"{FULL_BODY['protected_asset_id']}:{FULL_BODY['candidate_id']}"
    assert client.get(f"/v1/assessments/{key}", headers=AUTH).status_code == 200
    # And the longest legitimate key, two 64-character identifiers, is accepted rather than
    # refused by an off-by-one in the pattern's length bounds.
    longest = "a" * 64 + ":" + "b" * 64
    assert client.get(f"/v1/assessments/{longest}", headers=AUTH).status_code == 404


def test_a_preflight_to_a_probe_path_is_refused_without_an_audit_line(tmp_path: Path) -> None:
    """The contract, but not the audit line, on a path that touches nothing.

    Auditing these was 45% of the bytes an unauthenticated flood wrote, on the channel the
    forensic trail lives in, for an event that reveals nothing: a cross-origin preflight to
    /healthz is refused whoever sends it. Those six paths are exempt so the platform's own
    probes are never throttled, and the platform probes with GET, so the preflight is metered
    like everything else and simply not narrated.
    """
    stream = io.StringIO()
    config = make_config(
        tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN, PREE_ALLOWED_ORIGIN="https://pree.example"
    )
    client = build_client(config, build_logger(stream), StorageProber(cache_seconds=0.0))
    for path in ("/healthz", "/", STORAGE_PROBE_PATH):
        response = client.options(
            path,
            headers={"Origin": "https://evil.test", "Access-Control-Request-Method": "POST"},
        )
        assert response.status_code == 400, f"{path} gave {response.status_code}"
        assert response.json() == {"error": "request rejected"}, response.text
    assert "cors_reject" not in stream.getvalue(), (
        "a preflight to a probe path wrote an audit line, which is the amplification channel"
    )

    # And a preflight to a REAL path still audits, so the exemption is about the path and not
    # about preflights in general.
    metered = client.options(
        "/v1/assess",
        headers={"Origin": "https://evil.test", "Access-Control-Request-Method": "POST"},
    )
    assert metered.status_code == 400
    assert "cors_reject" in stream.getvalue()


def test_a_wrong_method_on_a_probe_path_is_metered_and_not_audited(tmp_path: Path) -> None:
    """The exemption is for the PROBE, not for the path.

    It used to cover the path whatever the method, so any verb other than GET on one of the six
    got a router 405 and a full audit line with nothing counting it: measured, 4,000 requests
    across eight threads, none refused, 624,000 bytes of log in 4.60 seconds, about 8.1 MB a
    minute per worker on the channel the forensic trail lives in. That is the same amplification,
    at the same measured rate, that the previous commit closed for preflights while claiming to
    have closed the last uncounted unauthenticated path.

    HEAD is now a liveness method rather than a 405, because a probe may be configured to use
    it and it was also the cheapest way in.
    """
    stream = io.StringIO()
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)
    client = build_client(
        config,
        build_logger(stream),
        StorageProber(cache_seconds=0.0),
        global_limiter=RateLimiter(4, 60.0),
    )
    codes = [client.request("DELETE", "/healthz").status_code for _ in range(10)]
    assert 429 in codes, f"a wrong method on a probe path is not metered: {codes}"
    assert 405 in codes, f"expected method-not-allowed before the limit bites: {codes}"
    assert "http_reject" not in stream.getvalue(), (
        "a 405 on a probe path wrote an audit line, which is the amplification channel"
    )

    # A 405 on a REAL route is still audited: the method a caller tried is worth knowing there.
    audited = io.StringIO()
    real = build_client(config, build_logger(audited), StorageProber(cache_seconds=0.0))
    assert real.request("DELETE", "/v1/assess", headers=AUTH).status_code == 405
    assert "http_reject" in audited.getvalue()


def test_every_liveness_path_answers_head_as_well_as_get(client: TestClient) -> None:
    """A probe configured with HEAD got a 405, on a path whose whole job is to answer 200."""
    for path in EXPECTED_LIVENESS_PATHS:
        response = client.head(path)
        assert response.status_code == 200, f"HEAD {path} gave {response.status_code}"
        assert response.content == b"", "a HEAD response carried a body"


def test_every_method_not_allowed_names_the_methods_that_are(client: TestClient) -> None:
    """RFC 9110 makes Allow a MUST on a 405, and this asserts the EXACT set on every shape.

    This test is load-bearing, and a previous version of its docstring said it was not. Starlette
    raises the 405 with Allow on the EXCEPTION, so removing `headers=exc.headers` from the
    audit-suppressed branch drops the header on all six probe paths and turns this red. The
    docstring claimed a non-reproduction because the measurement removed the argument from the
    other branch, which serves /diagnostics, and then measured the probe paths.

    The exact set, not a substring: asserting `"GET" in allowed` could not see that splitting GET
    and HEAD into two routes made a liveness path advertise `GET` alone while the resource also
    serves HEAD, because Starlette builds Allow from the matched route's own methods.
    """
    # DERIVED from the route table, not hand-written. The previous version mapped five literal
    # paths to their expected sets, so four registered paths were never checked and a route
    # added later was invisible to it. The table is the fact; the assertion reads the fact.
    checked = 0
    for route in _all_routes(client.app):
        if type(route) is not APIRoute:
            continue
        target = route.path.replace("{key}", "probe:key")
        expected = (route.methods or set()) - {"OPTIONS"}
        # SORTED, so the chosen method is the same on every run. `next(iter(set))` picked a
        # different one each time under string hash randomisation, which makes a failure
        # irreproducible from the seed, and it raised a bare StopIteration rather than a readable
        # assertion if a route ever declared all three.
        candidates = sorted({"DELETE", "PUT", "PATCH"} - expected)
        assert candidates, f"{target} serves every method this test could refuse with"
        refused = candidates[0]
        response = client.request(refused, target, headers=AUTH)
        assert response.status_code == 405, f"{refused} {target} gave {response.status_code}"
        allowed = response.headers.get("allow")
        assert allowed, f"405 on {target} carries no Allow header"
        advertised = {method.strip() for method in allowed.split(",")}
        assert advertised == expected, (
            f"405 on {target} advertises {sorted(advertised)}; the route serves {sorted(expected)}"
        )
        checked += 1
    assert checked >= 9, f"only {checked} routes were checked; the table has more"


def test_the_probe_exemption_is_per_path_and_per_method(tmp_path: Path) -> None:
    """The exemption is for the probe, which means the method the path actually serves.

    HEAD was exempt on every probe path including `/healthz/storage`, which serves only GET, so
    a 405 that can never be a platform probe was unmetered: 600 of 600 admitted, 33,000 bytes of
    access log in 0.62 seconds from an unauthenticated caller. That is the same reasoning used to
    meter preflights on those same six paths one commit earlier, applied inconsistently.
    """
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)
    # HEAD on a LIVENESS path is a real probe: exempt, and 200 however many times it is called.
    live = build_client(
        config,
        build_logger(io.StringIO()),
        StorageProber(cache_seconds=0.0),
        global_limiter=RateLimiter(3, 60.0),
    )
    assert [live.head("/healthz").status_code for _ in range(8)] == [200] * 8

    # HEAD on the STORAGE path is not a probe, because that route serves only GET. It is a 405,
    # and it is metered like any other traffic.
    metered = build_client(
        config,
        build_logger(io.StringIO()),
        StorageProber(cache_seconds=0.0),
        global_limiter=RateLimiter(3, 60.0),
    )
    codes = [metered.head(STORAGE_PROBE_PATH).status_code for _ in range(8)]
    assert 429 in codes, f"HEAD on the storage path is unmetered: {codes}"
    assert 405 in codes, f"expected method-not-allowed before the limit bites: {codes}"


def test_the_body_cap_is_derived_from_the_memory_the_platform_grants() -> None:
    """The cap is only a defence if concurrent worst-case bodies fit inside the request.

    32 KiB looks obviously small in isolation. What matters is the product: the coarse limiter
    admits GLOBAL_LIMIT requests per window, each able to buffer a full body before the token
    gate runs, so the number to bound is MAX_BODY_BYTES x GLOBAL_LIMIT against the memory the
    deployment sheet asks for. Asserting the product ties the three numbers together, so
    raising any one of them in isolation fails here rather than in production.
    """
    sheet = (Path(__file__).resolve().parent.parent / "docs" / "DEPLOYMENT.md").read_text(
        encoding="utf-8"
    )
    row = next(line for line in sheet.splitlines() if line.startswith("| Memory |"))
    stated = re.search(r"(\d+)Mi\b", row)
    assert stated is not None, f"the memory row states no Mi figure to check against: {row!r}"
    requested_mib = int(stated.group(1))

    concurrent_worst_case = MAX_BODY_BYTES * GLOBAL_LIMIT
    ceiling = 16 * 1024 * 1024
    assert concurrent_worst_case <= ceiling, (
        f"an unauthenticated caller can hold {concurrent_worst_case / 1024 / 1024:.1f} MiB of "
        f"request bodies at once, above the {ceiling / 1024 / 1024:.0f} MiB budgeted for them"
    )
    # And the ceiling itself must stay a small fraction of the request, so the interpreter, the
    # two workers and the dataset still fit alongside it.
    assert ceiling * 8 <= requested_mib * 1024 * 1024, (
        f"the {ceiling / 1024 / 1024:.0f} MiB body budget is not a small fraction of the "
        f"{requested_mib}Mi memory request in the deployment sheet"
    )


@pytest.mark.parametrize("bad_id", ["", "a" * 65, "../etc/passwd", "has space", "-leading"])
def test_a_malformed_identifier_is_rejected(client: TestClient, bad_id: str) -> None:
    response = client.post(
        "/v1/assess",
        json={"protected_asset_id": bad_id, "candidate_id": "cand-02", "indicators": {}},
        headers=AUTH,
    )
    assert response.status_code == 422


def test_auth_is_off_when_no_token_is_configured(open_client: TestClient) -> None:
    """Single-user local mode still serves the gated routes, by design."""
    assert open_client.post("/v1/assess", json=FULL_BODY).status_code == 200


@pytest.mark.parametrize("authenticated", [True, False])
def test_an_oversize_body_is_rejected_before_the_token_gate(
    client: TestClient, authenticated: bool
) -> None:
    """The framework buffers the whole body before resolving the token dependency, so without
    a cap an unauthenticated caller can make the process hold an arbitrary payload."""
    payload = {**FULL_BODY, "candidate_id": "cand-01"}
    raw = json.dumps(payload)[:-1] + ',"pad":"' + "x" * (MAX_BODY_BYTES * 2) + '"}'
    headers: dict[str, str] = {"content-type": "application/json"}
    if authenticated:
        headers.update(AUTH)
    response = client.post("/v1/assess", content=raw, headers=headers)
    assert response.status_code == 413
    assert response.json() == {"error": "request rejected"}


def test_an_oversize_streamed_body_with_no_declared_length_is_also_rejected(
    client: TestClient,
) -> None:
    def chunks() -> Any:
        yield b'{"protected_asset_id":"asset-01","candidate_id":"cand-01","indicators":{},"pad":"'
        for _ in range(4):
            yield b"x" * (MAX_BODY_BYTES // 2)
        yield b'"}'

    response = client.post(
        "/v1/assess", content=chunks(), headers={**AUTH, "content-type": "application/json"}
    )
    assert response.status_code == 413


def test_a_body_within_the_cap_still_succeeds(client: TestClient) -> None:
    assert client.post("/v1/assess", json=FULL_BODY, headers=AUTH).status_code == 200


def test_the_expensive_path_is_rate_limited(
    tmp_path: Path, quiet_logger: logging.Logger, prober: StorageProber
) -> None:
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)
    with build_client(config, quiet_logger, prober, actor_limiter=RateLimiter(2, 60.0)) as limited:
        assert limited.post("/v1/assess", json=FULL_BODY, headers=AUTH).status_code == 200
        assert limited.post("/v1/assess", json=FULL_BODY, headers=AUTH).status_code == 200
        third = limited.post("/v1/assess", json=FULL_BODY, headers=AUTH)
    assert third.status_code == 429
    assert int(third.headers["retry-after"]) >= 1


def test_a_spoofed_actor_header_cannot_reset_the_rate_limit(
    tmp_path: Path, quiet_logger: logging.Logger, prober: StorageProber
) -> None:
    """The fine tier was keyed on this caller-supplied header, so a fresh label per request
    bypassed the tier entirely. It is keyed on the peer address now."""
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)
    statuses = []
    with build_client(config, quiet_logger, prober, actor_limiter=RateLimiter(2, 60.0)) as limited:
        for index in range(5):
            response = limited.post(
                "/v1/assess",
                json=FULL_BODY,
                headers={**AUTH, "x-pree-actor": f"spoof-{index}"},
            )
            statuses.append(response.status_code)
    assert statuses == [200, 200, 429, 429, 429]


def test_the_coarse_limit_never_touches_a_platform_probe(
    tmp_path: Path, quiet_logger: logging.Logger, prober: StorageProber
) -> None:
    """Rejected traffic must not be able to turn the container HEALTHCHECK red and restart
    the pod, which is cheaper than attacking the application itself."""
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)
    with build_client(config, quiet_logger, prober, global_limiter=RateLimiter(1, 60.0)) as limited:
        assert limited.get("/v1/assessments/a:b", headers=AUTH).status_code == 404
        assert limited.get("/v1/assessments/a:b", headers=AUTH).status_code == 429
        for path in (*LIVENESS_PATHS, STORAGE_PROBE_PATH):
            assert limited.get(path).status_code in {200, 503}, path


def test_a_failing_store_returns_a_generic_503_and_still_audits_the_action(
    tmp_path: Path, prober: StorageProber
) -> None:
    """A privileged action that failed is still a privileged action."""
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)

    class RefusingStore(JsonStore):
        def upsert(self, key: str, record: dict[str, Any]) -> dict[str, Any]:
            raise StoreError("volume refused the write")

    store = RefusingStore(config.data_dir)
    store.seed()
    buffer = io.StringIO()
    app = create_app(config, store, logger=build_logger(buffer), prober=prober)
    with TestClient(app, raise_server_exceptions=False) as failing:
        response = failing.post(
            "/v1/assess", json=FULL_BODY, headers={**AUTH, "x-pree-actor": FORGING_ACTOR}
        )
    captured = buffer.getvalue()
    assert response.status_code == 503
    assert response.json() == {"error": "could not store the assessment"}
    audit_lines = [
        json.loads(line)
        for line in captured.splitlines()
        if line.startswith("{") and '"kind":"audit"' in line
    ]
    assert audit_lines, f"expected an audit line, got: {captured!r}"
    assert audit_lines[-1]["outcome"] == "error"
    # The SANITISED label, not one that survives sanitisation unchanged. Asserting "ops.lead"
    # here left the handler's sanitise_actor call unpinned: removing it kept the suite green.
    assert audit_lines[-1]["actor"] == sanitise_actor(FORGING_ACTOR)
    assert "\n" not in audit_lines[-1]["actor"]
    assert len(audit_lines[-1]["actor"]) == MAX_ACTOR_LENGTH


def test_cors_allows_only_the_configured_origin(
    tmp_path: Path, quiet_logger: logging.Logger, prober: StorageProber
) -> None:
    config = make_config(
        tmp_path,
        PREE_ENV="production",
        PREE_TEAM_TOKEN=TEST_TOKEN,
        PREE_ALLOWED_ORIGIN="https://pree.apps.bluestaq.com",
    )
    with build_client(config, quiet_logger, prober) as cors_client:
        allowed = cors_client.get("/healthz", headers={"Origin": "https://pree.apps.bluestaq.com"})
        denied = cors_client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert allowed.headers["access-control-allow-origin"] == "https://pree.apps.bluestaq.com"
    assert "access-control-allow-origin" not in denied.headers


def test_a_rate_limited_response_still_carries_cors_headers(
    tmp_path: Path, quiet_logger: logging.Logger, prober: StorageProber
) -> None:
    """CORS must wrap the limiter, or a browser client cannot read the 429 or its Retry-After."""
    origin = "https://pree.apps.bluestaq.com"
    config = make_config(
        tmp_path,
        PREE_ENV="production",
        PREE_TEAM_TOKEN=TEST_TOKEN,
        PREE_ALLOWED_ORIGIN=origin,
    )
    with build_client(config, quiet_logger, prober, global_limiter=RateLimiter(1, 60.0)) as limited:
        limited.get("/v1/assessments/a:b", headers={**AUTH, "Origin": origin})
        blocked = limited.get("/v1/assessments/a:b", headers={**AUTH, "Origin": origin})
    assert blocked.status_code == 429
    assert blocked.headers["access-control-allow-origin"] == origin


def test_the_interactive_docs_are_not_served_in_production(
    tmp_path: Path, quiet_logger: logging.Logger, prober: StorageProber
) -> None:
    """They exposed the whole route table, every field range and the token header name to an
    unauthenticated caller, and /docs loaded a floating-tag CDN script onto the app origin:
    the same origin CORS trusts with credentials."""
    config = make_config(
        tmp_path,
        PREE_ENV="production",
        PREE_TEAM_TOKEN=TEST_TOKEN,
        PREE_ALLOWED_ORIGIN="https://pree.apps.bluestaq.com",
    )
    with build_client(config, quiet_logger, prober) as production:
        for path in ("/openapi.json", "/docs", "/redoc"):
            assert production.get(path).status_code == 404, path
            assert production.get(path, headers=AUTH).status_code == 404, path


def test_the_interactive_docs_remain_available_in_development(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("content-security-policy", "default-src 'none'"),
        ("x-content-type-options", "nosniff"),
        ("x-frame-options", "DENY"),
        ("referrer-policy", "no-referrer"),
        ("cross-origin-opener-policy", "same-origin"),
    ],
)
def test_every_response_carries_the_hardening_headers(
    client: TestClient, header: str, expected: str
) -> None:
    for path in ("/healthz", "/v1/assessments/none:none"):
        response = client.get(path, headers=AUTH)
        assert expected in response.headers[header], f"{path} is missing {header}"


def test_the_hardening_headers_survive_a_rejection(client: TestClient) -> None:
    """A 401 and a 413 are responses too, and are exactly where a missing header matters."""
    rejected = client.post("/v1/assess", json=FULL_BODY)
    assert rejected.status_code == 401
    assert "default-src 'none'" in rejected.headers["content-security-policy"]

    oversize = json.dumps(FULL_BODY)[:-1] + ',"pad":"' + "x" * (MAX_BODY_BYTES * 2) + '"}'
    too_large = client.post(
        "/v1/assess", content=oversize, headers={**AUTH, "content-type": "application/json"}
    )
    assert too_large.status_code == 413
    assert "default-src 'none'" in too_large.headers["content-security-policy"]
    assert too_large.headers["x-content-type-options"] == "nosniff"


def test_a_real_storage_refusal_returns_503_and_audits_the_action(
    tmp_path: Path, prober: StorageProber, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented first-deploy state: a root-owned mount refusing every write.

    Injecting at the filesystem rather than stubbing `upsert` is the point. The store used to
    raise bare OSError from four call sites, so this produced a framework 500 with no audit
    line, while the security policy claimed a generic error and an audit line for this state.
    """
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)
    store = JsonStore(config.data_dir)
    store.seed()
    buffer = io.StringIO()
    app = create_app(config, store, logger=build_logger(buffer), prober=prober)

    def refuse(*_: object, **__: object) -> object:
        raise PermissionError(13, "permission denied")

    with TestClient(app, raise_server_exceptions=False) as failing:
        monkeypatch.setattr(Path, "open", refuse)
        response = failing.post(
            "/v1/assess", json=FULL_BODY, headers={**AUTH, "x-pree-actor": FORGING_ACTOR}
        )
        monkeypatch.undo()
    assert response.status_code == 503
    assert response.json() == {"error": "could not store the assessment"}
    audits = [
        json.loads(line)
        for line in buffer.getvalue().splitlines()
        if line.startswith("{") and '"kind":"audit"' in line
    ]
    assert audits, f"no audit line for a failed privileged action: {buffer.getvalue()!r}"
    assert audits[-1]["outcome"] == "error"
    # Asserting the SANITISED value, not a value sanitisation happens to leave alone. The
    # earlier version used "ops.lead", which passes through unchanged, so removing the
    # sanitiser call from the handler left the whole suite green.
    assert audits[-1]["actor"] == sanitise_actor(FORGING_ACTOR)


def test_the_storage_state_is_logged_once_per_transition_not_once_per_probe(
    tmp_path: Path,
    quiet_logger: logging.Logger,
    prober: StorageProber,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The log-once promise in the factory's docstring, which nothing asserted."""
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)
    with build_client(config, quiet_logger, prober) as probing:
        capsys.readouterr()
        for _ in range(3):
            probing.get(STORAGE_PROBE_PATH)
        printed = capsys.readouterr().out
    transitions = [line for line in printed.splitlines() if line.startswith("pree storage ")]
    assert len(transitions) == 1, f"expected one transition line, got {transitions}"
    assert "pree storage ready" in transitions[0]


def test_the_csp_exemption_cannot_reach_production(
    tmp_path: Path, quiet_logger: logging.Logger, prober: StorageProber
) -> None:
    """The one load-bearing guard in the headers middleware that nothing pinned.

    Removing the production conjunct from the exemption left all tests green. The doc paths do
    not exist in production, so their 404 must still carry the policy.
    """
    config = make_config(
        tmp_path,
        PREE_ENV="production",
        PREE_TEAM_TOKEN=TEST_TOKEN,
        PREE_ALLOWED_ORIGIN="https://pree.apps.bluestaq.com",
    )
    with build_client(config, quiet_logger, prober) as production:
        for path in ("/openapi.json", "/docs", "/redoc"):
            response = production.get(path)
            assert response.status_code == 404, path
            assert "default-src 'none'" in response.headers["content-security-policy"], path


def test_the_diagnostics_read_out_reports_whether_the_data_dir_was_configured(
    client: TestClient,
) -> None:
    body = client.get("/diagnostics", headers=AUTH).json()
    assert body["data_dir_was_configured"] is True


def test_the_liveness_contract_is_exactly_the_five_documented_paths() -> None:
    """Pin the set itself, not just the properties of whatever the set happens to contain.

    Deleting "/readyz" from LIVENESS_PATHS left the whole suite green while GET /readyz
    returned 404, because every liveness assertion, including the threadpool guard, derived
    its expectation from the constant being policed. The path also silently left
    UNMETERED_PATHS, so the rate-limit exemption stopped covering it with nothing turning red.
    """
    assert set(LIVENESS_PATHS) == set(EXPECTED_LIVENESS_PATHS)


def test_the_liveness_routes_never_occupy_the_shared_request_threadpool(
    tmp_path: Path, quiet_logger: logging.Logger, prober: StorageProber
) -> None:
    """Each documented path must exist AND be served by a coroutine.

    A sync handler runs in the shared request threadpool alongside probe callers, each of whom
    holds a worker for up to the probe budget. Measured with the handlers made sync: liveness
    p95 went from 3ms to 1588ms and throughput from 336/s to 1.25/s under a 120-way
    unauthenticated flood of the unmetered probe path, enough to push the platform's liveness
    probe past its timeout and restart the pod.

    Asserted positively, per literal path. A negative assertion over the registered routes was
    satisfied by a route that had gone missing, and by a sub-application mounted at "/", whose
    Mount path Starlette normalises to "" so the walk never saw its handlers.
    """
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)
    store = JsonStore(config.data_dir)
    store.seed()
    app = create_app(config, store, logger=quiet_logger, prober=prober)
    by_path = {
        getattr(route, "path", ""): route
        for route in _all_routes(app)
        if getattr(route, "endpoint", None) is not None
    }
    missing = sorted(p for p in EXPECTED_LIVENESS_PATHS if p not in by_path)
    assert not missing, f"documented liveness paths with no registered endpoint: {missing}"
    synchronous = sorted(
        path
        for path in EXPECTED_LIVENESS_PATHS
        if not inspect.iscoroutinefunction(getattr(by_path[path], "endpoint", None))
    )
    assert not synchronous, (
        f"sync liveness handlers queue behind probe callers in the threadpool: {synchronous}"
    )


def test_every_documented_liveness_path_is_exempt_from_rate_limiting(
    tmp_path: Path, quiet_logger: logging.Logger, prober: StorageProber
) -> None:
    """The exemption is derived from LIVENESS_PATHS, so it shrank silently along with it."""
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)
    with build_client(config, quiet_logger, prober, global_limiter=RateLimiter(1, 60.0)) as limited:
        assert limited.get("/v1/assessments/a:b", headers=AUTH).status_code == 404
        assert limited.get("/v1/assessments/a:b", headers=AUTH).status_code == 429
        for path in sorted(EXPECTED_LIVENESS_PATHS):
            assert limited.get(path).status_code == 200, f"{path} was metered"


def test_the_route_table_holds_nothing_but_api_routes_and_the_documentation() -> None:
    """A registration mechanism this suite cannot read is refused, not tolerated.

    The gate assertions below read an APIRoute's dependant tree, so a route registered any other
    way is invisible to them however carefully they walk the table. Rather than teach them every
    mechanism, the mechanisms are refused: the same reasoning that refuses a heredoc and a SHELL
    instruction in the boot contract instead of parsing them. `app.add_route(...)` and
    `app.mount(...)` each served the team token unauthenticated with 300 of 300 green, and a
    WebSocketRoute carries a path and no methods at all.

    The documentation routes are the one exemption, by PATH rather than by type: FastAPI
    registers them as plain Starlette routes and they exist only when PREE_ENV is development.

    EXACT TYPE, not `isinstance`. A SUBCLASS of APIRoute satisfies isinstance while overriding
    `get_route_handler`, which is the request handler itself: registered through
    `add_api_route(..., route_class_override=SupportRoute)`, a subclass returned the team token to
    any caller sending a chosen header, with 307 of 307 green and 100% coverage. It defeated three
    controls at once. `app.router.route_class` stayed `APIRoute`, because the override is
    per-route. `isinstance` passed, because a subclass is an instance. And `require_token` stayed
    visible in the route's dependant tree, so the gate walk read a correctly gated route while the
    handler wrapped around it ignored the gate. A subclass is a different handler wearing the
    type's name, so the type must match exactly.
    """
    for env, expected_docs in (("development", EXPECTED_DOC_PATHS), ("production", frozenset())):
        with _app_in(env) as app:
            foreign = [
                f"{type(route).__name__} {getattr(route, 'path', route)!r}"
                for route in _all_routes(app)
                if type(route) is not APIRoute
                and not (type(route) is Route and getattr(route, "path", None) in expected_docs)
            ]
            assert not foreign, (
                f"the {env} route table carries entries this suite cannot read the gate from, "
                f"and an unreadable route is an ungated route: {foreign}"
            )
            if env == "production":
                served = {str(getattr(route, "path", "")) for route in _all_routes(app)}
                overlap = served & EXPECTED_DOC_PATHS
                assert not overlap, f"production serves a documentation path: {sorted(overlap)}"


# The middleware stack, outermost first, pinned per environment. A layer is named by its class,
# or by its dispatch function where the class is Starlette's BaseHTTPMiddleware, which is what
# `@app.middleware("http")` registers.
#
# Pinned because a middleware is a registration mechanism the route walks cannot see AT ALL. Nine
# lines below `app.add_middleware(BodySizeLimit)`, a `@app.middleware("http")` returning
# `{"token": config.team_token}` for /v1/debug answered before the router, so it appeared in no
# route table: the categorical route refusal, both gate walks and the token-in-body test each
# iterate `app.routes` and saw nothing, the frame-guard position test stayed green because the
# layer sits inside FrameGuard, and the whole verification loop passed while the production app
# served the shared credential to any unauthenticated caller at `GET /v1/debug`. That is the same
# capability as the route fabrication, through the mechanism this application uses six times.
#
# So the stack is a pinned literal, like the liveness paths and the exemption set. A new layer is
# a deliberate diff a reviewer sees, and the ORDER is part of the pin because it is a security
# property: the hardening headers must wrap every rejection, and the framing guard must sit above
# CORS or a preflight is answered without reaching it.
EXPECTED_MIDDLEWARE = {
    "development": (
        ("BaseHTTPMiddleware", "security_headers"),
        ("FrameGuard", None),
        ("BaseHTTPMiddleware", "coarse_rate_limit"),
        ("BodySizeLimit", None),
    ),
    "production": (
        ("BaseHTTPMiddleware", "security_headers"),
        ("FrameGuard", None),
        ("BaseHTTPMiddleware", "normalise_cors_rejection"),
        ("CORSMiddleware", None),
        ("BaseHTTPMiddleware", "coarse_rate_limit"),
        ("BodySizeLimit", None),
    ),
}


def _middleware_stack(app: Any) -> tuple[tuple[str, str | None], ...]:
    """The registered stack, outermost first, as (class name, dispatch name) pairs."""
    stack: list[tuple[str, str | None]] = []
    for layer in app.user_middleware:
        cls = layer.cls
        options = getattr(layer, "kwargs", None) or getattr(layer, "options", {})
        dispatch = options.get("dispatch") if cls is BaseHTTPMiddleware else None
        stack.append((cls.__name__, getattr(dispatch, "__name__", None)))
    return tuple(stack)


# The exception types with a registered handler. A handler runs INSTEAD of the route, so one that
# returns a body of its own is a request handler that no route table and no middleware pin can
# see: a delegating `@app.exception_handler(404)` returned the team token to an unauthenticated
# caller while every other 404 stayed generic and audited, with the whole loop green.
#
# WebSocketRequestValidationError is Starlette's own default, present because FastAPI registers
# it on every app; HTTPException is the base the StarletteHTTPException handler binds to.
# BY IDENTITY, not by name. The first version pinned bare `__name__` strings, and two different
# exception types can share a name: registering a second, differently-typed `StoreError` left the
# name set exactly equal to the five pinned entries. The type objects themselves cannot collide.
EXPECTED_EXCEPTION_HANDLERS = frozenset(
    {
        StarletteHTTPException,
        RequestValidationError,
        WebSocketRequestValidationError,
        AuthError,
        StoreError,
    }
)


def _handler_keys(app: Any) -> frozenset[Any]:
    return frozenset(app.exception_handlers)


def _handler_labels(keys: frozenset[Any]) -> list[str]:
    """Module-qualified, so a report names the type rather than a name two types can share."""
    return sorted(
        f"{getattr(key, '__module__', '?')}.{getattr(key, '__qualname__', key)}" for key in keys
    )


def test_the_middleware_stack_is_exactly_the_pinned_one() -> None:
    """A layer nobody declared is a layer nothing asserts, and it answers before the router.

    The route table cannot see a middleware, so refusing an unreadable ROUTE closes one
    mechanism and leaves this one open. Both environments, because the CORS pair exists only
    when an origin is configured and pinning one environment leaves the other unexamined.
    """
    for env, expected in EXPECTED_MIDDLEWARE.items():
        with _app_in(env) as app:
            found = _middleware_stack(app)
            assert found == expected, (
                f"the {env} middleware stack is {found}, not the pinned {expected}. A layer "
                "answers before the router and appears in no route table, so an undeclared one "
                "is an ungated request handler"
            )


# The ORDERED inventory of every route on the app that LISTENS, per environment. Type name, path,
# methods, the endpoint's module and qualified name, and whether the token gate is in its dependant
# tree.
#
# A tuple, not a frozenset, and every route rather than the APIRoutes. Four one-region edits in
# `main.py` after the factory returns each served the team token to an unauthenticated caller in
# production with 310 of 310 green, and each defeated a different property of the previous version:
#
# ● `route.dependant.call = leak` on /healthz. FastAPI executes `dependant.call`, and the pin read
#   `route.endpoint.__qualname__`, so what runs and what is pinned were two different attributes.
# ● an endpoint substituted with `__qualname__` and `__module__` assigned to match. A label is a
#   string an attacker can set; identity is not.
# ● `app.add_route("/openapi.json", leak)` in production, where no documentation path may exist.
#   The inventory covered APIRoutes only, and the "production serves no documentation path"
#   assertion read a factory-built app rather than the listener.
# ● a duplicate, tuple-identical /healthz APIRoute inserted at index 0. A frozenset deduped it, and
#   nothing pinned the count. Starlette matches the FIRST route, so the duplicate answered.
#
# Every one of those needed no gate bypass at all: they sit on paths that answer without a token,
# where the inventory was the only control.
_LIVENESS = ("pree.app", "register_health_routes.<locals>.liveness")
EXPECTED_SERVED_ROUTES: dict[str, tuple[tuple[Any, ...], ...]] = {
    "development": (
        ("APIRoute", "/", ("GET", "HEAD"), *_LIVENESS, False),
        ("APIRoute", "/healthz", ("GET", "HEAD"), *_LIVENESS, False),
        ("APIRoute", "/readyz", ("GET", "HEAD"), *_LIVENESS, False),
        ("APIRoute", "/livez", ("GET", "HEAD"), *_LIVENESS, False),
        ("APIRoute", "/ping", ("GET", "HEAD"), *_LIVENESS, False),
        (
            "APIRoute",
            "/healthz/storage",
            ("GET",),
            "pree.app",
            "register_health_routes.<locals>.storage_health",
            False,
        ),
        (
            "APIRoute",
            "/diagnostics",
            ("GET",),
            "pree.app",
            "register_health_routes.<locals>.read_diagnostics",
            True,
        ),
        (
            "APIRoute",
            "/v1/assess",
            ("POST",),
            "pree.app",
            "register_api_routes.<locals>.create_assessment",
            True,
        ),
        (
            "APIRoute",
            "/v1/assessments/{key}",
            ("GET",),
            "pree.app",
            "register_api_routes.<locals>.read_assessment",
            True,
        ),
    ),
}
# The four FastAPI documentation routes are NOT in this table, deliberately. They were, by their
# `FastAPI.setup.<locals>.*` closure qualnames, which pins four strings from inside
# `fastapi/applications.py` on one FastAPI version: a routine dependency bump would then print two
# thirteen-row tuples for what may be a one-string change, and read to whoever did not write it as
# a compromise rather than an upgrade. They are asserted STRUCTURALLY instead, below: exactly
# `Route`, path in `EXPECTED_DOC_PATHS`, and an endpoint whose CODE comes from FastAPI's own file.
# The third property is the load-bearing one: `gated` is computed only for an APIRoute, so it is
# unconditionally False for a plain Route and asserts nothing, and `__module__` is an assignable
# string that a forged endpoint simply set. What those paths serve is checked for the header
# channel and for token absence at `test_the_unauthenticated_paths_on_the_listener_disclose_nothing`
# and NOT for a body shape, because they serve HTML by design; an earlier version of this comment
# claimed the body was pinned exactly, and it was not.
EXPECTED_SERVED_ROUTES["production"] = EXPECTED_SERVED_ROUTES["development"]

_STARLETTE_ROUTE_APP = "request_response.<locals>.app"


def _endpoint_origin(endpoint: Any) -> str:
    """Where an endpoint's CODE actually comes from, by code-object filename.

    Not `__module__`, which is an assignable string: a forged `Route("/redoc", leak)` with
    `leak.__module__ = "fastapi.applications"` landed in the framework branch of the inventory and
    dumped the whole store as HTML to an unauthenticated caller with the suite green. A code
    object's `co_filename` cannot be reassigned, and it survives a dependency bump, which is the
    property the structural assertion needs.
    """
    code = getattr(endpoint, "__code__", None)
    filename = getattr(code, "co_filename", "")
    if filename == inspect.getsourcefile(app_module):
        return "pree.app"
    if filename == inspect.getsourcefile(fastapi_applications):
        return "fastapi.applications"
    return f"unrecognised:{filename}"


def _served_inventory(app: Any) -> tuple[tuple[Any, ...], ...]:
    """Every route in table ORDER, as the tuples EXPECTED_SERVED_ROUTES pins."""
    rows: list[tuple[Any, ...]] = []
    for route in _all_routes(app):
        endpoint = getattr(route, "endpoint", None)
        rows.append(
            (
                type(route).__name__,
                getattr(route, "path", None),
                tuple(sorted((getattr(route, "methods", None) or set()) - {"OPTIONS"})),
                _endpoint_origin(endpoint),
                getattr(endpoint, "__qualname__", None),
                type(route) is APIRoute and "require_token" in _dependency_names(route),
            )
        )
    return tuple(rows)


@contextmanager
def _listener(env: str, directory: Path) -> Iterator[Any]:
    """The app `build()` returns, which is what gunicorn launches, per environment."""
    previous = dict(os.environ)
    os.environ.update(
        {
            "PREE_ENV": env,
            "PREE_DATA_DIR": str(directory / f"listener-{env}"),
            "PREE_BUILD_ID": "listener-pin",
        }
    )
    # A token in BOTH environments, because these tests assert the gate and the gate is what a
    # token turns on. Development with NO token is single-user local mode, open by design and
    # covered by the `open_client` fixture's own tests; asserting 401 there would be asserting
    # against the documented contract rather than for it.
    os.environ["PREE_TEAM_TOKEN"] = PRODUCTION_TOKEN
    if env == "production":
        os.environ["PREE_ALLOWED_ORIGIN"] = "https://pree.apps.bluestaq.com"
    else:
        os.environ.pop("PREE_ALLOWED_ORIGIN", None)
    try:
        yield build()
    finally:
        os.environ.clear()
        os.environ.update(previous)


def test_the_listener_serves_exactly_the_pinned_route_inventory(tmp_path: Path) -> None:
    """Asserted on `build()`, because `build()` is what gunicorn launches.

    Every gate control used to read a `create_app` app, and the one test that read `build()`
    checked types only. So `app.add_api_route("/v1/support", support, methods=["GET"])` in
    `main.py`, after the factory returns, served the team token to an unauthenticated caller in
    production configuration with 307 of 307 green, confirmed against a real listener over the
    wire. Pinning types on the listener while asserting the gate elsewhere left the gate
    unasserted on the thing that runs.
    """
    for env in ("development", "production"):
        with _listener(env, tmp_path) as app:
            # This project's OWN routes, exactly and in order.
            inventory = tuple(row for row in _served_inventory(app) if row[3] == "pree.app")
            expected = EXPECTED_SERVED_ROUTES[env]
            assert inventory == expected, (
                f"the {env} listener serves a different route table than the pinned one.\n"
                f"served:   {inventory}\nexpected: {expected}"
            )
            # And FastAPI's documentation routes, structurally: exactly `Route`, on a pinned path,
            # ungated. Nothing else may be in the table at all.
            foreign = [row for row in _served_inventory(app) if row[3] != "pree.app"]
            for kind, path, _methods, origin, _qualname, _gated in foreign:
                # The ORIGIN is asserted, not merely used to partition. It was computed and then
                # never compared, so a `Route("/redoc", leak)` defined in main.py landed in the
                # `unrecognised:` bucket and satisfied every property that WAS checked: `gated` is
                # computed only for an APIRoute and is unconditionally False for a plain Route, so
                # two of the three asserted nothing at all, and the route served the whole
                # assessment store as HTML to an unauthenticated caller.
                assert (kind, path in EXPECTED_DOC_PATHS, origin) == (
                    "Route",
                    True,
                    "fastapi.applications",
                ), f"the {env} listener carries a route this suite cannot account for: {path!r}"
            if env == "production":
                assert not foreign, f"production serves routes outside pree.app: {foreign}"
            else:
                assert len(foreign) == len(EXPECTED_DOC_PATHS), (
                    f"development serves {len(foreign)} framework routes, not "
                    f"{len(EXPECTED_DOC_PATHS)}: {foreign}"
                )
            # The EXECUTED callable, by identity. FastAPI runs `dependant.call`, and pinning the
            # endpoint's NAME let `route.dependant.call = leak` change what runs while every
            # pinned field stayed correct. A name is a string an attacker can assign; identity is
            # not, and neither is the file the code object came from.
            forged = [
                f"{route.path}: dependant.call={getattr(route.dependant.call, '__qualname__', '?')}"
                for route in _all_routes(app)
                if type(route) is APIRoute and route.dependant.call is not route.endpoint
            ]
            assert not forged, (
                f"the {env} listener executes a callable that is not the route's own endpoint, so "
                f"the pinned inventory names something that does not run: {forged}"
            )
            # And the endpoint's code must come from the reviewed module, not merely claim to.
            source = inspect.getsourcefile(app_module)
            outside = [
                f"{route.path}: {route.endpoint.__code__.co_filename}"
                for route in _all_routes(app)
                if type(route) is APIRoute and route.endpoint.__code__.co_filename != source
            ]
            assert not outside, (
                f"the {env} listener serves an endpoint defined outside {source}: {outside}"
            )
            # EVERY route, not only an APIRoute. The exclusion meant a plain Route's callable was
            # never examined, which is the other half of what let the forged /redoc through.
            swapped = [
                f"{getattr(route, 'path', route)}"
                for route in _all_routes(app)
                if getattr(getattr(route, "app", None), "__qualname__", None)
                != _STARLETTE_ROUTE_APP
            ]
            assert not swapped, (
                f"the {env} listener has routes whose ASGI callable is not Starlette's own "
                f"wrapper, so the handler that runs is not the endpoint the inventory names: "
                f"{swapped}"
            )
            if env == "production":
                served = {str(getattr(route, "path", "")) for route in _all_routes(app)}
                overlap = served & EXPECTED_DOC_PATHS
                assert not overlap, (
                    f"the production LISTENER serves a documentation path: {sorted(overlap)}. The "
                    "factory-level assertion does not reach main.py, so a plain Route squatting "
                    "/openapi.json passed it"
                )


def test_the_listener_refuses_every_unauthenticated_caller_outside_the_probe_set(
    tmp_path: Path,
) -> None:
    """The behavioural half, on the LISTENER rather than on a fixture-built app.

    Asking is the check that needs no knowledge of how a route was registered or wrapped, and
    running it against `build()` closes the gap between what is pinned and what is exercised.
    """
    for env in ("development", "production"):
        with _listener(env, tmp_path) as app:
            answered: list[str] = []
            with TestClient(app) as probe:
                for route in _all_routes(app):
                    path = getattr(route, "path", None)
                    if path is None or path in UNAUTHENTICATED_PATHS:
                        continue
                    target = path.replace("{key}", "probe:key")
                    methods = (getattr(route, "methods", None) or set()) - {"HEAD", "OPTIONS"}
                    for method in sorted(methods):
                        code = probe.request(method, target, json={}).status_code
                        if code != 401:
                            answered.append(f"{env}: {method} {target} -> {code}")
            assert not answered, (
                f"a gated route on the listener answered without a token: {answered}"
            )


# EVERY header value, pinned. Not a permitted-name list: that was the control here for two rounds
# and it lost twice to the same evasion, because a name list cannot see what a value carries.
# `vary: base64(token)` used a permitted name on every non-probe path and served the production
# credential to unauthenticated 401s and 404s with the suite green.
#
# So each value is an exact literal, a pattern, or a method list, and anything left over must equal
# the hardening set exactly. A credential can only live in a value, and there is no longer a value
# that is merely present.
HARDENING_HEADERS = {
    "content-security-policy": (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
}
PERMITTED_CONTENT_TYPES = frozenset(
    {"application/json", "text/plain; charset=utf-8", "text/html; charset=utf-8"}
)
# The development documentation pages, which legitimately load their own script and style and are
# therefore exempt from the Content-Security-Policy by design. They do not exist in production, so
# the exemption cannot reach a deployed app. Named here because this pin found the exemption on its
# first run and an exemption that is not named is an exemption nobody can audit.
#
# The exemption follows `DOC_PATHS`, which is THREE paths: `/docs/oauth2-redirect` is served by
# FastAPI and is not in that constant, so it keeps the full hardening set. The pin found that
# distinction too, and it is the reason the flag is keyed on `DOC_PATHS` rather than on "looks like
# a documentation page".
DOC_HEADERS = {
    name: value for name, value in HARDENING_HEADERS.items() if name != "content-security-policy"
}
# The conditional-read pair. The ETag is a SHA-256 over the record, so its shape is pinned rather
# than its value; 64 hex characters cannot encode a token.
ETAG_PATTERN = re.compile(r'^"[0-9a-f]{64}"$')
CACHE_CONTROL = "private, max-age=0, must-revalidate"
HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
# The CORS set, by exact value. `vary` is here because it is a CORS header and because pinning it
# to "Origin" is what closes the evasion above.
CORS_HEADER_VALUES = {
    "vary": "Origin",
    "access-control-allow-credentials": "true",
    "access-control-max-age": "600",
}


def _header_findings(
    response: Any, secret: str, where: str, origin: str = "", *, docs: bool = False
) -> list[str]:
    """Every complaint about a response's headers: a bad value, or the secret in one."""
    actual = {
        name.lower(): value
        for name, value in response.headers.items()
        # content-length follows the body, and the body is pinned separately. Excluding it is
        # not a disclosure channel: h11 refuses a non-numeric or mismatched length on the wire,
        # so a header carrying a secret there is a response no client ever receives. What it
        # WOULD hide is an unservable probe response, which the container's own health check
        # catches behaviourally.
        if name.lower() != "content-length"
    }
    complaints = [
        f"{where}: header {name!r} carries the credential"
        for name, value in actual.items()
        if secret and secret in value
    ]
    remainder = dict(actual)

    content_type = remainder.pop("content-type", None)
    if content_type is not None and content_type not in PERMITTED_CONTENT_TYPES:
        complaints.append(f"{where}: content-type is {content_type!r}")

    allowed = remainder.pop("allow", None)
    if allowed is not None:
        advertised = {method.strip() for method in allowed.split(",")}
        if not advertised or advertised - HTTP_METHODS:
            complaints.append(f"{where}: Allow advertises {allowed!r}")

    etag = remainder.pop("etag", None)
    if etag is not None and not ETAG_PATTERN.match(etag):
        complaints.append(f"{where}: ETag is {etag!r}, not a SHA-256 in quotes")

    cache = remainder.pop("cache-control", None)
    if cache is not None and cache != CACHE_CONTROL:
        complaints.append(f"{where}: cache-control is {cache!r}")

    for name, expected in CORS_HEADER_VALUES.items():
        value = remainder.pop(name, None)
        if value is not None and value != expected:
            complaints.append(f"{where}: {name} is {value!r}, not {expected!r}")

    echoed = remainder.pop("access-control-allow-origin", None)
    if echoed is not None and echoed != origin:
        complaints.append(f"{where}: allow-origin echoes {echoed!r}, not {origin!r}")
    for name in ("access-control-allow-methods", "access-control-allow-headers"):
        value = remainder.pop(name, None)
        if value is not None and (
            not value or any(part.strip() == "" for part in value.split(","))
        ):
            complaints.append(f"{where}: {name} is {value!r}")

    pinned: dict[str, str] = DOC_HEADERS if docs else HARDENING_HEADERS
    if remainder != pinned:
        complaints.append(f"{where}: unpinned headers {remainder}")
    return complaints


def test_the_unauthenticated_paths_on_the_listener_disclose_nothing(tmp_path: Path) -> None:
    """The behavioural half for the paths that ANSWER without a token, which had none.

    Every route walk skips these paths, so the pinned inventory was the only control on them, and
    four fabrications that beat that inventory sat here: they needed no gate bypass because these
    paths are meant to answer. Asking is what a label cannot fake.

    The body is pinned EXACTLY, not by key set. Pinning the keys and grepping the text for the raw
    token let `service = "pree-" + base64(token)` disclose the credential on all five liveness
    paths with the suite green: a value inside a pinned key is invisible to a key-set assertion and
    to a substring search at once. An exact body is the only form that holds, and it is also the
    control that makes the identity and provenance checks above worth having, since a forged
    `co_filename` satisfied those five assertions simultaneously.

    Redirects are NOT followed. `TestClient` follows by default, so a 307 out of a liveness handler
    with the token in `Location` reported as a 200 and passed, the only tell being two uncovered
    statements against an 80% floor.
    """
    for env in ("development", "production"):
        with _listener(env, tmp_path) as app:
            complaints: list[str] = []
            with TestClient(app) as probe:
                served = {str(getattr(route, "path", "")) for route in _all_routes(app)}
                # The PROBE paths, whose bodies are JSON and pinned. The documentation paths are
                # also unauthenticated, and they serve HTML, so they are checked separately below
                # for the header channel and for token absence but not for a body shape.
                for path in sorted(EXPECTED_UNMETERED_PATHS & served):
                    methods = ("GET", "HEAD") if path in EXPECTED_LIVENESS_PATHS else ("GET",)
                    for method in methods:
                        where = f"{env}: {method} {path}"
                        response = probe.request(method, path, follow_redirects=False)
                        assert response.status_code == 200, (
                            f"{where} gave {response.status_code}; an unauthenticated probe path "
                            "answers 200 and does not redirect"
                        )
                        assert "location" not in response.headers, (
                            f"{where} carries a Location header, which is a disclosure channel a "
                            "followed redirect hides"
                        )
                        complaints += _header_findings(response, PRODUCTION_TOKEN, where)
                        if method == "HEAD":
                            assert response.content == b"", f"{where} carried a body"
                            continue
                        if path in EXPECTED_LIVENESS_PATHS:
                            assert response.json() == {
                                "status": "ok",
                                "service": "pree",
                                "version": __version__,
                            }, f"{where} body is {response.json()}"
                        else:
                            # EXACTLY, apart from the measured duration. The key set alone let
                            # `errno_name = base64(token)` disclose the credential on this
                            # unauthenticated path with the suite green: the liveness body next
                            # to it was pinned exactly in the same commit and this one was not.
                            body = response.json()
                            duration = body.pop("probe_duration_ms", None)
                            assert body == {
                                "status": "ready",
                                "storage_writable": True,
                                "errno": None,
                                "errno_name": None,
                                "probe_timeout_ms": 1500,
                            }, f"{where} body is {body}"
                            assert isinstance(duration, int) and 0 <= duration < 1500, (
                                f"{where} reports a probe duration of {duration}"
                            )
                        assert PRODUCTION_TOKEN not in response.text, (
                            f"{where} body carries the team token"
                        )
                # The documentation pages, which exist only in development. HTML by design, so no
                # body shape is pinned; the header channel and the credential are.
                for path in sorted(EXPECTED_DOC_PATHS & served):
                    where = f"{env}: GET {path}"
                    response = probe.get(path, follow_redirects=False)
                    assert response.status_code == 200, f"{where} gave {response.status_code}"
                    complaints += _header_findings(
                        response, PRODUCTION_TOKEN, where, docs=path in DOC_PATHS
                    )
                    assert PRODUCTION_TOKEN not in response.text, (
                        f"{where} body carries the team token"
                    )
            assert not complaints, f"header findings: {complaints}"


def test_the_storage_failure_body_discloses_the_errno_and_nothing_else(tmp_path: Path) -> None:
    """The 503 branch, which no test had ever reached.

    Storage is writable under test, so `/healthz/storage` was only ever seen at 200 and the failure
    branch of `as_body()` was an unasserted disclosure surface on an unauthenticated path: one added
    key returned the raw token to any caller with the suite green and zero statement misses. The
    directory appears here deliberately, because a screenshot of this 503 is meant to be a complete
    diagnosis, so the key set is pinned WITH `data_dir` rather than against it.
    """
    unwritable = tmp_path / "sealed"
    unwritable.mkdir()
    blocker = unwritable / "data"
    blocker.write_text("not a directory\n", encoding="utf-8")
    previous = dict(os.environ)
    os.environ.update(
        {
            "PREE_ENV": "production",
            "PREE_DATA_DIR": str(blocker / "nested"),
            "PREE_BUILD_ID": "storage-failure",
            "PREE_TEAM_TOKEN": PRODUCTION_TOKEN,
            "PREE_ALLOWED_ORIGIN": "https://pree.apps.bluestaq.com",
        }
    )
    try:
        app = build()
    finally:
        os.environ.clear()
        os.environ.update(previous)
    with TestClient(app) as probe:
        response = probe.get(STORAGE_PROBE_PATH, follow_redirects=False)
    assert response.status_code == 503, (
        f"a data directory under a regular file answered {response.status_code}; the failure "
        "branch of the storage body is unreachable and therefore unasserted"
    )
    body = response.json()
    duration = body.pop("probe_duration_ms", None)
    assert body == {
        "status": "unready",
        "storage_writable": False,
        "errno": errno.ENOTDIR,
        "errno_name": "ENOTDIR",
        "probe_timeout_ms": 1500,
        "data_dir": str(blocker / "nested"),
    }, f"the 503 body is {body}"
    assert isinstance(duration, int) and 0 <= duration < 1500, (
        f"the 503 reports a probe duration of {duration}"
    )
    assert PRODUCTION_TOKEN not in response.text, "the 503 body carries the team token"
    assert not _header_findings(response, PRODUCTION_TOKEN, "storage 503")


def test_every_request_handling_surface_of_the_built_app_is_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FOUR surfaces, and the middleware pin covered one of them.

    `app.user_middleware` is not the only way to handle a request. Each of these was measured
    with the whole verification loop green, and each served the team token to an unauthenticated
    caller or granted one full access:

    ● a middleware added in `main.py` AFTER the factory returns, which the factory's own pin
      cannot see because it asserts on `create_app`'s output and gunicorn launches `build()`;
    ● a delegating `@app.exception_handler(404)`, which runs instead of the route;
    ● `FastAPI(dependencies=[...])`, a router-level dependency that stamped the token into a
      response header on unauthenticated `/healthz`;
    ● `app.router.route_class`, which forged the token header on every request, so `/diagnostics`
      and `/v1/assess` both opened while `require_token` stayed visible in every route's
      dependant tree and the gate walk saw nothing wrong.

    So the assertion is made on what LISTENS, not on what the factory returns.
    """
    # The real listener, built through the real environment, with storage pointed at a
    # throwaway directory so the call cannot write into the repository.
    monkeypatch.setenv("PREE_ENV", "development")
    monkeypatch.setenv("PREE_DATA_DIR", str(tmp_path / "listener"))
    monkeypatch.setenv("PREE_BUILD_ID", "surface-pin")
    monkeypatch.delenv("PREE_TEAM_TOKEN", raising=False)
    monkeypatch.delenv("PREE_ALLOWED_ORIGIN", raising=False)

    for env, expected in EXPECTED_MIDDLEWARE.items():
        with _app_in(env) as factory_app:
            surfaces = (("create_app", factory_app),) + (
                (("build", build()),) if env == "development" else ()
            )
            for name, app in surfaces:
                assert _middleware_stack(app) == expected, (
                    f"{name} in {env} has middleware {_middleware_stack(app)}, not {expected}"
                )
                assert _handler_keys(app) == EXPECTED_EXCEPTION_HANDLERS, (
                    f"{name} registers exception handlers "
                    f"{_handler_labels(_handler_keys(app))}, not "
                    f"{_handler_labels(EXPECTED_EXCEPTION_HANDLERS)}. A handler runs instead of "
                    "the route and appears in no route table"
                )
                assert app.router.route_class is APIRoute, (
                    f"{name} uses route class {app.router.route_class.__name__}; a custom route "
                    "class rewrites every request before the gate reads it, and leaves the gate "
                    "visible in the dependant tree while it does"
                )
                # And PER ROUTE, by exact type. The router-level assertion above reads the
                # DEFAULT factory only, so `add_api_route(..., route_class_override=SupportRoute)`
                # left it saying APIRoute while one route's handler forged the token header. Every
                # route's type is checked, because one is enough.
                subclassed = [
                    f"{type(route).__name__} {getattr(route, 'path', route)!r}"
                    for route in _all_routes(app)
                    if type(route) is not APIRoute
                    # A plain Route is permitted only at a DOCUMENTATION path. This clause used
                    # to accept `type(route) is Route` anywhere, so `app.add_route(...)` on any
                    # path satisfied it.
                    and not (
                        type(route) is Route and getattr(route, "path", None) in EXPECTED_DOC_PATHS
                    )
                ]
                assert not subclassed, (
                    f"{name} carries routes whose type is neither APIRoute nor Route: "
                    f"{subclassed}. An APIRoute SUBCLASS overrides get_route_handler, which is "
                    "the request handler, and satisfies every isinstance check while doing it"
                )
                assert app.router.dependencies == [], (
                    f"{name} carries router-level dependencies {app.router.dependencies}, which "
                    "run on every route including the unauthenticated probes"
                )
                assert app.dependency_overrides == {}, (
                    f"{name} carries dependency overrides {app.dependency_overrides}, which can "
                    "replace the token gate with anything at all"
                )


def test_every_route_outside_the_probe_set_carries_the_token_gate(client: TestClient) -> None:
    """Walked from the route table, and paired with the refusal above so the walk is complete.

    Gating used to be asserted route by route, by hand, and nothing looked at the table. This
    reads every APIRoute; the test above guarantees there is nothing else to read. The
    complement matters as much as the rule: a probe path that GAINS the gate would fail the
    platform's liveness check and restart the pod, so both directions are asserted here.
    """
    checked = 0
    for route in _all_routes(client.app):
        if type(route) is not APIRoute:
            continue
        gated = "require_token" in _dependency_names(route)
        if route.path in UNAUTHENTICATED_PATHS:
            assert not gated, (
                f"{route.path} is a probe or documentation path and must answer without a "
                "token; gating it turns a platform probe into a restart loop"
            )
            continue
        assert gated, (
            f"{route.path} carries no token gate. Every route outside "
            f"{sorted(UNAUTHENTICATED_PATHS)} is gated; its dependencies are "
            f"{sorted(_dependency_names(route))}"
        )
        checked += 1
    assert checked == 3, f"expected three gated routes, walked {checked}"


# The fields each audit record kind may carry. Pinned because nothing pinned them: a record is a
# disclosure channel with no body and no header, and `token=config.team_token` added to the success
# audit call wrote the shared credential into the pod log store on every write with the suite green.
# Every string-valued audit field, by exact set or bounded pattern. A number cannot carry a base64
# credential; a string can, which is why these and not the numeric fields.
class _ScrubIdempotent:
    """Matches a value only if the shipped sanitiser would leave it unchanged.

    A stand-in for a pattern, so the rule is the application's own function rather than a charset
    restated beside it. Two hand-written charsets in this table had already drifted from the code
    they described.
    """

    pattern = "sanitise_actor(value) == value and len(value) <= MAX_ACTOR_LENGTH"

    def match(self, value: str) -> bool:
        return sanitise_actor(value) == value and len(value) <= MAX_ACTOR_LENGTH


_SCRUB_IDEMPOTENT = _ScrubIdempotent()

AUDIT_STRING_VALUES: dict[str, frozenset[str] | re.Pattern[str] | _ScrubIdempotent] = {
    "kind": frozenset(
        {
            "audit",
            "auth_reject",
            "validation_reject",
            "http_reject",
            "cors_reject",
            "store_error",
        }
    ),
    "action": frozenset({"assess", "read_assessment"}),
    # DERIVED from the shipped scrub, not restated. The hand-written charset admitted ASCII only
    # while `_UNSAFE_LOG_CHARS` uses `\w`, which admits roughly 130,000 Unicode word characters, so
    # a legitimate operator name in any non-Latin script would have failed a pin that claimed to
    # describe the application. Idempotence under the real function is the property that matters
    # and it cannot drift: a value the scrub would change is a value that was never scrubbed.
    "actor": _SCRUB_IDEMPOTENT,
    # The five outcomes the application actually emits. "created" and "refused" were in this set
    # and produced by nothing, which is the same standing-exemption defect as the `reason` field
    # below, one value wide instead of one field wide.
    "outcome": frozenset({"ok", "error", "disclosed", "not_modified", "not_found"}),
    "confidence": frozenset({"low", "medium", "high"}),
    # The APPLICATION's own pattern, not a hand-written charset. The charset admitted roughly 113
    # characters of appended hex on a typical key, so 80 hex characters of the token appended to
    # the audit key passed. STORE_KEY_PATTERN requires exactly one colon with each half at most 64
    # characters, so an appended encoding overflows it, and importing the constant deletes a
    # duplicated fact at the same time.
    "key": re.compile(STORE_KEY_PATTERN),
    "path": re.compile(r"^[!-~]{1,160}$"),
    "reason": re.compile(r"^[ -~]{0,512}$"),
    # Inside `validation_reject.errors`, which the flat scan never reached. Both are bounded
    # server-side to MAX_ACTOR_LENGTH and both echo caller-shaped input, so they get the tightest
    # pattern that admits a pydantic location path and error type.
    # Same derivation, now that the application scrubs each part rather than only capping it.
    "loc": _SCRUB_IDEMPOTENT,
    "type": re.compile(r"^[a-z0-9_.]{0,64}$"),
}


# Every NUMERIC audit field, with a bound. An unbounded integer is a disclosure channel: the whole
# token fits inside one, and `int.from_bytes(token.encode(), "big")` on `duration_ms` decodes back
# to the credential exactly. The bounds are the ranges the application can legitimately produce.
AUDIT_NUMERIC_BOUNDS: dict[str, tuple[float, float]] = {
    # CORRELATED, not bounded. A bound of 300,000 still leaves about eighteen bits per record, and
    # `duration_ms = int.from_bytes(token[:2], "big") % 300_001` put two bytes of the credential in
    # every successful-write line with the suite green; rotate the offset and the pod log store
    # holds the whole token. Shrinking a covert channel is not closing it. The ceiling below is
    # asserted against the exercise's own measured wall clock, so the field cannot carry more than
    # the timing it reports.
    "duration_ms": (0, 0),
    "status": (400, 599),
    "error_count": (0, MAX_VALIDATION_ERRORS_LOGGED),
    "score": (0.0, 100.0),
    "evidence_coverage": (0.0, 1.0),
}


EXPECTED_AUDIT_KEYS: dict[str, set[str]] = {
    "audit": {
        "kind",
        "action",
        "actor",
        "duration_ms",
        "outcome",
        "key",
        # The assessment's own summary, which the operator needs in the trail. Each of these was
        # found by this pin on its first honest run, which is the pin working: the fields are
        # legitimate and were nonetheless unasserted by anything.
        "score",
        "confidence",
        "evidence_coverage",
    },
    "auth_reject": {"kind", "path", "reason"},
    "validation_reject": {"kind", "path", "error_count", "errors"},
    "http_reject": {"kind", "path", "reason", "status"},
    # NO "reason". The CORS handler emits kind, path and origin_allowed only, so a pinned name that
    # is never produced is not a pin: it is a standing exemption, and `reason` permits 512
    # printable characters on a record any unauthenticated caller triggers with one refused
    # preflight. Measured: base64 of the team token in that field, 314 tests green.
    "cors_reject": {"kind", "path", "origin_allowed"},
    "store_error": {"kind", "path", "reason"},
}


def _exercise_every_surface(
    tmp_path: Path, prober: StorageProber
) -> tuple[list[str], list[str], str]:
    """Drive every response shape and every log channel once, and return what came back.

    A helper rather than a test, because the two controls it feeds are separate: nothing may
    disclose the credential, and every audit record must match its pinned shape. Sharing the
    exercise keeps them measuring the same traffic.

    /diagnostics reports the token's LENGTH and whether it is set, deliberately, so a caller can
    confirm the deployment without learning the value. This walks every route, in both the
    authenticated and the rejected case, and greps the whole captured log stream as well as
    every body: a single interpolation of the value into an audit line or an error detail would
    put the shared credential in the platform's log aggregator.

    The token as read from `x-pree-token`, and a query string, which is the operator mistake the
    access-log filter exists to redact. NOT the token planted in a path segment or in the actor
    header: the audit line records the requested path and the caller-declared actor because that
    is what makes it an audit line, so a caller who writes their own credential into either has
    disclosed it themselves and no server-side redaction can un-write it. That boundary is
    recorded here rather than papered over, because a redaction wide enough to catch it would
    take the store key out of the audit trail.
    """
    stream = io.StringIO()
    # An allowed origin, so `register_cors` is actually installed. Without it the CORS layer is a
    # no-op and no `cors_reject` record can exist, which is what made that record's pinned field
    # list a literal nothing compared.
    config = make_config(
        tmp_path,
        PREE_TEAM_TOKEN=TEST_TOKEN,
        PREE_ALLOWED_ORIGIN="https://pree.apps.bluestaq.com",
    )
    logger = build_logger(stream)
    with build_client(config, logger, prober) as probe:
        bodies: list[str] = []
        leaked_headers: list[str] = []

        def record(answer: Any, where: str, *, docs: bool = False, origin: str = "") -> None:
            """Both channels of one response. The header half used to be missing entirely."""
            bodies.append(answer.text)
            leaked_headers.extend(_header_findings(answer, TEST_TOKEN, where, origin, docs=docs))

        for route in _all_routes(probe.app):
            target = getattr(route, "path", "").replace("{key}", "probe:key")
            methods = (getattr(route, "methods", None) or set()) - {"HEAD", "OPTIONS"}
            for method in sorted(methods):
                for headers in (AUTH, {"x-pree-token": "wrong-token-value"}, {}):
                    record(
                        probe.request(
                            method, target, headers=headers, json={}, follow_redirects=False
                        ),
                        f"{method} {target}",
                        docs=target in DOC_PATHS,
                    )
        # And the shapes that reflect caller input back: a validation failure whose detail
        # echoes the rejected body, and the token in a query string, which is the documented
        # operator mistake the access-log filter redacts.
        record(probe.post("/v1/assess", headers=AUTH, json={"bad": TEST_TOKEN}), "422 assess")
        record(probe.get(f"/healthz?token={TEST_TOKEN}"), "query string")
        # EVERY error shape, because the walk above only ever produces 401s and 422s and each of
        # these is built by a different handler. A header set on one of them would have been
        # invisible to a walk over the happy and rejected paths alone.
        record(probe.request("DELETE", "/v1/assess", headers=AUTH), "405")
        record(probe.get("/nowhere-at-all", headers=AUTH), "404")
        record(
            probe.post("/v1/assess", headers=AUTH, content=b"x" * (MAX_BODY_BYTES + 1)),
            "413",
        )
        record(
            probe.options(
                "/v1/assess",
                headers={
                    "origin": "https://evil.example",
                    "access-control-request-method": "POST",
                },
            ),
            "cors preflight",
        )
        # A SUCCESSFUL privileged call, so a success audit record reaches the stream. The walk
        # above sends `json={}` everywhere, which is a 422, so only REJECTION lines were ever
        # greped: `token=config.team_token` in the success audit call wrote the shared credential
        # into the pod log store on every write with the suite green.
        created = probe.post("/v1/assess", headers=AUTH, json=FULL_BODY)
        assert created.status_code == 200, created.text
        record(created, "assess success")
        # The store key is `<asset>:<candidate>`, which the response does not echo.
        key = f"{FULL_BODY['protected_asset_id']}:{FULL_BODY['candidate_id']}"
        read = probe.get(f"/v1/assessments/{key}", headers=AUTH)
        assert read.status_code == 200, read.text
        record(read, "read success")
        # And the CONDITIONAL read, whose 304 branch sets its own headers and was outside every
        # header pin: a raw token header there shipped undetected because nothing in the suite sent
        # an If-None-Match while reading headers.
        conditional = probe.get(
            f"/v1/assessments/{key}",
            headers={**AUTH, "if-none-match": read.headers["etag"]},
        )
        assert conditional.status_code == 304, conditional.status_code
        record(conditional, "304 read")
        # A refused preflight, which produces the `cors_reject` record.
        refused = probe.options(
            "/v1/assess",
            headers={
                "origin": "https://evil.example",
                "access-control-request-method": "POST",
            },
        )
        assert refused.status_code == 400, refused.status_code
        record(refused, "cors reject")
        # And a store failure, which produces the `store_error` record. Injected by making the
        # snapshot unreadable rather than by patching, so the real error path runs.
        snapshot = config.data_dir / "assessments.json"
        snapshot.write_text("{ not json at all", encoding="utf-8")
        (config.data_dir / "assessments.json.bak").write_text("also not json", encoding="utf-8")
        broken = probe.get(f"/v1/assessments/{key}", headers=AUTH)
        assert broken.status_code == 503, f"a corrupt snapshot answered {broken.status_code}"
        record(broken, "store error")
    return bodies, leaked_headers, stream.getvalue()


def test_the_team_token_reaches_no_response_body_header_or_log_record(
    tmp_path: Path, prober: StorageProber
) -> None:
    """The credential is compared, never echoed, never logged, in any channel.

    /diagnostics reports the token's LENGTH and whether it is set, deliberately, so a caller can
    confirm the deployment without learning the value.
    """
    bodies, leaked_headers, log = _exercise_every_surface(tmp_path, prober)
    leaked = [body for body in bodies if TEST_TOKEN in body]
    assert not leaked, f"a response body carried the team token: {leaked}"
    assert not leaked_headers, (
        f"a response header is unpinned or carries the token: {leaked_headers}"
    )
    assert TEST_TOKEN not in log, "the audit log carried the team token"


def _check_audit_value(
    field: str,
    value: Any,
    where: str,
    bounds: dict[str, tuple[float, float]],
    offending: list[str],
) -> None:
    """Every value at every depth. Strings by rule, numbers by bound, containers by recursion.

    The first version skipped every non-string on the stated reasoning that "a number cannot
    carry a base64 credential". It can carry the credential itself:
    `duration_ms=int.from_bytes(token.encode(), "big")` emitted a 77-digit integer that decodes
    byte for byte back to the token, on every successful write, with the suite green and the
    substring search blind to it. Strings nested inside `validation_reject.errors` were never
    reached either.
    """
    if isinstance(value, bool):
        return
    if isinstance(value, str):
        allowed = AUDIT_STRING_VALUES.get(field)
        if allowed is None:
            offending.append(f"{where} is a string no rule pins: {value!r}")
        elif isinstance(allowed, re.Pattern) and not allowed.match(value):
            offending.append(f"{where}={value!r} fails {allowed.pattern}")
        elif isinstance(allowed, frozenset) and value not in allowed:
            offending.append(f"{where}={value!r} outside {sorted(allowed)}")
        return
    if isinstance(value, (int, float)):
        bound = bounds.get(field)
        if bound is None:
            offending.append(f"{where} is a number no bound pins: {value!r}")
        elif not bound[0] <= value <= bound[1]:
            offending.append(f"{where}={value!r} outside {bound}")
        return
    if isinstance(value, dict):
        for inner, item in value.items():
            _check_audit_value(str(inner), item, f"{where}.{inner}", bounds, offending)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_audit_value(field, item, f"{where}[{index}]", bounds, offending)
        return
    offending.append(f"{where} is a {type(value).__name__}, which no rule pins")


def test_every_audit_record_matches_its_pinned_shape_and_values(
    tmp_path: Path, prober: StorageProber
) -> None:
    """A log line is a disclosure channel with no body and no header.

    Nothing in this suite pinned an audit record's fields until recently, and then the field list
    was pinned while the values were not, so an encoded credential inside a permitted field passed.
    Both halves are asserted here, and so is the requirement that every pinned kind is actually
    produced by the exercise: two of the six were not, which made their field lists literals that
    nothing compared.

    ONE RESIDUAL, recorded rather than implied away. A field emitted only under a condition the
    exercise does not create is invisible to both directions: it is absent from every emitted
    record, so the unexpected-field check never sees it, and its record IS produced, so the surplus
    check never fires. Measured with a field added to `auth_reject` only when an `x-pree-debug`
    header is present. That is an inherent limit of an exercise-driven pin rather than a gap in
    these assertions, and the countermeasure is the exercise, not the literal: it drives every
    route, every method, three token states and six error shapes, so a condition it misses has to
    be one no ordinary caller can reach either.
    """
    started = time.monotonic()
    _, _, log = _exercise_every_surface(tmp_path, prober)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    emitted = [json.loads(line) for line in log.splitlines() if line.strip()]
    # No single operation in the exercise can have taken longer than the whole exercise, so this is
    # the tightest ceiling available without re-instrumenting the application.
    bounds = {**AUDIT_NUMERIC_BOUNDS, "duration_ms": (0, elapsed_ms)}
    assert emitted, "the walk produced no audit records at all, so this greps an empty stream"
    # An unknown KIND is itself a finding, not a record to skip: a new record shape is a new
    # disclosure channel, and `.get(kind, set())` on a missing key would have waved it through.
    observed = {found.get("kind", "<none>") for found in emitted}
    unknown = sorted(observed - set(EXPECTED_AUDIT_KEYS))
    assert not unknown, f"an audit record kind is not pinned: {unknown}"
    # And every pinned kind must actually be PRODUCED here, or its field list is a literal nothing
    # compares. Two of the six were not: this walk configured no allowed origin, so `register_cors`
    # was a no-op and no `cors_reject` record existed, and no store failure was forced, so no
    # `store_error` record existed. A raw token in either wrote the shared credential to the pod log
    # on an event any unauthenticated caller can trigger at will, with the suite green. Same
    # dead-literal class as the header assertion that was never appended to.
    missing = sorted(set(EXPECTED_AUDIT_KEYS) - observed)
    assert not missing, (
        f"these record kinds are pinned and never produced here, so their field lists assert "
        f"nothing: {missing}"
    )
    unexpected = [
        f"{found['kind']}: {sorted(set(found) - EXPECTED_AUDIT_KEYS[found['kind']])}"
        for found in emitted
        if set(found) - EXPECTED_AUDIT_KEYS[found["kind"]]
    ]
    assert not unexpected, f"an audit record carries a field no test pins: {unexpected}"
    # BOTH DIRECTIONS, the way the kind check above already is. A surplus field NAME asserts
    # nothing and quietly permits whatever `AUDIT_STRING_VALUES` allows for it, which is how
    # `reason` became 512 free characters on the `cors_reject` record. Three literals were dead
    # when this assertion was written and it turned all three red.
    produced: dict[str, set[str]] = {}
    for found in emitted:
        produced.setdefault(found["kind"], set()).update(found)
    surplus = {
        kind: sorted(fields - produced.get(kind, set()))
        for kind, fields in EXPECTED_AUDIT_KEYS.items()
        if fields - produced.get(kind, set())
    }
    assert not surplus, (
        f"these field names are pinned and never emitted, so they are exemptions rather than "
        f"pins: {surplus}"
    )
    # The VALUES, not only the field names. `EXPECTED_AUDIT_KEYS` pinned names, and the only value
    # control on this channel was a raw substring search, so `key=key + "#" + base64(token)` put
    # the credential in the pod log store on every write and passed. Every string-valued field is
    # now an exact set member or a bounded pattern; a number cannot encode a token.
    offending: list[str] = []

    for found in emitted:
        for field, value in found.items():
            _check_audit_value(field, value, f"{found['kind']}.{field}", bounds, offending)
    assert not offending, f"an audit record value is unpinned: {offending}"
    # Every ENTRY in both tables must be exercised, or it is a standing exemption rather than a
    # pin. `declared_bytes` and `retry_after_seconds` were bounds for fields this application never
    # emits, which is the same defect as the `reason` field and the `created` outcome before them.
    seen: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for inner, item in value.items():
                seen.add(str(inner))
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for found in emitted:
        collect(found)
    unexercised = sorted((set(AUDIT_STRING_VALUES) | set(AUDIT_NUMERIC_BOUNDS)) - seen)
    assert not unexercised, (
        f"these value rules are never exercised, so they permit rather than pin: {unexercised}"
    )
