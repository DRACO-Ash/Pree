# CLAUDE.md

Always-true conventions for this project. Procedures live in `.claude/skills/`. The house voice is in `.claude/output-styles/house-voice.md`. When a rule here and a skill disagree, this file wins for conventions and the skill wins for procedure.

## What this project is

Pree fuses orbital geometry, manoeuvre-cadence baselining, and photometric and radio-frequency signatures from the Unified Data Library (UDL) into a single explainable, confidence-tiered score that shows Protect and Defend operators which satellites hold their protected assets at risk, and why. Archetype: `server` (one of: `static` single-file or built-static artifact; `server` backed container application). Deployment target: the Bluestaq App Store at `pree.apps.bluestaq.com`, detected template `python` (a `requirements.txt` and a root `Dockerfile` are both present, so it is the quality-gated `python` container template, not `docker-only`).

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
- **Listen on `PORT` from the environment, default 8080, bound to `0.0.0.0`.** Never add `ENV PORT=` or `ENV PREE_DATA_DIR=` to the Dockerfile; an image-level default shadows the code fallback chain, so the documented default becomes unreachable and untestable, and the image asserts a port the platform may not use. (It does NOT defeat platform injection, which is what this clause used to claim: a runtime-injected value overrides image `ENV` in both Docker and Kubernetes. The rule is right for the other two reasons, and a rule with a false mechanism does not survive the first engineer who tests it.) Answer `/`, `/healthz`, `/readyz`, `/livez`, `/ping` with HTTP 200, unauthenticated, touching nothing. The storage proof is a separate path, `/healthz/storage`, so a hung mount can never hang a liveness probe.
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

- Releases are `V0.1` style, held in `pyproject.toml` and `src/pree/__init__.py`, which must agree. Bump the version stamp and add one changelog row on every change.
- Delivery copies of the upload archive follow `Bluestaq_Limited_-_Pree_-_V0_1_-_<date>.zip`; these are the rollback source.
- The App Store slug is `pree`: lowercase, alphanumeric at both ends, no double hyphen (a double hyphen breaks platform naming and fails with zero pipeline stages run).

## House voice (applies to all prose, UI copy, comments, commits)

A guide, not a leash. Held everywhere: never fabricate data; avoid the long em-dash (a single dash is fine); no `+` meaning "and" in prose. The Bluestaq default, which is your call on your own project: UK English, the `£`/`$`/`%` symbols, expand an uncommon acronym on first use, lead with the decision then the reasoning. Anything publish-facing or Bluestaq-brand-facing follows the brand in full. Full voice: `.claude/output-styles/house-voice.md`.
