@.claude/bluestaq-rules.md

# Pree
Project facts only. Standing rules come from the import above and are updated through the kit.

This file carries the kit's template sections first, then Pree's own sections unchanged beneath
them. Nothing from the original file was removed when the kit was installed; the sections below
"Original project sections" are the file as it stood at commit 9584063, verbatim.

## Purpose
Pree fuses orbital geometry, manoeuvre-cadence baselining, and photometric and radio-frequency
signatures from the Unified Data Library (UDL) into a single explainable, confidence-tiered score
that shows Protect and Defend operators which satellites hold their protected assets at risk, and
why.

## Gate contract
Commands live in `.claude/gate.json`. Remote pipeline: Bluestaq App Store (SonarQube 80% coverage,
zero open issues, reviewed hotspots, SAST, DAST, secret detection, container scan). The local loop
is `sh scripts/verify.sh`; `sh scripts/simulate-pipeline.sh` simulates the platform and exits 2,
not 0, when the container leg is deferred for want of a Docker daemon. Exit 2 is not a pass.

## Controls (each must have a test that fails when removed)
Every row below was verified by locating the named test, and each has been killed by a mutation at
least once in review.

- Constant-time token compare: `src/pree/security.py` -> `tests/test_security.py::test_token_compare_invokes_the_constant_time_primitive`
- Route gating outside the probe set: `src/pree/app.py` -> `tests/test_api.py::test_every_route_outside_the_probe_set_carries_the_token_gate`
- Strict boundary validation: `src/pree/api_models.py` -> `tests/test_api.py::test_out_of_range_unknown_or_coercible_input_is_rejected_at_the_boundary`
- Body cap before the token gate: `src/pree/app.py` (`MAX_BODY_BYTES`) -> `tests/test_api.py::test_an_oversize_body_is_rejected_before_the_token_gate`
- Narrow CORS allowlists: `src/pree/app.py` -> `tests/test_api.py::test_the_cors_method_and_header_allowlists_are_the_narrow_ones`
- Limiter charges every key, never short-circuits: `src/pree/app.py` (`_first_refused`) -> `tests/test_api.py::test_a_refused_key_still_charges_every_other_bucket`
- Fail-closed origin shape: `src/pree/config.py` -> `tests/test_config.py::test_an_origin_that_is_not_a_concrete_origin_is_refused_in_any_environment`
- Credential output guard on the named streams: `src/pree/audit.py` -> `tests/test_api.py::test_the_runtime_guard_refuses_the_channels_it_covers`
- No handler that serialises a record reaches the process: `src/pree/audit.py` -> `tests/test_api.py::test_no_serialising_handler_reaches_the_process_registry`

## Guard scope
- Credential output guard scans: the finished line at `logging.Handler.format`, and the four `sys`
  text streams named in `GUARDED_STREAM_ATTRIBUTES` - `stdout`, `stderr`, `__stdout__`,
  `__stderr__`. The stderr half is load-bearing because the Dockerfile runs `--error-logfile -`.
- Handler sweep scans: `src/pree` with `rglob`, so subpackages are covered; held against a
  synthetic nested tree, because `glob` and `rglob` are indistinguishable on a flat package.
- Serialising-handler check scans: every handler in the live process registry after `create_app`,
  via the guard's own two-source walk, not just the ones `build_logger` constructs.
- Archive allowlist scans: path NAMES only. It cannot see inside an archived file, so a credential
  under an unlisted name still ships.
- NOT scanned: `.claude/skills/**/scripts/*.py`. `scripts/verify.sh` scopes `ruff` and `mypy` to
  `src` and `tests`, so the kit's and the gate skill's own tooling is linted by nothing. Recorded
  in `docs/SECURITY.md` rather than gated; it reaches neither shipped artefact.

## Runtime facts (confirmed, not assumed)
- Port: `PORT` env, default 8080, bound `0.0.0.0`. Verified live, not from text.
- Health: `GET /` -> 200 with `{"status":"ok","service":"pree","version":"0.1.0"}`, not a 302.
- Liveness paths: `/`, `/healthz`, `/readyz`, `/livez`, `/ping`, all 200 and unauthenticated,
  touching nothing. Readiness probe: `/readyz`.
- NO storage proof, and no storage. `/healthz/storage` and the whole store are gone: the
  platform's file storage is a mountpoint-s3 mount with no POSIX advisory locking, so the
  store's `flock` would silently do nothing and a second worker would erase the first. See
  `docs/APPSTORE-REDESIGN.md`. The container HEALTHCHECK probes `/healthz`.
- **DIVERGENCE FROM THE KIT TEMPLATE, deliberate.** The template states "Starts with no
  credentials (DAST runs unauthenticated): degraded mode, warning log, no crash". Pree does the
  opposite in production and this is a hard rule: with `PREE_ENV=production` and no
  `PREE_TEAM_TOKEN` it REFUSES TO START (`src/pree/config.py`, `_validate_production_auth`). It
  also refuses a token under 32 characters, a token built from repetition, a wildcard, `null` or
  a comma-list origin, and a cleartext origin in production. In development it starts without a
  token. If the App Store's DAST stage requires an unauthenticated boot, that is a conflict to
  resolve with the platform, not by weakening the boot check.
- **Authorisation is the platform's, with the token as fallback.** `X-Auth-Request-Role` gates:
  `VIEWER` reads, `EDITOR` and above write. Compared by RANK, never by name. An unrecognised
  role ranks as `VIEWER`; an EMPTY role is anonymous and below every gate, which is what a
  PUBLIC app sends on every request. A present-but-insufficient role is refused outright and
  never falls back to the token. Only `X-Auth-Request-Role` and `X-Auth-Request-Sub` are read:
  a header the platform does not set passes through from the client unchanged.
- **The audit `actor` is `X-Auth-Request-Sub`**, not a caller-supplied label. It was
  `x-pree-actor` until this release, which meant every line named whatever the caller typed.
- Log format: one line of JSON per privileged action on stdout, keys `kind`, `action`, `actor`,
  `outcome`, `duration_ms`, plus `key` where applicable. Access log is gunicorn's, with a
  truncating filter attached to `uvicorn.access` and `gunicorn.access`.
- Runtime user: `10001:10001`, non-root, numeric. Asserted against the built image in CI.

## Version locations
- `pyproject.toml` (`version`) and `src/pree/__init__.py` (`__version__`), which must agree. Both
  at `0.1.0`.
- `docs/CHANGELOG.md`, one row per change. Before the first release the stamp holds and only the
  changelog moves.
- Upload archive name: `Bluestaq_Limited_-_Pree_-_V0_1_-_<date>.zip`.
- `.claude/KIT_VERSION` records the installed Claude Code kit version.

## Units
- `closest_approach_km`: kilometres, source of truth `src/pree/api_models.py`.
- `relative_velocity_kms`: kilometres per second ON INPUT, source of truth
  `src/pree/api_models.py`. See the Pree section below for how this is rendered downstream.
- `manoeuvres_in_window`: a count. `baseline_manoeuvres`: a count, as a float.
- `photometric_sigma`: standard deviations from the object's own baseline, signed; the scorer uses
  the absolute value.
- `duration_ms`: milliseconds. (`probe_duration_ms` and `probe_timeout_ms` went with
  the storage proof.)
- `score`: 0 to 100. `evidence_coverage`: 0 to 1, weight present over total weight.

## Pree
- Pree is the upstream for code vendored into SPECTRE. Any change here that SPECTRE uses is flagged
  in the commit message as "SPECTRE port needed".
- Units: CONFIRMED from the code, 2026-11-13, resolving the `<UNCONFIRMED>` this line carried when
  it was pasted in. Pree's input field is `relative_velocity_kms`, kilometres per second, declared
  in `src/pree/api_models.py`; Pree's own rationale string also reads km/s. SPECTRE expects SI, so
  the vendored copy at `spectre/domain/pree_scoring.py` renders metres per second, CONVERTED and
  not merely relabelled. The score is unaffected: the arithmetic still runs on the km/s input and
  both copies return identical numbers for identical inputs. Only the human-readable sentence and
  the displayed unit differ, and `relative_velocity_kms` stays the field name because that is the
  unit callers pass.
  Every unit conversion has a known-value test. This one is
  `tests/unit/test_pree_scoring.py::test_the_velocity_rationale_is_converted_not_merely_relabelled`
  IN SPECTRE, not here, because that is where the conversion lives. It uses 0.25 km/s -> 250 m/s
  deliberately: a missed conversion prints 0 at that format precision and a double conversion
  250000, so neither can pass. It exists because SPECTRE's own house guard is string-based and
  CANNOT catch a label changed without its value - measured, that mutation leaves the house guard
  green at 27 passed.

## External references
- GitHub Actions are pinned to full commit SHAs and verified with gh api before use. Never guess a tag.
- Credential and API key formats come from provider documentation. Test fixtures use the real format
  with obviously fake values.

## Deploy gate
- Owner-level blockers (repo settings, secrets, branch protections) go in the handover with the exact
  action Ash must take, not left as open notes.

## Tests
- Every planted mutation must make at least one test fail. A mutation that changes no behaviour is not a test.

---

# Original project sections

Everything below this line is the file exactly as it stood before the kit was installed. It is kept
in full rather than summarised, because the hard rules below are the binding conventions and a
paraphrase of a rule is not the rule.

# CLAUDE.md

Always-true conventions for this project. Procedures live in `.claude/skills/`. The house voice is in `.claude/output-styles/house-voice.md`. When a rule here and a skill disagree, this file wins for conventions and the skill wins for procedure.

## What this project is

Pree fuses orbital geometry, manoeuvre-cadence baselining, and photometric and radio-frequency signatures from the Unified Data Library (UDL) into a single explainable, confidence-tiered score that shows Protect and Defend operators which satellites hold their protected assets at risk, and why. Archetype: `server` (one of: `static` single-file or built-static artifact; `server` backed container application). Deployment target: the Bluestaq App Store at `pree.appstore.bluestaq.com`, detected template `python` (a `requirements.txt` and a root `Dockerfile` are both present, so it is the quality-gated `python` container template, not `docker-only`).

## Hard rules (never violate)

**Both archetypes**
- **No secrets in any shippable file**, in source or in history. Read secrets from the environment; render any value in docs as `[REDACTED:type]`. The pre-write hook blocks a credential before it lands.
- **No client-side access gate.** A hardcoded Personal Identification Number (PIN), flag, or hidden field in the browser is a User Experience gate, never security. Real gates are server-side.
- **Surgical edits only.** Change the smallest region that satisfies the request. Do not reformat, re-indent, or reconstruct regions you were not asked to touch.
- **Never invent a name, title, date, organisation, or figure** in user-facing content or data. If a fact is not verifiable, mark it with the explicit unknown marker (`TBC, re-verify`); do not assert it.
- **Every untrusted value is escaped or validated at the boundary**, and a control that cannot be verified is treated as failed (fail closed).

**Static archetype**
- **No runtime network egress** from the artifact. No `fetch`, `XMLHttpRequest`, `WebSocket`, dynamic `import()`, remote fonts, or remote scripts. It runs fully offline; embed assets as `data:` Uniform Resource Identifiers (URIs).
- **No dynamic code execution.** No `eval`, `new Function`, `document.write`, or string-form `setTimeout`/`setInterval`. No cross-frame `message` listeners.
- **Locked Content-Security-Policy** (`default-src 'none'`, `connect-src 'none'`, framing denied, `referrer no-referrer`). Tighten only, never loosen.
- **One escaper** for every reflected user value; every `target="_blank"` link carries `rel="noopener noreferrer"`.

**Server archetype**
- **The container is the whole build.** A single `Dockerfile` installs from the hash-locked `requirements.txt` and runs the server; no separate bundler output. Runs as a non-root numeric user (`10001:10001`), no setuid or setgid bits on files or directories, and the shipped stage is flattened to one layer so the image-policy scan reads no history.
- **Listen on `PORT` from the environment, default 8080, bound to `0.0.0.0`.** Never add `ENV PORT=` to the Dockerfile: an image default shadows the code's own default so it is never reached in the container, and the image asserts a port the platform may not use. (This clause once said an image default "defeats platform injection", which is false: a runtime value overrides image `ENV`. A rule with a wrong mechanism does not survive the first engineer who tests it. It also once barred `ENV PREE_DATA_DIR=`, for a data directory the service no longer has.) Answer `/`, `/healthz`, `/readyz`, `/livez`, `/ping` with HTTP 200, unauthenticated, touching nothing.
- **Secrets are server-side only.** Compare the team token in constant time (`hmac.compare_digest`, never `==`). Validate every request body at the boundary through a typed model before a handler runs; the dataset merge never silently shrinks.

## Commands (fill the ones this project uses)

```
uv venv --python 3.12 .venv                                install: create the pinned runtime
uv pip install --require-hashes -r requirements-dev.txt     install: reproducible, hash-locked
sh scripts/verify.sh                                        the local verification loop
PREE_ENV=development uv run --with-requirements requirements.txt uvicorn --factory pree.main:build --reload --port 8080   dev
sh scripts/package-appstore.sh                              build the upload zip
sh scripts/simulate-pipeline.sh                             simulate the platform pipeline
docker build -t pree .                                      build the image
```


`scripts/simulate-pipeline.sh` exits 0 only when every stage including the image build is
green, and 2 when every stage except containerize is green and the container leg is deferred
to Continuous Integration for want of a Docker daemon. Exit 2 is not a pass.

The loop runs `ruff format --check`, `ruff check`, `mypy` over the source and the tests, `coverage run -m pytest` with a
Cobertura report at `coverage.xml`, and `pip-audit`. Coverage must be at least 80%; the gate
reads the report artefact, not the suite.

Every change runs the verification loop, then passes the `engineering-reviewer` and `security-reviewer` gates before it is done. Anything that deploys, publishes, or mutates external state requires the `deploy-gate` verdict and an explicit human confirmation.

## Directory layout

```
src/pree/               the application source; the factory is app.py, the listener main.py
Dockerfile              the whole build, at the repository root, flat
scripts/                verify.sh, package-appstore.sh, simulate-pipeline.sh
tests/                  the suite, run in-process through the factory
docs/                   deployment parameters, security policy, changelog
.claude/                this baseline (skills, agents, output style, hooks, settings)
```

Sources live under `src/` deliberately. `getting-started` records that the platform forces
`sonar.sources=src`, while `appstore-gate-compliance` records that a committed
`sonar-project.properties` is respected. `src/` satisfies both readings, so no upload cycle
is spent discovering which one holds.

## Naming and versioning

- Releases are `V0.1` style, held in `pyproject.toml` and `src/pree/__init__.py`, which must agree. Add one changelog row on every change. Bump the version stamp on every change AFTER the first release; before it, the stamp holds at the unreleased version and only the changelog moves, because a bump would assert a patch to a release that never happened. This clause once said "bump on every change" flat, and every commit in the project's history contradicted it, which is a convention that trains a reader to ignore conventions.
- Delivery copies of the upload archive follow `Bluestaq_Limited_-_Pree_-_V0_1_-_<date>.zip`; these are the rollback source.
- The App Store slug is `pree`: lowercase, alphanumeric at both ends, no double hyphen (a double hyphen breaks platform naming and fails with zero pipeline stages run).

## House voice (applies to all prose, UI copy, comments, commits)

A guide, not a leash. Held everywhere: never fabricate data; avoid the long em-dash (a single dash is fine); no `+` meaning "and" in prose. The Bluestaq default, which is your call on your own project: UK English, the `£`/`$`/`%` symbols, expand an uncommon acronym on first use, lead with the decision then the reasoning. Anything publish-facing or Bluestaq-brand-facing follows the brand in full. Full voice: `.claude/output-styles/house-voice.md`.
