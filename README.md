# Pree

Pree fuses orbital geometry, manoeuvre-cadence baselining, and photometric and radio-frequency
signatures from the Unified Data Library (UDL) into a single explainable, confidence-tiered score
that shows Protect and Defend operators which satellites hold their protected assets at risk,
and why.

## The governing idea

A threat score is only useful if the operator can see why it says what it says, and only
trustworthy if it admits what it does not know. Pree therefore does two things that a scoring
service usually does not:

● **An unknown is never invented.** A missing indicator is excluded from the weighting and
  reported as `TBC, re-verify`. It is never defaulted to a zero, a mean, or a safe value,
  because that would produce a confident score from no data.
● **Confidence is separate from the score.** The score says how threatening the evidence is.
  The confidence tier says how much of the evidence was actually present. A score of 100 at
  `insufficient` confidence is an honest statement, and a very different one from 100 at `high`.

## Quick start

```sh
uv venv --python 3.12 .venv
uv pip install --require-hashes -r requirements-dev.txt
sh scripts/verify.sh                    # the verification loop
PREE_ENV=development uv run --with-requirements requirements.txt \
  uvicorn --factory pree.main:build --reload --port 8080
```

`PREE_ENV` defaults to `production`, which is why the quick start sets `development`
explicitly. With no `PREE_TEAM_TOKEN` set, Pree runs with the auth gate open, which is
permitted in development only. In production the app refuses to start unless the token and
`PREE_ALLOWED_ORIGIN` are both set, because the server binds `0.0.0.0` and an open gate there
would expose the assessment store to anything that can reach the port.

The pipeline simulation (`sh scripts/simulate-pipeline.sh`) exits 0 when every stage including
the image build is green, and 2 when only the container leg is deferred to Continuous
Integration because no Docker daemon is reachable. Exit 2 is not a pass.

## The API

| Method and path | Gated | Purpose |
|---|---|---|
| `GET /`, `/healthz`, `/readyz`, `/livez`, `/ping` | no | liveness; 200, touches nothing |
| `GET /healthz/storage` | no | proves storage with a real write, races a hard timeout |
| `GET /diagnostics` | when a token exists | secret-free read-out: booleans and lengths, never values |
| `POST /v1/assess` | yes | score one candidate against one protected asset |
| `GET /v1/assessments/{key}` | yes | read a stored assessment, with ETag support |

`/openapi.json`, `/docs` and `/redoc` are served in development only, never in production.
Every response carries a locked `Content-Security-Policy` and the usual hardening headers.

```sh
curl -s localhost:8080/v1/assess \
  -H 'content-type: application/json' \
  -H 'x-pree-actor: watch-floor' \
  -d '{"protected_asset_id":"asset-01","candidate_id":"cand-99",
       "indicators":{"closest_approach_km":5.0,"manoeuvres_in_window":6,
                     "baseline_manoeuvres":1.0}}'
```

The response carries the score, the confidence tier, the evidence coverage, the indicators that
were missing, and one rationale line per indicator.

## Layout

```
src/pree/     the source: app.py is the factory, main.py the listener
tests/        the suite, run in-process through the factory
scripts/      verify.sh, package-appstore.sh, simulate-pipeline.sh
docs/         deployment parameters, security policy, changelog
Dockerfile    the whole build, at the root, flat
```

## Standards

Conventions are in `CLAUDE.md`; procedures are in `.claude/skills/`. The deployment parameters
table is in `docs/DEPLOYMENT.md` and the security policy, including every deliberately accepted
risk, is in `docs/SECURITY.md`.
