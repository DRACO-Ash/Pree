---
name: security-hardening
description: The whole defence posture in one place: authentication, authorisation, configuration, and secrets. Use for any security-relevant change or review, for gating a route, comparing a token, reading config, or handling a credential. Covers both threat models (static CSP-locked offline artifact; server protect-the-key-and-budget), server-side-only secrets, the env-only config contract, constant-time token compare, fail-closed validation, CSP and CORS, rate limiting, container hardening, and link safety. Every control is a build-failing check. The security-reviewer agent reads this skill.
---

# Security hardening

## Purpose and scope

The single place that states the threat model and the controls that follow, so a security-relevant change is judged against one coherent posture rather than scattered rules. It owns authentication and authorisation and configuration and secrets in full (see the two sections below), and indexes controls owned in detail by other skills (`data-layer`, `api-and-integration`, `observability-and-audit`). The `security-reviewer` agent reads this skill. The repository security policy is the authority a control must not contradict.

## When to use

- Any change touching secrets, auth, input validation, CORS/CSP, rate limiting, or logging.
- Reviewing a change for security.
- Answering "is this safe to expose?" for a field, endpoint, or log line.

## The threat models (state the asset before the control)

**Static archetype (offline single-file artifact).** The artifact ships to the open web and runs entirely in the browser. There is no server-side secret to protect and no egress. The defended properties are: it cannot be turned into an injection vector (cross-site scripting via reflected data), it cannot exfiltrate (no network), and it cannot be subverted by dynamic code. The trust boundary is the data baked in plus anything reflected into the DOM. Out of scope by design: there is no client-side access control (a browser gate is never a boundary).

**Server archetype (LLM-backed container).** The high-value asset is the server-side LLM key and the spend budget, not the low-sensitivity dataset; the dataset's integrity (not being shrunk or poisoned) matters more than its secrecy. The trust boundary is the HTTP edge: request bodies, the token, query params, and the third-party LLM output are untrusted until validated. Realistic attackers: an unauthenticated client probing the public ingress; a malicious body (prototype pollution, oversize, log injection); the LLM returning fabricated or schema-breaking output. Out of scope by deliberate decision: a hostile authenticated team member (shared-token model) and dataset confidentiality, both recorded as accepted.

## Procedure (controls, archetype-tagged, each a build-failing check)

1. **Secrets server-side only** (server; see Configuration and secrets below). Key from the environment, never to the browser, log, or repo; the browser learns only a boolean.
2. **Constant-time token compare and route gating** (server; see Authentication and authorisation below). `timingSafeEqual` with a length guard; every cost-incurring or state-changing route gated; only health paths public.
3. **Fail-closed boundary validation** (server; `data-layer`, `api-and-integration`). Reject a bad request body or schema-breaking LLM result; never coerce. The merge is anti-shrink.
4. **Prototype-pollution and size limits** (server; `state-management`). Whitelist fields, deep-strip `__proto__`/`constructor`/`prototype` at every level, cap collections (newest kept), reject over a byte cap.
5. **CSP strict and CORS fail-closed** (both; `api-and-integration`, see Authentication and authorisation below). Static: a locked CSP, `default-src 'none'`, `connect-src 'none'`, framing denied. Server: a strict CSP and an `ALLOWED_ORIGIN`; in production with a token, a wildcard origin refuses to start.
6. **Two-tier rate limiting** (server; `api-and-integration`). A broad limiter on all routes and a strict limiter on the expensive path.
7. **One escaper for all reflected input and link safety** (static; `frontend-and-rendering`). Every reflection site goes through one escaper neutralising `& < > " '`; links carry `rel=noopener`; only safe schemes pass. Escape at the SINK even when the value is trusted today: a diagnostic string fed into `innerHTML` (an errno, a resolved path in a failure banner) is a fresh injection site the moment its source changes, and the binding gate treats the unescaped sink as the defect regardless of today's data (`appstore-gate-compliance`).
8. **No dynamic code, no egress, no message listeners** (static). No `eval`, `new Function`, `document.write`, string-timeout, `fetch`, `XMLHttpRequest`, `WebSocket`, dynamic import, or `postMessage` listener in the artifact.
9. **No-secret audit line and generic client errors** (server; `observability-and-audit`). One audit line, no secret, actor sanitised and capped; client gets a generic error.
10. **Crypto honesty** (both). A non-cryptographic digest (ETag, cache key) is commented as such; a security-relevant digest is cryptographic.
11. **Harden the runtime container image** (server / any container; `app-store-deployment`, `deploy-recipes`). Run as a non-root numeric user. The final image carries no setuid/setgid bits (the container-scan policy STOPS on `suid_or_guid_set`) and no package manager or toolchain it does not need at runtime (the base image's bundled `npm` tree both adds setgid directories and carries CVEs such as `picomatch`). Patch OS packages on build. The canonical recipe: `apk -U upgrade`, `rm -rf` the global npm (or use a multi-stage build that copies only the app and production dependencies), then `find / -xdev -perm /6000 -type f -exec chmod a-s {} +` and the same for `-type d` (a file-only sweep misses setgid directories). Keep the fail-open step (`apk upgrade`) in its OWN `RUN` instruction, separate from the fail-closed strip and toolchain removal, so a tolerated miss can never swallow a mandatory step. One trap the policy scan exposes: it reads **layer history**, so a `chmod` in a later layer masks a bit the base image set in an earlier layer but does not remove it, leaving path-less (`N/A`) `suid_or_guid_set` findings. The durable fix is to FLATTEN, do all hygiene in a `prep` stage, then ship `FROM scratch` with a single `COPY --from=prep / /` and re-declared metadata, so the distributed image is one clean layer with no history. Make the sweep the LAST mutation in the prep stage, after user creation and all file copies: a later instruction can re-introduce the class the sweep just cleared (busybox `adduser` sets setgid on the home directory it creates), so any invariant-establishing step must run after everything that could violate it (`appstore-gate-compliance`, `deploy-recipes`).

## Authentication and authorisation

Who may call what, and how it is enforced. The server gates every cost-incurring or state-changing route; the static artifact has no real gate.

1. **Compare the team token in constant time.** `crypto.timingSafeEqual` with a length guard, never `===`, so the comparison leaks neither length nor position through timing.
   ```js
   function tokenOk(given, expected){ const a=Buffer.from(String(given||"")), b=Buffer.from(String(expected||"")); return a.length===b.length && crypto.timingSafeEqual(a,b); }
   ```
2. **Gate every cost-incurring or state-changing route**; reads of public data may be open. Only health paths are public, no exception for "internal" callers (the ingress is public).
3. **Fail closed on CORS in production.** The control is stated once in Procedure #5 (a wildcard `ALLOWED_ORIGIN` with a token set makes the app refuse to start); it belongs to the auth posture, so it is listed here by reference, not restated.
4. **Leave a single-sign-on (SSO) seam.** The shared team token is one middleware; document it as the single place a per-user identity provider would later attach, and record the shared-token model as an accepted limitation.
5. **Static: never gate in the browser.** A Personal Identification Number (PIN), flag, or hidden field in client code is a user-experience convenience, never a security boundary; state this wherever it appears.

Checkable: the token is compared with `timingSafeEqual` and a length guard, never `===`; every gated route returns 401 without a valid token and 200 with one; health paths are public and nothing else is; production with a token refuses to start on a wildcard origin; no client-side gate is relied on as a control.

## Configuration and secrets

How the project is configured and how credentials stay out of source. Configuration is read from the environment only; real secret values never enter the repository, the image, or any log.

1. **Read all config from the environment** (`process.env`), never a committed config file. Read injected add-on variables (database URL, storage path) at request time, not at module load, or you capture an empty value before the platform injects it. Normalise any value used as a path or a secret: strip surrounding quotes and control characters (a trailing newline, a tab) before use, because the operator console routinely smuggles invisible characters into a pasted value (a stray tab in a path has turned a save into `mkdir "\t"`, and an auth token with a trailing newline never matched). Never use such a value raw.
2. **Maintain a committed `.env.example`** listing every variable by name with a placeholder, never a real value; render any secret in docs as `[REDACTED:type]`. `.env` is git-ignored.
3. **Never bake a secret into the image.** No `ENV API_TOKEN=` in the Dockerfile; the secret-scan hook fails the build on a Dockerfile `ENV` naming a secret-like key.
4. **App Store: stage then apply.** `save_env_vars` writes the FULL set (a complete replacement, not a merge), then `apply_env_vars` makes it live; always send the complete set or you drop the omitted ones.
5. **Rotate deliberately and on exposure.** Rotate at the provider on a schedule and immediately if exposed; apply the new value through the env-var lifecycle and never reuse the old one. The repo only ever held a placeholder, so rotation touches the environment, not source.
6. **Match each input to a delivery channel by criticality.** The channels, most reliable to least on the field evidence: a baked-in non-secret default in source, then a plain-text environment variable, then writable-volume state, then an encrypted secret (which can be delivered stale, or by a different mechanism than a plain env var, or absent). Put a value that must not fail on the most reliable channel its sensitivity allows, and reserve an encrypted secret for a value that is genuinely sensitive and can tolerate a recovery step. A team token that gates admin authority sitting on an encrypted-secret channel with no recovery path locked one project's owner out for days across four deploy cycles.

Checkable: no real secret in the repo, image, or log (secret-scan clean); every runtime variable is in `.env.example` with a placeholder; the server reads `process.env`, not a committed config file; no Dockerfile `ENV` names a secret-like key; App Store env vars are sent to `save_env_vars` as the complete set; a credential is rotated on schedule and on exposure, the old value never reused; a must-not-fail input is on a delivery channel matched to its criticality, not an encrypted secret by default.

## Decision rules

- **Is this a security boundary?** If it runs in the browser, no. The server token and server validation are the only boundaries; a client-side PIN or flag is a user-experience gate. State this whenever a client-side gate is proposed as protection.
- **May this field be exposed?** Booleans and counts yes; secret values never. If unsure, expose a boolean.
- **New external input?** It crosses the trust boundary and is validated or sanitised before storage or return; no exception for "internal" callers, because the ingress is public.
- **Hashing choice?** ETag or cache key, fast non-cryptographic hash, commented as such; a secret or integrity claim, cryptographic.
- **Accepted risk?** Record a deliberately out-of-scope risk in the security policy rather than leaving it silent.

## Standards (checkable assertions)

- No secret reaches the browser, a log, an error response, or the repo; the browser learns only booleans about secrets.
- Static: no dynamic code, no egress, no message listeners; one escaper covers all five characters; links carry `rel=noopener`; the CSP is locked and tighten-only.
- Server: the token is compared in constant time; every cost-incurring or state-changing route is gated; CORS fails closed in production with a token; the expensive path is rate-limited separately.
- Server: every untrusted input is validated or sanitised at the boundary and rejected on failure; the merge never shrinks the dataset; the shared document is prototype-pollution-stripped and size-capped.
- A non-cryptographic digest is commented as such; a security-relevant digest is cryptographic.
- Every deliberately accepted risk is written in the security policy.
- Container: the runtime image runs as a non-root numeric user, has no setuid/setgid bits (on files or directories), ships no package manager it does not need at runtime, patches its OS packages, and is flattened to a single layer (`FROM scratch` with one `COPY --from=prep / /`) so no earlier layer's history carries a bit, so the container-scan policy does not stop on `suid_or_guid_set` or an unmitigated High/Critical CVE.

## Failure modes and remedies

- **A secret is about to be returned or logged.** Fix: return or log a boolean; rotate if already emitted.
- **A browser-side PIN is treated as access control.** Fix: correct it; the server token is the boundary.
- **An LLM result with a fabricated field is stored.** Fix: restore fail-closed validation (`llm-integration`, `data-layer`).
- **A crafted `__proto__` body mutates objects.** Fix: restore the deep strip; re-run the pollution test.
- **The expensive endpoint is hammered and spend spikes.** Fix: restore the two-tier limit.
- **A static analyser flags a fast hash as weak crypto.** Fix: confirm it is an ETag or cache use, mark it non-cryptographic, document why; do not swap to a slow hash for a non-security path.

## Verification

Static: `static-checks.sh` asserts no dynamic code, no egress, no message listeners, the escaper covers all five characters, link-safety counts match, and the CSP is locked. Server: `npm test` exercises auth 401/200, boundary rejection, anti-shrink merge, prototype-pollution strip, size cap, and the rev guard. A grep confirms no secret appears in a response or a log. A security change is not done until `security-reviewer` returns `VERDICT: PASS`.

## Worked example

Server: a change adds a workspace field and an endpoint triggering a paid lookup. The field joins the sanitiser whitelist with a prototype-pollution test; the endpoint sits behind auth and the strict rate limiter because it costs money; its third-party result is schema-validated before storage; the audit line carries cost but not the key; "internal caller" is not accepted as a reason to skip validation. `npm test` proves the boundary behaviour; `security-reviewer` returns PASS.

## Glossary

- **Threat model / trust boundary:** what is defended and the line between untrusted and trusted data.
- **Fail closed:** on an unverifiable check or invalid input, reject.
- **CSP / CORS:** Content Security Policy; Cross-Origin Resource Sharing.
- **Prototype pollution / timingSafeEqual / two-tier rate limiting:** see `glossary`.
- Other terms: `glossary`.

## Provenance

Merged from the static bundle's static-checks and CSP/escaper/link-safety/no-dynamic-code posture and the server bundle's security-hardening skill (threat model, server-side secrets, constant-time compare, fail-closed validation, prototype-pollution strip, CSP/CORS fail-closed, two-tier rate limiting, no-secret audit, crypto honesty), with controls archetype-tagged and each tied to a build-failing check.

## Field lesson: fail closed for security, but recoverable for operability

A control can be perfectly fail-closed and still be a design defect if its failure is unrecoverable. In one project, admin authority was coupled to a single exact-match secret injected into `process.env` at boot: one silent character of drift, or a stale encrypted delivery, meant total lockout with no partial credit, no hint, and no way back in that did not itself depend on the secret arriving intact. The code was sound and fail-closed; the operability was not. Three rules follow. First, never couple a critical function to a single fragile, unobservable, unrecoverable input; always provide a recovery path that does not depend on the thing that failed. Second, fail closed for security AND fail to a recoverable state for operations: a missing or mangled input must reject the request (security) without becoming an unrecoverable lockout (operability), and the two goals are not in tension if recovery is designed in. Third, assume nothing the platform provides is actually there: an identity header, a writable volume, a faithfully-injected secret, all are probed at runtime and their status surfaced through the diagnostics read-out (`observability-and-audit`), never assumed. Confirm, do not assume.

## Field lesson: CSP meta tag versus response header (static)

Not every Content-Security-Policy directive works in a `<meta http-equiv>` tag. A static single file can set `default-src`, `script-src`, `style-src`, `img-src`, `connect-src`, `base-uri` and `form-action` in meta, and these are the locked core. It cannot set `frame-ancestors`: browsers ignore that directive when it arrives via meta and log a console warning. The same holds for `X-Frame-Options` and `Strict-Transport-Security`. Framing denial and transport security for a static artifact are therefore a hosting-layer concern, set as response headers by the App Store static host. Do not add `frame-ancestors` to the meta policy; it does nothing, and a strict render-check that fails on console errors will go red.
