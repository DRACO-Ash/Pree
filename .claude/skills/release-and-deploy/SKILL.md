---
name: release-and-deploy
description: The deploy runbook for both archetypes. User-invoked, never autonomous. Run only when a human asks to ship. Covers the static-html object-storage path and the container-is-the-build path (non-root numeric user, platform port, health 200, lean context, env and storage contract, tested rollback). Calls deploy-gate before any irreversible step; the publish requires explicit human confirmation.
---

# Release and deploy

> Stack note: the commands shown here are the Node example. For Python, Java, Go, Rust, or .NET, run the equivalent canonical step from `toolchain-adapters`. The principle in this skill is what binds; the command is illustrative.

> This skill performs state-changing, hard-to-reverse actions. It is user-invoked and never runs autonomously. The `deploy-gate` agent verifies preconditions before any irreversible step, and the publish requires an explicit human "yes".

## Purpose and scope

The end-to-end deploy for both archetypes. The static-html artifact deploys to an object-storage-backed app store as an entrypoint-only package. The container app deploys as a hardened image that satisfies the platform runtime contract. Both verify, gate, submit (human-confirmed), watch the pipeline, run post-deploy pre-flight, and keep a tested rollback. Scope is the deploy procedure; the gate's verdict logic is in `agents/deploy-gate.md` and the full platform schema is in `app-store-deployment`. It never decides whether to ship (a human does) and never publishes without confirmation.

## When to use

- A human has explicitly asked to ship or resubmit.
- Never automatically; never as a side effect of another task.

## Prerequisites

- The verification loop green (`testing-standards`) and both review gates PASS (`engineering-reviewer`, `security-reviewer`).
- Package built and verified (`packaging`).
- Deploy access: the App Store MCP server available and authenticated by device-code SSO (`app-store-deployment`).
- First-ever static deploy only: the object-storage platform variables set (`security-hardening`).

## Procedure (container: the build-and-harden contract)

1. **Make the container the whole build.** A single Dockerfile installs from the lockfile and runs the server; no bundler, no `dist/`. Pin the runtime, patch the base, drop setuid/setgid bits, run as a non-root numeric user.
   ```dockerfile
   FROM node:22-alpine
   RUN apk -U upgrade --no-cache
   WORKDIR /app
   ENV NODE_ENV=production HOST=0.0.0.0 PORT=8080
   COPY package*.json ./
   RUN npm ci --omit=dev && npm prune --omit=dev
   COPY src ./src
   COPY public ./public
   RUN find / -xdev \( -perm -4000 -o -perm -2000 \) -exec chmod -s {} \; || true
   USER 1000:1000
   EXPOSE 8080
   CMD ["node","src/index.js"]
   ```
2. **Listen on the platform port (8080) bound to 0.0.0.0.** The server reads `process.env.PORT` defaulting to 8080; never set `ENV PORT=` to a different value and never depend on a custom port. `GET /` returns 200, not 302.
3. **Answer every conventional health path with 200, unauthenticated** (`observability-and-audit`).
4. **Keep the build context lean** via `.dockerignore` (`packaging`); no secret in any layer. Ship the `Dockerfile` and entrypoint at the package root, flat: the platform builds from the root context, and a nested `Dockerfile` fails with `context must be a directory` (`app-store-deployment`, `packaging`).
5. **Harden the runtime image** (`security-hardening`): non-root numeric user, no setuid/setgid bits (the container-scan stops on `suid_or_guid_set`, commonly the base image's bundled `npm` tree), no package manager it does not need at runtime (it carries CVEs such as `picomatch`), and OS packages patched. Prefer multi-stage; strip with `find / -xdev -perm /6000 -exec chmod -s {} +` as belt-and-braces.
6. **State the deploy-time contract**: every env var (secrets flagged), the storage mount (`STORAGE_MOUNT_PATH=/data`), the resource envelope. Config is deploy-time, not baked in.
7. **Give the operator console instructions that cannot be mis-pasted.** Every value is either COPY-PASTE EXACT or explicitly "delete this variable"; never a descriptive placeholder ("whatever your path is") near a paste-able field, because it gets pasted as a literal value and the app's fail-closed boot rejects it (a deploy cycle lost per variable). For a code-defaults app the correct console state is an EMPTY environment tab; platform-injected variables live at the pod level. A non-root container using the file-storage add-on needs an operations request to set `securityContext.fsGroup`, or the root-owned mount refuses every write (`EACCES`); this is platform-general. On any archetype change, ship an explicit console-changes list (variables to delete, add-ons to enable). See the deploy-stage triage in `app-store-deployment` and `appstore-gate-compliance`.
8. **On the FIRST delivery of a container app, ship an accompanying deployment parameters table.** The operator configures the App Store from it, so it is the single source of the platform settings, not left implicit in prose. Tabulate at least: the detected template; the container port and the `PORT` contract (reads `process.env.PORT`, default 8080, bound `0.0.0.0`, never `ENV PORT=`); every environment variable with its value or `[delete]` and whether it is operator-set or platform-injected (an empty tab for a code-defaults app); the add-ons to enable and the variables each injects (for example FILE_STORAGE and `STORAGE_MOUNT_PATH`); `securityContext.fsGroup` if the app is non-root and mounts a volume add-on; the resource budget (memory and CPU within the envelope); and the health paths that must return 200. Regenerate the table whenever any of these change; a later delivery need only ship the delta (`app-store-deployment`, `appstore-gate-compliance`).

## Procedure (both: gate, submit, watch, verify)

1. **Verify and package.** `npm test` green; build the package (`packaging`); for static, `unzip -l` shows only the entrypoint.
2. **Invoke the deploy gate.** Run `deploy-gate`; proceed only on `VERDICT: PASS`.
3. **Upload the package** (returns a single-use fileRef; never reuse it).
4. **Confirm, then submit.** State exactly what will happen and wait for an explicit human "yes" before `submit_app`.
5. **Watch the pipeline.** On any red stage run `get_pipeline_diagnosis` first, fix the cause, resubmit with a fresh upload. Never retry a real failure blindly.
6. **Post-deploy pre-flight** (Verification section).

## Deployability check before a major merge

Run a periodic "is this deployable to the App Store?" check before any major merge, and on a schedule to catch drift. The mechanical check is `scripts/deployability-check.sh` (wired as `npm run deployable` and enforced by the `deployable` CI workflow on every pull request into `main` and weekly): it runs the verification loop, builds the package, and asserts the entrypoint-only contract, the SHA-256 integrity match, the version stamp, the locked Content-Security-Policy, and that no real secret is tracked. It prints `DEPLOYABLE: YES` and exits 0, or lists the NO-GO items, prints `DEPLOYABLE: NO`, and exits 1.

If the answer is NO, fix the listed items before merging; never merge a NO. The mechanical check is the fast gate; the `deploy-gate` agent remains the deeper, human-confirmed check before the irreversible publish.

## Decision rules

- **`deploy-gate` returned FAIL?** Stop; clear every blocking finding; re-run. Never deploy past a FAIL.
- **Roll back?** There is no separate rollback; resubmit the previous dated package (static) or the previous good image tag (container). The gate confirms one exists first.
- **fileRef already used?** Single-use; re-upload for a fresh one.
- **Metadata-only change?** Use `update_app_details`; no redeploy.
- **Container port?** Read from `process.env.PORT` default 8080; match the platform, never invent a port.

## Standards (checkable assertions)

- Before a major merge, `npm run deployable` prints `DEPLOYABLE: YES`; a `NO` blocks the merge until fixed, and the `deployable` CI workflow enforces this on pull requests into `main`.
- `deploy-gate` `VERDICT: PASS` is recorded before any upload or submit.
- The publish was confirmed by a human in the session.
- Static: the package contained only `${ENTRYPOINT_NAME}`. Container: the image runs as a non-root numeric user, listens on 8080 bound to 0.0.0.0, and `GET /` plus health paths return 200.
- A first container delivery is accompanied by a deployment parameters table (template, port and the `PORT` contract, environment variables and their source, add-ons and the variables they inject, `securityContext.fsGroup` where applicable, the resource budget, and the health paths).
- The pipeline ended green and the URL is reachable and functional.
- A tested rollback path exists before release.

## Failure modes and remedies

- **Upload rejected by the scanner.** Fix: rebuild (`packaging`), re-check, re-upload.
- **Secret-detection stage red.** Fix: if a real secret, remove and rotate; never suppress an unconfirmed finding.
- **Deploy times out or probe unhealthy (container).** Fix: the container is not on 8080 or not on 0.0.0.0; set PORT/HOST to match the platform.
- **`CreateContainerConfigError` or refusal to run as root.** Fix: set a numeric USER and drop setuid/setgid bits.
- **Pod restarts and data is gone.** Fix: data was on the ephemeral filesystem; point it at the persistent mount.
- **Deployed but not reachable.** Fix: confirm the sync or ingress is green and the prefix matches; wait briefly; escalate if it is a platform seed.

## Verification (post-deploy pre-flight)

1. Pipeline: every stage green. 2. Reachability: the URL loads and renders. 3. Function: core flows work. 4. Console: clean except a documented benign warning. Container additionally: `docker run ... id` shows a non-root UID and `curl -i .../healthz` returns 200. If any check fails, do not declare success; diagnose and, if a partial deploy occurred, roll back.

## Worked example

A human says "ship V1.1". The engineer runs `npm test` (green), builds the package, invokes `deploy-gate` (PASS). They upload, state "I will submit V1.1 to ${APP_SLUG}", and wait. The human says "yes". `submit_app` runs; the pipeline goes green; the URL loads and core flows work; the console is clean. The previous dated package (static) or image tag (container) remains for rollback.

## Glossary

- **Container-is-the-build:** a single Dockerfile is the entire production build.
- **Non-root numeric user:** running as a UID such as 1000:1000, never root.
- **fileRef:** the single-use reference returned by upload; never reused.
- **Resubmit-to-rollback:** rolling back by resubmitting the previous package or image.
- Other terms: `glossary`.

## Provenance

Merged from the static bundle's release-and-deploy runbook (object-storage path, entrypoint-only package, resubmit-to-rollback) and the server bundle's release-and-deploy skill (container-is-the-build, non-root numeric user, platform port, health 200, lean context, env and storage contract, tested rollback), gated by `deploy-gate` and human confirmation, with platform specifics in `app-store-deployment`.
