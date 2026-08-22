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
from fastapi.testclient import TestClient

from pree.app import (
    LIVENESS_PATHS,
    MAX_BODY_BYTES,
    MAX_LOGGED_PATH,
    MAX_VALIDATION_ERRORS_LOGGED,
    STORAGE_PROBE_PATH,
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
    with build_client(config, logger, StorageProber(cache_seconds=0.0)) as bounded:
        long_path = "/v1/assessments/" + "%01" * 4_000
        assert bounded.get(long_path, headers={"x-pree-token": "wrong"}).status_code == 401

    lines = [line for line in stream.getvalue().splitlines() if "auth_reject" in line]
    assert lines, "the rejection was not audited at all"
    for line in lines:
        assert len(line) < 1024, f"audit line is {len(line)} bytes, unbounded by the caller"
        assert len(json.loads(line)["path"]) <= MAX_LOGGED_PATH


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
