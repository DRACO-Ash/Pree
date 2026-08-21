"""The HTTP surface, mounted in-process through the factory with isolated state."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pree.app import LIVENESS_PATHS, MAX_BODY_BYTES, STORAGE_PROBE_PATH, create_app
from pree.audit import build_logger
from pree.health import StorageProber
from pree.ratelimit import RateLimiter
from pree.store import JsonStore, StoreError
from tests.conftest import AUTH, TEST_TOKEN, build_client, make_config

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


@pytest.mark.parametrize("path", LIVENESS_PATHS)
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
            "/v1/assess", json=FULL_BODY, headers={**AUTH, "x-pree-actor": "ops.lead"}
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
    assert audit_lines[-1]["actor"] == "ops.lead"


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
