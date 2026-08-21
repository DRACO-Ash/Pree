---
name: testing-standards
description: Test strategy, tooling, and craft. Use when writing or running tests, debugging a red test, or judging whether a suite protects the code. Covers the static validate-plus-render-check-plus-security-greps loop, server node:test with lcov coverage, in-process HTTP via the app factory, parity and security-property tests, a Playwright browser smoke test, and building tests in as you code. Coverage at least 80%.
---

# Testing standards

> Stack note: the commands shown here are the Node example. For Python, Java, Go, Rust, or .NET, run the equivalent canonical step from `toolchain-adapters`. The principle in this skill is what binds; the command is illustrative.

## Purpose and scope

The test strategy and patterns that keep the suite reliable across both archetypes. The static artifact runs a three-part verification loop: inline-JS syntax validation, a headless render-check on desktop and mobile, and static security greps. The server app runs `node:test` unit tests with built-in coverage, in-process HTTP tests via the app factory, cross-runtime parity tests, security-property tests, and a single Playwright browser smoke test. Scope is testing and coverage. It does not cover CI orchestration (`ci-cd`) or the deploy gate (`release-and-deploy`).

## When to use

- Writing or changing a test, or judging whether coverage is adequate.
- Deciding which tier a behaviour belongs to.

## Prerequisites

- `environment-setup`. Static needs Node and the headless browser driver; server unit tests need only Node, the smoke test needs a browser.

## Procedure (static: the verification loop)

1. **Validate inline-JS syntax.** Extract the inline script and run `node --check`. Expected: `JS syntax: OK`.
2. **Render-check headless on two viewports.** Load the artifact in a headless browser at desktop and mobile sizes; assert core behaviour (search, selection, an injection-inertness probe) and fail on any non-allow-listed console error. Expected: two `PASS` lines.
3. **Run static security greps.** Assert no dynamic code, no egress, no message listeners; the escaper covers all five characters; link-safety counts match; the CSP is locked. Expected: `STATIC CHECKS: PASS`. These three are wired as `npm test`.
4. **Unit-test non-trivial inline logic out of band.** Render-check is a smoke test, not a logic test, and it needs a browser the build sandbox often cannot run. When the artifact holds real decision or validation logic (a recommender, a parser, a state machine), mark its pure functions with sentinel comments (for example `/* X:START */ ... /* X:END */`), and add a check script that reads the artifact, extracts the block between the sentinels, evaluates it in a `node:vm` context with light stubs, and asserts edge cases. Wire it into `npm test` and the deployability check so a regression in the logic blocks the loop and a merge, even when the browser leg is deferred to CI. The point: a single-file artifact must not have logic that no test can reach.

## Procedure (server: tiered tests)

1. **Unit tests with the built-in runner**, asserting with `node:assert`, run with coverage to lcov so static analysis can read it.
   ```
   node --test --experimental-test-coverage \
     --test-reporter=lcov --test-reporter-destination=coverage/lcov.info test/<files>
   ```
2. **In-process HTTP via the factory.** Build the app with injected fakes, `listen(0)` on an ephemeral port, drive it with `node:http` against temp-dir-isolated state.
3. **Parity tests** for any rule duplicated across runtimes: assert the server implementation and the client mirror agree (numeric rules to a fixed precision; shared constants deep-equal).
4. **Injected fixed time.** Functions depending on "now" take the date as a defaulted parameter so a test passes a fixed date.
5. **One browser smoke test** (Playwright) that boots the real seeded server, renders every view asserting zero page errors, and exercises a critical flow with expensive or external calls mocked.
6. **Security-property tests:** boundary rejection, anti-shrink merge, the concurrency rev guard, the prototype-pollution strip, and auth required/not-required.

## Craft: build, debug, and audit tests

The strategy above sets what to test and the coverage floor; this is the day-to-day craft of getting there. Language-agnostic; for the concrete test and coverage command, see `toolchain-adapters`.

**Build tests in as you code.**
- Test with the change, in the same commit; a change without its test is not done. Test first when the behaviour is well specified, immediately after when exploring, and keep the loop green throughout.
- Spend by the pyramid: many fast unit tests, fewer integration, very few end-to-end. Test logic where it is cheapest; do not push it into slow end-to-end tests.
- Shape every test arrange, act, assert: one action, one observable outcome, one reason to fail.
- Make tests deterministic by construction: inject time (pass "now" as a defaulted parameter), seed randomness, isolate state. No wall-clock or network dependence in the unit tier.
- Prefer fakes you control over mocking everything; mock only the expensive, external, or non-deterministic. Assert observable behaviour, not a private detail a harmless refactor would change.
- Mutation-proof a new guard before you submit it, do not let a reviewer prove it for you. A guard, validator, or invariant is unfinished until a mutation shows it can fail: in a COPY of the tree (never the working tree), delete or invert the code the guard protects, run only that suite, confirm it goes red, then discard the copy. Name the mutant and the suite it kills in the commit, so the proof is part of the record. A boundary guard needs a boundary mutant, not just an existence mutant: move the threshold by one in each direction (a floor of 4 that still passes at 1 is not tested), and the fixture must be able to exercise the bound it claims to test (a cap of 25 asserted with 9 rows asserts nothing). Measured: seven mutants over four guards took about four minutes against a copy; the same information arriving as a gate FAIL cost roughly forty plus the reviewer's whole re-read.
- Verification confirms a belief; it does not form one. Measure the value before you write it, do not assert it and let the gate check your reasoning. On one release the correlation was exact: every fix that computed the number first (a contrast ratio by WCAG arithmetic before a hex was chosen, a row count measured at each commit, a probe mutation-proved before submission) passed first time, and every fix that reasoned and left the gate to check it failed. A gate that keeps catching your figures is telling you the figures were guessed; produce each one from a command in the session, so the gate confirms what you already measured instead of discovering what you assumed.

**Debug a red test, hypothesis-first.**
- Reproduce it in isolation first; if it only fails in the full run, suspect shared state or order.
- Two strikes on a defect class means stop fixing and build a reproduction harness first. If the same class fails a check or a gate twice, each fix so far has been a partial model of the problem submitted as if complete; the third attempt must begin with a script that reproduces the original failure, not another guess. On one project a status-poller defect took four submissions, each re-opening the hole the last closed, and only passed once the reproduction came first. The harness is cheaper than the third round.
- Read the assertion (the symptom), form one hypothesis about the cause, test that one change, rerun. Do not change several things at once.
- Bisect the input or the diff (`git bisect`) to the smallest failing case.
- Decide whose fault it is: a real defect (fix the code) or a brittle test (fix the test). Never loosen an assertion to turn red green.
- Hunt flakiness deliberately (time, order, concurrency, network): loop it to confirm, then remove the cause. A flaky test is treated as failing.
- Never trust a check you did not run; paste the real failing output (the gate doctrine applies to your own debugging).

**When an interaction "does not work", debug the binding path first, not the handler body.**
- Reproduce before you fix. For any "X does not respond" report, the first artefact is a script (jsdom or Playwright) that drives the real build and reproduces X. Ship code only when that probe goes red then green. A theory you did not probe is a guess.
- Prove the handler was bound before you inspect what it does. "Did my code run" precedes "is my code correct": a one-line log in the binding loop, or an assertion that the listener is present, settles it in seconds.
- Ascend the ladder from the bottom; the bug is usually two rungs below where the eye lands. Rung 0: is the element in the deployed DOM (view the deployed artefact, not the source)? 1: is a handler bound? 2: did the binding function complete? 3: did it throw earlier (every raw `getElementById(id).addEventListener` is a null-deref if the element was removed, and one throw unwinds the rest of init and kills every later handler)? 4: is the click reaching the handler? 5: does the handler run but state not change? 6: is the DOM updated but not visible (`display`, `visibility`, `pointer-events`, `opacity`, `z-index` on the element and its ancestors)? Most interaction bugs live at rung 2 or 3.
- A try/catch is a shield after diagnosis, never a probe before it. A try/catch that never fires is a placebo; wrapping code to "catch the throw" tells you nothing. Use the stack trace or a targeted probe.
- A green suite while the app is unusable is a broken suite. Assert the primary journey, not just DOM shape: boot the app, click each tab and control, and assert the target view or state actually changed. The render-check already walks the four tabs (`scripts/render-check.mjs`); keep that habit, a suite that never simulates a click cannot catch a dead handler.

**Audit a suite so coverage is not mistaken for safety.**
- Treat coverage as a floor, not a target: a covered line can assert nothing.
- Check assertion strength: would the test pass against broken code? A test with no real assertion is theatre.
- Think in mutations: if you flipped a comparison, dropped a guard, or returned early, would a test go red? Where not, the line is covered but unprotected.
- Find and quarantine flakes; never let a known flake run un-quarantined.
- Catalogue the smells: over-mocking, brittle selectors, slow tests in the fast tier, hidden order dependence, wall-clock or network reliance.
- Cover the boundaries (empty, null, max, malformed) and the security properties (boundary rejection, injection-inertness of reflected input, auth required where it must be), not just the happy path.
- Test the visible STATE of every control, not only its data path. A control's visibility and enabled/disabled logic is code: assert the field shows when the server says it is needed, the button disables when it should. A whole feature once shipped dead behind a green 360-test suite because a control read `state.scanPinRequired`, a property nothing ever assigned (the real one was `state.status.scanPinRequired`), so the field was always hidden and no test exercised that path. Two cheap guards catch this class: an assertion on the control's rendered state, and, while authoring, a grep for where a property is ever assigned before trusting a read of it (a read of a never-assigned property is a silent typo the type-free path hides).
- A comment is a claim, and a claim is tested too. A superlative or a count in prose ("every committed file", "26 candidates", "always", "never") either gets a test that fails if it becomes untrue, or a recorded command that reproduces the number, or it is narrowed to what the code actually holds. In one project's last two releases, four separate findings were prose that outran the code, not code that was wrong; the worst was a guard commented "reads every committed text file" that walked a hand-kept list missing forty-two files, including the one whose whole purpose was to enumerate secret-bearing variables. Grade such a defect by what the control is FOR: a false comment on a convenience helper is cosmetic, but a false comment on the thing that verifies a hard rule is a fail-open. And when a control cannot see something, state the blind spot in the control itself, not only in a review reply.

## Decision rules

- **Which tier?** Pure logic to unit; route or middleware behaviour to in-process HTTP via the factory; whole-page render and a flow to the single Playwright smoke test. Do not push logic testing up into e2e.
- **Mock or real?** Mock only expensive, external, or non-deterministic calls; test everything else against the real code.
- **Coverage threshold?** At least 80% over the analysed source. Exclude only genuinely untestable integration glue, and only after unit-testing its pure helpers.
- **Flaky test?** Fix or quarantine immediately; a flaky test is treated as failing.
- **Using node's test runner?** Scope discovery with an explicit glob (`test/*.test.js`). A bare directory argument (`node --test test/`) misbehaves, and a non-test helper file basenamed with a test pattern (`test.mjs`, `x.test.js`) is auto-discovered and run, which can recurse. Never emit a bare `node --test` as the `test` script for a quality-gated template: the platform runs `npm test -- --coverage`, and node's runner rejects the unknown `--coverage` flag with "bad option", failing the test stage and skipping every later gate. The `test` script must own its flags, tolerate the platform's extra argument, scope discovery with an explicit glob, and emit `coverage/lcov.info` (`appstore-gate-compliance`, `deploy-recipes`).
- **A leg cannot run locally?** It is not a pass, and the partial loop is not the loop. The static render-check needs a browser the build sandbox often cannot launch, so it exits with a distinct non-zero code (not 0) and prints an unmissable "deferred to CI" banner: never a green line. Treat Continuous Integration as the binding source of truth for that leg, and never merge while its required check is red. Two specific traps to avoid: running the cheap subset (`test:fast`) and reading it as the whole loop; and merging a pull request before its checks report green. A leg that silently no-ops reads as covered when it was never run, which is how a real defect ships behind a loop that looked green.
- **A negative assertion ("X must not exist", "Y must never be set")?** It has an enforcement context, so classify it before you write it: is the rule about MY repository, or about EVERY checkout of this code? A rule that is correct locally can be guaranteed-false in the deploy environment, because the platform adds files and variables to your checkout (the App Store commits its own `.gitlab-ci.yml` and sets `GITLAB_CI`). Gate the platform-only cases on the runner's identity and record them as an explicit, honest pass; keep the everywhere cases failing loudly. Sweep for these systematically: grep the suite for negative file-existence checks. An assertion that is guaranteed-false on the machine that gates the deploy is a self-inflicted outage (`appstore-gate-compliance`).
- **A suite reads a generated artefact?** `npm test` (or the stack equivalent) must pass in a fresh clone with only an install step before it, because the App Store runs your tests against the uploaded zip with no build step of its own. The orchestrator owns generating any artefact a suite reads: one guard at the top that builds it when absent (`appstore-gate-compliance`).
- **Confusing a comprehensive suite with measured coverage?** They are independent claims. The App Store's SonarQube gate reads only the machine-readable coverage report your test stage emits (lcov at `coverage/lcov.info` for Node; per-stack paths in `deploy-recipes`), so a thorough suite that emits no report scores 0% and fails the gate. Run the suite under a coverage tool on the platform's exact flag, emit the report, and open the file to confirm the number is real; wire this and a Sonar-equivalent static-analysis pass into the loop at scaffold time, when the violation count is zero, not at upload time when it is hundreds (`appstore-gate-compliance`). Where a file cannot be measured honestly, exclude it from the coverage metric only, never from analysis, and record the rationale in `sonar-project.properties`.
- **Refactoring to clear a metric (extracting helpers to cut cognitive complexity)?** Prove the behaviour is unchanged independently of the suite you already had: a parity harness comparing old and new outputs on the same seeded inputs, plus a full-page render smoke (`code-architecture`).
- **Loop too slow or costly?** Optimise time and resource without losing depth. Order the loop cheapest-first and put the one heavy leg last: the near-instant checks (syntax validation, pure-logic unit tests, static greps) run before the headless browser render-check, so a cheap failure never pays for a browser launch. In Continuous Integration, cache the browser so it downloads once rather than every run, and gate the render-check behind a path filter so it runs only when the rendered artifact (for the static archetype, `index.html`) actually changed; the deterministic legs still run on every change. Fail safe: when the path filter cannot tell what changed, run the render-check. The coverage and the checks never shrink; only the wasted work does. Server stacks apply the same shape: cache dependencies and any browser, and run the slow suite or container scan only when its inputs change. Before assuming the tests themselves are slow, PROFILE the harness: the setup can dominate. One suite fell from 119s to 54s (2.2x) not by touching the tests but by sharing one in-memory database per process and truncating between tests, because it had been booting a fresh WASM database per test (about 1.9s each, seventy times) while the migrations it blamed cost about 0.1s. Share an expensive fixture across the tests that can safely reuse it, reset state between them, and measure where the wall-clock actually goes before optimising it. Measure the FIXED per-file cost and the MARGINAL per-test cost first (fit a line over a few suites of different sizes): a suite that boots a database, a browser, or a WASM runtime is almost all fixed cost, invisible to per-test profiling. Then split the suite by cost CLASS, not by feature area, and name the cheap tier so it is reachable in one command (a pure-logic tier that runs in a fraction of a second, separate from the expensive-fixture tier), so an edit to a pure module is verified in a blink instead of paying the whole setup. If you add an affected-suites resolver (run only the suites importing the changed files), make it PRINT its estimate and fall back to the full parallel run when the affected set exceeds about a third of the files: scoping wins hugely for a leaf module but can be a net LOSS for a hub module many suites import, because a scoped run serialises what the full run parallelises, and an optimisation silently slower than the thing it replaces is worse than none.

## Standards (checkable assertions)

- Static: `npm test` runs validate, render-check (desktop and mobile), and static-checks, and exits 0 with all green.
- Static: any non-trivial inline logic is reachable by a test, sentinel-extracted and unit-tested out of band, and wired into the loop and the deployability gate.
- A leg that cannot run in the local sandbox exits non-zero with a clear "deferred to CI" banner, never a green pass; CI is the binding source of truth for it and a red required check blocks the merge.
- Every negative assertion is classified per environment; none is guaranteed-false on the platform runner that gates the deploy, and `npm test` passes in a fresh clone with only an install step first (`appstore-gate-compliance`).
- For a quality-gated deploy, the suite emits a non-empty machine-readable coverage report at the path the gate reads, a `sonar-project.properties` scopes it, and a per-commit static-analysis pass keeps violations at zero (`appstore-gate-compliance`).
- Server: `npm test` runs the built-in runner with coverage to `coverage/lcov.info` and exits 0; coverage is at least 80%.
- Server: HTTP behaviour is tested in-process via the factory on an ephemeral port with isolated state.
- Server: every cross-runtime rule has a parity test; time-dependent logic uses an injected fixed date.
- Server: security properties each have a test; a browser smoke test renders every view with zero page errors.

## Failure modes and remedies

- **Render-check cannot find the browser driver (static).** Fix: install it and set `BROWSER_DRIVER_PATH` (`environment-setup`).
- **Coverage report missing or at the wrong path (server).** Fix: ensure the pretest step creates `coverage/` and the lcov reporter writes `coverage/lcov.info`.
- **An e2e test fails only in CI.** Fix: the browser is not installed locally; install it or treat CI as the source of truth for the smoke test.
- **A parity test fails after a change.** Fix: re-derive the client copy from the canonical server copy.
- **A test flakes on the wall clock.** Fix: inject a fixed date parameter.

## Verification

Static: `npm test` prints `JS syntax: OK`, two `PASS` lines, and `STATIC CHECKS: PASS`. Server: `npm test` passes with `coverage/lcov.info` produced and coverage at least 80%; `npm run test:e2e` passes with zero page errors.

## Worked example

Server: a new score dimension is added. A unit test asserts the server formula; a parity test asserts the client mirror matches to one decimal; an in-process HTTP test confirms the scored records endpoint; the Playwright smoke test confirms the score view renders with no page errors (the expensive endpoint mocked). `npm test` is all green with coverage written.

## Glossary

- **Verification loop (static):** validate, render-check, static-checks run by `npm test`.
- **In-process test:** building the app via the factory on an ephemeral port without a separate server.
- **Parity test:** asserting two implementations of one rule agree.
- **Smoke test:** the single browser test rendering every view with external calls mocked.
- **Coverage / lcov:** proportion of source exercised, reported as `lcov.info`.
- **Test pyramid:** many unit, fewer integration, very few end-to-end tests.
- **Arrange, act, assert:** the three-part shape of a clear test.
- **Mutation testing:** breaking the code on purpose to check a test catches it.
- **Flaky test:** one that passes and fails without a code change; treated as failing.
- **Test smell:** a pattern (over-mocking, brittleness, slowness, non-determinism) that predicts future failure.
- Other terms: `glossary`.

## Provenance

Merged from the static bundle's validate, render-check, and static-checks scripts and spec, and the server bundle's testing-standards skill (node:test with lcov, in-process HTTP via the factory, parity tests, injected time, security-property tests, Playwright smoke test).

## Field lesson: a strict render-check earns its keep

The static render-check should fail on any browser console error, seed the stored state it needs, and dismiss the splash before it probes the page. Failing on console errors caught a real defect, an ignored CSP directive, that no static grep would have found. Run the render-check as the binding leg in continuous integration: locally the headless browser download is often blocked by egress policy, so the check should exit "deferred, not a pass" rather than green, and the pipeline stays the source of truth.
