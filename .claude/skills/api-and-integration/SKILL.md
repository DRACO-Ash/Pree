---
name: api-and-integration
description: API and integration layer. Use when adding an HTTP route, a health endpoint, rate limiting, a background job, or any outbound integration. Covers the static no-egress posture, the server HTTP API layer, route shape, health and readiness paths, two-tier rate limiting, single-flight background jobs, CORS, and ETag caching.
---

# API and integration

## Purpose and scope

How the application exposes routes and talks to anything outside itself. The static artifact makes no network calls at all (no egress). The server app exposes a small HTTP API through the createApp factory, with health and readiness paths, two-tier rate limiting, single-flight background jobs, fail-closed CORS, and ETag-based caching. Scope is the request surface and outbound integrations. It does not cover authentication of those routes (`security-hardening`) or the data they read and write (`data-layer`).

## When to use

- Adding or changing an HTTP route on the server.
- Wiring a health or readiness endpoint.
- Adding rate limiting, a background job, or an outbound call.
- Confirming the static artifact stays egress-free.

## Prerequisites

- `code-architecture` (createApp factory, request pipeline).
- `security-hardening` (origins, tokens, upstream URLs from the environment).

## Procedure

1. **Static: confirm no egress.** The artifact never fetches. The locked Content Security Policy (CSP) sets `connect-src 'none'`; verify no `fetch`, `XMLHttpRequest`, `WebSocket`, or remote `src`.
   ```
   grep -nE "fetch\(|XMLHttpRequest|WebSocket|src=\"http" ${SOURCE_PATH}   # expect none
   ```
2. **Server: define routes on the factory.** Each route is a small handler; the factory wires them so tests can mount the app in-process.
3. **Provide health and readiness.** `GET /healthz` (liveness) and a readiness path return 200 without authentication. `GET /` returns 200, never a 302 redirect (the App Store router probes it).
4. **Rate-limit in two tiers.** A coarse global limit protects the process; a finer per-route or per-actor limit protects expensive endpoints. Exceeding a limit returns 429.
5. **Run background work single-flight.** A long task (an LLM call, a batch) runs at most once at a time per key; concurrent callers join the in-flight job rather than starting a second.
6. **Set CORS fail-closed.** In production with a token configured, a wildcard origin refuses to start. Allow only `ALLOWED_ORIGIN`.
7. **Cache with ETag.** For cacheable GETs, emit an ETag and honour `If-None-Match` with 304.

## Decision rules

- **Static needs data from elsewhere?** It cannot fetch; bake the data in (`data-layer`) or re-scope to server.
- **New route added?** Add a test that mounts the factory in-process and asserts status, shape, and limits.
- **Expensive endpoint?** Apply the finer rate-limit tier and consider single-flight.
- **Wildcard CORS in prod with a token?** Refuse to start; require an explicit `ALLOWED_ORIGIN`.
- **`GET /` returns 302?** Fix it to 200; the platform router treats a redirect as unhealthy.

## Standards (checkable assertions)

- Static artifact performs no network calls (grep clean; CSP `connect-src 'none'`).
- `GET /` and `GET /healthz` return 200 unauthenticated.
- Every route has an in-process test asserting status and shape.
- Rate limits return 429 when exceeded, in both tiers.
- Background jobs are single-flight per key.
- Production CORS is fail-closed; no wildcard origin with a token set.
- Cacheable GETs emit and honour ETags.

## Failure modes and remedies

- **Platform marks the app unhealthy.** Cause: `GET /` returns 302 or non-200. Fix: return 200 from root.
- **A burst overwhelms an expensive route.** Cause: only a global limit. Fix: add the per-route tier.
- **Duplicate background jobs run.** Cause: no single-flight guard. Fix: key the job and join the in-flight promise.
- **Browser blocks the API in prod.** Cause: origin not allowed. Fix: set `ALLOWED_ORIGIN`; never widen to wildcard with a token.

## Verification

In-process tests mount `createApp` and assert `GET /` is 200, `GET /healthz` is 200 unauthenticated, a flood returns 429, two concurrent background calls run one job, and a wildcard-origin prod config with a token refuses to start.

## Worked example

A server app adds `POST /summarise`. The handler is rate-limited at the finer tier, runs the LLM call single-flight per input hash, and returns JSON. `GET /healthz` stays 200 unauthenticated. An in-process test floods `/summarise` and asserts a 429 after the limit, and fires two identical requests asserting a single upstream call.

## The AI scan job

A deep AI update scan is the archetypal long single-flight job: the POST returns a job id immediately, the client polls a status endpoint, only one scan runs at a time (a double-click cannot start a second), and on the deepest tier the dataset persists after each pass so a restart survives and the result grows live. The end-to-end scan (job, prompt, superset merge) is `ai-update-scan`.

## Glossary

- **Egress:** any outbound network call; the static artifact makes none.
- **Readiness vs liveness:** readiness signals able-to-serve; liveness signals process-alive. Both return 200 unauthenticated.
- **Two-tier rate limiting:** a coarse global limit plus a finer per-route or per-actor limit.
- **Single-flight:** at most one in-flight job per key; concurrent callers join it.
- **ETag:** a content fingerprint enabling 304 Not Modified responses.
- Other terms: `glossary`.

## Provenance

Reconstructed and merged from the static no-egress CSP posture and the server HTTP API references in the source bundles (routes, health paths, two-tier rate limiting, single-flight jobs, fail-closed CORS, ETag caching), aligned to the App Store root-200 and port contract in `appstore.md`.
