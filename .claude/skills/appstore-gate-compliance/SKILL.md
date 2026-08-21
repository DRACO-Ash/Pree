---
name: appstore-gate-compliance
description: Bluestaq App Store upload-gate compliance for container apps. Use when packaging, submitting, or debugging an App Store deployment, or scaffolding a new app to be gate-compliant from the first commit. Covers the platform pipeline and deploy contract, a root-caused failure catalogue, a build-compliant-from-start checklist, and the pipeline-simulation practice, distilled from a real thirteen-failure cycle where a green local loop and binding PASSes still failed the platform.
---

# Bluestaq App Store upload-gate compliance

> The failure this skill prevents: a green local verification loop and passing binding reviews are necessary, never sufficient. The App Store runs its own pipeline against the literal contents of your uploaded zip, in its own environment, and every gap between what your loop checks and what the platform executes is a release you lose. This skill closes that gap. It composes with `app-store-deployment` (the authoritative platform reference), `packaging` (the package contract), `dependencies` and `security-hardening` (the scanners and container hardening), `deploy-recipes` (per-stack Dockerfiles), `testing-standards` (coverage and environment-dependent assertions), `code-architecture` (behaviour-preserving refactors), `observability-and-audit` (how failures surface), and `app-store-readiness` (the pre-flight score).

## Purpose and scope

Lessons distilled from a real thirteen-failure cycle. The single most important structural fact: the pipeline's stages run strictly in sequence, and each stage runs only after the one before it passes, so the platform reveals its requirements ONE GATE AT A TIME. You cannot see the image scanner's policy while SonarQube is failing, nor SonarQube's ruleset while the test stage is failing, nor the deploy-stage runtime faults until every earlier gate is green. Budget one upload per stage when retrofitting an unprepared codebase, or scaffold to the full checklist in section 3 and pay none of it. An app passed its full local loop (ten suites), a binding engineering review, a binding security review, and a binding deploy-gate, then failed the platform pipeline in under eight seconds; the corrected uploads then failed on an environment assertion, on five successive SonarQube waves, on three image policy scans, and on four deploy-stage runtime faults. Every root cause was a contract mismatch between what the local loop verified and what the platform executes; all were preventable at scaffold time. The failure classes map one-to-one onto the pipeline's stages, in order, because each stage only runs once the previous one passes. Scope is the upload-gate and deploy contract and the controls that keep you inside it. It does not restate the platform schema (`app-store-deployment`) or the deploy procedure (`release-and-deploy`).

## When to use

● Writing or reviewing a packaging script or a Dockerfile for an App Store submission.
● Scaffolding a new container app, so it is gate-compliant from the first commit.
● Diagnosing a pipeline that failed on upload despite a green local loop.

## 1. The platform contract (what actually happens to your upload)

The App Store does not run your Continuous Integration (CI). It copies your uploaded zip into a GitLab project it owns, **adds its own generated `.gitlab-ci.yml` to that checkout**, and runs the generated pipeline on a government-cloud runner behind a registry mirror, with no network guarantee to public endpoints.

For a node-container template the observed stages, strictly in order, are:

● **Install**: dependency install from your lockfile.
● **Test**: `npm test -- --coverage` executed at the checkout root, with `GITLAB_CI=true` in the environment. If this exits non-zero, every later stage is SKIPPED and the deploy is dead.
● **Code Quality**: a SonarQube scan with a hard server-side quality gate. Observed thresholds: line coverage at least 80% (read from the lcov report your test stage must emit), zero open violations (bugs, code smells), security hotspots all resolved or reviewed, and reliability and security-review ratings capped. A committed `sonar-project.properties` at the repo root is respected for sources and tests scoping, the coverage report path, and coverage exclusions.
● **Container Build and image policy scan**: `docker build` using the Dockerfile at the archive root, then a policy scan of the built image that judges the base image's own contents, including its layer history, not only your code.
● **Deploy**: the platform schedules the pod and probes its health. This is where runtime-only faults surface for the first time: a non-root container against a root-owned volume add-on needs `securityContext.fsGroup` or every write is `EACCES`; the operator's environment tab must be empty for a code-defaults app (anything typed there overrides the injected values); and a health probe that hangs (a stalled mount) is silently killed by the platform. A pipeline that fails with ZERO stages run is not a stage failure at all: it is app-record residue (a deleted-and-recreated app orphaned its records or GitLab project) or a slug with a double hyphen breaking platform naming.

Detection follows `app-store-deployment`'s template table: a `Dockerfile` at the archive root means a container template; a server manifest at root with no Dockerfile is detected as its stack template; static files with no server marker are static-html.

**Two binding consequences**:

1. For a quality-gated (node, python, java) template, your uploaded zip must be a self-sufficient, testable source tree that emits the coverage report the gate reads, not a build artefact and not a stripped runtime bundle.
2. Your test suite runs in the platform's environment, not yours: extra platform-committed files exist in the checkout (at minimum the generated `.gitlab-ci.yml`), the runner may be offline to public endpoints, and `GITLAB_CI=true` is set. Any assertion about the environment itself must be written for that environment.

## 2. Failure catalogue (observed, root-caused, fixed)

Thirteen real failures, each root-caused and fixed, are catalogued in full in `references/failure-catalogue.md`. Read that file when diagnosing a specific pipeline failure, or when you want the root-cause narrative behind a checklist item. The fixes are already condensed into the checklist (section 3), the decision rules, and the standards below, so scaffolding a compliant app does not require the catalogue; debugging a live failure usually does.

The classes map one-to-one onto the pipeline stages, in order, because each stage runs only once the previous one passes:

● **Install and Test** (Failures 1 to 5): the test suite banned from the package (died in eight seconds); a test orchestrator that assumed a prior build; a security scan that fail-opened on a registry outage; a baked `ENV` default that defeated the platform's injected value; and a negative environment assertion guaranteed-false only on the platform's checkout.
● **Code Quality** (Failure 6): the SonarQube gate across five successive waves (620 to 2 open violations, coverage 0% to 80%), teaching that a findings report is a sample not the population, that the profile reveals rules progressively, and that mechanical rule-clearing swaps carry semantic traps.
● **Image policy scan** (Failures 8 to 10): the scan flags the base image not your code (npm toolchain and suid bits); it reads layer history, so an in-place `chmod` cannot clear it (flatten to `FROM scratch`); and your own later instruction (`adduser` setting setgid on the home) re-introduces the class the sweep just cleared (sweep last).
● **Deploy** (Failures 11 to 13): guidance prose pasted as an environment-variable value; a root-owned volume refusing writes from the non-root container (`securityContext.fsGroup`); and a pod dying silently after a clean boot because a health probe hung rather than failed.

A prior-cycle failure (Failure 7) was stale platform configuration surviving an archetype pivot; a separate class is app-record residue or a double-hyphen slug, which fails with ZERO stages run rather than at a stage. All are detailed in the reference file.

## 3. Build-compliant-from-start checklist

Apply at scaffold time for any app targeting the App Store container template. For the Node reference stack these scaffold-time defaults ship as copy-ready files under `deploy-recipes/templates/node/` (the hardened flattened `Dockerfile`, `run-tests.mjs`, `sonar-project.properties`, `eslint.config.mjs`, `package-appstore.sh`, and `simulate-pipeline.sh`); copy them, do not re-derive them. A skill's checklist is a liability until it is materialised in the scaffold.

● `Dockerfile` at the repo and package root, multi-stage, lean base, non-root numeric user, `EXPOSE 8080`, and a healthcheck on an unauthenticated `/healthz` that proves storage with a real WRITE (not an existence check, which passes on a read-only or root-owned mount), races a hard TIMEOUT strictly shorter than the platform's probe, and returns the resolved data dir and the exact errno in its 503 body so a screenshot is a full diagnosis.
● Boot emits one decisive log line recording whether storage accepted a write, and ready and unready transitions log, so a pod that is silently killed still leaves a narrative. A health probe that can hang converts an infrastructure fault into an undiagnosable silent liveness kill.
● Runtime stage hardened for the image policy scan from day one: package-manager toolchain removed (npm/corepack/yarn on Node, pip on Python; the build stage keeps it), base packages upgraded, and nothing in the final stage the CMD does not need. The scanner judges what ships, not what runs.
● The suid/sgid sweep is the LAST mutation in the prep stage, after user creation and all file copies: `find / -xdev -perm /6000 \( -type f -o -type d \) -exec chmod a-s {} +`, fail closed. Later instructions can violate the invariant (busybox `adduser` sets setgid on the home it creates), so nothing may follow the sweep; leave the sticky bit (the policy tests `/6000` only).
● The shipped stage is FLATTENED: hygiene in a `prep` stage, then `FROM scratch` with one `COPY --from=prep / /` and re-declared metadata (including explicit PATH). The scanner reads layer history, and a single clean layer is the only construction with none.
● The operator console state for a code-defaults app is an EMPTY environment tab; platform-injected variables live at the pod level, so anything typed into the tab is an override and a liability. A non-root container using the file-storage add-on needs an operations request to set `securityContext.fsGroup`, or the root-owned mount refuses every write.
● No `ENV PORT=` and no `ENV DATA_DIR=` in the Dockerfile. Code defaults carry those values; platform injection wins.
● Storage-path resolution: explicit variable, then platform-injected variable, then default. Boot validation: absolute, not root, writable; fail closed loudly.
● `npm test` is green in a fresh clone with only an install step before it; the orchestrator self-builds any artefact a suite reads.
● Build and test entrypoints run in the platform's minimal shell, not just your login shell. The container base is commonly Alpine, whose `/bin/sh` is BusyBox with no `bash`; a `package.json` script or a build step that shells out to `bash`, or uses a bash-only feature (arrays, `[[ ]]`, `pipefail`), dies with `sh: bash: not found` at the platform build even though it ran locally. Keep the build and test steps pure to your runtime (for Node, pure `node` scripts), or `sh`-portable, and simulate them under `sh`.
● The package script produces a source zip: Dockerfile and lockfiles at root, source, build tooling, tests, docs. Banned: `node_modules`, built output, version-control metadata, any `.env`.
● `.dockerignore` and the package allowlist are separate contracts. One shapes the image, the other shapes the upload.
● Every external scanner has its offline behaviour defined and tested: honest skip on offline runners, hard fail on the authoritative networked runner.
● Every negative assertion is classified per environment: enforced everywhere, or gated on `GITLAB_CI`. No assertion may be guaranteed-false on the machine that gates the deploy.
● `.gitignore` blocks `.env`, `.env.local`, `.env.*.local`, local data directories, and `coverage/` from day one.
● Coverage tooling wired from the first commit: the platform's exact test command emits the report the gate reads (lcov at `coverage/lcov.info` for Node; per-stack paths in `deploy-recipes`); `sonar-project.properties` committed with sources, tests, report path, and any honest exclusions.
● A Sonar-equivalent static-analysis pass in the per-commit loop (cognitive complexity at most 15, loop shapes, comparators, ARIA and contrast rules), so violations are fixed one at a time instead of six hundred at once. Make it concrete, not aspirational: for a Node project wire the ESLint `unicorn` and `sonarjs` plugin sets and cap cognitive complexity at 15 (SonarQube rule S3776), because the local default `complexity` rule measures cyclomatic complexity at 20, which is looser and different, so a function passes locally and fails on upload. Other stacks wire the equivalent SonarQube-profile analyser (`deploy-recipes`). If the local profile is looser than the platform profile anywhere, that gap is a future upload failure: two whole classes surfaced only on upload in one project because the local profile lacked them, the preferred-optional-chain rule (`x && x.y` to `x?.y`) and the missing-sort-comparator rule (S2871).
● In a shared-global-scope bundle, a naming convention for module helpers (unique per-module prefixes) from the start.
● A release version bump edits the lockfile by path, never by search-and-replace (see Decision rules).
● A pipeline-simulation script exists and runs before every upload (section 4).
● Ship often, so the new-code window stays small. The SonarQube gate scores NEW code against a zero-violations bar, and every shipped release resets that baseline; a long unshipped backlog means the next upload re-scans the whole accumulated range as new code, so one missed rule spelling anywhere in six stacked releases fails the whole upload. Prefer small, shipped increments; if work must stack, run the local analyser over the WHOLE accumulated range, not just the latest diff. (This trades against batching for gate cost in `resource-discipline`: batch the local binding gates, but do not let releases stack unshipped against the platform's new-code window.)

## 4. The transferable practice: simulate the platform pipeline exactly

The single highest-value control, refined by each failure. Simulate what the platform does, INCLUDING the files and environment it adds to your checkout, against the artefact, not the repo:

```
npm run package:appstore                              # produce the actual upload artefact
unzip -q the-package.zip -d /tmp/sim                  # what the platform will check out
printf 'stages: [test]\n' > /tmp/sim/.gitlab-ci.yml   # the platform commits its own CI file
cd /tmp/sim
npm ci                                                # the platform's install stage
GITLAB_CI=true npm test -- --coverage                 # the platform's exact command AND environment
test -s coverage/lcov.info                            # the artefact the SonarQube gate reads
```

If that sequence is not green, the upload will fail regardless of what the repo's own CI says. The simulation must reproduce the platform's checkout state and environment, not just its command, and must verify the coverage artefact exists. The two gates the simulation cannot fully reproduce are the SonarQube ruleset and the image policy scan (both server-side); the mitigations are a per-commit analyser that keeps the violation count at zero, and a flattened, hardened runtime image (Failures 8 and 9) that passes the scan by construction.

## 5. Behavioural traits (for working with Claude on a deployment)

Phrased as habits to ask for and to review against (`working-with-ai`):

● **One gate at a time.** The pipeline reveals its requirements sequentially; a report for one stage is not the whole contract. Scaffold to the full checklist so you never pay per-stage discovery.
● **A findings report is a sample, not the work list.** Fix by rule class, eradicate by grep across every spelling, and measure the population directly; never treat one scan's page of issues as complete.
● **Contract-first packaging.** Obtain the platform's execution contract (what runs, where, with what network, with what added files) before writing a packaging script. Distrust any inherited exclusion list.
● **Fresh-environment paranoia.** Treat "works here" as unverified until proven from the actual artefact, under the platform's environment, with its added files present. Make the simulation a named script and harden it after every platform failure.
● **Negative-assertion environment audit.** Every "must not exist" rule has an enforcement context; a rule guaranteed-false on the deploy runner is a self-inflicted outage.
● **Coverage is an artefact, not a virtue.** Emit the report the gate reads and confirm it exists; a comprehensive suite that emits nothing scores 0%.
● **Fail-open hunting on gates.** Ask what a tool emits when its dependency is absent, and whether your parser distinguishes "clean" from "could not check". Reproduce the outage; do not reason about it.
● **The scanner judges what ships, not what runs.** Base-image contents and layer history are in scope; harden and flatten the runtime rather than editing your code when an image scan fails.
● **Silent-success suspicion.** The most expensive failures worked visibly while the durable behaviour was wrong (an ephemeral write, an unchecked advisory, a suid bit in an earlier layer). Verify the durable effect: restart and re-read, sever the dependency and re-scan.
● **Behaviour-preserving refactors, proved independently.** When you refactor to clear a metric, prove behaviour over hostile inputs with a parity harness and a render smoke, not just the suite you had.
● **User-confirmed actions fail loudly.** A compliance sweep that adds `.catch` handlers must surface an operator-initiated failure (a restore, a destructive delete) as a visible error, never to `console.debug`; only best-effort background work may log quietly (`observability-and-audit`).
● **Honest degradation vocabulary.** Passed (verified), skipped (could not verify, stated loudly, compensating control named), failed (verified bad); never conflate them.
● **After two failures of one mechanism, change the mechanism, not the value.** A deploy cycle is expensive; re-tuning the same approach a third time (the same env-var channel, the same fix spelling) usually means the mechanism is wrong, not the value. When the same fix fails twice, step out and change the approach.
● **Never let the app die silently.** Health probes race a hard timeout, boot logs its storage verdict once, and state transitions narrate; an infrastructure fault must become a named log line, not an undiagnosable kill. Health-check with a real write, and return the errno in the 503 so a screenshot diagnoses it.
● **Read the failing log before theorising, and the RAW log not the summary.** Every platform failure was diagnosed in one pass because the log was read verbatim and the failure reproduced locally before any fix was written. The failure summary prints `N/A` where the raw job log names the file and mode; obtain the raw log first, and when a hypothesis and a log line disagree, the log line wins. A pod log can interleave multiple pod generations, and a crash-looping app pod beside a healthy add-on pod (ClamAV) localises the fault to configuration or the app, not the infrastructure.

## Decision rules

● **Green local loop, about to upload?** Run the section 4 simulation against the actual artefact, with the platform's added file, `GITLAB_CI=true`, and a `coverage/lcov.info` check. A green repo loop is not a green upload.
● **An image scan fails on `suid_or_guid_set` or base-package CVEs?** It is the base image, not your code. Remove the toolchain, strip suid bits from files and directories, upgrade base packages, and flatten to `FROM scratch` with one `COPY --from=prep / /`. If a partial fix moves the count rather than zeroing it, the scanner is reading layer history; flatten.
● **A negative assertion ("X must not exist")?** Classify it ours-only (gated on `GITLAB_CI`) or everywhere; never let one be guaranteed-false on the deploy runner.
● **Mirroring an external analyser that reveals findings one wave at a time?** Inherit its WHOLE recommended profile locally, do not hand-pick the rules it has so far named: hand-picking guarantees a next wave, because the local gate can only hold the classes the platform has already caught. One project cleared five SonarQube waves this way before inheriting the full recommended profile (217 rules, not 21 hand-picked) and grepping for the two classes the linter cannot see without type information. Where the local mirror genuinely cannot enforce a rule the analyser runs, record the gap explicitly and enforce it another way, naming the real detector rather than inventing a plausible rule name. And decline a suggested fix that changes behaviour, in writing, with the divergence stated: an analyser once offered `x <= 0` for `!(x > 0)`, which differ on NaN, so the code kept an explicit finite check instead.
● **A findings report of any kind (Sonar, audit, a reviewer's named line)?** It is a SAMPLE, never the population. Before fixing, enumerate the population: grep every syntactic spelling of the defect across the whole analysed scope, deliberately (positive and negated forms, guard and expression forms, single-line and multi-line forms, and the copying variant of any mutating API), and record the count and the exact command that produced it so the number is checkable. Then ship a MECHANISED check that fails on the CLASS, not tests for the named instances, so the next occurrence cannot reappear; where the linter cannot see the class (no type information, generated code, a prose rule), a grep test is the right instrument and must state what it cannot see. The anti-pattern this closes was measured at roughly three calendar days: eight consecutive review rounds on one defect class, each fixing only the sites the previous reviewer happened to name, ending only when the sweep finally became a test that fails on the whole class, which is what round one should have produced.
● **A release version bump?** Surgical, never search-and-replace. It touches exactly the lockfile's root `version` and `packages[""].version` and `package.json`; verify `git diff package-lock.json` shows exactly two changed lines and never touch a line containing `node_modules/` (a repo-wide replace has twice corrupted a lockfile record, caught only by the binding gate).
● **A mechanical swap to clear a rule?** It can change behaviour on the wider input domain; prove the truth table over hostile inputs and keep a risky guard inside a try block if the domain is not pinned. Known traps from the field: `localeCompare` is not the code-unit sort order, so swapping a comparator into it silently reorders anything whose checksums or migration order depend on the exact order; `Number.isNaN` does not coerce where the old `isNaN` did; and `x && x.y` to `x?.y` changes the produced value from the falsy left operand to `undefined`. Require a parity harness or an exact-order comparator, not just a green existing suite.
● **An app pod crash-loops or dies after a clean boot (deploy stage)?** It is configuration or the app, not infrastructure, if a sibling add-on pod is healthy. Read the raw pod log first. A fail-closed boot error quoting a value means a variable was set wrong (often guidance prose pasted into the env tab; delete it, the tab should be empty). `EACCES` on a write means the non-root container hit a root-owned mount (an ops request for `securityContext.fsGroup`). A clean boot then a silent `SIGTERM` with growing restart gaps means a hanging health probe killed by kubelet liveness back-off (add a hard probe timeout and a boot storage-write log line).
● **A pipeline fails with ZERO stages run?** Not a stage failure: app-record residue from a deleted-and-recreated app, or a slug with a double hyphen. Recreate under a fresh single-hyphen slug and confirm the old records and GitLab project are cleared.
● **A build or test step dies with `sh: bash: not found` (or a bash-only feature is undefined)?** The platform ran it under a minimal `sh` (BusyBox on Alpine), not bash. Rewrite the step pure to your runtime or `sh`-portable, and simulate it under `sh`; do not add bash to the image to paper over it.

## Standards (checkable assertions)

● For a quality-gated template, the uploaded package is a testable source tree that emits coverage: `npm test -- --coverage` at the checkout root passes in a fresh unzip after `npm ci`, under `GITLAB_CI=true`, and writes a non-empty `coverage/lcov.info`.
● A `sonar-project.properties` is committed declaring sources, tests, the coverage path, and any exclusions (coverage metric only, each with a written rationale); a per-commit static-analysis pass keeps violations at zero.
● The runtime image ships no package manager, no suid/sgid bits on files or directories, and is flattened to a single layer (`FROM scratch` with one `COPY --from=prep / /`), so the policy scan finds nothing in layer history.
● The packaging allowlist and `.dockerignore` are distinct; every negative assertion is classified per environment; no `ENV PORT=`/`ENV DATA_DIR=`; injected paths resolve in code with boot validation.
● A release bump changes exactly two lockfile lines; operator-initiated failures surface visibly.
● The health endpoint proves storage with a real write, races a timeout shorter than the platform's probe, and returns the resolved dir and errno in its 503 body; boot logs its storage verdict once; the operator env tab is empty for a code-defaults app; a non-root container using the volume add-on has `securityContext.fsGroup` set.
● A pipeline-simulation script reproducing the platform's checkout, environment, and coverage artefact runs before every upload.
● Build and test entrypoints are pure to the runtime or `sh`-portable (no `bash` dependency and no bash-only feature), because the platform runs them under a minimal shell (BusyBox `sh` on Alpine), and are simulated under `sh`.
● The local static-analysis profile is the platform's, not a looser default (for Node, `unicorn` and `sonarjs` with cognitive complexity capped at 15); a rule that can fire on the platform can fire locally. Releases are shipped small rather than stacked unshipped, or the whole accumulated range is analysed before upload, so the new-code window and its blast radius stay small.

## Failure modes and remedies

● **Pipeline dies seconds in, downstream skipped.** A suite or config was excluded from the package. Fix: ship tests, docs, runner config; keep them out of the image via `.dockerignore`; re-run the simulation.
● **One assertion fails only on the platform.** A negative assertion is guaranteed-false in the platform's checkout. Fix: gate it on `GITLAB_CI`.
● **Code Quality fails on coverage 0%.** The suite emitted no lcov. Fix: run under a coverage tool on `--coverage`, emit and commit the config, verify the file is non-empty.
● **Code Quality fails on hundreds of violations, and again on rescan.** No per-commit analyser, and reports are samples. Fix: wire the analyser; eradicate by rule class and residual grep across every spelling; prove refactors behaviour-preserving.
● **Image scan fails on `suid_or_guid_set N/A` after a chmod.** The scanner reads layer history. Fix: flatten to one clean layer via a `prep` stage and `FROM scratch`.
● **A user-confirmed action silently failed.** A compliance `.catch` logged instead of surfacing. Fix: classify handlers by who initiated; operator-initiated failures show a visible error.

## Verification

Simulate the pipeline (section 4): green, with a non-empty `coverage/lcov.info`. Run the per-commit analyser: zero violations. Grep the suite for negative file-existence checks: each is gated per environment. Build the image (or, if the registry is blocked, verify against the upstream base and let the platform build fail loudly): the final stage is `FROM scratch` with one `COPY`, no toolchain, no suid bits, and the policy scan reports no critical. `git diff package-lock.json` on a release bump shows exactly two lines. Deploy, restart, re-read persisted state: it survives.

## Glossary

● **One gate at a time:** the pipeline runs stages in sequence and reveals each stage's requirements only after the previous passes; budget one upload per stage when retrofitting.
● **Image policy scan:** the platform's scan of the built image; it judges the base image's contents and layer history (suid/sgid bits, vendored CVEs), not only your code. Owner: `security-hardening`, `app-store-deployment`.
● **Layer history / flatten:** an image is immutable layers; a later `chmod` cannot clear an earlier layer's bit, so ship a single clean layer via `FROM scratch` and one `COPY --from=prep / /`.
● **Quality gate:** the SonarQube Code Quality gate; fails on coverage below 80%, any open violation, an unreviewed hotspot, or a rating over cap.
● **Progressive rule revelation:** the profile surfaces new rule classes across successive scans; plan three to five cycles when retrofitting.
● **Fail-open:** a control that maps "could not verify" to "passed"; the opposite of fail closed.
● **Deploy stage:** where the platform schedules and probes the running pod; runtime-only faults (fsGroup, env-tab overrides, hanging health probes) surface here for the first time. Owner: `release-and-deploy`, `observability-and-audit`.
● **fsGroup:** the pod `securityContext` field that makes a mounted volume group-writable to a non-root container; without it a root-owned add-on volume refuses every write (`EACCES`).
● **Liveness back-off:** kubelet restarting the SAME pod name with growing gaps after failed liveness probes; a NEW pod name each time is instead the deployment controller.
● **Raw job log vs failure summary:** the summary prints `N/A` for paths the raw job log names in full; obtain the raw log before theorising, and the log line beats the hypothesis.
● Other terms: `glossary`.

## Provenance

Distilled from a real App Store thirteen-failure cycle across five gate classes: packaging (the test suite excluded from the zip), an environment-dependent negative assertion (false only on the platform's checkout), five successive SonarQube quality-gate waves (620 to 2 open violations, converging only once fixing switched from report-driven to grep-verified class eradication across every syntactic spelling), three container image policy scans (base-image contents, then layer history closed by flattening, then instruction ordering closed by running the sweep last), and four deploy-stage runtime faults (guidance prose pasted as an env value in two variables, a root-owned volume refusing non-root writes, and a hanging health probe silently killed), with an app-record residue class where a recreated app or a double-hyphen slug fails with zero stages run. The remediations root-caused each defect, reproduced each adversarial attack, proved every refactor behaviour-preserving with parity harnesses and a render smoke, and hardened the pipeline simulation, the runtime image, and the health probe so each class cannot recur. The controls are cross-referenced to the owning baseline skills so a gap routes back to its owner.
