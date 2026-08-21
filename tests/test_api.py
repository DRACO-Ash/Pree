"""The HTTP surface, mounted in-process through the factory with isolated state."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.conftest import TEST_TOKEN, make_config

from pree.app import LIVENESS_PATHS, create_app
from pree.ratelimit import RateLimiter
from pree.store import JsonStore

AUTH = {"x-pree-token": TEST_TOKEN}
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
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200


def test_storage_health_proves_a_real_write(client: TestClient) -> None:
    body = client.get("/healthz/storage").json()
    assert body["storage_writable"] is True
    assert body["errno"] is None
    assert body["probe_duration_ms"] < body["probe_timeout_ms"]


def test_storage_health_reports_the_directory_and_errno_on_failure(
    tmp_path: Path, quiet_logger: object, executor: ThreadPoolExecutor
) -> None:
    """A read-only mount must yield a 503 whose body is a full diagnosis."""
    data_dir = tmp_path / "readonly"
    data_dir.mkdir()
    config = make_config(tmp_path, PREE_DATA_DIR=str(data_dir))
    app = create_app(config, JsonStore(config.data_dir), logger=quiet_logger, executor=executor)
    data_dir.chmod(0o500)
    try:
        with TestClient(app) as probe_client:
            response = probe_client.get("/healthz/storage")
    finally:
        data_dir.chmod(0o700)
    if os.getuid() == 0:
        pytest.skip("running as root, which can write through a read-only directory mode")
    assert response.status_code == 503
    body = response.json()
    assert body["storage_writable"] is False
    assert body["data_dir"] == str(data_dir)
    assert body["errno_name"] == "EACCES"


def test_diagnostics_reports_secrets_as_a_boolean_and_a_length_only(
    client: TestClient,
) -> None:
    body = client.get("/diagnostics").json()
    assert body["team_token_present"] is True
    assert body["team_token_length"] == len(TEST_TOKEN)
    assert TEST_TOKEN not in client.get("/diagnostics").text
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


def test_a_stored_assessment_emits_an_etag_and_honours_if_none_match(
    client: TestClient,
) -> None:
    client.post("/v1/assess", json=FULL_BODY, headers=AUTH)
    first = client.get("/v1/assessments/asset-01:cand-99", headers=AUTH)
    etag = first.headers["etag"]
    again = client.get("/v1/assessments/asset-01:cand-99", headers={**AUTH, "if-none-match": etag})
    assert again.status_code == 304


def test_an_unknown_assessment_is_a_404(client: TestClient) -> None:
    response = client.get("/v1/assessments/nope:nope", headers=AUTH)
    assert response.status_code == 404


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
    ],
)
def test_out_of_range_or_unknown_input_is_rejected_at_the_boundary(
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


def test_the_expensive_path_is_rate_limited_per_actor(
    tmp_path: Path, quiet_logger: object, executor: ThreadPoolExecutor
) -> None:
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)
    store = JsonStore(config.data_dir)
    store.seed()
    app = create_app(
        config,
        store,
        logger=quiet_logger,
        executor=executor,
        actor_limiter=RateLimiter(2, 60.0),
    )
    with TestClient(app) as limited:
        headers = {**AUTH, "x-pree-actor": "watch-floor"}
        assert limited.post("/v1/assess", json=FULL_BODY, headers=headers).status_code == 200
        assert limited.post("/v1/assess", json=FULL_BODY, headers=headers).status_code == 200
        third = limited.post("/v1/assess", json=FULL_BODY, headers=headers)
    assert third.status_code == 429
    assert int(third.headers["retry-after"]) >= 1


def test_the_coarse_limit_protects_the_process_but_never_the_health_probe(
    tmp_path: Path, quiet_logger: object, executor: ThreadPoolExecutor
) -> None:
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)
    store = JsonStore(config.data_dir)
    store.seed()
    app = create_app(
        config,
        store,
        logger=quiet_logger,
        executor=executor,
        global_limiter=RateLimiter(1, 60.0),
    )
    with TestClient(app) as limited:
        assert limited.get("/diagnostics").status_code == 200
        assert limited.get("/diagnostics").status_code == 429
        # Liveness stays exempt: rate-limiting the platform probe would present an
        # infrastructure fault as an application failure.
        for path in LIVENESS_PATHS:
            assert limited.get(path).status_code == 200


def test_cors_allows_only_the_configured_origin(
    tmp_path: Path, quiet_logger: object, executor: ThreadPoolExecutor
) -> None:
    config = make_config(
        tmp_path,
        PREE_ENV="production",
        PREE_TEAM_TOKEN=TEST_TOKEN,
        PREE_ALLOWED_ORIGIN="https://pree.apps.bluestaq.com",
    )
    store = JsonStore(config.data_dir)
    store.seed()
    app = create_app(config, store, logger=quiet_logger, executor=executor)
    with TestClient(app) as cors_client:
        allowed = cors_client.get("/healthz", headers={"Origin": "https://pree.apps.bluestaq.com"})
        denied = cors_client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert allowed.headers["access-control-allow-origin"] == "https://pree.apps.bluestaq.com"
    assert "access-control-allow-origin" not in denied.headers
