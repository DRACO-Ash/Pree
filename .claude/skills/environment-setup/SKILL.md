---
name: environment-setup
description: Clean machine to a running, verifiable project. Installs the pinned Node runtime, the headless browser driver (static archetype) or Docker (server archetype), sets the required environment variables, and proves the toolchain works before any work. Use on a fresh machine or when a script fails in a way that looks environmental (missing binary, wrong version, unresolved module).
---

# Environment setup

> Stack note: the commands shown here are the Node example. For Python, Java, Go, Rust, or .NET, run the equivalent canonical step from `toolchain-adapters`. The principle in this skill is what binds; the command is illustrative.

## Purpose and scope

Takes a clean machine to a state where the verification loop runs green. Covers runtimes and exact versions, the package manager, the headless browser driver and its path resolution (static), Docker (server), the environment variables the tooling reads, and a verification step. It does not cover platform deploy credentials (`security-hardening`) or product authoring (`code-architecture`).

## When to use

- First time on a new machine or container.
- A script fails in a way that looks environmental (missing binary, wrong version, unresolved module).

## Prerequisites

- A POSIX shell. Windows: Windows Subsystem for Linux or Git Bash.
- Network access to the package registry for the one-time install. The static artifact never needs network; its tooling install does.
- Permission to install a Node runtime, and (static) a browser or (server) Docker.

## Procedure

1. **Install the pinned Node runtime.** This project targets Node `${NODE_VERSION}` (commonly 22).
   ```
   nvm install ${NODE_VERSION} && nvm use ${NODE_VERSION}   # if a .nvmrc exists, nvm use alone suffices
   node --version            # expect v${NODE_VERSION}.x
   npm --version
   ```
   No nvm? Install the pinned Node from the official distribution or your OS package manager; confirm `node --version` before continuing.
2. **Install pinned tooling dependencies.**
   ```
   npm ci                    # expect "added N packages", no errors; lockfile unchanged
   ```
3. **Static archetype: install the headless browser** used by the render-check.
   ```
   npx playwright install --with-deps chromium   # --with-deps installs the OS libraries too
   ```
4. **Static archetype: resolve the browser driver path if non-standard.** The render-check tries, in order: the `${BROWSER_DRIVER_PATH}` env var, two known install paths, then a bare specifier. If it cannot find the driver, set the env var to the absolute path of the driver entrypoint:
   ```
   export BROWSER_DRIVER_PATH=/absolute/path/to/playwright/index.js   # only if auto-resolution fails
   node -e "console.log(require.resolve('playwright'))"               # finds the real path
   ```
   The fallback chain exists because the same script runs across local machines and Continuous Integration (CI) where the driver lives in different places.
5. **Server archetype: install and verify Docker.**
   ```
   docker --version          # expect a version
   docker run --rm hello-world   # expect the success message
   ```
6. **Verify the environment.**
   ```
   npm test                  # static: JS syntax: OK, two PASS lines, STATIC CHECKS: PASS
                             # server: unit tests pass, coverage/lcov.info produced
   ```
7. **Clear the environment debt that would tax every cycle, once, now.** Some environment faults do not stop the loop but re-charge on every turn for the life of the project, so fix them at scaffold rather than living with them. Provision a valid commit-signing key (a zero-byte or missing key flags every commit Unverified and re-triggers the stop-hook on every turn); confirm a Docker daemon is available where the pre-upload image build and policy scan run (a deploy-gate that cannot build the image cannot verify it); and stabilise any browser smoke so it is not timing-flaky (bind every click with a timeout and filter handled expected statuses out of the fatal-error check), so a re-run is never needed to tell a real failure from a race. Each of these is a fixed cost paid once here or paid repeatedly forever.

## Decision rules

- **CI vs local (static).** In CI, steps 2 to 3 are `npm ci` then `npx playwright install --with-deps chromium`; do not set `${BROWSER_DRIVER_PATH}` (the bare specifier resolves from `node_modules`).
- **Offline machine (static).** `validate` and `static-checks` run offline; `render-check` needs the browser installed once while online.
- **Server browsers.** The Playwright smoke test needs a browser too; install it locally or rely on CI as the source of truth for that one test.
- **Render-check still cannot find the browser after step 3?** Set `${BROWSER_DRIVER_PATH}` (step 4); if still failing, see Failure modes.

## Standards (checkable assertions)

- `node --version` equals the pinned major `${NODE_VERSION}`.
- `npm ci` leaves `package-lock.json` unchanged (`git status --porcelain package-lock.json` empty).
- `npm test` exits 0 with the expected green output before any work begins.
- Server: `docker run --rm hello-world` succeeds.
- Environment debt is cleared at scaffold: the commit-signing key is valid (commits are not flagged Unverified), Docker is available for the pre-upload image build and scan, and any browser smoke is stabilised (bounded clicks, handled-status filtering), so none of these re-charges on every cycle.

## Failure modes and remedies

- **`ERR_MODULE_NOT_FOUND .../playwright/index.js`.** Driver path wrong. Fix: run step 3; if it persists, set `${BROWSER_DRIVER_PATH}` from `require.resolve`.
- **`npm ci` errors "lockfile not in sync".** Node major mismatch or a hand-edited manifest. Fix: match `${NODE_VERSION}`; never hand-edit the lockfile (`dependencies`).
- **`render-check` launches but times out.** Missing OS libraries. Fix: `npx playwright install --with-deps chromium`.
- **`static-checks.sh: permission denied`.** Fix: `bash scripts/static-checks.sh`, or `chmod +x scripts/*.sh`.
- **`docker build` cannot reach the base image.** The registry is unreachable or the tag is gated. Fix: use a base image available through the platform registry/mirror (`app-store-deployment`); check your network policy.

## Verification

The environment is correct when `npm test` (and, server, `npm run test:e2e`) passes from a clean checkout with no source changes. If it does not, the environment, not the code, is wrong.

## Worked example

Fresh container, static project: `nvm install 22 && nvm use 22` gives v22.x; `npm ci` adds the one pinned dependency; `npx playwright install --with-deps chromium` downloads the browser; `npm test` prints `JS syntax: OK`, two `PASS` lines, `STATIC CHECKS: PASS`. `${BROWSER_DRIVER_PATH}` was not needed because the bare specifier resolved. Environment confirmed.

## Glossary

- **Pinned version:** an exact dependency version, not a range (`dependencies`).
- **Headless browser / driver:** the browser the render-check or smoke test automates without a window.
- `${NODE_VERSION}`, `${BROWSER_DRIVER_PATH}`: parameters in `AUDIT.md`.
- Other terms: `glossary`.

## Provenance

Merged from both source bundles' environment skills, the dependency manifest and lockfile, the render-check path-resolution chain, the CI setup steps, and the Docker build prerequisites.
