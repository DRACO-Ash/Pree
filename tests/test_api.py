"""The HTTP surface, mounted in-process through the factory with isolated state."""

from __future__ import annotations

import inspect
import io
import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from pree.app import (
    LIVENESS_PATHS,
    MAX_BODY_BYTES,
    MAX_LOGGED_PATH,
    MAX_VALIDATION_ERRORS_LOGGED,
    STORAGE_PROBE_PATH,
    _limit_keys,
    create_app,
)
from pree.audit import build_logger
from pree.health import StorageProber
from pree.ratelimit import GLOBAL_LIMIT, RateLimiter
from pree.security import MAX_ACTOR_LENGTH, sanitise_actor
from pree.store import JsonStore, StoreError
from tests.conftest import AUTH, TEST_TOKEN, build_client, make_config

# Pinned literals, deliberately NOT derived from LIVENESS_PATHS. These five paths are the
# contract in CLAUDE.md and in the deployment sheet; a test that reads them from the constant
# it is checking cannot notice the constant shrinking.
EXPECTED_LIVENESS_PATHS = frozenset({"/", "/healthz", "/readyz", "/livez", "/ping"})

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
    for field in (
        "build_id",
        "environment",
        "port",
        "auth_enabled",
        "allowed_origin_present",
        "allowed_origin_is_wildcard",
        "data_dir_is_absolute",
        "storage_writable",
        "storage_errno_name",
        "identity_uid",
        "identity_is_root",
    ):
        assert field in body, f"diagnostics is missing {field}"


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
    """RFC 9110 makes Allow a MUST on a 405, and this asserts it across every shape.

    A review reported that the audit-suppressed 405 on a probe path dropped the Allow header. I
    could not reproduce that: Starlette's router sets Allow on the outgoing response, not only on
    the exception it raises, so the header survives whether or not the handler forwards
    `exc.headers`. This test therefore asserts a property the framework provides rather than one
    this code provides, and it is recorded as such: it will not fail if that argument is removed.
    It is worth keeping anyway, because the property is part of the app's HTTP contract and a
    future handler that builds its own 405 would break it.
    """
    for path in ("/healthz", "/", STORAGE_PROBE_PATH, "/diagnostics", "/v1/assess"):
        response = client.request("DELETE", path, headers=AUTH)
        assert response.status_code == 405, f"{path} gave {response.status_code}"
        allowed = response.headers.get("allow")
        assert allowed, f"405 on {path} carries no Allow header"
        # The method the route actually serves, which is POST for the scoring path.
        expected = "POST" if path == "/v1/assess" else "GET"
        assert expected in allowed, f"405 on {path} allows {allowed!r}, not {expected}"


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
        for route in app.routes
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
