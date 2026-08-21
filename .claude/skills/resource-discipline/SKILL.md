---
name: resource-discipline
description: How to spend the least time and tokens for the best result in this baseline. Use when deciding whether to run a gate, spawn a subagent, launch a workflow, or attempt an App Store upload, and whenever a task feels like it is costing more cycles than it should. Covers the cost ladder from a grep to an upload, the rule to catch every check at the cheapest rung that can catch it, the operating cadence, token hygiene, right-sizing effort, and a pre-invocation checklist. Pairs with working-with-ai.
---

# Resource discipline

> The governing principle: every check can be run at one of several rungs that differ in cost by more than two orders of magnitude. Push every check to the cheapest rung that can catch it, and reserve the expensive rungs for confirmation, not discovery. Nearly all wasted spend comes from catching at a high rung what a low rung could have caught.

## Purpose and scope

The standard is executed with an AI assistant that spends time and tokens, and the App Store gate spends whole upload cycles, so spending them well is itself a skill. This is the durable way to get the best result without over-committing resources. Scope is the economics of the tooling. The collaboration pattern is `working-with-ai`; the loop legs are `testing-standards` and `ci-cd`; the upload contract is `appstore-gate-compliance`.

## When to use

● Deciding whether to run a gate, spawn a subagent, launch a workflow, or attempt an upload.
● A task is taking more gate re-runs or upload cycles than it should.
● Scoping a change so it clears every rung in one pass.

## The cost ladder (the governing model)

Cheapest to most expensive, for this baseline:

| Rung | Tool | Rough cost | Catches |
|---|---|---|---|
| 1 | `Grep` / `Glob` / region `Read` | near-free, instant | known facts, cross-file consistency, "does X exist" |
| 2 | `build.mjs --check` | seconds | artifact drift from source |
| 3 | `npm run test:fast` | seconds | syntax, analyser and zip assertions, static security greps |
| 4 | full `npm test` (browser leg) | seconds plus a browser | render regressions on desktop and mobile |
| 5 | `Explore` / a general-purpose subagent | thousands of tokens | fan-out reads, "where does this live" |
| 6 | one binding gate subagent | tens of thousands of tokens | a correctness, security, or deploy verdict |
| 7 | a parallel gate pair or a workflow | a multiple of rung 6 | independent verification at scale |
| 8 | an App Store upload cycle | a full human-in-the-loop deploy attempt | only the server-side SonarQube ruleset and image scan |

The rule that falls out of it: never spend a rung-6 verifier to find what a rung-1 grep would find, and never spend a rung-8 upload to discover a rule you could have written to from the start.

## Enforce by cost tier (quality and efficiency are one lever)

Building to standard from the start and spending the least time are not opposites; they are solved by the same move, assigning each check to the cheapest rung that enforces it. A deterministic, universal rule (a secret, an em-dash, a baked `ENV PORT`, a stale lockfile) belongs in a PRE-WRITE HOOK: it fires in milliseconds, blocks at the keystroke before the artifact exists, and so can never cause a round-trip or reach a gate or your attention. This is why "silly" trivia costs nothing when placed right: the answer to not wanting to spend time on em-dashes is not to drop the rule, it is to enforce it invisibly at rung 0. Mechanical faults (drift, contrast, coverage, a lint class) belong in the deterministic loop, seconds on every edit. Only judgment, is this logic correct, is it secure, does it read right, belongs in a binding gate, run once per change on a self-verified green tree so it CONFIRMS rather than discovers.

Seen this way, the doom loop of endless remediation and the fear of over-checking trivia are the SAME defect: a check running at too expensive a rung. An em-dash caught by a gate is slow and infuriating; the same em-dash caught by a hook is free and invisible. A correctness bug discovered by a gate after building is a doom loop; the same bug caught by writing the check first is a pass. So the efficient move is never to add checks, it is to RELOCATE each to its cheapest rung, and to build only the checks a project's real risks warrant (a guard for a defect class this project does not have is the process-bloat `learn-from-feedback` warns against). Push trivia down to hooks, mechanical faults to the loop, and reserve the gate's expensive attention for what only judgment can settle; then the standard is met from the first commit and the round-trip count, the dominant cost, stays at one.

## Compute the route per ask (adaptive, one fixed objective)

There is no single fixed process to run every time; there is one fixed OBJECTIVE, fully cleared and releasable, as fast as possible, with the standards held, and a route to it computed fresh for each ask. So at every point where you plan or execute a request, spend the first moment on the route, not the habit: name the aim, scope to the smallest change that achieves it, then work out which rungs and gates that change actually needs and in what order, and take that path. A pure version stamp or comment fix is a trivial amend, verified by the loop and not re-gated (the operating cadence below); a logic change needs the loop and the engineering gate; a change to a security surface adds the security gate; a visual change adds the design critic; a deploy adds the deploy-gate and a human yes. Running more than the ask triggers is waste; running less is a defect. Then sequence the chosen route to one clean pass, self-verify to green, freeze the tree, gate once, so the cheapest complete route is also the fastest. The route changes with every ask; the objective does not. This is the same principle as the cost ladder and the cost tiers above, applied one level up, to the whole shape of a task rather than to a single check: optimise the path to cleared-and-releasable, per ask, every time.

## Per-tool discipline

● **The verification loop (rungs 2 to 4).** Run `test:fast` after every source edit; it is deterministic and cheap and catches most regressions. Run the full loop with the browser leg only at commit boundaries, and treat continuous integration as the binding source of truth for that leg when the local browser is awkward. Batch edits, then verify once, rather than running the full loop between micro-edits.
● **The binding gates (rung 6) are verifiers, not iterators.** They are the most expensive thing invoked routinely. Front-load internal consistency with a rung-1 sweep and a self-review before calling one, because a FAIL that needs a re-review costs roughly twice. Batch the change so a gate runs once, not per half-change. Run independent gates concurrently (engineering and security in one message), and re-run only the gate whose inputs actually changed.
● **Subagents and Explore (rung 5).** Use `Explore` for genuine fan-out, because it returns the conclusion not the file dumps and keeps the caller's context small. Read a region directly (rung 1) when the file and line are already known. Do not spawn a subagent for a single known fact. Send independent subagents in one message so they run in parallel. Read a subagent's verdict from its FINAL message only, anchored to line start: a substring grep over the whole transcript matches the instruction in your own prompt (which literally contains "VERDICT: PASS or VERDICT: FAIL") and reports a false result.
● **Workflows (rung 7) are for scale and adversarial confidence, not for bounded tasks.** Orchestrating many agents for a task one context can hold is the exact over-commitment to avoid; reserve them for a repo-wide migration or an exhaustive audit with adversarial verification.
● **Skills (rung 1 to load).** Invoking the skill that owns a concern is cheap and deterministic; rediscovering its content by reading source is not. Load the owner rather than re-deriving the standard. This is a token saver.
● **App Store uploads (rung 8) are the most expensive resource there is,** because each failed upload is a full human-in-the-loop cycle and failures come in waves. This is why writing to the gate from the first line and running a local checker are the highest-return investments in the system: every rule moved from "discovered at upload" to "written correctly" or "caught locally" converts a rung-8 cycle into a rung-3 second (`appstore-gate-compliance`).

## Token hygiene (always on)

● Read the region you need, not the whole file; prefer `Grep` and `Glob` over shell reads.
● Do not re-read a file you just edited to verify it; the edit already confirmed the write.
● Do not re-derive facts already established, or re-run a search another agent ran.
● Keep diffs minimal (the surgical-edit rule): fewer changed lines is less for a gate to read and fewer new-code lines for the SonarQube gate to flag, so efficiency and compliance point the same way.
● Use the scratchpad for temporary files so the tree stays clean and no extra check is triggered.
● Keep running commentary minimal: report the decision, the blocker, and what shipped, not a step-by-step narration of every edit and check. Prose the reader must read is itself a token cost; a change is documented by its diff and audit row, not by a paragraph describing it.

## Right-sizing effort and model

Match the reasoning tier to the task: a rename, a version bump, or a grep sweep does not need high reasoning effort or the top model; the hardest verify or adjudicate step does. When delegating, let a subagent inherit the session model unless a cheaper tier clearly fits. Do not pay for maximum reasoning on work a lower rung could finish.

## Operating cadence (how a change moves from edit to shipped)

The cheapest cycle is the one not repeated. Move a change through these steps, not a gate per micro-edit:

● **Batch to a whole, self-consistent change, then gate once.** Group related edits into one release rather than gating each half-change; the surgical-edit rule keeps the batched diff readable. Fewer releases means proportionally fewer rung-6 cycles, which are the dominant spend.
● **Sweep before you gate (rung 1).** Before invoking a gate, grep the cross-file invariants a gate should never be spent discovering: counts, ids, version and edition stamps, references to a renamed thing. A gate that FAILs on a grep-findable fact cost a whole extra cycle.
● **Route the gates by what changed.** The binding engineering gate runs on every shippable change, on its final state. The security gate runs only when the security surface changed. The advisory design-critic is for net-new visual or interaction design, or a real contrast, theme, or accessibility risk; it is not run for reusing an already-approved pattern or removing an element, where a single self-checked screenshot settles it.
● **Do not gate what is not yet reachable at runtime.** A binding gate is for code a user or the platform can actually reach. Scaffolding, an unwired module, or infrastructure with no runtime path yet is verified by the deterministic loop and a read, not by a rung-6 review; gate it when it goes live. Gating unreachable infra spends the most expensive rung on code nothing runs.
● **Self-verify a trivial amend; do not re-spawn the gate.** After a gate PASS, a trivial follow-up (a count or comment fix, dead-code deletion, or a change the reviewer itself named safe) is confirmed by the deterministic loop, not another rung-6 review. Re-run a gate only when the amend changes behaviour the gate reasoned about.
● **Budget the expensive confirmations.** Screenshot only a visual change that genuinely needs an eye (a new layout, a contrast or theme risk), one or two shots, not a full matrix; trust the render-check for interaction. Keep status to a line unless a gate FAILs, a decision is needed, or more is asked.

● **Pin the subjective target before building it.** Cycles-to-green is about verification; the same waste exists in requirements. A feel, mood, or look target left as a bare adjective ("make it flow", "less corporate") is discovered one release at a time, and each pass carries a full loop, gate, merge, and package. Fix the target up front with a concrete reference and an acceptance line (`flight-plan`), then build to it once and batch the passes behind it. Across one project roughly a dozen presentation-only releases converged on an identity a pinned reference would have reached in a few.
● **Integrate continuously; do not diverge a branch.** A long-lived branch that drifts from the line others are committing to buys a reconciliation release with no feature in it. Merge little and often, or keep the branch a strict superset, so the merge back is clean.
● **Remove what a change orphans, in the same change.** Deleting an element leaves its CSS and JavaScript dead; clean them in the same edit rather than deferring a tidy the pace never reaches. Deferred dead code is re-noted release after release and read by every gate in between.
● **The environment is part of the build; do not contend with a live gate, and diagnose "stuck" with evidence.** A binding gate subagent is itself a heavy consumer, so a running gate makes the box busy: on a small core count, launching the full test suite while a gate runs starves both and both crawl. A detached background agent also does not survive an ephemeral container being reclaimed during an idle wait, so it silently dies and a re-run tells you nothing new; keep wait windows short or schedule a check-in rather than ending a turn into a long idle. And when work seems "stuck", spend a minute on evidence before hypothesising: uptime (a reclaimed container), the process list (a gate eating the cores), and the output file's mtime (progressing slowly versus hung) separate the three causes fast. Guessing the cause of stuck is itself the waste.

The target is cycles-to-green = 1 at each rung: a change that was right before it was checked.

## Verification round-trips: the dominant cost, measured

When verification feels expensive, the bill is almost always the NUMBER of gate round-trips, not the runtime of any single check. Measured on one release, 74% of commits were gate remediation rather than feature work, and each gate round paid for the verification loop roughly three times: yours before submitting, plus one inside each reviewer agent that re-runs the loop to verify its own findings. Narrowing what a round inspects saves minutes; making each round the LAST round saves days. So:

● **Meter it.** Count feature commits against remediation commits per release; above 50% remediation, the defect is in the fix method (fix by class, `appstore-gate-compliance`), not the gate. Count the full-loop runs per gate round; three is typical and usually two too many. Attribute the waste to the right axis, round-trip count, per-run runtime, or re-run frequency, and fix the largest, which is almost always round-trip count.
● **Freeze the tree at gate submission.** Run the full loop on the exact tree you submit, and do not commit again until the gate returns; a change made while a reviewer is measuring invalidates its verdict (a reviewer cannot certify a state that moved while it was measuring it). Freezing is not etiquette, it is what makes a verdict mean anything.
● **Run the loop once, share the result.** On the frozen tree, run the loop once and hand the reviewers the recorded result (exit status, counts, coverage, the lcov path) rather than have each re-run it; a reviewer re-runs only to challenge the recorded result.
● **Capture once, query the capture.** Never re-run a suite to read a different part of its output (the coverage table on one run, the pass and fail totals on another); write the run to a file and grep the file.
● **Never diff-scope a binding REVIEW gate.** Scope the TEST tier by what changed; never narrow the reviewer. The gate's value is precisely that it looks where the fix did not: on one release a defect class survived seven rounds because each round fixed only the sites the reviewer named, and a diff-scoped gate would have passed rounds two through seven while the class still lived in untouched files. Route WHICH gates run by what changed (skip the security gate when no security surface moved), but never narrow a gate you do run so it cannot catch the class elsewhere.

## Decision rules (the pre-invocation checklist)

Before invoking anything above rung 3, ask:
● **Can a cheaper rung catch this?** If a grep or the fast loop can, use it.
● **Is this discovery or confirmation?** Use expensive verifiers to confirm a change believed correct, never to find what a cheap check would have caught.
● **Is the change whole and self-consistent yet?** If not, finish it before gating, so the gate runs once.
● **Are these subtasks independent?** If yes, run them in one parallel batch.
● **Does the scale justify orchestration?** A workflow for a bounded task is over-commitment.

## Standards (checkable assertions)

● The deterministic loop is run before any gate; the gates are run only on a green loop.
● Gates are routed by what changed and run in parallel; only the gate whose inputs changed is re-run after a fix.
● No expensive rung is used to discover what a cheaper rung would catch; no upload is spent to discover a writeable-from-the-start rule.
● Related changes are batched to one gated release; a rung-1 consistency sweep precedes the gate; a trivial amend after a PASS is loop-verified, not re-gated; the advisory design review is reserved for net-new design or a real invariant risk.
● Diffs are minimal; temporary work stays in the scratchpad; established facts are not re-derived.

## Failure modes and remedies

● **A gate FAILs on something a grep would have shown.** Cause: discovery at rung 6. Fix: a rung-1 consistency sweep and self-review before gating.
● **Two gate passes for one change.** Cause: gating a half-change, or an internal contradiction. Fix: finish and self-check the whole change, then gate once.
● **A workflow spun up for a bounded task.** Cause: over-orchestration. Fix: do it in one context; reserve workflows for genuine scale.
● **Repeated upload cycles.** Cause: rules discovered at rung 8. Fix: write to the gate from the first line and run the local checker (`appstore-gate-compliance`, `ci-cd`).
● **A dozen presentation releases converging on one feel.** Cause: a subjective target discovered by iteration. Fix: pin it with a reference and an acceptance line at kickoff (`flight-plan`), then batch the passes behind it.
● **A reconciliation release with no feature in it.** Cause: an avoidable branch divergence. Fix: integrate continuously or keep the branch a superset.

## Verification

Track cycles-to-green: the number of gate re-runs and upload attempts a change takes. The target is one clean pass at each rung, which is simultaneously the cheapest, the fastest, and the best-quality outcome, because a change that passes first time was right before it was checked.

## Glossary

● **Cost ladder:** the rungs a check can run at, cheapest to most expensive, from a grep to an App Store upload.
● **Cheapest rung that can catch it:** the rule to run each check as low on the ladder as will still catch the problem.
● **Verifier, not iterator:** a gate confirms a change believed correct; it is not a loop for finding what a cheap check would catch.
● **Cycles-to-green:** the count of gate re-runs and upload attempts to reach a pass; the metric to drive to one.
● Other terms: `glossary`.

## Provenance

Distilled from this project's own working evidence, where a gate FAIL that a cross-file grep would have prevented cost an extra binding-review cycle, and from the App Store field reports where rules discovered at upload cost waves of full cycles. The remedy in both was the same: catch every check at the cheapest rung that can catch it, and reserve the expensive rungs for confirmation. Cross-referenced to the owning skills so a gap routes back to its owner.
