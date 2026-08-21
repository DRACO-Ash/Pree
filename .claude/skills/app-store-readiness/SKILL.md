---
name: app-store-readiness
description: Pre-submit readiness check that scores a project's likelihood of passing the Bluestaq App Store and tells you exactly which skills to download from Launchpad to close each gap. Use when someone asks "will this pass the App Store", "check my project against the standard", "am I ready to submit", or "score my acceptance likelihood". Runs the project's own verification loop, orchestrates the binding gates, and writes a READINESS.md report with a band, findings, fixes, and the specific skills to fetch. A pre-flight estimate, not the platform's binding decision.
---

# App Store readiness check

## Purpose and scope

A dress rehearsal for the binding deploy gate and the App Store's own review. It scores a project against the Foundations standard and the App Store runtime and submission contract, then writes a single report: a likelihood band, the findings that move it, a concrete fix for each, and the exact Launchpad skills to download to close each gap. It does not re-implement review; it runs the project's real verification loop and orchestrates the existing gate agents, then synthesises one verdict. Scope is the readiness estimate and the report. It is not the binding decision: the App Store's quality gate, continuous Authority to Operate (cATO) score, and human review are the real gate, and the binding `deploy-gate` is the last internal word. This check raises your odds and removes surprises; it does not grant acceptance.

It works standalone (drop this one skill into a session and it carries the checkable rubric below), and it works better with the full baseline present, where it defers to the owning skills and invokes the `engineering-reviewer`, `security-reviewer`, and `deploy-gate` agents.

## When to use

- Before submitting to the App Store, as the pre-flight before the binding `deploy-gate`.
- At any point in Expert mode to get a running readiness read on a change.
- When a project was not built on the baseline and you want to know how far off the standard it is.

## Procedure

1. **Detect the shape.** Read `CLAUDE.md` for the archetype and stack; if absent, infer from package markers (`toolchain-adapters`): `index.html` only and no server marker means static; a `Dockerfile` or a server manifest means server. The rubric below is routed by this.
2. **Run the project's real loop, do not guess.** Run the project's own verification loop and deployability check (`npm test` / the stack equivalent in `toolchain-adapters`; `npm run deployable` if present). Record the real output. No loop present is itself a finding (owner: `testing-standards`, `ci-cd`). If a leg defers to Continuous Integration (the static render-check exits non-zero where no browser is available), it is not a local pass: read the result from CI instead, never assume it.
3. **Read the real CI result, do not infer it from the workflow file.** If the repository has CI, fetch the latest workflow run conclusion for the current head commit (the GitHub MCP tools, or `gh run list`/`gh run view`) and record it. A red required check is a blocker, exactly like a red local loop. The existence of a workflow file is not evidence that it passed; scoring this dimension from file presence alone is the failure this step exists to prevent.
4. **Score the mechanical and contract dimensions** (the rubric). Each is pass, fail, or not-applicable, with the weight shown.
5. **Deep mode (recommended before submit):** invoke the binding gates against the diff or the artifact and collect their verdicts: `engineering-reviewer`, `security-reviewer` (if any security surface changed), `deploy-gate`. A gate `FAIL` is a blocker.
6. **Compute the band, fail closed.** Score is the weighted pass rate of applicable dimensions, but any blocker (a failing binding gate, a secret in the repo, a broken health or port contract, the loop red, or a red required CI check) caps the band at "Not yet" regardless of the rest. Bands: **Ready** (no blockers, loop green, CI green, all gates PASS, contract met), **Likely after fixes** (no blockers but minor gaps), **Not yet** (one or more blockers).
7. **Write `READINESS.md`** in the project root (an audit artifact, `observability-and-audit`): the band and score, a per-dimension table, blockers first, and for every gap the finding with file and line, the owning skill, a concrete fix or a copy-ready fix prompt, and a "what would raise the band" line. **Deliver the full report to the chat as well, not only to the file.** The file is the durable audit record; the chat copy is what the user reads now. Print the band, the per-dimension table, the blockers, and the skills to download directly in the response, then name the file path. Never reply with only "see READINESS.md".
8. **Recommend the skills to download.** For every gap, name the owning skill from the map below and, if it is not already in the project's `.claude/skills/`, tell the user to download it from Launchpad (flight manual, find the skill, Download), or to use Launchpad's tailored bundle. List the missing skills as a single copy-ready set at the end of the report.

## The rubric (checkable dimensions, both archetypes unless noted)

- **Verification loop green** (weight: blocker). The loop runs and exits 0. Owner: `testing-standards`.
- **Coverage at least 80%** (server) (heavy). Owner: `testing-standards`.
- **No secret in source or history** (blocker). Regex and a history scan. Owner: `security-hardening`.
- **Static: locked CSP, no egress, no dynamic code, one escaper** (blocker, static). Owner: `security-hardening`, `frontend-and-rendering`.
- **Server: reads PORT default 8080, binds 0.0.0.0, GET / and health return 200, non-root numeric user, no ENV PORT** (blocker, server). Owner: `app-store-deployment`, `deploy-recipes`, `release-and-deploy`.
- **Container package is flat: `Dockerfile` and entrypoint at the package root, not nested in a subdirectory** (blocker, container). A nested `Dockerfile` defeats template detection and the build context (`context must be a directory`). Owner: `packaging`, `app-store-deployment`.
- **Runtime image is hardened AND flattened: non-root, no setuid/setgid bits on files or directories, no unused package manager, OS patched, and shipped as a single layer** (blocker, container). The container-scan policy stops on `suid_or_guid_set` (commonly the base image's bundled npm tree) and on a High/Critical CVE in a tool the runtime does not need. The scan reads layer history, so an in-place strip leaves path-less (`N/A`) findings unless the runtime is flattened (`FROM scratch` with one `COPY --from=prep / /`). Owner: `security-hardening`, `deploy-recipes`, `app-store-deployment`, `appstore-gate-compliance`.
- **Coverage report at the path the SonarQube gate reads** (server) (heavy). Owner: `deploy-recipes`, `toolchain-adapters`.
- **Reproducible install from a committed lockfile; no unaddressed High or Critical CVE** (heavy). Owner: `dependencies`.
- **Container (quality-gated): the upload is a testable source tree, the platform-pipeline simulation is green, and it emits the coverage the SonarQube gate reads** (blocker before submit, container). Unzip the actual artefact into a clean directory, add a platform-style `.gitlab-ci.yml`, `npm ci`, then `GITLAB_CI=true npm test -- --coverage`, and confirm `coverage/lcov.info` is non-empty; a comprehensive suite that emits no report still scores 0%. A committed `sonar-project.properties` scopes sources, tests, the report path, and any exclusions. A green repo loop is not a green upload. Owner: `appstore-gate-compliance`, `packaging`, `testing-standards`.
- **Container (quality-gated): a per-commit static-analysis pass keeps SonarQube violations at zero** (heavy, container). The gate fails on any open violation, so run a Sonar-equivalent analyser locally or in CI from scaffold time; hundreds of accumulated findings are a self-inflicted retrofit. Owner: `appstore-gate-compliance`, `ci-cd`.
- **The `test` script tolerates the platform invocation and emits coverage** (blocker, quality-gated container). The platform runs `npm test -- --coverage`; a bare `node --test` rejects the unknown flag and fails the stage, skipping every later gate. The script must own its flags, ignore extra CLI args, scope discovery with an explicit glob, and write `coverage/lcov.info`. Owner: `testing-standards`, `appstore-gate-compliance`.
- **Every negative assertion is classified per environment, and every scanner distinguishes "clean" from "could not check"** (blocker, container/server). No assertion may be guaranteed-false on the platform runner (it commits `.gitlab-ci.yml` and sets `GITLAB_CI`); no gate may fail-open on an outage. Owner: `appstore-gate-compliance`, `testing-standards`, `dependencies`.
- **A CI pipeline that mirrors the local loop, least privilege, and its latest run is green** (blocker if a required check is red, else medium). Score from the actual run conclusion for the head commit, not from the workflow file existing. Owner: `ci-cd`.
- **Version stamp and an audit row; generic client errors; no secret in logs** (medium). Owner: `observability-and-audit`.
- **Accessibility to WCAG AA; tokens only, one accent** (medium, UI). Owner: `accessibility`, `design-system`.
- **Surgical structure, no dead code, the documented architecture** (medium). Owner: `code-architecture`.
- **House voice in user-facing copy** (light). Owner: the `house-voice` output style.

## Failure to owning-skill map (what to download from Launchpad)

| If this fails | Download and read |
|---|---|
| Loop red, weak tests, low coverage | `testing-standards` |
| Secret in repo, no `.env.example` | `security-hardening` |
| Static: CSP, egress, dynamic code, escaping | `security-hardening`, `frontend-and-rendering` |
| Server: port, health, non-root, ENV PORT | `app-store-deployment`, `deploy-recipes`, `release-and-deploy` |
| Coverage path the gate cannot read | `deploy-recipes`, `toolchain-adapters` |
| No lockfile, unpinned deps, a CVE | `dependencies` |
| No CI, or CI drifts from the loop | `ci-cd` |
| Green local loop but fails on upload; tests excluded from the package; a fail-open scanner; a baked ENV; an assertion false only on the platform | `appstore-gate-compliance` |
| No health, no audit row, a secret in a log | `observability-and-audit` |
| Contrast, keyboard, colour-only meaning | `accessibility`, `design-system` |
| Sprawling structure, dead code | `code-architecture` |
| Not sure where to start | `getting-started` |

If many skills are missing, the simplest fix is Launchpad's tailored bundle for the project's shape, which ships exactly this set.

## Decision rules

- **Fast or deep?** Run the mechanical and contract dimensions first (cheap, deterministic). Escalate to the gate agents before an actual submit, or on request. Same routing as `working-with-ai`.
- **A blocker is present?** The band is "Not yet" no matter how high the weighted score; report the blocker first and do not soften it.
- **A dimension cannot be evaluated** (a tool missing, a file absent)? Mark it unknown, never pass; an unverifiable control is treated as failed.
- **CI is present?** Read its actual latest conclusion for the head commit, not the workflow file's existence. A red required check is a blocker that caps the band at "Not yet", even when the local loop is green; the two can disagree precisely because a leg defers to CI. Never report "Ready" over a red pipeline.
- **Standalone, no baseline present?** Score against the rubric here and recommend the skills to download rather than deferring to absent skills or gates.

## Standards (checkable assertions)

- The report runs the project's real loop and records its actual output, not an assumed result.
- Every gap names a file and line, an owning skill, and a concrete fix.
- A blocker caps the band at "Not yet"; the report never reads "Ready" with a failing gate, a red loop, a red required CI check, or a secret in the repo.
- The CI dimension is scored from the latest run's actual conclusion for the head commit, never from the workflow file merely existing.
- The report names the exact skills to download from Launchpad for the gaps found.
- The report states plainly that it is a pre-flight estimate, not the binding App Store decision.

## Failure modes and remedies

- **A green report, then the App Store rejects.** Cause: the check passed the mechanical and contract dimensions but the platform's human or quality gate found more. Remedy: the report must say it is an estimate; run the deep gate orchestration before submit, never the fast pass alone.
- **The check rubber-stamps without running anything.** Cause: it asserted instead of executing. Remedy: it must run the real loop and gates and quote their output; an assertion is not a result.
- **Recommends skills already present.** Cause: it did not check `.claude/skills/`. Remedy: only list skills that are missing or whose standard is being violated.

## Verification

Run it on a known-good baseline project: the report reads "Ready", the loop output is real, no skills are recommended. Run it on a project with a deliberately broken control (an unlocked CSP, a missing health path, a secret): the band is "Not yet", the blocker is named first with its file and line, and the owning skill is listed to download. The report file exists at the project root and is reproducible across runs on the same code.

## Worked example

A static artifact with a `fetch()` call and no version stamp. The check detects static, runs `npm test` (the static security greps fail on the egress), scores the egress as a blocker, and writes `READINESS.md`: band "Not yet", blocker first ("network egress at index.html:412, forbidden for a static artifact"), fix ("remove the fetch; embed the data as a literal"), owning skill `security-hardening` with "download it from Launchpad if it is not in your .claude/skills", and a medium finding for the missing version stamp (owner `observability-and-audit`). Re-run after the fix: the loop is green, the band moves to "Likely after fixes", and the only remaining item is the stamp.

## Glossary

- **Readiness band:** Ready, Likely after fixes, or Not yet; the headline result.
- **Blocker:** a failing binding gate, a red loop, a secret in the repo, or a broken port or health contract; it caps the band at "Not yet".
- **Deep mode:** running the binding gate agents in addition to the mechanical and contract checks.
- **cATO / SonarQube quality gate:** the App Store's own publication scores; this check estimates, it does not set them. See `app-store-deployment`, `glossary`.

## Provenance

Authored to compose the existing verification loop, the deployability check, and the binding `engineering-reviewer`/`security-reviewer`/`deploy-gate` agents into one scored, reproducible pre-submit report, with a failure-to-skill map so a gap routes the user straight back to the owning Launchpad skill. The rubric is drawn from the checkable assertions of those skills and the App Store runtime and submission contract in `app-store-deployment`.
