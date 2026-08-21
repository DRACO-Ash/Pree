---
name: code-architecture
description: Stack, structure, and editing conventions for both archetypes. Read before editing. Covers the static no-build single-file model and the server createApp factory and container-is-the-build model, the data-to-render and request pipelines, error handling, performance, and the surgical-edit rule. Use whenever deciding where new code or data belongs, or before any edit.
---

# Code architecture

> Stack note: the commands shown here are the Node example. For Python, Java, Go, Rust, or .NET, run the equivalent canonical step from `toolchain-adapters`. The principle in this skill is what binds; the command is illustrative.

## Purpose and scope

The architectural conventions for both archetypes: the static no-build single-file model, and the server `createApp(deps)` factory with container-is-the-build. Folder layout, the data-to-render pipeline (static) and the request pipeline (server), error-handling and performance conventions, and the editing discipline. It is the overview; depth lives in the data, frontend, state, testing, and security skills. It does not cover deployment (`release-and-deploy`) or design tokens (`design-system`).

## When to use

- Before any edit.
- When deciding where new code or data belongs.

## Prerequisites

- Environment set up (`environment-setup`). Read `glossary` for the core terms.

## Procedure (static archetype: how to make a change)

1. **Locate the section.** The artifact is organised by comment banners inside one file: data literals, then build functions, then renderers, then interaction wiring. Find the banner (`grep -n "====" ${SOURCE_PATH}`).
2. **Edit surgically.** Change the smallest region that satisfies the request, anchored on a unique surrounding string. Do not reformat or reconstruct anything you were not asked to touch. Duplicated CSS/markup variants are intentional in a no-build file; do not dedupe them.
3. **Keep it inline and offline.** No new file, no `src=`/`href=` to a remote, no network API. Embed any asset as a `data:` URI.
4. **Run the verification loop** (`testing-standards`), then bump the version stamp and add an audit row (`observability-and-audit`).
5. **Removing a feature or element? Sweep the residue in the same commit, not a deferred tidy.** After deleting any DOM id, class, function, or config key, grep the whole tree (`grep -rn 'removedId\|removedFn' src/`) and delete or guard every remaining reference; a reference to a removed element is a null-deref waiting to throw. This includes the now-orphaned CSS: a style rule, keyframe, or token that only targeted the element you removed is dead and comes out in the same change. Do not leave it for "a later tidy" the pace never reaches; carried dead CSS and dormant JS are re-noted release after release and read by every gate in between, which one project paid repeatedly (its own field lesson). This is distinct from the intentional duplicate CSS variants of a live element in step 2, which stay: the test is whether the element the rule targets still exists, not whether the rule looks redundant. In particular, never chain `addEventListener` (or any method) onto a raw `getElementById(...)`; bind through the null-safe `on()` helper so a missing element is skipped, not a synchronous throw that unwinds the rest of init and kills every later handler. Every bug is a class of bugs: after fixing a data-shape, field-name, null-shape, or unit mismatch, grep the repo for the same shape and fix all instances at once, not one file at a time across separate commits.

## Procedure (server archetype: how it is structured)

1. **The HTTP app is built by a factory.** `createApp(deps)` wires routes, middleware, and injected dependencies and returns the app without listening; `src/index.js` reads the port from the environment and listens. This makes the app testable in-process with fakes (`testing-standards`).
   ```js
   // src/index.js
   const app = createApp(realDeps);
   const port = Number(process.env.PORT) || 8080;
   app.listen(port, "0.0.0.0", () => console.log(`listening on 0.0.0.0:${port}`));
   ```
2. **Config comes only from the environment** with validated fallbacks (`security-hardening`). Never hard-code a port, key, or origin.
3. **The request pipeline is:** rate limit, then auth on cost-incurring/state-changing routes, then boundary validation of the body, then the handler, then a generic error response with detail logged server-side. Health paths are public and touch nothing.
4. **Long work is a single-flight background job,** not a long request (`api-and-integration`).
5. **The container is the whole build** (`release-and-deploy`): no separate bundler output.

## Decision rules

- **New data?** Static: add a literal near the others; normalise in the build pass (`data-layer`); do not fetch it. Server: write through the atomic store / a DB add-on with boundary validation (`data-layer`).
- **New visual element?** If it renders per frame, through the renderer with culling/LOD (`frontend-and-rendering`); static chrome is plain inline markup/CSS.
- **Need persistence?** Static: the storage wrapper, innocuous UI state only (`state-management`). Server: the atomic store or the synced document (`state-management`).
- **Tempted to add a build step or framework to a static artifact?** Do not; the no-build single-file property is a hard rule. Re-scope the task.
- **Tempted to do long work in a request (server)?** Do not; make it a single-flight background job.
- **A "tidy" reformat balloons the diff?** Revert and redo as a targeted string replacement.
- **A function trips a cognitive-complexity limit (the App Store's SonarQube gate caps it at 15)?** Reduce it by extracting cohesive steps into named helpers with no behaviour change, not by rewriting the logic. In a static single-file bundle every helper shares one global scope, so give each a unique per-module prefix to avoid collisions, and if a test harness extracts the function into a sandbox (a `vm`), keep its helpers nested inside it or the sandbox loses them. Prove the extraction preserved behaviour with a parity harness and a render smoke, not just the existing suite (`testing-standards`, `appstore-gate-compliance`).
- **A mechanical swap to satisfy a linter or analyser?** Many are not behaviour-identical on the full input domain: `x && x.y` to `x?.y` changes the produced value from the falsy left operand to `undefined`; `Number.isNaN` does not coerce where the old check did; a `| 0` in a PRNG is a 32-bit wrap, not `Math.trunc`; `localeCompare` is not the code-unit sort order; and `String.replace` with a string replacement expands `$` sequences in the inserted text. Convert only where the surrounding contract makes it safe, and prove the truth table over hostile inputs before trusting the swap (`appstore-gate-compliance`).

## Standards (checkable assertions)

- Static: one file; no runtime dependency added (`grep -c "src=\"http" ${SOURCE_PATH}` is 0); no dynamic code (static-checks pass).
- Server: the app is built via `createApp(deps)` and tested in-process; config is read from the environment, never hard-coded.
- Every change keeps the verification loop green, adds one audit row, and is diff-minimal (touches only the region required).

## Failure modes and remedies

- **An edit breaks the inline script (static) or a changed file (server).** Detect: `validate` fails / format-gate hook fires. Fix: read the error line, correct the syntax.
- **A new feature reaches the network from a static artifact.** Detect: static-checks fail on `fetch`/`import(`. Fix: embed the data or remove the feature.
- **A server handler does long synchronous work.** Detect: a request that blocks for seconds. Fix: move it to a single-flight background job (`api-and-integration`).

## Verification

The loop is green, both review gates PASS, the diff is minimal and touches only the intended region, and one audit row was added.

## Worked example

Static: to add a data field to an entity, `grep -n "const ENTITIES" ${SOURCE_PATH}`, add the field to the literal, extend the renderer anchored on a unique nearby string, run the loop, add an audit row. The diff is a handful of lines. Server: to add an endpoint, add a route in `createApp`, place it behind auth and the rate limiter if it costs money, validate its body at the boundary, add an in-process HTTP test, and an audit line for the privileged action.

## Glossary

- **Build pass (static):** the one-time `buildX()` turning data literals into the render model.
- **App factory (server):** `createApp(deps)` returning the app without listening.
- **Banner:** a comment line marking a section of the single file.
- Other terms: `glossary`.

## Provenance

Merged from the static artifact's inline-script structure (data literals, build functions, renderers, wiring) and the server's `createApp(deps)` factory, listener, request pipeline, and container-is-the-build model, plus both engineering personas' surgical-edit and no-build rules.

## Field lesson: author the source, guard the drift

For the static archetype the shipped `index.html` is generated, never hand-edited: author the modular source and rebuild. Put a drift guard in the loop, a `build --check` that rebuilds and compares byte for byte, so a stale artifact fails the loop instead of shipping. This caught edited-output mistakes repeatedly and kept the build reproducible, which the deploy gate then trusts.
