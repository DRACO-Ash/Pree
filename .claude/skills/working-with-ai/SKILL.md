---
name: working-with-ai
description: How to get reliable work out of Claude across the lifecycle, from planning to review to debugging. Use when briefing Claude on a task, reviewing its output, debugging with it, or deciding how much to delegate. Codifies the prompt patterns (plan before code, tests first, adversarial review, hypothesis-driven debugging, behaviour-preserving refactor) and how they map to the gates and the flight-plan interview. Language- and archetype-agnostic.
---

# Working with Claude

## Purpose and scope

The standard is built to be executed with an AI engineering assistant, so working with one well is itself a skill. This skill is the durable, high-leverage way to brief, review, debug, and refactor with Claude, so its output meets the same bar as hand-written work and clears the gates. Scope is the collaboration pattern. The brief it produces is shaped in `flight-plan`; the bar its output must clear is in the review agents; the copy-ready prompt library lives in the Launchpad app's Useful tools section.

## When to use

- Briefing Claude on a feature, fix, or review.
- Judging or correcting what it produced.
- Deciding what to delegate and what to keep in your own hands.

## Principles (how to get reliable work)

- **Plan before code.** Have Claude read the real code and write a plan first; approve the plan before it edits. Separating research and planning from execution stops it solving the wrong problem. For a new project, run the `flight-plan` interview to produce the brief.
- **Give a checkable definition of done.** State the acceptance criteria as things that must be true, and an end-to-end check it can run. A verification loop it can run and read closes the self-correction loop.
- **Tests first, and do not let it weaken them.** Ask for failing tests that capture the behaviour, then the minimum code to pass. Instruct it never to edit a test to make it pass; if a test is wrong it must stop and say so (`testing-standards`).
- **Anchor to existing patterns.** Tell it to read an existing example and follow that style and stack; build from libraries already present; no new dependency without a recorded reason (`dependencies`).
- **Review in a fresh context, adversarially.** Have a clean session (or the `engineering-reviewer`/`security-reviewer` agents) review the diff as if it had never seen it, hunting logic errors, edge cases, and security holes, citing file and line, and ignoring style. A reviewer with no stake in the code is less biased than the author.
- **Red-team your own analysis, and read the architecture before optimising it.** An analysis is as fallible as code. Before acting on a plan (a cost study, a design, a root-cause), have a fresh pass try to refute it against the actual source, and budget that adversarial pass INTO the first analysis, not a round trip later. One cost study ranked a caching flag as the top lever; an adversarial re-read of how the orchestrator actually decomposed the work found the dominant waste was the work matrix re-fetching each region once per angle, a far larger saving the first pass had missed. Optimise what the architecture does, not what the first read assumed it does.
- **Debug by hypothesis, not by guess.** Reproduce reliably first, explain the cause in plain language, then change the smallest thing. Forbid retry-or-sleep as a fix for flakiness (`testing-standards`).
- **"No change" means the fix is presumed wrong; probe, do not re-guess.** When the user reports the same symptom after a fix, stop shipping theories. Write a probe that proves whether the previous change even took effect, and show its result, before writing more code. Believe the reported symptom and doubt your theory: if they describe the same behaviour five ways, that layer is real, debug it, not the one you find convenient.
- **Do not blame the cache without integrity evidence.** "Hard-reload" or "try incognito" is deflection unless you can point to a concrete mismatch (a SHA-256 or version-stamp difference between what you built and what is deployed). The artifact carries both a version stamp and a packaged SHA-256; use them to prove deployed-equals-source before suggesting the user's environment is at fault. Otherwise the user is right and the code is wrong.
- **Say "shipped for you to verify", not "done".** A green local loop is necessary, not sufficient. Claim success only after the change is confirmed working in the environment that matters; premature victory spends trust you do not get back.
- **Refactor behaviour-preserving.** Lock current behaviour in characterisation tests first, then refactor in small steps, running tests after each.
- **Close a gate finding at the cheapest terminator, and reverting is often it.** A reviewer asking "why is this narrower now?" is answered completely by "it is not any more"; a revert closes a finding in one move where an argument only adds new surface to read. Two more terminators: a test-only or defence-in-depth guard (one already covered by a hook and a review) cannot block a release, so improve it BETWEEN releases, never iterate it to completeness during one; and a heuristic pattern has a precision cost curve that turns upward, a point past which each refinement adds more reviewable surface than it removes risk, so name that point in the file in advance and treat crossing it as the defect. One project spent three gate rounds making a grep-guard's regex ever more precise, each round's new precision becoming the next round's finding, until a revert to the earlier pattern ended it in one commit.
- **Keep it minimal.** Ask for the smallest change that satisfies the task; no speculative abstraction, no gold-plating, validation only at real boundaries.
- **Never accept a fabricated fact.** A name, date, figure, or citation it cannot verify is marked as an explicit unknown, never asserted. Trust, then verify against the source.

## Decision rules

- **Underspecified task?** Have Claude interview you first (the `flight-plan` questions) and write a spec before any code.
- **Large or risky change?** Plan-and-approve, then implement in reviewable steps, then gate. Do not let one prompt make a sweeping unreviewed change.
- **Output looks plausible but unverified?** Treat it as a hypothesis: run the loop, read the lines, run the gates. Plausible is not verified.
- **Security-relevant change?** It is not done until `security-reviewer` returns PASS; do not take Claude's own assurance as the gate.
- **Delegate or keep?** Delegate well-specified, checkable work; keep the irreversible decisions (what to build, what to deploy, what to publish) and confirm them yourself.
- **Spending too much time and cost on the gates?** Keep the depth, cut the waste. Run the deterministic loop first (syntax, unit tests, static checks, deployability); it is near-free and catches most regressions, so only escalate to the reviewer agents when it is green. Then route the reviewers by what changed rather than running all three every time: the engineering reviewer on any logic change, the security reviewer only when the change touches a security surface (input, auth, secrets, escaping, egress, CSP, a new parser), the design critic only when interface, CSS, or copy changed. A version-stamp or prose-only change needs none. Scope each reviewer to the diff and the code around it, not the whole artifact, and name the specific claims to verify, but let a reviewer follow a defect CLASS wherever it lives: catching the class in files the fix did not touch is the gate's whole value, so never narrow a review gate so it cannot (`resource-discipline`, `appstore-gate-compliance`). Run independent reviewers in parallel. The full adversarial pass is reserved for where the risk is; a mechanical change rides the cheap loop.
- **Spending too much time and cost overall (not just the gates)?** Think of every check as sitting on a cost ladder, from a near-free grep, through the deterministic loop and the browser leg, up to a binding gate subagent, a workflow, and at the top a full App Store upload cycle. Push every check to the cheapest rung that can catch it, and reserve the expensive rungs for confirmation, not discovery: never spend a gate to find what a grep would show, nor an upload to discover a rule you could have written to from the start. The full ladder, the per-tool discipline, token hygiene, and a pre-invocation checklist are in `resource-discipline`.
- **Need a human decision and the popup is unavailable?** Do not stall and do not silently pick an irreversible option. State the decision plainly, give a recommendation with a one-line rationale, take the reversible default, and mark it clearly so the human can redirect. A reversible default announced is recoverable; a silent guess is not.
- **Orchestrating sub-agent gates?** Read each verdict from the FINAL assistant message only, anchored to line start. A substring grep over the whole transcript matches the instruction in your own prompt (which literally contains "VERDICT: PASS or VERDICT: FAIL") and reports a false result (`resource-discipline`).

## Standards (checkable assertions)

- A non-trivial task starts from an approved plan or brief, not a cold edit.
- Every change Claude makes ships with tests and keeps the verification loop green.
- Claude's output is reviewed against the plan and by the binding gates before it is called done.
- No test was weakened to pass; no fact was fabricated; no irreversible action was taken without explicit human confirmation.

## Failure modes and remedies

- **It built the wrong thing.** Cause: no plan or brief. Fix: interview and plan first, approve, then build.
- **Green locally, broken in review.** Cause: weak or self-edited tests. Fix: tests first, forbid editing tests to pass, audit assertion strength (`testing-standards`).
- **A plausible but wrong explanation or citation.** Fix: require a reproduction and a source; mark unknowns explicitly.
- **A sweeping diff that is hard to review.** Fix: re-scope into small, reviewable steps, each gated.

## Verification

The collaboration is working when, for each change: there was a plan or brief, the change shipped with tests, the loop is green, a fresh-context review and the binding gates passed, and no test was weakened, no fact fabricated, and no irreversible step taken without a human yes. The Launchpad app's Useful tools section holds copy-ready prompts for each pattern above.

## Worked example

A vague request ("make login more secure") becomes: Claude interviews to pin scope (what threat, what users, what is in scope), writes a short spec with acceptance criteria, writes failing tests for the new behaviour, implements minimally, runs the loop green, then a fresh-context `security-reviewer` attacks each control and returns PASS. No assurance was taken on trust; every claim was checked against a line.

## Glossary

- **Plan before code:** read and plan first, approve, then edit.
- **Fresh-context review:** a reviewer that sees only the diff, not the conversation that produced it.
- **Characterisation test:** a test that locks current behaviour before a refactor.
- **Definition of done:** checkable acceptance criteria plus a runnable end-to-end check.
- Other terms: `glossary`.

## Provenance

Authored from the Launchpad prompt library (planning, codebase understanding, writing to a standard, review, debugging, testing, refactoring, documentation, git, architecture), the `flight-plan` interview, the `testing-standards` debugging and audit method, and the binding-gate model, distilled into durable collaboration principles that hold regardless of language or archetype.

## Field lesson: a fix is not done until re-reviewed

Remediation introduces regressions. During the Launchpad audit, the change that promoted a control to primary reintroduced a contrast failure that only a second review caught. Treat review as a loop: score, fix, then re-score the changed surface, because the fix is itself a change. The same applies to a green pipeline after a fix you pushed to make it green: confirm the specific leg that was red is now the leg that is green.

## Field lesson: act on feedback literally and verify visually (edition 1.12)

Repeated rounds of "you are not listening" almost always trace to one of these, not to the instruction being unclear. Earned the hard way:
- Read feedback literally and do exactly what was asked before improving on it. If an instruction is given more than once, it was not done or not delivered; treat the repeat as a process failure, not a request for clarification.
- Verify against the user's standard, not the build's. "It renders" is not "it is good". For visual work, screenshot the result (light, dark, mobile) and compare it to the request before claiming done.
- Match the delivery cadence to the feedback loop. Shipping many small gated increments while the reviewer reacts to the last merged build makes correct work look ignored. For a large punch-list, do one consolidated, verified push and deliver that.
- When you remove or restructure something the user relies on, say so and fill the gap; a silent regression such as newly wasted space reads as not listening.

## Field lesson: repeated dissatisfaction means change altitude (edition 1.18)

When the owner gives the same dissatisfaction after several rounds ("still boring", "still not listening"), the increments are not too small, they are aimed at the wrong level. More polish on the same approach produces more of the same rejection. Earned on the Launchpad redesign:
- Stop iterating and re-question the premise. Ask what would have to be true for the complaint to disappear, and check the work against the original brief rather than the last diff. The fault was treating reorganisation and polish as redesign when the owner wanted an owned visual identity.
- Break the stale-artifact loop first. Before defending the work, confirm the owner is looking at the current build: rebuild, screenshot the real rendered state, and show it. Much "you ignored me" is the owner reacting to an older build than the one you changed.
- Then make one decisive, visible move and prove it with a before-and-after screenshot, rather than another round of small adjustments the owner cannot distinguish from the last.
