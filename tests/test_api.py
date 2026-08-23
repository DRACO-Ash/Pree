"""The HTTP surface, mounted in-process through the factory with isolated state."""

from __future__ import annotations

import ast
import base64
import dataclasses
import errno
import inspect
import io
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from string import Formatter
from types import ModuleType
from typing import Any, NamedTuple, Protocol
from unittest import mock

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi import applications as fastapi_applications
from fastapi.exceptions import RequestValidationError, WebSocketRequestValidationError
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Route

from pree import __version__
from pree import app as app_module
from pree import audit as audit_module
from pree import main as main_module
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
from pree.audit import (
    CREDENTIAL_ALARM,
    _GuardedStream,
    audit,
    build_logger,
    install_credential_guard,
)
from pree.config import ServiceConfig
from pree.health import StorageProber
from pree.main import build
from pree.ratelimit import GLOBAL_LIMIT, RateLimiter
from pree.scoring import ConfidenceTier
from pree.security import (
    MAX_ACTOR_LENGTH,
    UNPRINTABLE_MARKER,
    AuthError,
    sanitise_actor,
    sanitise_log_part,
    sanitise_log_path,
    token_verifier,
)
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
    team token unauthenticated with the whole suite green, and so did `app.mount("/admin", admin)`
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
            yield create_app(
                config.for_service(),
                JsonStore(config.data_dir),
                verify_token=token_verifier(config),
                prober=prober,
            )
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
        # THREE shapes at once, because each defeats a different bound, and body ORDER is
        # load-bearing: pydantic reports errors in body order and only the first
        # MAX_VALIDATION_ERRORS_LOGGED reach the record, so a shape appended after the tiny
        # flood is outside the logged window and asserts nothing.
        #
        # Which shapes are WHERE, measured rather than asserted in prose: the ten logged slots
        # hold the two declared fields, the five astral names and three of the five long keys.
        # The tiny flood is COUNTED and not logged, and it earns its place that way, by lifting
        # `error_count` past the cap so deleting the cap logs all of them and the byte ceiling
        # fires. A previous version of this comment said "all three sit inside it", which was
        # false about the flood.
        #
        # Astral field NAMES are the shape this test did not have, and the omission cost a
        # major: with `sanitise_log_part` reverted to the Unicode charset in ONE line, twelve
        # of them wrote a 6,684-byte record against 548 shipped, and all 316 tests stayed
        # green. The astral probe existed, but only in `_drive_every_error_shape`, which feeds
        # no byte or charset assertion, and the `loc` rule is derived from the shipped scrub so
        # it moves with the mutation. I recorded in three places that "astral inputs are in both
        # bound tests"; they were in one.
        payload.update({(chr(0x1D400) * 70) + str(index): 1 for index in range(5)})
        # Five 5,000-character keys sit under the 32 KiB body cap, so truncation and not the
        # cap is what stops them.
        payload.update({f"k{index}{'x' * 5_000}": 1 for index in range(5)})
        # And MANY tiny keys exceed MAX_VALIDATION_ERRORS_LOGGED, so the cap is what stops
        # those. The first version of this test sent only the long keys, which left the cap
        # unasserted: deleting it wrote a 142,290-byte record with the suite green.
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
        # The astral shape must actually BE in the logged window, or the assertion below is
        # vacuous and nobody would know. Recorded by the security gate as a fragility rather than
        # a defect: the astral names sit inside the window only because pydantic reports
        # declared-field errors before extras, which is stable under the hash-locked pin today
        # and could move under a bump, silently re-opening the hole this test exists to close.
        # A single scrubbed astral name is one digit, the index that survives the scrub.
        # The COUNT, not `any`. A digit-only `loc` part is a scrubbed astral name today, and there
        # are five of them by construction of the payload, but a list index or a numerically named
        # field added later would satisfy `any` with no astral name present and silently restore
        # the vacuity this assertion closes. Five is the number sent, so the assertion moves only
        # when the payload does.
        digits = sum(1 for item in record["errors"] for part in item["loc"] if part.isdigit())
        assert digits == 5, (
            f"expected the five scrubbed astral names in the logged window, found {digits}; the "
            f"charset assertion below is vacuous without them: "
            f"{[item['loc'] for item in record['errors']]}"
        )
        for item in record["errors"]:
            for part in item["loc"]:
                assert len(part) <= MAX_ACTOR_LENGTH
                # The PROPERTY, not the byte arithmetic. A length bound in characters says
                # nothing about bytes, and a whole-line ceiling depends on how many of the
                # logged slots this payload happens to fill; this assertion fires on the first
                # non-ASCII part whatever the slot count, which is what makes it robust to the
                # one-line charset reversion rather than incidentally sensitive to it.
                assert part.isascii() and part.isprintable(), (
                    f"a non-ASCII or control character survived into a logged field name: {part!r}"
                )


def test_two_unauthenticated_requests_whose_paths_differ_cannot_share_one_audit_record(
    tmp_path: Path,
) -> None:
    """END TO END, because the unit property held while the application still aliased.

    Three rounds of this control were asserted on the sanitiser alone and defeated at the call
    site: the sanitiser was injective and the application handed it a DECODED path, which aliases
    before the sanitiser ever runs. `GET /v1/assessments/a:b%20` was audited byte-identically to
    `GET /v1/assessments/a:b`, and `GET /v1/%assess` shared a record with `GET /v1/%25assess`, both
    unauthenticated and needing no token. This drives real requests through the real handler and
    compares the records, which is the only version of the assertion the call site cannot escape.

    "WHOSE PATHS DIFFER" is in the name because the previous name asserted a universal that a real
    request defeats. uvicorn partitions the target on `?` before the scope exists, so `raw_path`
    carries no query string and `?x=1` and `?x=2` share a record. Every probe here differed in its
    path, so the body could not see it: the name claimed what the body did not check, which is the
    defect this file keeps finding one level up from wherever it last found it.
    """
    stream = io.StringIO()
    logger = build_logger(stream)
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)
    targets = [
        "/v1/assessments/a:b%20",
        "/v1/assessments/a:b",
        "/v1/%assess",
        "/v1/%25assess",
        "/v1/assessments/a%2Fb:c",
        "/v1/assessments/a/b:c",
        "/v1/,assess",
        "/v1/assess",
    ]
    with build_client(config, logger, StorageProber(cache_seconds=0.0)) as bounded:
        for target in targets:
            bounded.get(target, headers={"x-pree-token": "wrong"})

    logged = [
        json.loads(line)["path"] for line in stream.getvalue().splitlines() if '"path"' in line
    ]
    assert len(logged) == len(targets), (
        f"expected one audited record per request, got {len(logged)}: {logged}"
    )
    assert len(set(logged)) == len(targets), (
        f"two requests with distinct paths produced one audit record, so an unauthenticated caller "
        f"can put a route they never requested into the trail: "
        f"{sorted({path for path in logged if logged.count(path) > 1})}"
    )
    for path in logged:
        assert path.isascii() and path.isprintable(), f"the logged path is not ASCII: {path!r}"


# The probe identity and the ids the correlation drive uses. Named constants rather than inline
# literals, because the assertions downstream recompute from these and an inline literal in two
# places is two places for them to drift apart.
ACTOR_HEADER = "x-pree-actor"
# The literal each handler's reason begins with, so the field is recomputed rather than
# charset-checked. `store_error` embeds a configured path after its prefix, which is why the
# assertion accepts a prefix match for that one and equality for the rest.
# What the boundary produces for the correlation drive's body, `{"bad": 1}`, against a model with
# `extra="forbid"` and two required identifiers: one refusal for the unknown field and one for each
# missing one. Pinned as literals so a change in what the boundary reports is a named failure
# rather than a widening.
_EXPECTED_PROBE_ERROR_COUNT = 3
_EXPECTED_PROBE_ERROR_TYPES = frozenset({"extra_forbidden", "missing"})
_EXPECTED_PROBE_ERROR_LOCS = frozenset(
    {("body", "bad"), ("body", "protected_asset_id"), ("body", "candidate_id")}
)
_EXPECTED_REASONS = {
    "auth_reject": "token rejected",
    "http_reject": "Not Found",
    "store_error": "snapshot at ",
}
PROBE_ACTOR = "watch-floor.lead@example.test"
PROBE_ASSET = "asset-probe-01"
PROBE_CANDIDATE = "cand-probe-99"


# The token axis, and what it can and cannot deliver, stated before it is used.
#
# TWO tokens does NOT refuse the class, and I claimed it did at three prose sites. Measured by the
# gate: `bool(_had_query(request) and "-" in (config.team_token or ""))` was green, because BOTH
# fixture tokens contain a hyphen, and the same conjunct on `origin_allowed` was green too, so two
# real bits of the deployed credential shipped per refused preflight with nothing red. A two-sample
# axis catches only a predicate that DISAGREES between those two samples; any predicate constant
# across them survives. The related claim, that "a single-token version of this test passes against
# the leaking expression", was also false: that expression is request-independent, so the
# both-directions axis alone kills it, single token or not.
#
# What this set delivers instead: for a character-class predicate over the token to survive, it must
# agree across every member. The fixed members are chosen so the obvious classes each split them -
# hyphen, underscore, digit, dot, tilde, case, and the parity of the first byte - and the random
# member makes an unanticipated class improbable rather than merely unlisted. IMPROBABLE, not
# impossible: a predicate can still be constant by luck, so this raises the cost of the channel and
# does not close it, and that is the honest statement of what the axis is worth.
_LEAK_PROBE_TOKENS = (
    TEST_TOKEN,
    PRODUCTION_TOKEN,
    # No hyphen, no underscore, no dot, no tilde, no digit, all one case.
    "abcdefghijklmnopqrstuvwxyzabcdefgh",
    # Digits only, so every alphabetic class flips.
    "9182736450918273645091827364509182",
    # Leading byte of the opposite parity to the two fixtures, and every separator at once.
    "Bc4-De7_Fg1.Hi3~Jk6Lm9No2Pq5Rs8Tu",
)


def test_the_query_bit_is_the_query_and_nothing_else_on_every_kind_that_emits_it(
    tmp_path: Path,
) -> None:
    """A boolean is a one-bit channel, and a NAME pin cannot see inside one.

    This is the `origin_allowed` defect, reintroduced by me and caught by the gate: I added
    `had_query` to five record kinds, pinned its value on ONE, and wrote a comment claiming it was
    "asserted in both directions on every kind that emits it" across "the two-token axis". Neither
    was true. `_check_audit_value` returns after checking a boolean's name, so on the other four
    kinds the field was unpinned, and

        "had_query": bool(config.team_token and ord(config.team_token[0]) & 1)

    left the whole suite green while handing an unauthenticated caller one bit of the team token
    per refused preflight.

    EVERY kind, BOTH directions, and the token axis described above `_LEAK_PROBE_TOKENS`, whose
    limits are stated there rather than overstated here.

    And the PATH is recomputed, not charset-checked, which is the second half of the same lesson.
    The `path` rule is a charset of word characters, dot, slash, percent, at, colon and hyphen, and
    hex is inside it, so

        sanitise_log_path(...) + "/" + config.team_token.encode().hex()

    at the CORS site put the WHOLE credential into a record any unauthenticated caller can trigger,
    240 a minute on the coarse allowance, with all 331 tests green and `TEST_TOKEN not in log` still
    true because the value was hex. A charset cannot see an appended value; recomputation can, and
    this exercise knows every target it drove.
    """
    random_token = secrets.token_urlsafe(32)
    for token in (*_LEAK_PROBE_TOKENS, random_token):
        # BOTH environments. Every audit test built through `make_config`, which hard-codes
        # development, so one `if config.is_production` gated every leak past the whole apparatus:
        # the path attack, an attacker-indexed bit in a boolean, all of it, suite green. The
        # scope closure refuses that class outright now, and this refuses the instance, which is
        # the pair the review asked for rather than either alone.
        for environment in ("development", "production"):
            for query, expected in (("?x=1", True), ("", False)):
                label = f"{environment}-{len(token)}-{token[:4]}-{expected}"
                observed = _every_kind_with(tmp_path / label, token, query, environment)
                assert set(observed) == set(EXPECTED_AUDIT_KEYS), (
                    f"the exercise did not produce every record kind, so the kinds it missed are "
                    f"unpinned by value: {sorted(set(EXPECTED_AUDIT_KEYS) - set(observed))}"
                )
                for kind, seen in observed.items():
                    _assert_record_recomputes(kind, seen, token, query, expected)


def _assert_record_recomputes(
    kind: str, seen: dict[str, Any], token: str, query: str, expected_query: bool
) -> None:
    """Every caller-influenced value in one record, RECOMPUTED from what the request carried.

    A shape check cannot see a value appended to a field that keeps its shape. The gate recovered
    the whole credential from four separate fields that way, each inside its pin: hex appended to
    `path`, hex split across the two halves of `key`, 64 hex characters as an `actor` (exactly
    MAX_ACTOR_LENGTH and scrub-invariant), base64 in a `reason`, and four token bytes in a `score`
    mantissa inside the (0.0, 100.0) bound. Every one of them is recomputed here.
    """
    record, target, body = seen["record"], seen["target"], seen["body"]
    if kind != "audit":
        assert record["had_query"] is expected_query, (
            f"{kind} recorded had_query={record['had_query']!r} for query {query!r} under a token "
            f"starting {token[:4]!r}; expected {expected_query}. A boolean whose value tracks "
            f"anything but the request is a channel, not a fact about the request"
        )
        expected_path = sanitise_log_path(target.encode(), MAX_LOGGED_PATH)
        assert record["path"] == expected_path, (
            f"{kind} logged path {record['path']!r} for target {target!r}, which recomputes to "
            f"{expected_path!r}. Anything appended to this field is inside the charset the rule "
            f"checks, so a difference here is the field carrying something other than the path"
        )
    if kind == "audit":
        assert record["actor"] == sanitise_actor(PROBE_ACTOR), (
            f"the audited actor {record['actor']!r} is not the scrub of the header sent "
            f"({PROBE_ACTOR!r}); 64 hex characters is exactly MAX_ACTOR_LENGTH and survives the "
            f"scrub unchanged, so this field accepted the whole token under its own pin"
        )
        assert record["key"] == f"{PROBE_ASSET}:{PROBE_CANDIDATE}", (
            f"the audited key {record['key']!r} is not the two validated ids and the colon "
            f"between them; hex is inside STORE_KEY_PATTERN and each half fits in 64 characters"
        )
        for field in ("score", "evidence_coverage"):
            assert record[field] == body[field], (
                f"the audited {field} {record[field]!r} is not the value the response returned "
                f"({body[field]!r}); a bound constrains the range and says nothing about the "
                f"mantissa, which carries four credential bytes per write"
            )
    # EQUALITY, not a prefix. The prefix form left 512 minus 14 free printable characters on
    # `auth_reject`, and that tail is exactly the channel a review used to recover the whole
    # credential from an unauthenticated 401. `store_error` embeds a configured path, so its
    # expectation is RECOMPUTED from the data directory rather than conceded as a suffix.
    if "reason" in record:
        expected_reason = (
            f"snapshot at {seen['data_dir']}/assessments.json.bak is unreadable"
            if kind == "store_error"
            else _EXPECTED_REASONS[kind]
        )
        assert record["reason"] == expected_reason, (
            f"the {kind} reason {record['reason']!r} is not the handler's own text "
            f"({expected_reason!r}); this field permits 512 printable characters, which is room "
            f"for the credential in any encoding on a record needing no token"
        )
    # The STATUS against the response, not merely inside 400-599.
    if "status" in record:
        assert record["status"] == seen["status"], (
            f"the {kind} record says status {record['status']} while the response was "
            f"{seen['status']}; a record that disagrees with the response it describes is a "
            f"channel, and the 400-599 bound admits about 7.6 bits of one"
        )
    if kind == "validation_reject":
        # `error_count` against the errors the boundary actually produced for this body, and the
        # `loc` and `type` of each against what it can produce. `type` is pydantic's vocabulary and
        # was pattern-checked only: unpadded lowercase base32 fits `^[a-z0-9_.]{0,64}$`, is not one
        # of the six encodings the secrecy sweep enumerates, and carried the whole credential.
        assert record["error_count"] == _EXPECTED_PROBE_ERROR_COUNT, (
            f"the driven body produces {_EXPECTED_PROBE_ERROR_COUNT} boundary errors and the "
            f"record says {record['error_count']}; the derived ceiling admits about 12.4 bits"
        )
        for item in record["errors"]:
            assert item["type"] in _EXPECTED_PROBE_ERROR_TYPES, (
                f"the boundary reported error type {item['type']!r}, which is outside what this "
                f"body can produce ({sorted(_EXPECTED_PROBE_ERROR_TYPES)}); the charset pin admits "
                f"64 characters of anything lowercase, which is room for a whole credential"
            )
            assert tuple(item["loc"]) in _EXPECTED_PROBE_ERROR_LOCS, (
                f"the boundary reported loc {item['loc']!r}, which is not a field this body sent "
                f"({sorted(_EXPECTED_PROBE_ERROR_LOCS)})"
            )
    # EVERY encoding, not verbatim and hex. Base64 is what the gate used on `reason` precisely
    # because the two-form sweep did not look for it, and a sweep enumerating forms will always be
    # one short - so this is the honest limit of this assertion, and the scope closure above is
    # what actually refuses the class.
    rendered = json.dumps(record)
    raw = token.encode()
    for name, encoded in (
        ("verbatim", token),
        ("hex", raw.hex()),
        ("base64", base64.b64encode(raw).decode()),
        ("base64url", base64.urlsafe_b64encode(raw).decode()),
        ("base32", base64.b32encode(raw).decode()),
        ("reversed", token[::-1]),
    ):
        assert encoded not in rendered, f"the team token is in the {kind} record, {name}: {record}"


def _every_kind_with(
    directory: Path, token: str, query: str, environment: str = "development"
) -> dict[str, dict[str, Any]]:
    """Drive one request per rejection kind, and return each kind's record beside its TARGET.

    One helper rather than five copies, because five copies of the drive are five places for a
    kind to be quietly dropped and read as "not emitted" instead of "not asserted".

    The target is returned WITH the record because a charset check on a field cannot see a value
    appended to it, and recomputation can. `path` was pinned by charset alone, and hex is inside
    that charset, so appending `config.team_token.encode().hex()` at the CORS site put the whole
    credential into a record any unauthenticated caller can trigger, 240 a minute on the coarse
    allowance, with the whole suite green and `TEST_TOKEN not in log` still true.
    """
    stream = io.StringIO()
    config = make_config(
        directory,
        PREE_ENV=environment,
        PREE_TEAM_TOKEN=token,
        PREE_ALLOWED_ORIGIN="https://pree.example",
    )
    auth = {"x-pree-token": token, ACTOR_HEADER: PROBE_ACTOR}
    # The path each drive requests, WITHOUT the query, which is what `_raw_path` hands the scrub.
    targets = {
        "audit": "/v1/assess",
        "auth_reject": "/v1/assessments/a:b",
        "validation_reject": "/v1/assess",
        "http_reject": "/nowhere-at-all",
        "cors_reject": "/v1/assess",
        "store_error": "/v1/assessments/a:b",
    }
    body: dict[str, Any] = {}
    # The response status each drive actually received, so the audited `status` can be recomputed
    # against it rather than merely bounded to 400-599. `400 + (exc.status_code % 7) * 13` sat
    # inside that bound and made the record disagree with the response it describes, about 7.6 bits
    # per unauthenticated 404, with the whole suite green.
    statuses: dict[str, int] = {}
    with build_client(config, build_logger(stream), StorageProber(cache_seconds=0.0)) as probe:
        # audit: the SUCCESS record, which no earlier version of this exercise reached, so its
        # `key`, `actor`, `score` and `evidence_coverage` were shape-checked and never recomputed.
        accepted = probe.post(
            f"{targets['audit']}{query}",
            headers=auth,
            json={
                "protected_asset_id": PROBE_ASSET,
                "candidate_id": PROBE_CANDIDATE,
                "indicators": {"closest_approach_km": 12.5, "relative_velocity_kms": 3.25},
            },
        )
        assert accepted.status_code == 200, accepted.text
        body = accepted.json()
        # auth_reject: a gated route with the wrong token, which needs no token at all.
        statuses["auth_reject"] = probe.get(
            f"{targets['auth_reject']}{query}", headers={"x-pree-token": "wrong"}
        ).status_code
        # validation_reject: authenticated, body refused at the boundary.
        statuses["validation_reject"] = probe.post(
            f"{targets['validation_reject']}{query}", headers=auth, json={"bad": 1}
        ).status_code
        # http_reject: a route that does not exist.
        statuses["http_reject"] = probe.get(
            f"{targets['http_reject']}{query}", headers=auth
        ).status_code
        # cors_reject: a preflight from a disallowed origin.
        statuses["cors_reject"] = probe.options(
            f"{targets['cors_reject']}{query}",
            headers={
                "Origin": "https://evil.test",
                "Access-Control-Request-Method": "POST",
            },
        ).status_code
        # store_error: a corrupt snapshot AND a corrupt backup, so the real 503 path runs.
        (config.data_dir / "assessments.json").write_text("{ not json", encoding="utf-8")
        (config.data_dir / "assessments.json.bak").write_text("nor this", encoding="utf-8")
        statuses["store_error"] = probe.get(
            f"{targets['store_error']}{query}", headers=auth
        ).status_code

    # EVERY record, not only the ones carrying `had_query`. Selecting on that field left the
    # `audit` kind outside the correlation entirely, so its four caller-influenced values were
    # shape-checked and never recomputed, and the gate recovered the whole credential from three
    # of them.
    observed: dict[str, dict[str, Any]] = {}
    for line in stream.getvalue().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        kind = record.get("kind")
        if kind not in targets:
            continue
        observed[kind] = {
            "record": record,
            "target": targets[kind],
            "body": body,
            "status": statuses.get(kind),
            "data_dir": config.data_dir,
        }
    return observed


def test_the_query_string_aliases_and_the_record_says_a_query_was_present(
    tmp_path: Path,
) -> None:
    """The ACCEPTED limit, asserted so it cannot be rediscovered as a surprise or overclaimed.

    `raw_path` excludes the query string, so `?x=1` and `?x=2` genuinely share one `path` value.
    The query is not recovered on purpose: `audit.py` records that
    `GET /diagnostics?x-pree-token=<the real token>` was refused for authentication and then
    written verbatim into the pod log store, which is the only time this application held the
    credential in cleartext. Re-adding the query to an audited field would put that back in the
    forensic channel.

    What IS recorded is one bit: a query was present. This asserts both halves - that the aliasing
    is real, and that the bit distinguishes the query-bearing request from the bare one - so
    nobody reads the boolean as a claim that injectivity was restored. It was not.
    """
    stream = io.StringIO()
    logger = build_logger(stream)
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)
    with build_client(config, logger, StorageProber(cache_seconds=0.0)) as bounded:
        for target in ("/v1/assessments/a:b?x=1", "/v1/assessments/a:b?x=2", "/v1/assessments/a:b"):
            assert bounded.get(target, headers={"x-pree-token": "wrong"}).status_code == 401

    records = [
        json.loads(line) for line in stream.getvalue().splitlines() if '"auth_reject"' in line
    ]
    assert len(records) == 3, f"expected three audited rejections, got {len(records)}"
    assert {record["path"] for record in records} == {"/v1/assessments/a:b"}, (
        f"the query string reached the audited path field, where a team token has been seen "
        f"before: {[record['path'] for record in records]}"
    )
    assert [record["had_query"] for record in records] == [True, True, False], (
        f"the had_query bit does not distinguish a query-bearing request from a bare one: "
        f"{[record['had_query'] for record in records]}"
    )
    # And the VALUE never appears anywhere in the stream, which is the whole reason it is a bit.
    assert "x=1" not in stream.getvalue() and "x=2" not in stream.getvalue(), (
        "a query string value reached the audit stream"
    )


def test_a_server_that_puts_the_query_in_raw_path_still_cannot_reach_the_audit_field(
    tmp_path: Path,
) -> None:
    """The defensive partition, exercised against the server it defends against.

    Removing the partition leaves the whole suite green, and that is not a hole in this test: h11,
    httptools and Starlette's TestClient all split the target before the scope exists, so under the
    shipped stack the partition cuts nothing and no mutation of it is observable. A defence with no
    reachable failure is a defence nobody can verify, which is how the last four rounds of this
    control were defeated in prose.

    So the server is SIMULATED. This middleware puts the full request target in `raw_path`, which is
    what an ASGI implementation or an upstream middleware is free to do, and asserts the query still
    does not reach the audited field. `src/pree/audit.py` records why that field matters: a query
    string carrying the team token was written verbatim into the pod log store, the only time this
    application has held the credential in cleartext.
    """
    stream = io.StringIO()
    logger = build_logger(stream)
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)

    class _RawPathCarriesTheQuery:
        def __init__(self, app: Any) -> None:
            self._app = app

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http" and scope.get("query_string"):
                scope["raw_path"] = scope["path"].encode() + b"?" + scope["query_string"]
            await self._app(scope, receive, send)

    store = JsonStore(config.data_dir)
    store.seed()
    app = create_app(
        config.for_service(),
        store,
        verify_token=token_verifier(config),
        logger=logger,
        prober=StorageProber(cache_seconds=0.0),
    )
    with TestClient(_RawPathCarriesTheQuery(app)) as hostile:
        refused = hostile.get(
            f"/diagnostics?x-pree-token={TEST_TOKEN}", headers={"x-pree-token": "wrong"}
        )
        assert refused.status_code == 401, refused.text

    trail = stream.getvalue()
    records = [json.loads(line) for line in trail.splitlines() if '"path"' in line]
    assert records, "the rejection was not audited"
    assert records[0]["path"] == "/diagnostics", (
        f"the query string reached the audited path field through raw_path: {records[0]['path']!r}"
    )
    assert records[0]["had_query"] is True, "the query bit did not survive the partition"
    assert TEST_TOKEN not in trail, "the team token reached the audit stream through raw_path"


@pytest.mark.parametrize("supplied", [None, "str", "bytearray"])
def test_the_audited_path_falls_back_when_no_usable_raw_path_is_supplied(
    tmp_path: Path, supplied: str | None
) -> None:
    """`raw_path` is an ASGI extension, not a guaranteed key, and not a guaranteed TYPE either.

    Asserted rather than assumed, because an unexercised fallback is where a crash waits: an
    accessor that raises on a missing key would turn every audited rejection into a 500 under a
    server that omits it. What the fallback loses is injectivity, not safety, and it is no worse
    than every version of this field before the raw path was used.

    THREE cases, because the first version of this test popped the key and nothing else, so it
    covered one of the accessor's two guards. The `isinstance(raw, bytes)` check is load-bearing
    and not defensive: a `str` reaching the scrub's `f"%{byte:02X}"` raises `ValueError`, so a
    server supplying one would turn every audited rejection into a 500, and weakening the check to
    `raw is not None` left the whole suite green. A `bytearray` is the same shape of surprise from
    the other direction: it iterates to ints and would work, but it is not what the guard admits,
    so pinning the behaviour stops the guard being loosened to "anything iterable".
    """
    stream = io.StringIO()
    logger = build_logger(stream)
    config = make_config(tmp_path, PREE_TEAM_TOKEN=TEST_TOKEN)

    class _ReplaceRawPath:
        def __init__(self, app: Any) -> None:
            self._app = app

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                if supplied is None:
                    scope.pop("raw_path", None)
                elif supplied == "str":
                    scope["raw_path"] = scope["path"]
                else:
                    scope["raw_path"] = bytearray(scope["path"].encode())
            await self._app(scope, receive, send)

    store = JsonStore(config.data_dir)
    store.seed()
    app = create_app(
        config.for_service(),
        store,
        verify_token=token_verifier(config),
        logger=logger,
        prober=StorageProber(cache_seconds=0.0),
    )
    with TestClient(_ReplaceRawPath(app)) as stripped:
        refused = stripped.get("/diagnostics", headers={"x-pree-token": "wrong"})
        assert refused.status_code == 401, refused.text

    records = [json.loads(line) for line in stream.getvalue().splitlines() if '"path"' in line]
    assert records, f"the rejection was not audited with raw_path as {supplied}"
    assert records[0]["path"] == "/diagnostics", (
        f"the fallback did not name the decoded path with raw_path as {supplied}: "
        f"{records[0]['path']!r}"
    )


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
    # FOUR inputs now, because the path is scrubbed as well as truncated and the two bound
    # different things. The escapes used to be the whole test: %01 rendered as six JSON bytes and
    # an emoji as a 12-byte surrogate pair, so 160 characters could cost nearly 2 KB a record. The
    # scrub strips both classes outright, which removes the amplification rather than bounding it,
    # so a legitimate-character path is now the case that exercises the truncation.
    with build_client(config, logger, StorageProber(cache_seconds=0.0)) as bounded:
        # `%F0%9D%90%80` is U+1D400, an astral LETTER, and it is the input that distinguishes the
        # two charsets: `\w` in a str pattern keeps it, so it survived the scrub and cost twelve
        # bytes each as a surrogate escape. 160 characters wrote 1,802 bytes against the 416 this
        # test asserts. The emoji is not a letter and was always stripped, which is why choosing
        # it hid the hole rather than finding it.
        for escaped in ("%01", "%F0%9F%98%80", "%F0%9D%90%80", "a"):
            path = "/v1/assessments/" + escaped * 4_000
            assert bounded.get(path, headers={"x-pree-token": "wrong"}).status_code == 401

    lines = [line for line in stream.getvalue().splitlines() if "auth_reject" in line]
    assert len(lines) == 4, f"expected one audit line per rejection, got {len(lines)}"
    paths = [json.loads(line)["path"] for line in lines]
    for line, path in zip(lines, paths, strict=True):
        assert len(path) <= MAX_LOGGED_PATH, f"the logged path is {len(path)} characters"
        # One byte per character now, plus the JSON envelope, because every character that cost
        # more than one has been scrubbed away rather than merely counted.
        assert len(line) <= MAX_LOGGED_PATH + 256, (
            f"audit line is {len(line)} bytes, above the bound the scrub and truncation imply"
        )
        assert path.isascii() and path.isprintable(), (
            f"a non-ASCII or control character survived into the logged path: {path!r}"
        )
    # The legitimate-character case must actually reach the cap, or nothing exercised truncation.
    assert max(len(path) for path in paths) == MAX_LOGGED_PATH, (
        f"the truncation was never exercised: {[len(path) for path in paths]}"
    )


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
    app = create_app(
        config.for_service(),
        store,
        verify_token=token_verifier(config),
        logger=logger,
        prober=StorageProber(cache_seconds=0.0),
    )

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
    and never the wiring: replacing the factory's call with `pass` left the whole suite green.
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
    # The field's VALUE against an independent recomputation, across a matrix. Naming the field in
    # `AUDIT_BOOLEAN_FIELDS` closes nothing on its own, and asserting one direction each was
    # satisfiable by a leaking expression: `(origin == allowed) and bool(token[len(acrh)+1] & 1)`
    # short-circuits to False for a disallowed origin and reports True for the allowed one, so it
    # passed both directions while handing an unauthenticated caller one attacker-indexed bit of
    # the team token per preflight.
    #
    # The decisive axis is TWO DISTINCT TOKENS. Any token-derived expression changes when the token
    # changes while the origin does not, so the matrix catches the class rather than the member.
    # This is the same correlate-do-not-name reasoning the project already applied to duration_ms.
    # The SAME axis as `had_query`, and for the same measured reason. Two fixture tokens both
    # contain a hyphen, so `... and "-" in (config.team_token or "")` on this field stayed green
    # while shipping one real bit of the deployed credential per refused preflight. Two samples
    # catch only a predicate that disagrees between them.
    for token in (*_LEAK_PROBE_TOKENS, secrets.token_urlsafe(32)):
        for origin, allowed in (("https://pree.example", True), ("https://evil.test", False)):
            for extra in ({}, {"Access-Control-Request-Headers": "x-not-permitted"}):
                probe_stream = io.StringIO()
                probe_config = make_config(
                    tmp_path,
                    PREE_TEAM_TOKEN=token,
                    PREE_ALLOWED_ORIGIN="https://pree.example",
                )
                with build_client(
                    probe_config, build_logger(probe_stream), StorageProber(cache_seconds=0.0)
                ) as probe:
                    answer = probe.options(
                        "/v1/assess",
                        headers={
                            "Origin": origin,
                            "Access-Control-Request-Method": "POST",
                            **extra,
                        },
                    )
                if answer.status_code != 400:
                    continue
                refusals = [
                    json.loads(line)
                    for line in probe_stream.getvalue().splitlines()
                    if "cors_reject" in line
                ]
                assert len(refusals) == 1, f"{origin} left {len(refusals)} cors_reject lines"
                assert refusals[0]["origin_allowed"] is allowed, (
                    f"origin {origin!r} with token of length {len(token)} recorded "
                    f"origin_allowed={refusals[0]['origin_allowed']!r}, expected {allowed}; a "
                    f"value that changes with the token is a channel, not a fact about the origin"
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
        config.for_service(),
        store,
        verify_token=token_verifier(config),
        logger=build_logger(io.StringIO()),
        prober=StorageProber(cache_seconds=0.0),
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
    app = create_app(
        config.for_service(),
        store,
        verify_token=token_verifier(config),
        logger=build_logger(buffer),
        prober=prober,
    )
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
    app = create_app(
        config.for_service(),
        store,
        verify_token=token_verifier(config),
        logger=build_logger(buffer),
        prober=prober,
    )

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
    app = create_app(
        config.for_service(),
        store,
        verify_token=token_verifier(config),
        logger=quiet_logger,
        prober=prober,
    )
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
    `app.mount(...)` each served the team token unauthenticated with the whole suite green, and a
    WebSocketRoute carries a path and no methods at all.

    The documentation routes are the one exemption, by PATH rather than by type: FastAPI
    registers them as plain Starlette routes and they exist only when PREE_ENV is development.

    EXACT TYPE, not `isinstance`. A SUBCLASS of APIRoute satisfies isinstance while overriding
    `get_route_handler`, which is the request handler itself: registered through
    `add_api_route(..., route_class_override=SupportRoute)`, a subclass returned the team token to
    any caller sending a chosen header, with the whole suite green and 100% coverage. It defeated
    three controls at once. `app.router.route_class` stayed `APIRoute`, because the override is
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
# production with the whole suite green, and each defeated a different property of the previous
# version:
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
    production configuration with the whole suite green, confirmed against a real listener over the
    wire. Pinning types on the listener while asserting the gate elsewhere left the gate
    unasserted on the thing that runs.
    """
    # A reference app FastAPI builds for itself, so the documentation endpoints can be compared by
    # code-object identity rather than by any string an endpoint can set about itself.
    reference = FastAPI(docs_url="/docs", redoc_url="/redoc", openapi_url="/openapi.json")
    reference_codes = {
        route.path: route.endpoint.__code__
        for route in _all_routes(reference)
        if type(route) is Route and hasattr(route.endpoint, "__code__")
    }
    assert set(reference_codes) >= EXPECTED_DOC_PATHS, (
        f"the reference app exposes {sorted(reference_codes)}, missing "
        f"{sorted(EXPECTED_DOC_PATHS - set(reference_codes))}"
    )
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
            for kind, path, _methods, _origin, _qualname, _gated in foreign:
                # The origin string partitions the table and is deliberately NOT the assertion:
                # it derives from `co_filename`, which is whatever string was handed to `compile()`,
                # so asserting it only moved the forgery one step. The identity check below is the
                # assertion. `gated` is not asserted either, because it is derived only for an
                # APIRoute and is unconditionally False for a plain Route, so it never said
                # anything.
                assert (kind, path in EXPECTED_DOC_PATHS) == ("Route", True), (
                    f"the {env} listener carries a route this suite cannot account for: {path!r}"
                )
                # And the endpoint's CODE OBJECT, by identity against a reference app FastAPI
                # builds itself. `co_filename` is not identity: it is whatever string was handed to
                # `compile()`, so `compile(src, getsourcefile(fastapi.applications), "exec")` gives
                # any function this origin, and because it is a plain function Starlette wraps it in
                # its own `request_response` app, satisfying the callable check too. A code object
                # cannot be forged into being FastAPI's.
                assert path in reference_codes, f"no reference documentation route for {path!r}"
                serving = next(r for r in _all_routes(app) if getattr(r, "path", None) == path)
                assert serving.endpoint.__code__ is reference_codes[path], (
                    f"the {env} listener's {path!r} is not FastAPI's own endpoint"
                )
                # AND the origin, because the two defeat different attacks and the previous version
                # traded one for the other. Identity catches an endpoint compiled with FastAPI's
                # filename; origin catches a POISONED reference, where `FastAPI.setup` is replaced
                # at import so the reference and the app share the same forged code object and
                # identity holds. Neither alone is enough.
                assert serving.endpoint.__code__.co_filename == fastapi_applications.__file__, (
                    f"the {env} listener's {path!r} endpoint is defined in "
                    f"{serving.endpoint.__code__.co_filename}, not FastAPI's own module"
                )
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
class _ValueRule:
    """One interface for every string rule, so the dispatch cannot fail to recognise one.

    See `_check_audit_string` for what a duck-typed rule cost.
    """

    def __init__(self, description: str) -> None:
        self.description = description

    def rejects(self, value: str) -> bool:
        raise NotImplementedError


class _Pattern(_ValueRule):
    """A regex rule, matched with `fullmatch`.

    `re.match` with a `$` anchor accepts a trailing newline, because `$` matches before one, so
    every pattern here admitted the single character it exists to exclude: `"/v1/assess\n"`,
    `"token rejected\n"`, `"value_error\n"` and `"a:b\n"` were all accepted. A newline in a log
    value is the whole point of scrubbing one.
    """

    def __init__(self, pattern: str) -> None:
        super().__init__(pattern)
        self._compiled = re.compile(pattern.removeprefix("^").removesuffix("$"))

    def rejects(self, value: str) -> bool:
        return self._compiled.fullmatch(value) is None


class _OneOf(_ValueRule):
    def __init__(self, *permitted: str) -> None:
        super().__init__(f"one of {sorted(permitted)}")
        self._permitted = frozenset(permitted)

    @property
    def permitted(self) -> frozenset[str]:
        """Readable, so a drift guard can compare the pin against the application's own literals.

        A rule that can only answer "do you reject this?" cannot be asked "what do you permit that
        nothing emits?", and that second direction is the one the `confidence` pin failed in.
        """
        return self._permitted

    def rejects(self, value: str) -> bool:
        return value not in self._permitted


def _pattern_within(rule: _ValueRule) -> _Pattern | None:
    """The `_Pattern` a rule ultimately applies, through any marker wrapper, or None.

    One accessor so the newline sweep and its own completeness check ask the same question. They
    both asked `isinstance(rule, _Pattern)`, so wrapping the `path` pattern in a marker removed it
    from the sweep AND from the check that every pattern is swept, in one edit, with nothing red.
    """
    if isinstance(rule, _Pattern):
        return rule
    if isinstance(rule, _MarkerOr):
        return _pattern_within(rule.inner)
    return None


class _MarkerOr(_ValueRule):
    """Accepts one exact marker, or delegates.

    `sanitise_log_part` emits `[unprintable]` for a field name that scrubs to nothing, and the
    brackets are characters the scrub itself strips, so the marker fails an idempotence rule. A
    caller cannot forge it for the same reason, which is what makes accepting it exactly safe.
    """

    def __init__(self, marker: str, inner: _ValueRule) -> None:
        super().__init__(f"{marker!r} or {inner.description}")
        self._marker = marker
        self._inner = inner

    def rejects(self, value: str) -> bool:
        return value != self._marker and self._inner.rejects(value)

    @property
    def inner(self) -> _ValueRule:
        """The delegate, so wrapping a rule in a marker cannot exempt it from the canary sweep.

        Wrapping the `path` pattern in this class made it stop being a `_Pattern` by identity, so
        the trailing-newline sweep skipped it silently and the sweep's own completeness check
        agreed, because both asked `isinstance(rule, _Pattern)` of the wrapper. A marker changes
        which values are accepted; it does not change whether the delegate anchors correctly.
        """
        return self._inner


class _ScrubIdempotent(_ValueRule):
    """Rejects any value the shipped sanitiser would change, or that exceeds its cap.

    The rule is the application's own function rather than a charset restated beside it, because
    two hand-written charsets in this table had already drifted from the code they described. It is
    the right property for log-injection safety and it admits a non-Latin operator name; it does
    NOT carry secrecy, which is the separate substring and header checks' job.
    """

    def __init__(self, scrub: Callable[[str], str], name: str) -> None:
        _ValueRule.__init__(self, f"unchanged by {name} and within MAX_ACTOR_LENGTH")
        self._scrub = scrub

    def rejects(self, value: str) -> bool:
        return self._scrub(value) != value or len(value) > MAX_ACTOR_LENGTH


_ACTOR_SCRUBBED = _ScrubIdempotent(sanitise_actor, "sanitise_actor")
_LOG_PART_SCRUBBED = _ScrubIdempotent(sanitise_log_part, "sanitise_log_part")

# The BOOLEAN audit fields. A bool carries one bit, which is a covert channel across enough
# records, and the value scan returned early on every one of them. Naming them is the floor; the
# handler-level test asserts what each one actually reports.
# Each boolean audit field, and the test that asserts its VALUE against the request. A boolean is a
# one-bit channel and the value scan cannot see inside one, so a pinned NAME with an unpinned value
# is a free bit: the gate shipped `preflight_seen` on `cors_reject` with two table edits and nothing
# red. Deriving the field set from this registry means a new boolean cannot be named without naming
# the test that correlates it, which is this project's own rule applied to data instead of prose.
#
# The honest limit: nothing here proves the named test does what its name says. That gap is the one
# the register-row guard has, and it is closed differently - by
# `test_no_audit_expression_can_reach_the_deployed_credential`, which refuses a token-derived value
# in ANY audit field regardless of what pins it.
_BOOLEAN_CORRELATIONS = {
    "origin_allowed": "test_a_refused_cors_preflight_uses_the_same_contract_and_is_audited",
    "had_query": "test_the_query_bit_is_the_query_and_nothing_else_on_every_kind_that_emits_it",
}

# `had_query` joins `origin_allowed`, and it needs the same two-token treatment for the same
# reason: a boolean is a one-bit channel per record and the value scan cannot see inside one, so a
# pinned NAME with an unpinned value shipped a token bit per refused preflight once already. It is
# asserted in both directions on every kind that emits it, and the two-token axis is what makes
# that a class check rather than a member check.
AUDIT_BOOLEAN_FIELDS = frozenset(_BOOLEAN_CORRELATIONS)

AUDIT_STRING_VALUES: dict[str, _ValueRule] = {
    "kind": _OneOf(
        "audit",
        "auth_reject",
        "validation_reject",
        "http_reject",
        "cors_reject",
        "store_error",
    ),
    "action": _OneOf("assess", "read_assessment"),
    # DERIVED from the shipped scrub rather than restated beside it. A hand-written charset admitted
    # ASCII only while `_UNSAFE_LOG_CHARS` uses `\w`, so a legitimate operator name in a non-Latin
    # script would have failed a pin claiming to describe the application.
    "actor": _ACTOR_SCRUBBED,
    # The five outcomes the application actually emits. "created" and "refused" were here and
    # produced by nothing, which is a standing exemption rather than a pin.
    "outcome": _OneOf("ok", "error", "disclosed", "not_modified", "not_found"),
    # A LITERAL set, checked against the enum by a meta-assertion below rather than derived from
    # it: derived would move with any mutation of the enum, and a literal alone drifts silently.
    # This pin was wrong in two directions at once. It permitted `medium`, which
    # `ConfidenceTier` has never had, so that was a standing exemption for a value nothing emits;
    # and it omitted `moderate` and `insufficient`, both of which the application does emit, the
    # second observably from `POST /v1/assess` with `"indicators": {}`. The review found the
    # omission; the phantom entry was beside it.
    "confidence": _OneOf("high", "moderate", "low", "insufficient"),
    # The APPLICATION's own pattern. A hand-written charset admitted roughly 113 characters of
    # appended hex, so the token's hex appended to the audit key passed. STORE_KEY_PATTERN requires
    # exactly one colon with each half at most 64 characters, so an appended encoding overflows it.
    "key": _Pattern(STORE_KEY_PATTERN),
    # The charset the application can EMIT after scrubbing, not the wider printable range. The
    # wider one admitted characters the scrub removes, so a reader concluded the separator was
    # preserved when it was being deleted, and a slash-preserving change to the scrub was
    # invisible in both directions.
    # `(?a:` because `\w` in a str pattern is Unicode-aware, so this pin accepted every astral
    # letter the scrub exists to strip and could not have caught the charset regression that
    # `test_a_long_request_path_cannot_write_an_unbounded_audit_line` did. A pin wider than what
    # the application can emit is not a pin.
    # `_MarkerOr`, mirroring `loc`, because `sanitise_log_path` returns UNPRINTABLE_MARKER for an
    # empty target and `[`/`]` are outside this charset, so the rule refused a value the
    # application can emit. Latent (it needs a server supplying an empty `raw_path`) and it failed
    # closed, but it is the other half of the pin-versus-code divergence swept this round.
    "path": _MarkerOr(UNPRINTABLE_MARKER, _Pattern(r"^(?a:[\w./%@:-]{1,160})$")),
    "reason": _Pattern(r"^[ -~]{0,512}$"),
    # Inside `validation_reject.errors`, which the flat scan never reached. `loc` echoes a
    # caller-supplied field name and the application scrubs each part, so the same derivation
    # applies; `type` is pydantic's own vocabulary.
    "loc": _MarkerOr(UNPRINTABLE_MARKER, _LOG_PART_SCRUBBED),
    "type": _Pattern(r"^[a-z0-9_.]{0,64}$"),
}


# Every NUMERIC audit field, with a bound. An unbounded integer is a disclosure channel: the whole
# token fits inside one, and `int.from_bytes(token.encode(), "big")` on `duration_ms` decodes back
# to the credential exactly. The bounds are the ranges the application can legitimately produce.
# An absolute ceiling for a duration, alongside the correlated one. A handler that sleeps for the
# secret and reports its true duration inflates the correlated ceiling to fit; this one it cannot
# reach. Measured in-process at 0 to 2 ms per call.
ABSOLUTE_DURATION_CEILING_MS = 50
# The smallest JSON a rejected field can be inside an object: `"a":1,` is six bytes, so a body
# under MAX_BODY_BYTES cannot produce more than this many validation errors. Derived rather than
# measured, because a measurement is a floor and a bound needs a ceiling; the honest maximum the
# security gate could actually drive was 4,696, so this is loose by about a sixth rather than by
# the six-fold factor MAX_BODY_BYTES gave it.
MIN_BYTES_PER_REJECTED_FIELD = 6
MAX_VALIDATION_ERROR_COUNT = MAX_BODY_BYTES // MIN_BYTES_PER_REJECTED_FIELD

AUDIT_NUMERIC_BOUNDS: dict[str, tuple[float, float]] = {
    # Correlated at the call site, not bounded here: see the `bounds` override in the test.
    "duration_ms": (0, 0),
    "status": (400, 599),
    # The TRUE total, not the logged cap. `error_count` is `len(exc.errors())` while the `errors`
    # list is truncated to MAX_VALIDATION_ERRORS_LOGGED, which is the point of reporting both: an
    # operator sees that more were rejected than are shown. Bounding it by the cap was wrong, and
    # a body of twelve unknown fields proved it.
    #
    # Then MAX_BODY_BYTES was wrong the other way, and the comment here called it "the field count
    # a body can carry" when it is a BYTE count. `len(exc.errors()) + 20000` passed. The ceiling is
    # DERIVED below and the arithmetic is in the open, so the next reader can check it rather than
    # trust it.
    "error_count": (0, MAX_VALIDATION_ERROR_COUNT),
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
    "auth_reject": {"kind", "path", "had_query", "reason"},
    "validation_reject": {"kind", "path", "had_query", "error_count", "errors"},
    "http_reject": {"kind", "path", "had_query", "reason", "status"},
    # NO "reason". The CORS handler emits kind, path and origin_allowed only, so a pinned name that
    # is never produced is not a pin: it is a standing exemption, and `reason` permits 512
    # printable characters on a record any unauthenticated caller triggers with one refused
    # preflight. Measured: base64 of the team token in that field, the whole suite green.
    "cors_reject": {"kind", "path", "had_query", "origin_allowed"},
    "store_error": {"kind", "path", "had_query", "reason"},
}


class _Recorder(Protocol):
    """The `record` callable the exercise passes down.

    Typed, because the split that created this seam typed it `Any`, and a caller that dropped
    `docs=` would then have been a silent gap in header coverage rather than a type error.
    """

    def __call__(
        self, answer: Any, where: str, *, docs: bool = False, origin: str = ""
    ) -> None: ...


def _drive_every_error_shape(probe: TestClient, record: _Recorder) -> None:
    """Every error shape, each built by a different handler.

    A helper because the walk that calls it only ever produces 401s and 422s, and a header set on
    one of the others would have been invisible to it. Extracted so the caller stays under the
    statement limit rather than because the list is reusable.
    """
    record(probe.post("/v1/assess", headers=AUTH, json={"bad": TEST_TOKEN}), "422 assess")
    # A HOSTILE field name, so the `loc` rule bites on the input class it exists for. Without it
    # the rule was only ever handed `body`, `protected_asset_id` and `candidate_id`, so reverting
    # the application's scrub back to a bare length cap left the suite green.
    record(
        probe.post(
            "/v1/assess",
            headers=AUTH,
            json={'x"}\n{"kind":"audit","actor":"root"}\x1b[2J': 1},
        ),
        "422 hostile field name",
    )
    # A field name that scrubs to EMPTY, which is the one input `sanitise_log_part` exists for.
    record(probe.post("/v1/assess", headers=AUTH, json={"*": 1}), "422 unprintable field name")
    # ASTRAL LETTER field names, twelve of them. `\w` kept every one and each cost twelve bytes as
    # a surrogate escape, so this body wrote a 6,516-byte record against an assertion of 4,096.
    # Authenticated, so inside the shared-token exception, and the assertion was still false.
    record(
        probe.post(
            "/v1/assess",
            headers=AUTH,
            json={chr(0x1D400) * 70 + str(index): 1 for index in range(12)},
        ),
        "422 astral field names",
    )
    record(probe.get(f"/healthz?token={TEST_TOKEN}"), "query string")
    record(probe.request("DELETE", "/v1/assess", headers=AUTH), "405")
    record(probe.get("/nowhere-at-all", headers=AUTH), "404")
    # A control character in the PATH, so the `path` rule bites on its own input class. The rule
    # was correct and never fired, because nothing in the exercise sent such a path, which is how
    # the audited path stayed unscrubbed while `loc` beside it was scrubbed.
    record(probe.get("/v1/%1b%5b2J", headers=AUTH), "404 control character in the path")
    record(probe.post("/v1/assess", headers=AUTH, content=b"x" * (MAX_BODY_BYTES + 1)), "413")
    record(
        probe.options(
            "/v1/assess",
            headers={"origin": "https://evil.example", "access-control-request-method": "POST"},
        ),
        "cors preflight",
    )


def _exercise_every_surface(
    tmp_path: Path, prober: StorageProber
) -> tuple[list[str], list[str], str, int]:
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
        # Every request timed, by wrapping the one method the others call through. The slowest
        # single request is the honest ceiling for a per-request duration field: bounding it by the
        # whole exercise's elapsed time left about 7.5 bits a record, so `token[i] % 128` still
        # passed and 32 writes carried a 32-character token. A timing field is a covert channel of
        # its bound's width, and only correlating it against an independent measurement narrows it
        # to the width of a real measurement.
        timings: list[int] = []
        issue = probe.request

        def timed(*args: Any, **kwargs: Any) -> Any:
            began = time.monotonic()
            try:
                return issue(*args, **kwargs)
            finally:
                timings.append(int((time.monotonic() - began) * 1000))

        probe.request = timed  # type: ignore[method-assign]
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
        _drive_every_error_shape(probe, record)
        # A SUCCESSFUL privileged call, so a success audit record reaches the stream. The walk
        # above sends `json={}` everywhere, which is a 422, so only REJECTION lines were ever
        # grepped: `token=config.team_token` in the success audit call wrote the shared credential
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
    return bodies, leaked_headers, stream.getvalue(), max(timings, default=0)


def test_the_team_token_reaches_no_response_body_header_or_log_record(
    tmp_path: Path, prober: StorageProber
) -> None:
    """The credential is compared, never echoed, never logged, in any channel.

    /diagnostics reports the token's LENGTH and whether it is set, deliberately, so a caller can
    confirm the deployment without learning the value.
    """
    bodies, leaked_headers, log, _slowest = _exercise_every_surface(tmp_path, prober)
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
        # A bool is PINNED BY NAME, not skipped. `origin_allowed` was in the field list and in
        # nothing else, and this early return meant no bool was ever checked, so
        # `origin_allowed = bool(token[0] & 1)` shipped a one-bit-per-record channel on an event
        # any unauthenticated caller triggers at will, with the whole suite green.
        if field not in AUDIT_BOOLEAN_FIELDS:
            offending.append(f"{where} is a boolean no rule pins: {value!r}")
        return
    if isinstance(value, str):
        offending.extend(_check_audit_string(field, value, where))
        return
    if isinstance(value, (int, float)):
        offending.extend(_check_audit_number(field, value, where, bounds))
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


def _check_audit_string(field: str, value: str, where: str) -> list[str]:
    """One string against its rule, failing closed on a rule this cannot evaluate."""
    offending: list[str] = []
    allowed = AUDIT_STRING_VALUES.get(field)
    # FAIL CLOSED on a rule this cannot evaluate. The chain of isinstance arms had no else, so
    # an unrecognised rule type accepted everything it governed in silence.
    if allowed is None:
        offending.append(f"{where} is a string no rule pins: {value!r}")
    elif not isinstance(allowed, _ValueRule):
        # mypy proves this unreachable from the dict's annotation, and that is exactly why the
        # RUNTIME guard stays: the previous version's types said the same and the dispatch still
        # accepted every value under a rule it did not recognise. A type is a claim about the code
        # as written; this is the behaviour when the claim stops holding.
        offending.append(  # type: ignore[unreachable]
            f"{where}: rule type {type(allowed).__name__} is not evaluated"
        )
    elif allowed.rejects(value):
        offending.append(f"{where}={value!r} fails: {allowed.description}")
    return offending


def _check_audit_number(
    field: str, value: float, where: str, bounds: dict[str, tuple[float, float]]
) -> list[str]:
    """One number against its bound. An unbounded number is a covert channel of any width."""
    bound = bounds.get(field)
    if bound is None:
        return [f"{where} is a number no bound pins: {value!r}"]
    if not bound[0] <= value <= bound[1]:
        return [f"{where}={value!r} outside {bound}"]
    return []


# A known-bad value for every string rule, so a rule that cannot reject anything is red. This table
# is checked by neither gate otherwise: mypy sees nothing wrong with an unreachable branch and
# coverage measures `src/` only.
# A TRAILING NEWLINE per pattern rule, because reverting `_Pattern` to `re.match` with a `$`
# anchor left the whole suite green: `$` matches before a newline, so every pattern admitted the
# one character it exists to exclude, and the anchor-strip half of the fix was the only part any
# canary could see. Same class as the round's own major: a fix whose absence nothing detects.
AUDIT_PATTERN_NEWLINE_CANARIES: dict[str, str] = {
    "key": "a:b\n",
    "path": "/v1/assess\n",
    "reason": "token rejected\n",
    "type": "value_error\n",
}

AUDIT_RULE_CANARIES: dict[str, str] = {
    "kind": "not_a_kind",
    "action": "exfiltrate",
    "actor": 'ops"}\n{"kind":"audit","actor":"root"}',
    "outcome": "created",
    "confidence": "certain",
    "key": "no-colon-at-all",
    # A control character, which is what the path rule exists to exclude. The canary used to be
    # "/has a space", and a space is neither dangerous in a log value nor removed by the scrub, so
    # once the rule's charset matched what the application can emit the canary stopped biting.
    "path": "/v1/\x1b[2J",
    "reason": "\x1b[2Jclear screen",
    "loc": "body\r\ninjected",
    "type": "NotLowercase",
}


# Every audit field pinned as a CLOSED SET of literals. `kind` was outside this sweep and the gate
# proved the consequence: `"phantom_kind_nothing_emits"` added to its `_OneOf` left 332 tests green,
# because `kind` had only the emit-to-pin direction, through EXPECTED_AUDIT_KEYS, and nothing
# checked the other way. The register row claimed "in both directions" for all of them, which was
# false for exactly this field.
# The logger names an audit record is written through. Both, because the two modules name the same
# object differently and a one-name walk silently missed the module that builds the `audit` record.
_AUDIT_LOGGER_NAMES = frozenset({"audit_log", "logger"})
_LOG_EMIT_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log"}
)
CLOSED_SET_AUDIT_FIELDS = ("kind", "action", "outcome")


class _Emission(NamedTuple):
    """One expression whose value can reach an audit record, with enough context to judge it.

    `where` names the module and line, because an error message that hardcodes one module lies as
    soon as the walk covers two: mine said "app.py:135" for a line in `audit.py`. `parameters` are
    the enclosing function's parameter names, which is what distinguishes a PASS-THROUGH from a
    computation - `audit()` forwards `"action": action` from its own signature, which carries no
    value of its own, while `outcome="harvest" if ord(token[0]) & 1 else "ok"` computes one.
    """

    where: str
    field: str
    expression: ast.expr
    parameters: frozenset[str]


def _enclosing_parameters(tree: ast.Module) -> dict[int, frozenset[str]]:
    """Which function's parameters are in scope at each line.

    Needed to tell a PASS-THROUGH from a computation: `audit()` forwarding `"action": action` from
    its own signature originates no value, while an expression that computes one does.
    """
    scopes: dict[int, frozenset[str]] = {}
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        names = frozenset(
            argument.arg
            for group in (
                function.args.posonlyargs,
                function.args.args,
                function.args.kwonlyargs,
            )
            for argument in group
        )
        for inner in ast.walk(function):
            line = getattr(inner, "lineno", None)
            if line is not None:
                scopes[line] = scopes.get(line, frozenset()) | names
    return scopes


def _bound_record_dicts(tree: ast.Module) -> dict[str, ast.Dict]:
    """Dict literals bound to a name, so a record BUILT then logged is reachable.

    `audit.py` does exactly that: `record = {...}` then `logger.info(json.dumps(record, ...))`. A
    call-argument-only walk therefore reported the `kind` pin's `audit` entry as unemitted, which
    was the third time a partial walk here produced a confident wrong answer.
    """
    bound: dict[str, ast.Dict] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign | ast.AnnAssign) or not isinstance(node.value, ast.Dict):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                bound[target.id] = node.value
    return bound


def _audit_call_fields(node: ast.Call, parameters: list[str]) -> list[tuple[str, ast.expr]]:
    """Each (field, expression) an `audit()` call supplies, positional and keyword.

    Positional arguments are bound to the real signature rather than to hard-coded indices,
    because an index would be a second copy of the parameter order and would drift from it.
    """
    fields: list[tuple[str, ast.expr]] = [
        (parameters[index], value)
        for index, value in enumerate(node.args)
        if index < len(parameters)
    ]
    fields.extend((argument.arg, argument.value) for argument in node.keywords if argument.arg)
    return fields


def _resolve_payload(argument: ast.expr, bound: dict[str, ast.Dict]) -> ast.Dict | None:
    """The dict literal a logger argument actually carries, or None if it cannot be followed.

    Unwraps the `json.dumps(...)` the application wraps every record in, then follows at most one
    level of name binding, which is what `audit.py` uses. Anything else - a `dict(...)` call, a
    comprehension, a second level of binding - returns None and is COUNTED as unresolved, so it
    fails loudly instead of being skipped while a decoy elsewhere in the same call kept the count
    at zero.
    """
    inner = argument
    while isinstance(inner, ast.Call) and inner.args:
        inner = inner.args[0]
    if isinstance(inner, ast.Dict):
        return inner
    if isinstance(inner, ast.Name) and inner.id in bound:
        return bound[inner.id]
    return None


def _emissions_in(module: ModuleType) -> tuple[list[_Emission], int]:
    """Every audit emission expression in one module, and how many payloads went unresolved.

    ONE function per module rather than a loop with a closure over per-module state: the closure
    version bound the scope map from the enclosing loop, which is the late-binding bug that reads
    correctly only because every call happens in the same iteration.
    """
    path = Path(module.__file__ or "").resolve()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    scopes = _enclosing_parameters(tree)
    bound = _bound_record_dicts(tree)
    audit_parameters = list(inspect.signature(audit).parameters)
    found: list[_Emission] = []
    unresolved = 0

    def emit(value: ast.expr, field: str) -> None:
        found.append(
            _Emission(
                f"{path.name}:{value.lineno}",
                field,
                value,
                scopes.get(value.lineno, frozenset()),
            )
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = node.func
        if isinstance(called, ast.Name) and called.id == "audit":
            for field, value in _audit_call_fields(node, audit_parameters):
                emit(value, field)
            continue
        if not isinstance(called, ast.Attribute) or not isinstance(called.value, ast.Name):
            continue
        # EMITTING methods only. `addHandler`, `setLevel` and `addFilter` are calls on the same
        # object that write no record, and counting them as unresolvable payloads made this walk
        # refuse the tree it was measuring.
        if called.value.id not in _AUDIT_LOGGER_NAMES or called.attr not in _LOG_EMIT_METHODS:
            continue
        # The payload ACTUALLY passed, resolved from the call's first positional argument through
        # at most one level of name binding. Walking the whole argument for any dict literal was
        # bypassable by a decoy: a `dict(...)` payload beside any unreached dict literal in the same
        # call left `unresolved` at zero, so the emitted record's fields went unchecked while this
        # guard reported success. That is the decoy trick already recorded for the `outcome` pin,
        # one level up.
        payloads = []
        for argument in node.args:
            resolved_payload = _resolve_payload(argument, bound)
            if resolved_payload is None:
                unresolved += 1
            else:
                payloads.append(resolved_payload)
        for record in payloads:
            for key, value in zip(record.keys, record.values, strict=True):
                emit(value, str(key.value if isinstance(key, ast.Constant) else "?"))
    return found, unresolved


def _audit_emission_expressions() -> list[_Emission]:
    """Every expression whose value can reach an audit record, across both emitting modules.

    ONE accessor, because three separate checks need the same set and three hand-rolled walks over
    it is three places to miss a call site.

    BOTH modules, named explicitly. `app.py` builds the five rejection records and calls `audit()`;
    `audit.py` builds the `audit` record itself, and the literal `"kind": "audit"` lives only there.
    """
    found: list[_Emission] = []
    unresolved = 0
    for module in (app_module, audit_module):
        module_found, module_unresolved = _emissions_in(module)
        found.extend(module_found)
        unresolved += module_unresolved
    assert not unresolved, (
        f"{unresolved} audit logger calls have a payload this walk cannot resolve to a dict "
        f"literal, so their fields are unchecked by every caller of this accessor"
    )
    assert found, "no audit payload was resolved at all, so every check over this is vacuous"
    return found


# The ONE config attribute an audit expression may read, and the field it may read it for. Every
# other attribute of the config object is off limits inside a record, which is what makes this a
# closure rather than a sample.
PERMITTED_AUDIT_CONFIG_READ = ("origin_allowed", "allowed_origin")
# Every `config.<attribute>` the HTTP layer reads, pinned exactly. A new one is a named failure a
# reviewer sees in the diff, which is the point: this list is short because the layer needs little,
# and `team_token` is deliberately not on it.
EXPECTED_CONFIG_READS = frozenset({"allowed_origin", "is_production", "data_dir"})


def test_every_boolean_audit_field_names_a_test_that_correlates_it() -> None:
    """A boolean pinned by name only is a free bit, so the set is derived from a registry.

    The gate shipped `preflight_seen` on `cors_reject` carrying a token bit with two table edits and
    nothing red, because `AUDIT_BOOLEAN_FIELDS` was a literal and the two correlations were bespoke
    matrices nothing bound to it. Deriving the set from `_BOOLEAN_CORRELATIONS` means a new boolean
    cannot be registered without naming the test that checks its value.

    This asserts the named test EXISTS, which is existence and not aboutness - the same gap the
    control-register guard has. It is worth having anyway, because it turns "add a boolean and edit
    two tables" into "add a boolean and write a test", and the credential channel it used to open is
    closed independently by the scope rule below.
    """
    module = sys.modules[__name__]
    for field, test_name in _BOOLEAN_CORRELATIONS.items():
        assert hasattr(module, test_name), (
            f"the boolean audit field {field!r} names {test_name!r} as its correlation, and "
            f"no such test exists in this module"
        )
    assert frozenset(_BOOLEAN_CORRELATIONS) == AUDIT_BOOLEAN_FIELDS, (
        "the boolean field set is no longer derived from the correlation registry, so a field can "
        "be pinned by name with nothing asserting its value"
    )


# The functions permitted to read the credential, and nothing else in the whole package. An
# ALLOWLIST of readers, because the previous protection was a denylist of spellings and the
# attacker picks the spellings.
# Keyed on (MODULE, FUNCTION), not on a bare function name. A bare name gave any module a free read
# by calling a helper `for_service` or `authorise`, and a bare `load_config` gave any module a free
# environment read: a review planted exactly those and neither guard saw them. The pair is what
# makes this an allowlist of PLACES rather than of words.
CREDENTIAL_READERS = frozenset(
    {
        # The closure that hands the HTTP layer a callable instead of a secret, and the arming of
        # the runtime output guard, which needs the plaintext to compare against. `authorise` is
        # NOT here any more: it was a second copy of the compare that no served request reached,
        # so it is now a thin caller of the closure and reads nothing.
        ("security.py", "token_verifier"),
        ("security.py", "arm_output_guard"),
        # Config's own derivations of the token-free facts the service layer receives.
        ("config.py", "auth_enabled"),
        ("config.py", "for_service"),
    }
)


def test_the_credential_has_exactly_one_set_of_readers_across_the_whole_package() -> None:
    """The BLOCKER's fix, and the reason the previous version of this rule was never going to hold.

    The old protection was two static rules: refuse `config.<attr>` inside an expression that
    reaches an audit record, and pin which `config` attributes `app.py` reads. A security review
    defeated both twice in nine lines, because both check the SPELLING of a name and the attacker
    chooses the names:

      ● a helper in `security.py` (unwalked, because the emission walk covers two modules) called
        as `rejection_reason(config, exc)` from the `auth_reject` record, and
      ● a helper in `app.py` whose parameter was named `cfg`, so `cfg.team_token` was not a read
        of anything named `config`.

    Each recovered the deployed token verbatim from the pod log on every unauthenticated 401, with
    the whole loop green at 335 tests.

    The structural fix is that the HTTP layer no longer has the secret in its object graph:
    `create_app` takes a `ServiceConfig`, which has no token field, plus a callable closed over the
    credential. This test is what keeps that true. It is an ALLOWLIST over the whole package rather
    than a denylist of spellings, so a new reader is a named failure wherever it is added and
    whatever it calls its parameter.
    """
    source_root = Path(app_module.__file__ or "").resolve().parent
    offenders: list[str] = []
    for module_path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        # Which function each line belongs to, so a read can be attributed to its enclosing name.
        owner: dict[int, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for inner in ast.walk(node):
                    line = getattr(inner, "lineno", None)
                    if line is not None:
                        owner.setdefault(line, node.name)
        for node in ast.walk(tree):
            # ANY attribute named team_token, on any base, under any spelling of that base. That is
            # the whole point: `cfg.team_token`, `c.team_token` and `self.team_token` all count.
            if not isinstance(node, ast.Attribute) or node.attr != "team_token":
                continue
            where = owner.get(node.lineno, "<module level>")
            if (module_path.name, where) in CREDENTIAL_READERS:
                continue
            offenders.append(f"{module_path.name}:{node.lineno} in {where}()")
    assert not offenders, (
        f"the deployed credential is read outside its permitted readers "
        f"{sorted(CREDENTIAL_READERS)}: {offenders}. A reader anywhere else can pass the value to "
        f"anything, including an audit record, and no rule about how a name is spelt will catch it"
    )


# Introspection attributes that reach past a name into an object's guts, and the one place the
# process environment may be read. An ALLOWLIST of one reader and a denylist of LANGUAGE features,
# which is the distinction that matters: the alphabet here is Python's and fixed, not the author's
# and chosen, so unlike a list of variable spellings it cannot be one short.
_INTROSPECTION_ATTRIBUTES = frozenset(
    {
        # The METHOD spelling of attribute access, which was the decisive bypass: the whole dunder
        # list below is reachable through `x.__getattribute__("__closure__")`, and the classifier
        # returned None for any call whose func was an attribute, so nothing saw it.
        "__getattribute__",
        "__getattr__",
        "__setattr__",
        "__delattr__",
        "__closure__",
        "cell_contents",
        "__globals__",
        "__wrapped__",
        "__dict__",
        "__self__",
        "__func__",
        "__code__",
        "__defaults__",
        "__kwdefaults__",
    }
)
_INTROSPECTION_CALLS = frozenset({"globals", "vars", "locals", "eval", "exec", "compile"})
# `load_config` is the only function that may read the process environment. Anywhere else, an
# `os.environ` read is a second source of truth for the credential that bypasses every boundary.
# Also keyed on (MODULE, FUNCTION), for the same measured reason.
ENVIRONMENT_READERS = frozenset({("config.py", "load_config")})


def _attribute_complaint(node: ast.Attribute, owner: dict[int, str], module: str) -> str | None:
    """What is wrong with one attribute access, or None."""
    if node.attr in _INTROSPECTION_ATTRIBUTES:
        return f"introspects {node.attr}"
    if node.attr == "environ" and (module, owner.get(node.lineno)) not in ENVIRONMENT_READERS:
        return (
            f"reads os.environ in {owner.get(node.lineno)}(), outside {sorted(ENVIRONMENT_READERS)}"
        )
    return None


def _format_field_complaint(node: ast.Constant) -> str | None:
    """Whether a string literal is a format template whose FIELD PATH reaches an attribute.

    `"{0.__closure__[0].cell_contents}".format(x)` reaches two attributes without producing a
    single `ast.Attribute` node, because the path lives inside a string constant. That blinded both
    the introspection guard and the reader allowlist, and it is why `string.Formatter` is used here
    rather than another regular expression over source: the format mini-language is a real grammar
    with a real parser, and reimplementing it is how the next gap gets introduced.
    """
    if not isinstance(node.value, str) or "{" not in node.value:
        return None
    try:
        fields = [field for _, field, _, _ in Formatter().parse(node.value) if field]
    except ValueError:
        # An unparseable template cannot be a working leak, but it also cannot be cleared, so it is
        # reported rather than skipped.
        return "contains an unparseable format template"
    for field in fields:
        for part in field.replace("[", ".").replace("]", ".").split("."):
            if part in _INTROSPECTION_ATTRIBUTES or part == "team_token":
                return f"has a format field path reaching {part!r}"
    return None


def _call_complaint(node: ast.Call) -> str | None:
    """What is wrong with one call, or None."""
    if not isinstance(node.func, ast.Name):
        return None
    if node.func.id in _INTROSPECTION_CALLS:
        return f"calls {node.func.id}()"
    # A COMPUTED attribute name is the whole point: `getattr(x, "team" + "_token")` reaches an
    # attribute no static rule about attribute NAMES can see.
    if node.func.id == "getattr" and len(node.args) >= 2:
        name = node.args[1]
        if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
            return "calls getattr with a computed name"
    return None


def _introspection_complaint(node: ast.AST, owner: dict[int, str], module: str) -> str | None:
    """What is wrong with one node, or None. Split so each half stays inside the branch limit."""
    if isinstance(node, ast.Attribute):
        return _attribute_complaint(node, owner, module)
    if isinstance(node, ast.Call):
        return _call_complaint(node)
    if isinstance(node, ast.Constant):
        return _format_field_complaint(node)
    return None


# Every route a review has actually used to reach the credential, spelled as the source it would
# appear in. This list is NOT the control - it is a set of witnesses that the control is not inert.
# The control is a runtime check on the emitted bytes, which is what makes it indifferent to
# routes nobody has thought of.
_MEASURED_LEAK_ROUTES = (
    'verify_token.__getattribute__("__closure__")[0].__getattribute__("cell_contents")',
    "verify_token.__closure__[0].cell_contents",
    '"{0.__closure__[0].cell_contents}".format(verify_token)',
    '"{0.team_token}".format(config)',
    'os.getenv("PREE_TEAM_TOKEN")',
    'environ.get("PREE_TEAM_TOKEN")',
    'inspect.getclosurevars(verify_token).nonlocals["expected"]',
    'operator.attrgetter("team_token")(config)',
    'dataclasses.asdict(config)["team_token"]',
    "pickle.dumps(config)",
    "config.__getstate__()",
    "config.__reduce__()",
)


def test_the_runtime_guard_refuses_the_channels_it_covers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control that is not an enumeration, and the reason it had to exist.

    Three static guards were each defeated by stepping outside the set they enumerated. Sampled
    tokens lost to a predicate constant across the sample. A denylist of name spellings lost to a
    parameter called `cfg`. A denylist of language features lost to NINE routes, decisively
    `verify_token.__getattribute__("__closure__")` - because attribute access has a method
    spelling, a string spelling (`str.format`, `operator.attrgetter`) and a library spelling
    (`inspect`, `pickle`, `dataclasses`), and no list of those is closed.

    This checks the BYTES leaving the process against the actual secret, at the last point before
    they leave, on both channels a leak has actually used: the audit logger and stdout, which is
    the pod log the platform aggregates. It is indifferent to how the value was obtained, which is
    exactly what the enumerations were not.

    Every route in `_MEASURED_LEAK_ROUTES` is a witness rather than a case: the guard sees only the
    resulting string, so the assertion below is over the OUTPUT of a leak, not its source form. What
    the list documents is that each was measured to work against the static guards, which is why
    those are now described as refusing named spellings rather than as closing the class.
    """
    secret = "Zq7-Wx9_Yt2.Ur5~Ip8Ok1Aj4Sh6Dg3F"
    sink = io.StringIO()
    # Captured BEFORE arming, so the guard wraps this sink exactly as it wraps the real stdout.
    monkeypatch.setattr(sys, "stdout", sink)
    install_credential_guard(secret)
    try:
        # stdout, which is what every measured route used.
        for route in _MEASURED_LEAK_ROUTES:
            print(f"leak via {route}: {secret}")
        print("a benign line that must survive")
        emitted = sink.getvalue()

        # And the audit logger, independently.
        logged = io.StringIO()
        handler = logging.StreamHandler(logged)
        handler.setFormatter(logging.Formatter("%(message)s"))
        audit_logger = logging.getLogger("pree.audit")
        previous, previous_level = audit_logger.handlers, audit_logger.level
        audit_logger.handlers = [handler]
        # The LEVEL explicitly, because a logger left at the WARNING default emits nothing for
        # `info` and the trail would be empty. An empty trail satisfies "the secret is absent"
        # trivially, which is the vacuous-pass shape this suite keeps finding, so the count
        # assertions below are what make it non-vacuous and the level is what lets them fire.
        audit_logger.setLevel(logging.INFO)
        try:
            audit_logger.info('{"kind":"auth_reject","reason":"token rejected %s"}', secret)
            audit_logger.info('{"kind":"auth_reject","reason":"token rejected"}')
        finally:
            audit_logger.handlers = previous
            audit_logger.setLevel(previous_level)
        trail = logged.getvalue()
    finally:
        install_credential_guard(None)

    assert secret not in emitted, "the credential reached stdout, which is the pod log"
    assert emitted.count(CREDENTIAL_ALARM) == len(_MEASURED_LEAK_ROUTES), (
        f"expected one alarm per measured route, got "
        f"{emitted.count(CREDENTIAL_ALARM)} of {len(_MEASURED_LEAK_ROUTES)}"
    )
    assert "a benign line that must survive" in emitted, (
        "the guard suppressed a line carrying no credential, so it is a denial of service on the "
        "log rather than a control on it"
    )
    assert secret not in trail, "the credential reached the audit stream"
    assert trail.count(CREDENTIAL_ALARM) == 1, f"expected exactly one alarmed record: {trail!r}"
    assert '"reason":"token rejected"' in trail, "the guard suppressed a clean audit record"


def test_the_runtime_guard_survives_a_broken_record_and_a_bare_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard's own edge paths, exercised rather than left as unmeasured lines.

    Three lines were uncovered after the guard landed, and an unexercised line in a security control
    is the class this suite has been burned by twice: a fallback nobody drives is where a crash or a
    silent pass waits. So each is driven.

    A record whose `%`-formatting raises (wrong argument count) must not become an emitted secret
    and must not take the process down: the filter lets it through unscrubbed, because a record that
    cannot be rendered cannot be scanned, and logging would otherwise swallow the exception and emit
    it anyway. The `isatty` and `encoding` fallbacks exist because the wrapper forwards only what a
    stream user actually touches, and something asking a wrapped stream for either must not get an
    AttributeError.
    """
    secret = "Ip8Ok1Aj4Sh6Dg3F-Zq7-Wx9_Yt2.Ur5"
    logged = io.StringIO()
    handler = logging.StreamHandler(logged)
    handler.setFormatter(logging.Formatter("%(message)s"))
    audit_logger = logging.getLogger("pree.audit")
    previous, previous_level = audit_logger.handlers, audit_logger.level
    # PROPAGATE explicitly. This test passed only because an EARLIER test in the file had called
    # `build_logger()` and left `propagate=False` on the process-wide logger: run in isolation, with
    # `-k`, or under a shuffle, the record reached pytest's capture handler, whose `handleError`
    # re-raises, and the test failed with `TypeError: not enough arguments for format string`. A
    # test that depends on another test's side effect is not evidence about the code, and the three
    # guard lines it covers were not independently established.
    previous_propagate = audit_logger.propagate
    audit_logger.handlers = [handler]
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False
    install_credential_guard(secret)
    try:
        # Two format placeholders, one argument: `getMessage()` raises inside the filter. The
        # lint rule that flags this is exactly right in production code, and this is the one place
        # the malformed record IS the input under test, so it is silenced here and nowhere else.
        audit_logger.info("a broken record %s %s", "only-one")  # noqa: PLE1206
    finally:
        audit_logger.handlers = previous
        audit_logger.setLevel(previous_level)
        audit_logger.propagate = previous_propagate
        install_credential_guard(None)
    assert secret not in logged.getvalue(), "a record that could not be rendered leaked the secret"

    # The stream fallbacks, on a stream that has neither attribute.
    class _Bare:
        # `write` returns None, which is legal for a stream and is the case the guard's `or 0`
        # exists for: `int(None)` raised, so a guard installed over such a stream took the process
        # down on the first write. A review named it; this is what drives it.
        def write(self, text: str) -> None:
            return None

        def flush(self) -> None:
            return None

    wrapped = _GuardedStream(_Bare())
    assert wrapped.isatty() is False
    assert wrapped.encoding == "utf-8"
    wrapped.flush()
    # A stream whose `write` returns None is legal, and `int(None)` used to raise here.
    assert wrapped.write("plain") == 0

    # The passthroughs a review measured as breaking real callers by their absence:
    # `subprocess(stdout=sys.stdout)` needs `fileno`, `faulthandler.enable()` needs it too, and
    # `print(file=sys.stdout.buffer)` needs `buffer`. Their absence broke nothing shipped today,
    # which is exactly why it would have been found by an operator rather than by this suite.
    real = _GuardedStream(sys.__stdout__)
    assert isinstance(real.fileno(), int)
    assert real.buffer is not None
    lines_written: list[str] = []

    class _Recording:
        def write(self, text: str) -> int:
            lines_written.append(text)
            return len(text)

    recorder = _GuardedStream(_Recording())
    recorder.writelines(["one", "two"])
    assert lines_written == ["one", "two"], "writelines does not reach the stream"

    # And arming must be REVERSIBLE. It used to leave the wrapper installed for the life of the
    # process, so every later test ran against a wrapped stdout with no way back.
    original = sys.stdout
    install_credential_guard(secret)
    assert isinstance(sys.stdout, _GuardedStream)
    install_credential_guard(None)
    assert sys.stdout is original, "disarming did not restore the real stream"


def test_the_runtime_guard_covers_handlers_it_was_never_told_about() -> None:
    """The bypass that failed the previous round, and why the fix is at HANDLER level.

    The guard was attached to three logger NAMES and it replaced the `sys.stdout` object. A review
    measured seven channels straight past it, and two independent causes:

      ● A logger-level filter does not run for a record propagated from a DESCENDANT - only the
        ancestor's handlers do - so `pree.audit.child` walked past the filter on `pree.audit`.
      ● `gunicorn.Arbiter.setup` builds its handlers before the worker imports the app factory, so
        they hold the PRE-WRAP stream. Measured under the shipped launch command:
        `gunicorn.error`'s handler had `guarded=False` and `filters=[]`. Replacing `sys.stdout`
        does nothing for an object that already captured the old one.

    Three lines in a handler then put the credential in the aggregated pod log on an unauthenticated
    401 with 343 tests and both linters green.

    So the filter attaches to handlers, existing and future, and a handler holding the pre-wrap
    stream is re-pointed at the wrapper. `Handler.handle` runs handler filters for every record that
    reaches it, whatever logger emitted it, which removes the name enumeration.

    Every channel below is a logger this module was NEVER told about, with its handler built BEFORE
    arming, which is exactly gunicorn's ordering.
    """
    secret = "Zq7-Wx9_Yt2.Ur5~Ip8Ok1Aj4Sh6Dg3F"
    sink = io.StringIO()
    unknown_names = ("gunicorn.error", "uvicorn.error", "some.brand.new.name", "")
    restore: list[tuple[logging.Logger, list[logging.Handler], int, bool]] = []
    for name in unknown_names:
        logger = logging.getLogger(name)
        restore.append((logger, logger.handlers, logger.level, logger.propagate))
        handler = logging.StreamHandler(sink)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
    # And a DESCENDANT of the one logger the old version did filter, which propagates to it.
    parent = logging.getLogger("pree.audit")
    child = logging.getLogger("pree.audit.child")
    restore.append((parent, parent.handlers, parent.level, parent.propagate))
    restore.append((child, child.handlers, child.level, child.propagate))
    parent_handler = logging.StreamHandler(sink)
    parent_handler.setFormatter(logging.Formatter("%(message)s"))
    parent.handlers = [parent_handler]
    parent.setLevel(logging.INFO)
    parent.propagate = False
    child.setLevel(logging.INFO)

    install_credential_guard(secret)
    try:
        for name in unknown_names:
            logging.getLogger(name).error("leak via %s: %s", name or "root", secret)
        child.info("leak via a propagated descendant: %s", secret)
        # A handler built AFTER arming, which the construction patch must cover.
        later = logging.getLogger("made.after.arming")
        restore.append((later, later.handlers, later.level, later.propagate))
        later_handler = logging.StreamHandler(sink)
        later_handler.setFormatter(logging.Formatter("%(message)s"))
        later.handlers = [later_handler]
        later.setLevel(logging.INFO)
        later.propagate = False
        later.error("leak via a handler built after arming: %s", secret)
        emitted = sink.getvalue()
    finally:
        install_credential_guard(None)
        for logger, handlers, level, propagate in restore:
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate

    expected_channels = len(unknown_names) + 2
    assert secret not in emitted, (
        f"the credential reached a channel the guard was not told about: {emitted!r}"
    )
    assert emitted.count(CREDENTIAL_ALARM) == expected_channels, (
        f"expected one alarm per channel ({expected_channels}), got "
        f"{emitted.count(CREDENTIAL_ALARM)}: {emitted!r}"
    )


def test_the_guard_repoints_a_handler_that_captured_the_stream_before_arming() -> None:
    """The half the guard's own docstring calls necessary, and no test held it.

    Deleting the re-point left the whole suite green at 344 passed. In-process it is not cosmetic:
    for any handler that existed before arming - which is exactly gunicorn's ordering, since
    `Arbiter.setup` builds its handlers before the worker imports the factory - both
    `handler.emit(record)` and `handler.stream.write(...)` put the credential in the pod log in
    plaintext. Replacing the `sys.stdout` OBJECT does nothing for a handler holding the old one.

    Driven through `emit` directly rather than through a logger, because that is the path a
    re-pointed stream is the only defence on: the filter would catch a record routed through
    `handle`, so a test that only logs cannot tell the two halves apart.
    """
    secret = "Ur5~Ip8Ok1Aj4Sh6Dg3F-Zq7-Wx9_Yt2"
    sink = io.StringIO()
    original_stdout = sys.stdout
    sys.stdout = sink
    try:
        captured = logging.StreamHandler(sys.stdout)
        captured.setFormatter(logging.Formatter("%(message)s"))
        assert captured.stream is sink, "the handler did not capture the pre-arm stream"
        install_credential_guard(secret)
        try:
            assert isinstance(captured.stream, _GuardedStream), (
                "the handler still holds the pre-wrap stream, so replacing sys.stdout bought "
                "nothing for it"
            )
            assert captured.stream.wrapped is sink, "re-pointed at the wrong wrapper"
            # `emit`, not a logger call: this is the path where the stream IS the only defence.
            captured.emit(logging.LogRecord("x", logging.ERROR, "f", 1, "leak %s", (secret,), None))
            # And the write path on the same handler.
            captured.stream.write(f"direct write {secret}\n")
        finally:
            install_credential_guard(None)
        emitted = sink.getvalue()
    finally:
        sys.stdout = original_stdout

    assert secret not in emitted, f"a pre-arm handler leaked the credential: {emitted!r}"
    assert emitted.count(CREDENTIAL_ALARM) == 2, (
        f"expected an alarm from both emit and write, got {emitted.count(CREDENTIAL_ALARM)}: "
        f"{emitted!r}"
    )


def test_the_guard_finds_a_handler_the_private_registry_has_forgotten() -> None:
    """Why the handler walk has TWO sources, asserted rather than asserted-in-prose.

    The docstring says "either alone has a gap" and nothing exercised the second source: coverage
    reported the manager walk as a miss on a green run, and deleting it left 344 passed.

    It is genuinely load-bearing. `logging.config.dictConfig({"version": 1})` clears
    `logging._handlerList`, so a handler still attached to a logger becomes invisible to the private
    registry and is found only by walking the manager's loggers.
    """
    secret = "Ip8Ok1Aj4Sh6Dg3F-Ur5-Zq7-Wx9_Yt2x"
    sink = io.StringIO()
    logger = logging.getLogger("forgotten.by.the.registry")
    previous, previous_level, previous_propagate = (
        logger.handlers,
        logger.level,
        logger.propagate,
    )
    handler = logging.StreamHandler(sink)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # Clear the private registry, exactly as dictConfig does, leaving the handler attached.
    registry = getattr(logging, "_handlerList", None)
    saved = list(registry) if registry is not None else []
    if registry is not None:
        registry.clear()
    try:
        assert handler not in [
            reference() if callable(reference) else reference
            for reference in list(getattr(logging, "_handlerList", []))
        ], "the private registry still knows the handler, so this test proves nothing"
        install_credential_guard(secret)
        try:
            logger.error("leak via a forgotten handler: %s", secret)
            emitted = sink.getvalue()
        finally:
            install_credential_guard(None)
    finally:
        if registry is not None:
            registry.extend(saved)
        logger.handlers = previous
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate

    assert secret not in emitted, f"a handler missing from the private registry leaked: {emitted!r}"
    assert emitted.count(CREDENTIAL_ALARM) == 1, f"expected one alarm: {emitted!r}"


def test_the_guard_covers_the_pre_wrap_dunder_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """`sys.__stdout__` holds the PRE-WRAP object, one underscore from the covered name.

    A review measured the consequence: `print(token, file=sys.__stdout__)` and
    `sys.__stdout__.write(token)` both reached the pod log in plaintext. Neither is an fd-level
    write nor a re-encoding, so both sat outside the two limits the guard named while the register
    claimed its covered set was stated exactly. Same shape as the gunicorn hole - a live reference
    to the pre-wrap object held somewhere the guard did not re-point - and wrappable, so a missed
    wrap rather than a boundary.

    Driven through `monkeypatch` and read back through a deliberately untyped accessor, because
    typeshed declares this attribute `Final` and `TextIOWrapper | None`: assigning it directly makes
    mypy treat every following line as unreachable, which would silently delete the assertions.
    """
    secret = "Sh6Dg3F-Zq7-Wx9_Yt2.Ur5~Ip8Ok1Aj"
    sink = io.StringIO()

    def dunder_stdout() -> Any:
        return sys.__stdout__

    monkeypatch.setattr(sys, "stdout", sink)
    monkeypatch.setattr(sys, "__stdout__", sink)
    install_credential_guard(secret)
    try:
        assert isinstance(dunder_stdout(), _GuardedStream), "the dunder stream is unwrapped"
        # ONE wrapper shared, or a handler re-pointed at one is not recognised via the other.
        assert dunder_stdout() is sys.stdout, "stdout and its dunder hold different wrappers"
        print(f"leak via the dunder stream: {secret}", file=dunder_stdout())
        dunder_stdout().write(f"direct dunder write: {secret}\n")
        emitted = sink.getvalue()
    finally:
        install_credential_guard(None)

    assert dunder_stdout() is sink, "disarming did not restore the dunder stream"
    assert secret not in emitted, f"the dunder stream leaked the credential: {emitted!r}"
    assert emitted.count(CREDENTIAL_ALARM) == 2, f"expected two alarms: {emitted!r}"


def test_the_guard_scans_the_whole_rendered_record_and_not_only_the_message() -> None:
    """`getMessage()` is not the rendered record, and the gap between the two was a live leak.

    `Formatter.format` appends the exception text and then the stack text AFTER the formatted
    message, so a scan of the message alone let `logger.error("auth failed", exc_info=...)` put the
    credential on the emitted line with the guard armed and no alarm raised. The filter's own
    docstring said "any log record whose rendered text contains the credential" for the whole of
    that round, which is the failure mode this suite keeps meeting: the sentence was right and the
    code implemented a narrower thing.

    Three parts, because a formatter reaches the credential three ways: `exc_info` it renders
    itself, `exc_text` it finds already cached by an earlier handler and reuses verbatim, and
    `stack_info` it appends as given. Alarming `msg` closes none of them, so the guard clears all
    three on a refusal.

    Driven on a handler whose stream is NOT a wrapped `sys` stream, deliberately. That is where the
    wrapper half cannot cover for the filter half, and it is the shape of every handler that writes
    to a file, a socket, or a test sink.
    """
    secret = "Wx9-Yt2_Ur5.Ip8~Ok1Aj4Sh6Dg3FZq7"
    sink = io.StringIO()
    # Built BEFORE arming, so the handler walk is what attaches the filter rather than the patched
    # constructor, and the stream is a sink no wrapper owns so no re-point can happen.
    handler = logging.StreamHandler(sink)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("rendered.record.scan")
    previous, previous_level, previous_propagate = (
        logger.handlers,
        logger.level,
        logger.propagate,
    )
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    def bare_record(**fields: Any) -> logging.LogRecord:
        record = logging.LogRecord(
            name=logger.name,
            level=logging.ERROR,
            pathname=__file__,
            lineno=0,
            msg="an authentication failure",
            args=(),
            exc_info=None,
        )
        for field, value in fields.items():
            setattr(record, field, value)
        return record

    def reject(message: str) -> None:
        """Raise from a frame of its own, so the traceback under test is a real one."""
        raise ValueError(message)

    install_credential_guard(secret)
    try:
        # exc_info, rendered by the formatter from a traceback that genuinely happened.
        try:
            reject(f"token rejected: {secret}")
        except ValueError:
            logger.error("an authentication failure", exc_info=True)
        # exc_text, the cache an earlier handler's formatter leaves behind.
        handler.handle(bare_record(exc_text=f"ValueError: token rejected: {secret}"))
        # stack_info, appended by the formatter exactly as given.
        handler.handle(bare_record(stack_info=f'  File "x", line 1, in f\n    token = "{secret}"'))
        # A traceback carrying NO credential must survive intact, or the guard is a control on
        # diagnosability rather than on the credential.
        try:
            reject("token rejected")
        except ValueError:
            logger.error("a benign authentication failure", exc_info=True)
        guarded = sink.getvalue()

        # NON-VACUOUS by construction. With the guard disarmed, the same record on the same handler
        # must reach the same sink in plaintext: without this half, a formatter that never emitted
        # the traceback at all would satisfy every assertion above. It also shows what disarming
        # does and does not undo - the filter is still attached and is inert, not removed.
        install_credential_guard(None)
        sink.truncate(0)
        sink.seek(0)
        try:
            reject(f"token rejected: {secret}")
        except ValueError:
            logger.error("an authentication failure", exc_info=True)
        unguarded = sink.getvalue()
    finally:
        install_credential_guard(None)
        logger.handlers = previous
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate

    assert secret in unguarded, (
        "the traceback channel does not reach this sink even unguarded, so the assertions below "
        "prove nothing about the guard"
    )
    assert secret not in guarded, f"the credential reached the log through a traceback: {guarded!r}"
    assert guarded.count(CREDENTIAL_ALARM) == 3, (
        f"expected one alarm for each of exc_info, exc_text and stack_info: {guarded!r}"
    )
    assert "ValueError: token rejected" in guarded, (
        "the guard stripped a traceback that carried no credential, so it costs every diagnosis "
        "rather than the leaking ones"
    )


def test_the_guard_does_not_crash_the_boot_on_a_handler_whose_stream_is_read_only() -> None:
    """The re-point runs on the BOOT path, so an `AttributeError` there is CrashLoopBackOff.

    `handler.stream = wrapper` assumes an assignable attribute, and a handler is free to expose
    `stream` as a read-only property or to slot it. That handler is reached by the walk in
    `install_credential_guard`, which runs inside `main.build()` before the app binds: the guard
    would not have leaked the credential, it would have stopped the worker importing the app, and a
    control that takes the process down is one an operator removes.

    Both halves are asserted, because the soft failure is only acceptable if the other half holds:
    arming completes, and the record is still alarmed by the FILTER even though the stream could not
    be re-pointed.
    """
    secret = "Yt2-Ur5_Ip8.Ok1~Aj4Sh6Dg3FZq7Wx9"
    sink = io.StringIO()

    class _ReadOnlyStream(logging.Handler):
        """A handler whose captured stream cannot be reassigned."""

        def __init__(self, captured: Any) -> None:
            self._captured = captured
            super().__init__()

        @property
        def stream(self) -> Any:
            return self._captured

        def emit(self, record: logging.LogRecord) -> None:
            self._captured.write(self.format(record) + "\n")

    handler = _ReadOnlyStream(sink)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("read.only.stream")
    previous, previous_level, previous_propagate = (
        logger.handlers,
        logger.level,
        logger.propagate,
    )
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # `sys.stdout` IS this sink, so the wrapper the guard installs owns it and the re-point
    # branch is entered rather than skipped. Without this the test would pass on a handler the
    # guard never tried to re-point, which measures nothing.
    saved_stdout = sys.stdout
    sys.stdout = sink
    try:
        install_credential_guard(secret)
        try:
            logger.error("leak past a read-only stream: %s", secret)
            emitted = sink.getvalue()
        finally:
            install_credential_guard(None)
    finally:
        sys.stdout = saved_stdout
        logger.handlers = previous
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
        handler.close()

    assert handler.stream is sink, "the read-only stream was somehow reassigned"
    assert secret not in emitted, (
        f"the filter half did not cover a handler whose stream could not be re-pointed: {emitted!r}"
    )
    assert emitted.count(CREDENTIAL_ALARM) == 1, f"expected one alarm: {emitted!r}"


def test_the_guard_reports_the_callers_length_when_it_refuses_a_write() -> None:
    """The one item of a round's six findings that nothing held: reverting it left the suite green.

    `write` may legally return a SHORT count, and its contract lets the caller loop on the
    remainder. Returning the ALARM's length therefore tells a looping caller that the tail of the
    refused text is still unwritten, and the tail of a refused text is the credential. `print` and
    `StreamHandler` both discard the return value, which is what made this a trap rather than a
    fault, and what made it invisible to every test that drove the guard through either.

    So the caller's loop is DRIVEN here rather than described. Under `return written` it runs a
    second time on the credential-bearing tail, which is the harm; a test that only compared two
    numbers would report the same failure without showing it.
    """
    secret = "Ur5-Ip8_Ok1.Aj4~Sh6Dg3FZq7Wx9Yt2"
    accepted: list[str] = []

    class _Counting:
        def write(self, text: str) -> int:
            accepted.append(text)
            return len(text)

        def flush(self) -> None:
            return None

    stream = _GuardedStream(_Counting())
    alarm = f"{CREDENTIAL_ALARM}\n"
    prefix = (
        "a diagnostic line long enough that the alarm is much shorter than what comes "
        "before the credential, with room to spare: "
    )
    leak = f"{prefix}{secret}\n"
    # META-ASSERTION on the fixture. The tail a short count re-submits has to carry the WHOLE
    # credential, or this test measures a wrong number rather than a leak.
    assert secret in leak[len(alarm) :], "the fixture does not exercise the harm"

    install_credential_guard(secret)
    try:
        reported = stream.write(leak)
        accepted.clear()
        remaining, rounds = leak, 0
        while remaining and rounds < 5:
            rounds += 1
            remaining = remaining[stream.write(remaining) :]
    finally:
        install_credential_guard(None)

    assert reported == len(leak), (
        f"the guard reported {reported} for a {len(leak)}-character refused write, so a caller "
        f"looping until everything is written re-submits its last {len(leak) - reported} characters"
    )
    assert rounds == 1, f"the caller's write loop ran {rounds} times for one refused write"
    assert accepted == [alarm], f"the refused text reached the stream on a retry: {accepted!r}"


def test_the_runtime_guard_is_armed_by_the_boot_path_and_not_by_the_factory() -> None:
    """WHERE it is armed is part of the control, so it is asserted rather than assumed.

    `main.build()` arms it immediately after `load_config()`, before any line can be written and
    before the boundary that drops the credential. Arming it in `create_app` instead would leave
    the boot line itself unguarded, and the boot line is the one that reports the token's length.
    """
    source = Path(main_module.__file__ or "").resolve().read_text(encoding="utf-8")
    tree = ast.parse(source)
    build = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "build"
    )
    calls = [
        node.func.id
        for node in ast.walk(build)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    # `arm_output_guard`, not `install_credential_guard`. The read of `config.team_token` lives in
    # `security.py` with the credential's other readers, so `build()` never touches the value: it
    # hands the Config to a named reader instead. Putting the read here would have made a fifth
    # reader in a third module, which is the argument that removed the boot line's own read.
    assert "arm_output_guard" in calls, "the boot path does not arm the runtime output guard"
    assert calls.index("load_config") < calls.index("arm_output_guard"), (
        "the guard is armed before the config is resolved, so it cannot know the credential"
    )
    assert calls.index("arm_output_guard") < calls.index("create_app"), (
        "the app is built before the guard is armed, so early output is unguarded"
    )


# What each module in the package may import. THIS is the set that is genuinely small and fixed,
# which the attribute alphabet was not: a 900-statement package's import list is a dozen names per
# module and every addition is visible in a diff. The reviewer's own framing, and it is the right
# one - `inspect`, `pickle`, `operator`, `copy`, `gc`, `traceback` and `sys` are how attribute
# access is spelled as a LIBRARY call, and none of them belong in a module that handles requests.
EXPECTED_MODULE_IMPORTS: dict[str, frozenset[str]] = {
    "__init__.py": frozenset(),
    "api_models.py": frozenset({"__future__", "pydantic"}),
    "app.py": frozenset(
        {
            "__future__",
            "collections",
            "fastapi",
            "hashlib",
            "json",
            "logging",
            "starlette",
            "time",
            "typing",
        }
    ),
    # `sys` is here and nowhere else that serves a request: the output guard has to replace the
    # streams, and that is the one place in the package that needs to. `contextlib` is here for
    # `suppress`, on the three paths where a guard that raises is worse than the leak it prevents:
    # a record whose rendering raises, and a handler whose `stream` cannot be reassigned.
    "audit.py": frozenset({"__future__", "contextlib", "json", "logging", "sys", "typing"}),
    "config.py": frozenset({"__future__", "dataclasses", "os", "pathlib", "re"}),
    "health.py": frozenset(
        {
            "__future__",
            "collections",
            "concurrent",
            "dataclasses",
            "errno",
            "os",
            "pathlib",
            "threading",
            "time",
            "typing",
        }
    ),
    "main.py": frozenset({"__future__", "fastapi"}),
    "ratelimit.py": frozenset({"__future__", "collections", "threading", "time"}),
    "scoring.py": frozenset({"__future__", "dataclasses", "enum"}),
    "security.py": frozenset({"__future__", "collections", "hmac", "re"}),
    "store.py": frozenset(
        {
            "__future__",
            "collections",
            "contextlib",
            "fcntl",
            "json",
            "os",
            "pathlib",
            "shutil",
            "tempfile",
            "typing",
        }
    ),
}


def test_every_module_imports_exactly_what_it_is_permitted_to() -> None:
    """The structural half of closing the library-spelling route.

    Attribute access has a method spelling (`__getattribute__`), a string spelling (`str.format`,
    `operator.attrgetter`) and a LIBRARY spelling (`inspect.getclosurevars`, `pickle.dumps`,
    `dataclasses.asdict`, `gc.get_referrers`, `sys._getframe`). A review used six of those and the
    feature denylist saw none of them, because a denylist of attribute names cannot enumerate the
    libraries that reach attributes for you.

    The import set can be enumerated, and that is the asymmetry this test exploits: it is a dozen
    names per module, every addition shows in a diff, and `inspect`, `pickle`, `copy`, `gc`,
    `operator`, `traceback` and `sys` have no business in a module that handles a request. So the
    library route needs an import that is not on the list, which is a visible change rather than an
    invisible one.

    EXACT, in both directions. A permitted import nothing uses is a standing exemption for whatever
    arrives next, which is the lesson the ENV allowlist in the boot contract taught seven rounds
    running.
    """
    source_root = Path(app_module.__file__ or "").resolve().parent
    modules = sorted(path for path in source_root.rglob("*.py"))
    assert {path.name for path in modules} == set(EXPECTED_MODULE_IMPORTS), (
        f"the package's module set changed: "
        f"{sorted({p.name for p in modules} ^ set(EXPECTED_MODULE_IMPORTS))}"
    )
    for path in modules:
        imported: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        permitted = EXPECTED_MODULE_IMPORTS[path.name]
        assert imported == permitted, (
            f"{path.name} imports {sorted(imported - permitted)} which it is not permitted, and "
            f"is permitted {sorted(permitted - imported)} which it does not use. A library import "
            f"is how attribute access is spelled without naming an attribute"
        )


def test_no_module_reaches_the_credential_by_introspection_or_the_environment() -> None:
    """The route past the boundary, and the reason the previous claim was an overclaim.

    The structural fix took the credential out of the HTTP layer's object graph, and the register
    then claimed that "no helper, parameter name, module, or encoding in `app.py` can reach it".
    **That was false**, and a review took it in four lines, twice:

        cell = verify_token.__closure__[0].cell_contents
        return b32encode((getattr(cell, "team" + "_token", None) or "").encode()).decode()

    and

        return b32encode(os.environ.get("PREE_TEAM_TOKEN", "").encode()).decode()

    Either, appended to a `print` in this module, wrote the whole credential to the pod log the
    platform aggregates, with 337 tests green and all four scope tests passing. The reader allowlist
    matches `ast.Attribute` whose `attr` is `team_token`, and neither a computed `getattr` name nor
    a `repr` of a closure cell ever spells it.

    So this refuses the LANGUAGE FEATURES that reach past a name, rather than more spellings of a
    name. Two other narrowings landed with it and are recorded where they live: the cell now closes
    over the expected string rather than the whole `Config`, and `Config.team_token` is
    `repr=False`, which removes it from the DEFAULT dataclass `__repr__` and only from there: an
    explicit field path and every serialiser still render it. Two copies of this sentence claimed it
    "closes the class in one keyword"; both are corrected, and what covers the rest is the runtime
    guard, within the limits that guard states about itself.

    `rglob`, not `glob`. The reader allowlist walked only the top level while its docstring said
    "every module", so the first subpackage would have been outside it silently.
    """
    source_root = Path(app_module.__file__ or "").resolve().parent
    offenders: list[str] = []
    for module_path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        owner: dict[int, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for inner in ast.walk(node):
                    line = getattr(inner, "lineno", None)
                    if line is not None:
                        owner.setdefault(line, node.name)
        for node in ast.walk(tree):
            complaint = _introspection_complaint(node, owner, module_path.name)
            if complaint:
                offenders.append(f"{module_path.name}:{getattr(node, 'lineno', 0)} {complaint}")
    assert not offenders, (
        f"a module reaches past a name into an object's guts, or reads the process environment "
        f"outside {sorted(ENVIRONMENT_READERS)}: {offenders}. Either route reaches the credential "
        f"without ever spelling a token-shaped attribute, which is what the reader allowlist "
        f"cannot see"
    )


def test_the_config_never_prints_the_credential_in_any_representation(tmp_path: Path) -> None:
    """`repr=False` on the field, asserted rather than assumed.

    A review found that `repr()` of a `Config` printed `team_token='Zq7-Wx9_...'` verbatim, so an
    implicit render - `print(config)`, `f"{config}"`, an exception carrying one - disclosed the
    credential without spelling a token-shaped attribute.

    The keyword removes it from the DEFAULT `__repr__` and ONLY from there. An explicit field path
    (`"{0.team_token}".format(config)`) and every serialiser (`asdict`, `astuple`, `pickle.dumps`,
    `__getstate__`, `__reduce__`) still render it, all measured. Two copies of this docstring said
    the keyword "closes the class"; it closes one half of it.

    This drives the real renderings rather than inspecting the field metadata, because the metadata
    is what would be edited alongside the field.
    """
    secret = "Zq7-Wx9_Yt2.Ur5~Ip8Ok1Aj4Sh6Dg3F"
    config = make_config(tmp_path, PREE_TEAM_TOKEN=secret)
    assert config.team_token == secret, "the fixture did not carry the token at all"
    for rendering in (repr(config), str(config), f"{config}", format(config), f"{config!r}"):
        assert secret not in rendering, (
            f"a Config rendering discloses the credential: {rendering[:120]}"
        )
    # And the token-free view must be clean too, which it is by not having the field.
    assert secret not in repr(config.for_service())


def test_the_service_config_the_http_layer_receives_carries_no_credential() -> None:
    """The other half: the type itself must not have the field.

    The reader allowlist above constrains who may read `team_token`. This constrains what the HTTP
    layer is even given, which is what makes the class closed rather than policed: there is no
    attribute on `ServiceConfig` to reach, so no helper, parameter name, module or encoding in
    `app.py` can reach it.

    `token_length` is permitted and named explicitly. It is a precomputed integer, not the value,
    and `/diagnostics` needs it: without it a stale token is invisible on a deployed pod, because
    every client gets 401 and the read-out that would show it sits behind the wrong token.
    """
    fields = {field.name for field in dataclasses.fields(ServiceConfig)}
    assert "team_token" not in fields, (
        "ServiceConfig carries the credential, so the HTTP layer has it in scope again and the "
        "reader allowlist is the only thing standing between it and an audit record"
    )
    secret_shaped = {
        name for name in fields if "token" in name or "secret" in name or "key" in name
    }
    assert secret_shaped == {"token_length"}, (
        f"ServiceConfig carries secret-shaped fields beyond the permitted length: "
        f"{sorted(secret_shaped - {'token_length'})}"
    )
    # And the module must not import the full Config, which would let a caller hand one in.
    app_source = Path(app_module.__file__ or "").resolve().read_text(encoding="utf-8")
    imported = {
        alias.name
        for node in ast.walk(ast.parse(app_source))
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "Config" not in imported, (
        "app.py imports the full Config, so a caller can pass one and every expression in the "
        "module is back in reach of the credential"
    )


def test_no_audit_expression_can_reach_the_deployed_credential() -> None:
    """The CLASS closure, and the reason a sampled token axis was never going to be enough.

    Four rounds of this control were sampled: pick two tokens, then five, then five plus a random
    draw, and assert a boolean or a string does not move with them. Each round the gate found a
    predicate the sample did not separate, and the last round found several that survive a
    `token_urlsafe` draw with probability one - `"=" in token`, `"+" in token`, `len(token) < 64`.
    A sample can only raise the cost of a channel. It cannot close one, and saying "improbable
    rather than impossible" was the most that framing could ever honestly claim.

    This closes it instead. Every value that reaches an audit record was measured, and exactly ONE
    of them reads the config object: the `origin_allowed` comparison against `allowed_origin`. The
    deployed credential is reachable in this application only through `config`, because the token a
    caller presents is caller-supplied and echoing that is their own disclosure (a recorded
    residual), while `config.team_token` is the deployment's secret. So an audit expression that
    cannot read `config` cannot encode the credential, whatever encoding it would have used, and
    whatever the token happens to look like.

    Every leaking mutation the gate demonstrated goes through `config`: the token hex appended to a
    path, base64 in a `reason`, hex split across the two halves of a `key`, four token bytes in a
    `score` mantissa, an attacker-indexed bit in a boolean, and every one of those gated on
    `config.is_production` so a development-only exercise could not see it. This one rule refuses
    all of them at once, including the environment-gated forms, because the gate is itself a
    `config` read.
    """
    offenders: list[str] = []
    for emission in _audit_emission_expressions():
        for node in ast.walk(emission.expression):
            if not isinstance(node, ast.Attribute):
                continue
            base = node.value
            if not (isinstance(base, ast.Name) and base.id == "config"):
                continue
            if (emission.field, node.attr) == PERMITTED_AUDIT_CONFIG_READ:
                continue
            offenders.append(f"{emission.where} {emission.field}= reads config.{node.attr}")
    assert not offenders, (
        f"an audit expression reads the deployment's configuration, so it can encode the "
        f"credential in any form a shape check will accept: {offenders}. The only permitted read "
        f"is {PERMITTED_AUDIT_CONFIG_READ[0]}= config.{PERMITTED_AUDIT_CONFIG_READ[1]}"
    )
    # And the permitted read must still BE there, or this test is pinning a set of zero.
    reads = {
        (emission.field, node.attr)
        for emission in _audit_emission_expressions()
        for node in ast.walk(emission.expression)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "config"
    }
    assert reads == {PERMITTED_AUDIT_CONFIG_READ}, (
        f"the permitted audit config read is not the one that exists: {sorted(reads)}"
    )


def test_the_http_layer_never_reads_the_deployed_token_at_all() -> None:
    """The indirect route, closed by the same argument one level out.

    The rule above constrains an audit EXPRESSION, so it does not by itself stop
    `leaked = config.team_token` on a line above followed by `reason=leaked`. This closes that: the
    HTTP layer never reads `team_token`, so there is nothing in scope to bind. Comparison happens
    in `security.authorise`, which takes the config and returns an actor, and that is the whole of
    the credential's reach.

    The full read set is pinned rather than just the one name refused, because a denylist of
    attribute names would be one short the moment a config field is added - which is how the ENV
    name denylist in the boot contract failed seven rounds running before it became an allowlist.
    """
    source = Path(app_module.__file__ or "").resolve().read_text(encoding="utf-8")
    reads = {
        node.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "config"
    }
    assert "team_token" not in reads, (
        "the HTTP layer reads config.team_token, so the deployed credential is in scope for every "
        "expression in the module, including the ones that build audit records"
    )
    assert reads == EXPECTED_CONFIG_READS, (
        f"the HTTP layer's config reads have changed: added "
        f"{sorted(reads - EXPECTED_CONFIG_READS)}, removed "
        f"{sorted(EXPECTED_CONFIG_READS - reads)}. Each addition widens what an audit "
        f"expression could reach, so the set is pinned rather than the one name refused"
    )


def test_every_closed_set_pin_is_exactly_what_the_application_can_emit() -> None:
    """The CLASS, not just the `confidence` instance the review found.

    `kind`, `action`, `outcome` and `confidence` are each an exact literal set. `kind` was already
    backstopped by `EXPECTED_AUDIT_KEYS`, which fails on an unknown kind; the other three were
    literals nothing cross-checked, so adding a member the canary table does not happen to use is a
    silent one-of-N channel. `confidence` proved the risk is not theoretical: it permitted `medium`,
    which the enum has never had, and omitted two values the application does emit.

    The application's own literals are read from the SOURCE rather than imported, deliberately.
    Importing them would make the pin derived, so it would move with any mutation of the code it
    exists to constrain; reading the string literals the handlers actually pass is an independent
    measurement of the same fact.
    """
    emitted: dict[str, set[str]] = {field: set() for field in CLOSED_SET_AUDIT_FIELDS}
    # Collected from the EMISSION SITES ONLY, via the shared accessor, because a walk over every
    # dict literal in the module proved which literals EXIST, not which ones a record carries. The
    # gate demonstrated the difference: widening the `outcome` pin to include `"harvest"`, adding
    # two unrelated `{"outcome": ...}` decoy literals so the walk collected both, and changing the
    # emission to `outcome="harvest" if ord(token[0]) & 1 else "ok"` left the suite green with a
    # token bit shipping inside a "closed" set.
    #
    # So a closed-set field's emitted expression must be an `ast.Constant`. That is what refuses the
    # conditional, and it is stronger than collecting its branches would be: an expression is
    # either one fixed literal or it is a failure with its line number.
    for emission in _audit_emission_expressions():
        if emission.field not in emitted:
            continue
        expression = emission.expression
        # A PASS-THROUGH is not a computation. `audit()` forwards `"action": action` from its own
        # signature, which originates no value; the check is on the sites that supply one. An
        # earlier version refused the forward and reported it against the wrong module, because the
        # message hardcoded a filename while the walk covered two.
        if isinstance(expression, ast.Name) and expression.id in emission.parameters:
            continue
        assert isinstance(expression, ast.Constant) and isinstance(expression.value, str), (
            f"{emission.where} emits {emission.field}= as an expression rather than a literal "
            f"({ast.unparse(expression)!r}). A closed-set field computed at the call site can "
            f"carry a bit of anything in scope while every value it produces stays inside the pin"
        )
        emitted[emission.field].add(expression.value)
    for field, values in emitted.items():
        assert values, f"no literal {field!r} values found in app.py, so this check is vacuous"
        rule = AUDIT_STRING_VALUES[field]
        for emitted_value in values:
            assert not rule.rejects(emitted_value), (
                f"the application emits {field}={emitted_value!r} and the pin refuses it, so a "
                f"real record would fail the shape check: {rule.description}"
            )
        # And the pin must permit NOTHING beyond them, which is the direction `confidence` failed.
        assert isinstance(rule, _OneOf), f"{field!r} is no longer a closed-set rule: {rule!r}"
        assert rule.permitted == values, (
            f"the {field!r} pin and the application disagree: pin has "
            f"{sorted(rule.permitted - values)} that nothing emits, and is missing "
            f"{sorted(values - rule.permitted)} that it does"
        )


def test_the_confidence_pin_is_exactly_the_tiers_the_application_can_emit() -> None:
    """The meta-assertion that makes a literal pin safe to keep as a literal.

    The pin was wrong in both directions and the suite could not see either: it permitted `medium`,
    which `ConfidenceTier` has never had, so that entry was a standing exemption for a value
    nothing emits; and it omitted `moderate` and `insufficient`, both of which the application
    emits, the second from `POST /v1/assess` with an empty indicator set. The exercise happens to
    drive only two tiers, so neither error showed up as a failure.

    A DERIVED pin would have avoided this and bought a worse problem: it moves with any mutation of
    the enum, so adding a tier that carries caller data would satisfy the rule that exists to
    refuse it. The literal stays, and this asserts the literal and the enum agree, which turns
    drift into a named failure instead of a silent widening.
    """
    pinned = AUDIT_STRING_VALUES["confidence"]
    emitted = {tier.value for tier in ConfidenceTier}
    for value in emitted:
        assert not pinned.rejects(value), (
            f"the application can emit confidence={value!r} and the pin refuses it, so a real "
            f"record would fail the shape check: {pinned.description}"
        )
    for phantom in ("medium", "certain", "none", ""):
        if phantom in emitted:
            continue
        assert pinned.rejects(phantom), (
            f"the pin permits confidence={phantom!r}, which ConfidenceTier cannot produce; a "
            f"permitted value nothing emits is a standing exemption: {pinned.description}"
        )


def test_the_log_part_scrub_marks_an_unprintable_name_distinctly() -> None:
    """The one fact `sanitise_log_part` exists for, which nothing asserted.

    Reverting the application to `sanitise_actor` left the whole suite green: the two scrubs differ
    only in the empty case, and no test drove a field name that scrubs to nothing. `anonymous` is
    the sentinel for "no actor supplied", so reusing it made `{"*": 1}` indistinguishable from an
    anonymous caller in the field that exists for diagnosis.
    """
    assert sanitise_log_part("*") == UNPRINTABLE_MARKER
    assert sanitise_actor("*") == "anonymous"
    assert sanitise_log_part("*") != sanitise_actor("*")
    # And the marker cannot be forged: the brackets are characters the scrub strips.
    assert sanitise_log_part(UNPRINTABLE_MARKER) == "unprintable"


def test_every_audit_value_rule_can_actually_reject_something() -> None:
    """A rule nothing can fail is a rule nothing pins.

    `_ScrubIdempotent` was a duck-typed stand-in for `re.Pattern`, the dispatch recognised neither
    it nor anything else it did not expect, and there was no else, so every value it governed was
    accepted in silence. Poisoning its `match` with a raise left the whole suite green. This asserts
    the property that was missing: each rule is handed a value it must reject, and the dispatch is
    handed a rule type it does not know and must complain about.
    """
    assert set(AUDIT_RULE_CANARIES) == set(AUDIT_STRING_VALUES), (
        "every string rule needs a canary: "
        f"{sorted(set(AUDIT_STRING_VALUES) ^ set(AUDIT_RULE_CANARIES))}"
    )
    for field, bad in AUDIT_RULE_CANARIES.items():
        rule = AUDIT_STRING_VALUES[field]
        assert rule.rejects(bad), (
            f"the rule for {field!r} accepts {bad!r}, so it pins nothing: {rule.description}"
        )
    # Every PATTERN rule must reject a trailing newline. `re.match` with `$` accepts one, and a
    # pattern reached THROUGH a marker wrapper is still a pattern: asking `isinstance` of the
    # wrapper let `path` out of this sweep, and out of the completeness check below it, at the
    # moment it was wrapped.
    for field, bad in AUDIT_PATTERN_NEWLINE_CANARIES.items():
        rule = AUDIT_STRING_VALUES[field]
        assert _pattern_within(rule) is not None, f"{field!r} is no longer a pattern rule: {rule!r}"
        assert rule.rejects(bad), (
            f"the rule for {field!r} accepts a trailing newline, so `$` is anchoring rather than "
            f"`fullmatch`: {rule.description}"
        )
    assert set(AUDIT_PATTERN_NEWLINE_CANARIES) == {
        field for field, rule in AUDIT_STRING_VALUES.items() if _pattern_within(rule) is not None
    }, "every pattern rule needs a trailing-newline canary"
    # And EVERY fail-closed arm, because four of the five could be deleted with the suite green, in
    # the commit whose whole thesis is that a claim without a canary is a fact nobody asserts.
    for field, value, expected in (
        ("no_such_field", "anything", "is a string no rule pins"),
        ("no_such_number", 1, "is a number no bound pins"),
        ("no_such_flag", True, "is a boolean no rule pins"),
        ("path", object(), "which no rule pins"),
    ):
        complaints: list[str] = []
        _check_audit_value(field, value, f"canary.{field}", {}, complaints)
        assert complaints and expected in complaints[0], (
            f"the fail-closed arm for {field!r} did not fire: {complaints}"
        )
    # And the dispatch itself must refuse a rule it cannot evaluate, rather than passing the value.
    complaints = []
    _check_audit_value("actor", "anything", "canary.actor", {}, complaints)
    assert not complaints, complaints
    unknown: dict[str, Any] = {"actor": object()}
    with mock.patch.dict(AUDIT_STRING_VALUES, unknown, clear=False):
        complaints = []
        _check_audit_value("actor", "anything", "canary.actor", {}, complaints)
    assert complaints and "is not evaluated" in complaints[0], (
        f"the dispatch accepted a value under an unrecognised rule type: {complaints}"
    )


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
    _, _, log, slowest_ms = _exercise_every_surface(tmp_path, prober)
    emitted = [json.loads(line) for line in log.splitlines() if line.strip()]
    # The SLOWEST SINGLE REQUEST, not the whole exercise. No handler can have taken longer than the
    # request that contained it. The residual is honest and worth stating in bits rather than
    # implying closure: a ceiling of a few milliseconds still admits a handful of bits per record,
    # so a determined leak could spread a credential across many writes. Closing it entirely means
    # the application not reporting a duration at all, which would cost the operator the one field
    # that shows a slow store.
    # BOTH bounds. The correlated one is derived from a measurement the leak itself inflates: a
    # handler that sleeps for the secret and reports its true duration raises its own ceiling to
    # accommodate it, and passed. These are in-process calls measured at 0 to 2 ms, so an absolute
    # ceiling of 50 ms is generous and is not attacker-controlled.
    bounds = {
        **AUDIT_NUMERIC_BOUNDS,
        "duration_ms": (0, min(max(slowest_ms, 1), ABSOLUTE_DURATION_CEILING_MS)),
    }
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
    # The CALL SITE, not only the function. `sanitise_log_part` differs from `sanitise_actor` in the
    # empty case alone, so a unit test on the function passes whichever the application calls:
    # reverting `app.py` to `sanitise_actor` left every test green because `anonymous` satisfies the
    # `loc` rule too. This asserts what the application actually emitted for a field name that
    # scrubs away, which is the only thing that distinguishes them.
    scrubbed = [
        part
        for found in emitted
        if found["kind"] == "validation_reject"
        for error in found.get("errors", ())
        for part in error.get("loc", ())
    ]
    assert UNPRINTABLE_MARKER in scrubbed, (
        f"a field name that scrubs to nothing was not marked {UNPRINTABLE_MARKER!r}; the "
        f"application is using the actor scrub, whose empty case is the anonymous sentinel: "
        f"{sorted(set(scrubbed))}"
    )
    # The separator survives, which is what these two assertions check and ALL they check. The
    # comment here used to say "DISTINCT paths must stay distinct", which the assertions below do
    # not test at all: they check that one probe path appears verbatim and that some path starts
    # `/v1/`. Injectivity is asserted where it can be, as a property over colliding inputs, in
    # tests/test_security.py::test_the_path_scrub_maps_distinct_paths_to_distinct_records.
    logged_paths = {found["path"] for found in emitted if "path" in found}
    assert "/nowhere-at-all" in logged_paths, (
        f"the 404 probe's path is not in the trail as written: {sorted(logged_paths)}"
    )
    assert any(path.startswith("/v1/") for path in logged_paths), (
        f"no logged path retains its separator, so the field cannot identify its subject: "
        f"{sorted(logged_paths)}"
    )
    unexercised = sorted((set(AUDIT_STRING_VALUES) | set(AUDIT_NUMERIC_BOUNDS)) - seen)
    assert not unexercised, (
        f"these value rules are never exercised, so they permit rather than pin: {unexercised}"
    )
