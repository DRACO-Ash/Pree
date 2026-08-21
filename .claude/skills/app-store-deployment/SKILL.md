---
name: app-store-deployment
description: The Bluestaq App Store deployment reference. User-invoked, never autonomous. Use when preparing or validating an App Store submission. Covers the five templates, the port 8080 rule, reference Dockerfiles, coverage configs, the pipeline stages, the SonarQube quality gate, add-ons, the resource budget, env-var two-stage save then apply, lifecycle states, the appstore MCP server and its tools, failure triage by stage, the golden path, pre-flight, and what never to do.
---

# App Store deployment

> The authoritative schema and runbook for deploying to the Bluestaq App Store (`*.apps.bluestaq.com`). User-invoked, never autonomous. `deploy-gate` validates a prepared submission field by field against this before any irreversible submit; the submit itself is human-confirmed.

## Purpose and scope

Everything needed to take a verified package to a live App Store deployment without heavy intervention: template selection, the runtime contract, the pipeline, the quality gate, add-ons, the resource envelope, environment variables, lifecycle states, the MCP tooling, and failure triage. Scope is the deployment target and its tooling. The build of the artifact is `packaging`; the deploy procedure that drives this is `release-and-deploy`.

## When to use

- Preparing or validating a submission to the Bluestaq App Store.
- Diagnosing a failed pipeline or deploy.
- `deploy-gate` references this to validate field by field.

## The five templates (auto-detected from package contents)

| Template | Detected by | Quality gate |
|---|---|---|
| node-react | `package.json` | yes |
| java-spring | `pom.xml` | yes |
| python | `requirements.txt` | yes |
| docker-only | `Dockerfile` only | skipped |
| static-html | static files, no server marker, no container file | skipped |

If a container file is present you are a container template (node-react, java-spring, python, or docker-only); only the entrypoint and no container file means static-html.

## The port 8080 rule (the single most common failure)

The platform sets `containerPort: 8080`. The app reads `process.env.PORT` defaulting to 8080. Never set `ENV PORT=` to another value in the Dockerfile (the secret-scan hook flags this anti-pattern), and never type `PORT` into the operator's Environment Variables tab: either overrides the platform-injected value and breaks the readiness probe. `GET /` must return 200, not a 302 redirect, because the platform router probes the root. Bind to 0.0.0.0, not loopback.

> `TBC, re-verify`: a field report from one node-react project stated the node-react template fixes the service and health-probe port at **3000** (not configurable in the console), reserving 8080 for docker-only. This could not be verified against the platform reference and contradicts the 8080 contract asserted throughout this baseline, so it is recorded here as an open item to confirm with the platform team before relying on it, not applied. Because the code reads `process.env.PORT`, a platform that injects `PORT=3000` for node-react would already be honoured by the 8080-defaulting code; the open question is only the probe port, not the code shape.

## Reference Dockerfile shape (container templates)

```dockerfile
FROM node:22-alpine
RUN apk -U upgrade --no-cache
WORKDIR /app
ENV NODE_ENV=production HOST=0.0.0.0
COPY package*.json ./
RUN npm ci --omit=dev && npm prune --omit=dev
COPY . .
# Drop the global npm (not needed at runtime; it carries CVEs such as picomatch and adds
# setgid directories), then strip every setuid/setgid bit. The container-scan policy STOPS on
# both an unmitigated High/Critical CVE and any suid_or_guid_set file or directory.
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx \
 && find / -xdev -perm /6000 -exec chmod -s {} + 2>/dev/null || true
USER 1000:1000
EXPOSE 8080
CMD ["node","src/index.js"]
```
Read the env-injected add-on variables (database URL, storage path) at request time, not at module load. Never bake an injected path or port into the image with `ENV` (for example `ENV DATA_DIR=/data`): an `ENV` line always beats a code fallback chain, so a baked default silently defeats the platform's injected value and writes land on the ephemeral layer, lost on redeploy. Resolve such values in code (explicit variable, then the injected variable, then a default), audit the Dockerfile `ENV` block against every fallback chain in code, and validate the resolved path at boot (absolute, not the filesystem root, writable), failing closed with a clear message (`security-hardening`, `appstore-gate-compliance`). Keep the runtime image lean and clean: a multi-stage build copies only the built app and production `node_modules` into the final stage, the package manager and toolchain stay in the build stage, and the final image carries no setuid/setgid bits (the container-scan stops on them).

## Container package layout (Dockerfile at the package root)

The platform detects the template from a `Dockerfile` at the **root** of the uploaded package and generates a pipeline that builds with the **root** as the context (equivalent to `podman build -f Dockerfile .`). So the package must be flat: the `Dockerfile`, the entrypoint, and `.dockerignore` sit at the package root, never inside a subdirectory.

- A `Dockerfile` nested in a subdirectory (for example `launchpad-docker/Dockerfile`) breaks two things at once: template auto-detection looks for a root-level `Dockerfile` and may miss it, and the generated build cannot find its context, failing with `Error: context must be a directory: ".../launchpad-docker"`.
- The fix is always to flatten the package so the `Dockerfile` sits at the root and re-upload, never to hand-edit the generated pipeline to chase the subdirectory (you never edit that pipeline; see "What never to do"). A stale pipeline that still points at a subdirectory was generated from an earlier nested upload; a fresh root-level upload regenerates it correctly.
- This mirrors the static-html rule (entrypoint at the root, nothing nested) and the flat-download rule for skill bundles: importers and generated pipelines key off the root.

## Pipeline stages (in order)

`setup` to `check` to `build` to `test` to `scan` to `package` to `containerize` to `container-scan` to `deploy`. A later stage is reached only when the earlier passes. The platform generates its own pipeline configuration; you never hand-edit it (`ci-cd`). Because the stages are strictly sequential, the platform reveals its requirements ONE GATE AT A TIME: you cannot see the image scan's policy while the quality gate is failing, nor the quality gate's ruleset while the test stage is failing. Retrofitting an unprepared codebase therefore costs roughly one upload per stage; scaffold to the full checklist in `appstore-gate-compliance` and pay none of it.

For a quality-gated template (node, python, java) the `test` stage runs your tests with coverage (for node, `npm test -- --coverage`) against the **root of your uploaded zip**, before any image is built, and it runs in the platform's environment, not yours: the platform commits its own generated `.gitlab-ci.yml` into the checkout and sets `GITLAB_CI=true`. So the uploaded package must be a self-sufficient, testable source tree (tests and runner config included, not a stripped runtime bundle), and any negative assertion in the suite ("this file must not exist") must be gated on the environment, or it will be guaranteed-false on the platform's own checkout. If the test stage exits non-zero, Code Quality and Container Build are skipped and the deploy is dead. Simulate this exactly, added file and environment included, before every upload (`appstore-gate-compliance`, `testing-standards`).

## The SonarQube quality gate ("App Store Apps")

Applies to node-react, java-spring, and python (skipped for docker-only and static-html). It fails when: violations are greater than 0; coverage is below 80%; the security or reliability rating is below A; or a security hotspot is unreviewed. Coverage is read from `coverage/lcov.info` (node), `target/site/jacoco/jacoco.xml` (java), or `coverage.xml` (python). To pass: produce coverage at the right path, drive violations to zero, review every hotspot. Note two traps that pass locally and fail here (`appstore-gate-compliance`): a comprehensive suite that emits no machine-readable report scores 0%, because the gate reads the artefact, not the suite; and hundreds of violations accumulate when there is no per-commit analyser, so wire a Sonar-equivalent pass at scaffold time and resolve findings behaviour-preserving. A committed `sonar-project.properties` at the repo root is respected for sources and tests scoping, the coverage report path, and coverage exclusions (from the coverage metric only, never from analysis, each with a written rationale). Fix hotspots in the code (de-backtrack a regex, use a cryptographic random), not by dashboard review an uploader cannot perform.

## Add-ons (injected env vars, read at request time)

| Add-on | Provides |
|---|---|
| POSTGRESQL | a managed database and its connection env vars |
| REDIS | a managed cache and its connection env vars |
| FILE_STORAGE | a persistent volume at `STORAGE_MOUNT_PATH=/data` |
| CLAMAV | a malware-scan service for uploaded files |

## Resource budget

Up to 8Gi memory and 6 CPU per app. Size within the envelope; the gate blocks an unset container budget.

## Environment variables (two-stage)

`save_env_vars` writes the FULL set (a complete replacement, not a merge), then `apply_env_vars` makes them live. Always send the complete set to save. For static-html, set `S3_BUCKET=bluestaq-appstore-uploads` and an `S3_PREFIX`. Note `bluestaq-appstore-bucket` is a STALE name; do not use it.

## The appstore MCP server

Install: `npx @bluestaq/appstore-mcp`. Auth: Keycloak device-code flow; tokens at `~/.appstore/tokens.json`. Read-only tools (status, diagnosis, docs) run free. Write tools confirm-first: `submit_app`, `resubmit_app`, `upload_package`, `save_env_vars`, `apply_env_vars`, `retry_job`, `retry_deploy`, `update_app_details`. A `fileRef` from `upload_package` is single-use. Roll back by `resubmit_app` of the previous package; there is no separate rollback tool. ArgoCD reports the deploy state.

## The golden path

1. Verify (`testing-standards`) and build (`packaging`). 2. Detect the template. 3. `deploy-gate` validates field by field. 4. `upload_package` (single-use fileRef). 5. `save_env_vars` (full set) then `apply_env_vars`. 6. State what will happen; on human "yes", `submit_app`. 7. Watch stages with `get_pipeline_status`. 8. Post-deploy pre-flight (`release-and-deploy`). 9. Confirm rollback remains available.

## Failure triage (diagnose first, never blind-retry)

- **build / containerize red with `context must be a directory` or a wrong template:** the `Dockerfile` is nested in a subdirectory, not at the package root. Flatten the package so the `Dockerfile` and entrypoint sit at the root, and re-upload; do not point the pipeline at the subdirectory.
- **container-scan red on `suid_or_guid_set`:** the image has setuid/setgid files or directories (commonly the base image's bundled `npm` tree under `/usr/local/lib/node_modules/npm`, mode `02755`). Strip them (`find / -xdev -perm /6000 -type f -exec chmod a-s {} +`, then `-type d` for setgid directories a file sweep misses) and remove the package manager from the runtime image; do not relax the policy. The scan judges what ships, not what runs, so this is a Dockerfile fix, not an application change. If a strip clears the final filesystem but the scan still shows path-less (`N/A`) `suid_or_guid_set` findings, the scanner is reading **layer history**: an earlier base-image layer still physically carries the bits a later `chmod` masked. You cannot edit layer history, only stop shipping it, so **flatten** the runtime into one clean layer, all hygiene in a `prep` stage then `FROM scratch` with a single `COPY --from=prep / /`, re-declaring all metadata and an explicit `PATH` (`appstore-gate-compliance`, `deploy-recipes`). The scan stops, not warns, on this.
- **container-scan red on a High/Critical package CVE:** often in a tool the runtime does not need (the global `npm` and its deps, for example `picomatch`). Remove the package manager and toolchain from the final image (multi-stage, or `rm -rf` the global npm), and `apk upgrade`/rebuild on a patched base for OS packages. Address the finding; never suppress it.
- **scan / container-scan red:** a real finding or a credential-looking string. Read `get_pipeline_diagnosis`; remove and rotate a real secret; never suppress an unmitigated finding.
- **test red:** a failing test or missing coverage path. Fix the test or wire the coverage path; do not retry.
- **quality gate red:** violations, coverage below 80%, rating below A, or an unreviewed hotspot. Address the specific metric.
- **deploy red (ArgoCD):** wrong port, 302 at root, not bound to 0.0.0.0, or a non-root user the platform cannot resolve. Fix the runtime contract.
- **deploy red, pod crash-loops or dies after a clean boot:** a runtime-only fault the pipeline never sees; a healthy sibling add-on pod (ClamAV) localises it to configuration or the app. Read the RAW pod log (the failure summary hides paths as `N/A`). Three shapes (`appstore-gate-compliance`): (a) a fail-closed boot error quoting a value means a variable was set wrong, usually guidance prose pasted into the Environment Variables tab; delete it, the tab must be empty for a code-defaults app. (b) `EACCES` writing to the mount means the non-root container hit a root-owned volume add-on; raise an operations request to set `securityContext.fsGroup` (platform-general for every non-root workload using the add-on). (c) a clean boot then a silent `SIGTERM` with growing restart gaps is kubelet liveness back-off killing a hanging health probe; the app needs a hard probe timeout and a boot storage-write log line.
- **pipeline green but the app 401s in the browser:** the platform puts a Keycloak single-sign-on gateway IN FRONT of the app, and it can return 401 (or redirect to Keycloak) for the browser's calls and sub-resources independently of the app. This is a runtime platform-configuration matter, not an onboarding gate: the pipeline goes fully green (the readiness probe passes inside the container) while the browser still 401s. Diagnostic tell: if an endpoint the app serves UNAUTHENTICATED (for example `/healthz` or `/api/health`) returns 401, the 401 is the gateway, not the app. Route it to the platform team (exempt the app or its `/api/*` paths from the SSO layer), never to a code change.
- **pipeline fails with ZERO stages run:** not a stage failure. App-record residue from a deleted-and-recreated app (orphaned records or GitLab project), or a slug containing a double hyphen breaking platform naming. Recreate under a fresh single-hyphen slug and confirm the old records are cleared.
- **storage sync red (static):** wrong `S3_BUCKET`/`S3_PREFIX`. Set the correct values (not the stale bucket name).
- Use `retry_job`/`retry_deploy` only for a confirmed transient, after diagnosis, never for a real failure.

## Pre-flight verification routine

`unzip -l` shows the expected contents (static: only the entrypoint); the version field equals the artifact stamp; env vars are the complete set before apply; `deploy-gate` returns PASS; for a container, `docker run ... id` is non-root and `curl -i .../healthz` is 200 and `GET /` is 200. For a quality-gated template, simulate the platform pipeline against the actual artefact first: unzip the upload into a clean directory, add a platform-style `.gitlab-ci.yml`, `npm ci`, then `GITLAB_CI=true npm test -- --coverage` there, confirm it is green, and confirm `coverage/lcov.info` is non-empty (`appstore-gate-compliance`). A green repo loop is not a green upload; the simulation must reproduce the platform's added file and environment and verify the coverage artefact, not just that tests pass.

## What never to do

Never reuse a fileRef. Never suppress an unmitigated security finding. Never retry a real failure without diagnosing it first. Never set `ENV PORT=` to a non-8080 value, and never type `PORT` into the console env tab; the server reads `process.env.PORT` defaulting to 8080. Never send a partial set to `save_env_vars` (it replaces the whole set). Never use the stale `bluestaq-appstore-bucket` name. Never nest the `Dockerfile` in a subdirectory of the package; it must sit at the root so the template is detected and the generated pipeline builds from the root context. Never hand-edit the generated pipeline (flatten the package instead). Never publish without `deploy-gate` PASS and explicit human confirmation.

## Standards (checkable assertions)

- The template matches the package; required fields for that template are set; the version equals the artifact stamp.
- Container apps read `process.env.PORT` (default 8080), bind 0.0.0.0, return 200 at `GET /` and health paths, and run non-root.
- The container package is flat: the `Dockerfile` and entrypoint sit at the package root (`unzip -l` shows no wrapping subdirectory), so the template is detected and the build context is the root.
- The runtime image carries no setuid/setgid bits and no package manager it does not need at runtime (the container-scan stops on `suid_or_guid_set` and on a High/Critical CVE in a tool like the global `npm`).
- Quality-gated templates produce coverage at the correct path at 80% or more with zero violations and reviewed hotspots.
- Env vars are sent to `save_env_vars` as the complete set, then applied.
- No fileRef is reused; no unmitigated finding is suppressed; no real failure is blind-retried.

## Failure modes and remedies

- **Wrong template detected.** Fix: remove the stray marker (a container file in a static app); rebuild.
- **Root probe returns 302.** Fix: return 200 from `GET /`.
- **Coverage gate fails.** Fix: write coverage to the template's path and raise it above 80%.
- **Env vars vanish after save.** Fix: send the full set to `save_env_vars`.
- **Deploy stuck in ArgoCD.** Fix: read the diagnosis; correct the runtime contract; resubmit.

## Verification

`deploy-gate` validates the prepared submission field by field and returns PASS only when every required field for the template is set and the version matches; the submit is human-confirmed; the pipeline ends green and the URL is reachable (`release-and-deploy` pre-flight).

## Worked example

A node-react app: template detected by `package.json`; Dockerfile reads `process.env.PORT` default 8080, binds 0.0.0.0, `GET /` returns 200, runs `USER 1000:1000`. Tests write `coverage/lcov.info` at 86%; SonarQube shows zero violations and reviewed hotspots. `save_env_vars` sends the full set including the Postgres URL (read at request time) then `apply_env_vars`. `deploy-gate` PASSes; the human confirms; the pipeline runs setup to deploy green; ArgoCD reports Healthy; `/healthz` returns 200.

## Glossary

- **Template:** the build type auto-detected from package contents (one of five).
- **Quality gate:** the SonarQube "App Store Apps" gate (violations 0, coverage 80%, ratings A, hotspots reviewed).
- **Add-on:** a managed service (Postgres, Redis, file storage, ClamAV) injecting env vars read at request time.
- **fileRef:** the single-use upload reference.
- **save then apply:** the two-stage env-var lifecycle (save is a full replacement).
- Other terms: `glossary`.

## Provenance

Distilled from the authoritative Bluestaq App Store reference (`appstore.md`, v2.0): the five templates, the port 8080 rule, the pipeline stages, the SonarQube quality gate and coverage paths, the add-on catalogue and resource budget, the env-var two-stage lifecycle, the appstore MCP server and its read-only and write tools, the lifecycle states, and the never-do list.
