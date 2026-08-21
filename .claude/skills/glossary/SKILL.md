---
name: glossary
description: Every term, acronym, convention, and named concept used across the Bluestaq Foundations bundle, expanded and defined, including App Store platform terms. Consult whenever any other skill uses a word you do not recognise. Make sure to use this skill the moment a term is unfamiliar rather than guessing.
---

# Glossary

## Purpose and scope

Defines every term, acronym, parameter, and convention the other skills use, so nothing in the bundle is unexplained. A reference, not a procedure. Scope is the vocabulary of this bundle and the Bluestaq App Store; product-domain terms specific to one application are excluded, because the bundle is portable.

## When to use

- Any skill uses a term you do not recognise. Look it up here first.
- You are reviewing the bundle and want one place proving no jargon is undefined.

## Prerequisites

None.

## Procedure

Find the term below and read its definition. If a term used in another skill is missing here, that is a defect: add it with its owning skill.

## Terms

- **A11Y (accessibility):** designing so the product is usable by people with disabilities (keyboard operation, contrast, reduced motion, semantics). Owner: `design-system`.
- **Anti-shrink merge:** a dataset refresh that may grow and correct records but never deletes a valid one; ended records are archived. Guarded by a test. Owner: `data-layer`.
- **App factory:** `createApp(deps)`, a function that builds and returns the HTTP app without listening, so it is testable in-process with fake dependencies. Owner: `code-architecture`.
- **Archetype:** the project shape, static (single-file or built-static, no server) or server (container). Decided in `getting-started` Step 0.
- **Archived (record):** flagged past/ended, hidden from the active view, retained and restorable. Owner: `data-layer`.
- **ArgoCD:** the App Store's GitOps deploy controller; reports Synced/Healthy. Owner: `app-store-deployment`.
- **Artifact (static):** the single shippable HTML file at `${SOURCE_PATH}`, hand-edited, ships as-is. Owner: `code-architecture`.
- **Atomic write:** writing to a temporary file then renaming over the target, so a crash cannot leave a half-written file. Owner: `data-layer`.
- **Audit row / audit line:** static, a human-written record kept in the app of what changed, why, and how verified; server, one structured JSON log line per privileged action with actor, timings, and cost, never a secret. Owner: `observability-and-audit`.
- **Back-face cull / limb:** skipping or breaking geometry on the far side of a projected sphere before vertex work. Owner: `frontend-and-rendering`.
- **Bearer token:** a credential sent in the `Authorization: Bearer <token>` header. Owner: `security-hardening`.
- **Boundary validation:** validating or rejecting every untrusted input at the trust boundary before it is stored or returned; bad input is rejected, never coerced. Owner: `data-layer`.
- **BYOK (bring-your-own-key):** a local tool that takes the user's key at runtime and never persists it. Owner: `api-and-integration`.
- **cATO (continuous Authority to Operate):** an App Store publication score; below 70 blocks publication. Owner: `app-store-deployment`.
- **CI/CD:** Continuous Integration and Continuous Deployment. Automated build, test, and release. Owner: `ci-cd`.
- **Container-is-the-build:** a single `Dockerfile` is the entire production build; no separate bundler step or `dist/`. Owner: `release-and-deploy`.
- **Container image policy:** a platform gate rejecting an image that breaks its rules (root, setuid/setgid, critical CVEs); it judges the base image's own contents and layer history, not only your code. Owner: `app-store-deployment`, `appstore-gate-compliance`.
- **fsGroup:** the pod `securityContext` field that makes a mounted volume group-writable to a non-root container; without it a root-owned storage add-on returns `EACCES` on every write. Owner: `app-store-deployment`, `release-and-deploy`.
- **Liveness back-off:** kubelet restarting the same pod name with growing gaps after failed liveness probes; a new pod name each time is the deployment controller instead. Read which before theorising. Owner: `observability-and-audit`, `appstore-gate-compliance`.
- **Layer history / image flatten:** an image is a stack of immutable layers, and a policy scan reads their blobs, so a `chmod` in a later layer masks but does not remove a bit an earlier layer set; the fix is to flatten to one clean layer (`FROM scratch` with a single `COPY --from=prep / /`). Owner: `appstore-gate-compliance`, `security-hardening`.
- **CORS (Cross-Origin Resource Sharing):** the browser rule controlling which origins may call an API, set by `Access-Control-Allow-Origin`. Owner: `api-and-integration`.
- **Coverage (test):** the proportion of source lines exercised by the suite; reported as `lcov.info` (Node), `jacoco.xml` (Java), `coverage.xml` (Python). A comprehensive suite that emits no report reads as 0% at the gate; the gate reads the artefact, not the suite. Owner: `testing-standards`.
- **Coverage exclusion:** a file removed from the coverage metric (never from analysis) because it cannot be measured honestly, with the rationale recorded in `sonar-project.properties`. Owner: `appstore-gate-compliance`.
- **Cognitive complexity:** SonarQube's measure of how hard a function is to follow; the App Store gate caps it at 15, cleared by behaviour-preserving extraction into named helpers. Owner: `appstore-gate-compliance`, `code-architecture`.
- **CSP (Content-Security-Policy):** a response header (or meta tag) restricting what a page may load and execute. Static: locked to `default-src 'none'`, `connect-src 'none'`. Owner: `security-hardening`.
- **CSS custom property:** a CSS variable (`--name`, read via `var(--name)`) holding a design token. Owner: `design-system`.
- **CVE (Common Vulnerabilities and Exposures):** a catalogued security vulnerability. Owner: `dependencies`.
- **Data-as-literals:** source data held as in-script array/object literals, normalised into a render model once at init. Static. Owner: `data-layer`.
- **Debounce:** deferring an action until input has settled, so rapid changes trigger one trailing action. Owner: `state-management`.
- **Deferred paint:** delaying a heavy synchronous render behind a double `requestAnimationFrame` so opening/closing a panel is never blocked; the deferred draw aborts if the panel closed. Owner: `frontend-and-rendering`.
- **Degraded pod:** the build succeeded but the running container does not meet the runtime contract (wrong port, not bound to all interfaces, no health path, cannot run as the user). Owner: `release-and-deploy`.
- **Deploy gate:** the binding agent that must return PASS before any deploy; the irreversible publish additionally needs explicit human confirmation. Owner: `agents/deploy-gate`.
- **Design token:** a named visual constant (colour, space, radius, type size) defined once as a CSS custom property and referenced everywhere. Owner: `design-system`.
- **Device-code flow:** an SSO login where a code is shown and confirmed in a browser; used by deploy tooling and the App Store MCP server. Owner: `security-hardening`.
- **Dual palette:** separate product (operational) and document (corporate) colour sets, deliberately not mixed. Owner: `design-system`.
- **esc():** the single HTML escaper neutralising `& < > " '`, used at every reflection site. Owner: `security-hardening`.
- **ETag:** a response header carrying a content fingerprint, letting an unchanged resource return 304 Not Modified. Owner: `api-and-integration`.
- **Echo suppression:** suppressing the client's own write while hydrating from a pulled snapshot, so a sync pull does not bounce back as a write loop. Owner: `state-management`.
- **Fail closed:** when a check cannot be verified or an input cannot be validated, treat it as a failure/reject, never a pass. Owner: the gate agents.
- **Fail-open:** the defect of mapping "could not verify" to "passed" (for example a scanner that exits 0 on an outage); the opposite of fail closed. Owner: `appstore-gate-compliance`, `security-hardening`.
- **fileRef:** a single-use Universally Unique Identifier (UUID) returned by App Store upload tools; passed to `submit_app`/`resubmit_app`. Owner: `app-store-deployment`.
- **Gate:** a blocking review agent or step returning `VERDICT: PASS`/`FAIL`; work does not proceed past a FAIL. An advisory critic returns recommendations, not a verdict. Owner: `agents/`.
- **GitOps repo:** the platform-managed repo holding Helm/ArgoCD config; the deploy stage commits there on your behalf. Owner: `app-store-deployment`.
- **Harbor:** the on-prem container registry images are pushed to before ArgoCD pulls them. Owner: `app-store-deployment`.
- **Health path / readiness probe:** an HTTP path the platform calls to decide if a pod is ready; must return 200. The App Store probe is port 8080, path `/`. Owner: `observability-and-audit`.
- **Hook (Claude Code):** a deterministic command the harness runs before or after a tool call, used as a guardrail. Owner: `settings.json`.
- **LOD (level of detail):** drawing fewer vertices during active interaction, full detail on settle. Owner: `frontend-and-rendering`.
- **LLM (Large Language Model):** the AI model a research scan calls. Owner: `llm-integration`.
- **Lockfile:** `package-lock.json`, the exact resolved dependency tree for reproducible installs. Owner: `dependencies`.
- **Log injection:** smuggling newlines/control characters through a user-supplied log field to forge log lines; defended by sanitising and length-bounding. Owner: `observability-and-audit`.
- **Monotonic rev:** an integer version on a shared document that only increases; a write must carry the rev it read or it is rejected as stale. Owner: `state-management`.
- **MQR (Multi-Quality Rating):** SonarQube mode where ratings reflect the highest-severity unresolved issue per dimension. Owner: `app-store-deployment`.
- **Network egress:** any outbound request from the static artifact. Forbidden; the artifact is offline by design. Owner: `api-and-integration`.
- **Non-root user:** running the container as a user other than root (uid 0), specified numerically (`USER 1000:1000`); required by the image policy. Owner: `release-and-deploy`.
- **Offline-first:** the frontend prefers the live API but falls back to a baked seed when the backend is unreachable, never throwing to the top level. Owner: `frontend-and-rendering`.
- **Optimistic concurrency:** allowing concurrent reads and detecting conflicting writes by version, rather than locking. Owner: `state-management`.
- **Parity test:** a test asserting two implementations of one rule (server and client) agree. Owner: `testing-standards`.
- **pause_turn:** an LLM streaming signal that the model paused mid-turn (to run a server-side tool); the request must be resumed. Owner: `llm-integration`.
- **Persistent volume / storage add-on:** durable storage mounted into the container (`STORAGE_MOUNT_PATH`, `/data`); data to retain goes here, never the ephemeral filesystem. Owner: `release-and-deploy`, `app-store-deployment`.
- **Pipeline gate / stage:** one stage in the App Store CI pipeline (`setup`, `check`, `build`, `test`, `scan`, `package`, `containerize`, `container-scan`, `deploy`); a later stage is reached only when the earlier passes. Owner: `app-store-deployment`.
- **Pipeline simulation:** running the platform's install and test commands against your actual upload artefact in a clean directory, reproducing the files and environment the platform adds to the checkout (its generated `.gitlab-ci.yml`, `GITLAB_CI=true`), before upload. A green repo loop is not a green upload. Owner: `appstore-gate-compliance`.
- **Port 8080 rule:** the App Store sets `containerPort: 8080` and probes it; never set `ENV PORT=`; read `process.env.PORT` defaulting to 8080. Owner: `app-store-deployment`.
- **Prompt caching:** marking a stable system/tools prefix so repeat LLM runs are cheaper. Owner: `llm-integration`.
- **Prototype pollution:** injecting `__proto__`/`constructor`/`prototype` keys to corrupt all objects; defended by deep-stripping those keys. Owner: `state-management`.
- **Provenance:** the source file paths a generalised standard was distilled from. Owner: each skill.
- **Quality gate (SonarQube "App Store Apps"):** the scan-stage pass/fail: zero open violations, coverage at least 80%, security and reliability ratings A, security hotspots reviewed. Owner: `app-store-deployment`.
- **rAF coalescing:** collapsing many input events into at most one render per animation frame via `requestAnimationFrame`. Owner: `state-management`.
- **Rate limiting:** capping how often an endpoint may be called, to protect cost and availability; two tiers (broad plus a strict limiter on the expensive path). Owner: `api-and-integration`.
- **Render model:** the runtime structures the renderer consumes, derived from the data literals. Owner: `data-layer`.
- **Resubmit-to-rollback:** rolling back by repackaging/resubmitting the previous build (there is no separate rollback flow). Owner: `release-and-deploy`, `app-store-deployment`.
- **SAST (Static Application Security Testing):** analysing source for vulnerabilities without running it (Semgrep in the App Store `check` stage). Owner: `security-hardening`.
- **Seed:** initial data written to the store so the app is not empty on first run. Owner: `data-layer`.
- **Single-flight:** allowing at most one instance of an expensive operation to run at a time. Owner: `api-and-integration`.
- **Single-file model:** all CSS, JavaScript, fonts, and images inline in one HTML file with no build step. Static. Owner: `code-architecture`.
- **Slug:** the App Store path identifier and URL component: `{app-name}.apps.bluestaq.com`. Owner: `app-store-deployment`.
- **Smoke test:** the single browser (Playwright) test that boots the seeded server, renders every view asserting zero page errors, and exercises a critical flow with external calls mocked. Owner: `testing-standards`.
- **SPA (Single-Page Application):** a web frontend that runs in one page and updates without full reloads. Owner: `frontend-and-rendering`.
- **SSO (Single Sign-On):** a per-user identity provider; the shared team token leaves a seam where SSO would later attach. Owner: `security-hardening`.
- **Static-html app:** an App Store template that is a package of static files with no server marker; deploys via S3 sync. Owner: `app-store-deployment`.
- **Storage wrapper:** `stGet`/`stSet`, wrapping `localStorage` in try/catch with an in-memory fallback so private mode never throws. Owner: `state-management`.
- **Template auto-detection:** the App Store choosing a build template from package contents (`package.json` to node-react, `pom.xml` to java-spring, `requirements.txt` to python, `Dockerfile`-only to docker-only, static files to static-html). Owner: `app-store-deployment`.
- **Theme:** a named set of token values (light/dark) switched by one attribute or media query; components read tokens, never branch on theme. Owner: `design-system`.
- **Threat model:** the explicit statement of what is defended, the trust boundary, the realistic attackers, and what is deliberately out of scope. Owner: `security-hardening`.
- **timingSafeEqual:** a constant-time string comparison that does not leak through timing, used to compare secret tokens. Owner: `security-hardening`.
- **Trust boundary:** the line between untrusted and trusted data; here the HTTP edge. Owner: `security-hardening`.
- **Verdict contract:** the fixed `VERDICT: PASS`/`FAIL` final line the binding agents must emit. Owner: the gate agents.
- **Verification loop:** static, the three local checks run by `npm test` (syntax validate, render-check, static greps); server, `npm test` (unit tests with coverage) plus the smoke test. Owner: `testing-standards`.
- **View-state object:** the single mutable object holding camera/view state for an interactive surface. Owner: `state-management`.
- **Web manifest:** a `site.webmanifest` declaring icons, name, and theme colour so the app installs and shows correct icons. Owner: `frontend-and-rendering`.
- **`${PARAM}`:** a parameter to set per project; all are tabulated in `AUDIT.md`.

## Decision rules

- If a term is ambiguous between two skills, the **owner** named here is authoritative.
- If a term means one thing in this bundle and another generally, this bundle's definition wins within the bundle's procedures.

## Standards (checkable assertions)

- Every acronym used anywhere in the bundle appears here, expanded on first use in its skill and defined here.
- Every `${PARAM}` used anywhere appears in `AUDIT.md`'s parameter table.

## Observability, accessibility, and deployment terms

- **Golden signals:** latency, traffic, errors, saturation; the four measures of a user-facing service (`observability-and-audit`).
- **SLI / SLO / error budget:** a service-level indicator (good events over valid events), its target over a window, and one minus that target as the allowable failure (`observability-and-audit`).
- **RED / USE:** rate-errors-duration per service; utilisation-saturation-errors per resource (`observability-and-audit`).
- **Trace context:** the W3C `traceparent` carried across services so one request is one trace of spans (`observability-and-audit`).
- **WCAG 2.2 AA:** the web accessibility conformance level this standard meets (`accessibility`, `design-system`).
- **ARIA:** Accessible Rich Internet Applications; roles and attributes that add semantics to custom widgets (`accessibility`).
- **Focus trap / inert / `aria-activedescendant`:** keeping Tab within a modal; marking background unfocusable; tracking a combobox's active option without moving focus (`accessibility`).
- **Distroless / chiselled:** a minimal container base with no shell or setuid binaries and a numeric non-root user (`deploy-recipes`).
- **JaCoCo / Cobertura / OpenCover / lcov:** per-language coverage report formats the SonarQube gate reads (`deploy-recipes`, `toolchain-adapters`).

## Failure modes and remedies

- **A skill uses an undefined term.** Detect: a reader cannot find it here. Fix: add it with its owner. A completeness defect; fix before the bundle is declared done.

## Verification

Grep the bundle for acronyms (`[A-Z]{2,}`) and confirm each appears here. Any miss is a defect.

## Worked example

A newcomer reading `release-and-deploy` hits "resubmit-to-rollback" and "non-root numeric UID". They open `glossary`, find both with their owning skills, and continue without asking anyone.

## Provenance

Merged from both source bundles' glossaries and the App Store doctrine's term list; generalised, business-specific product terms removed.
