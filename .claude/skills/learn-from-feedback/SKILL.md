---
name: learn-from-feedback
description: Turn a corpus of Teach Bob retrospective reports into a prioritised, evidence-led plan of improvements to this Foundations baseline, then apply the approved items through the binding gates. Use when a batch of retrospectives has built up, when someone asks to "review the feedback", "fold the retrospectives back in", "improve the baseline from feedback", or "run the learning loop". Clusters recurring themes across the six retrospective lenses, names the target skill, agent, or guidance for each change with the report evidence and contributor names, and never edits or deploys anything without human approval and the binding reviewers.
---

# Learn from feedback (the Teach Bob learning loop)

> Teach Bob captures a retrospective at the end of a build and stores it. This skill closes the loop: it reads the collected reports, works out what keeps coming up, and proposes concrete improvements to the baseline, so the lessons change the standard instead of sitting in a list. It analyses and proposes; a human approves; the normal gates execute. Nothing here edits a skill or deploys anything on its own.

## Purpose and scope

A retrospective is only worth writing if it changes what happens next. This skill is the bridge from the Teach Bob reports to the baseline they should improve. It runs a synthesis across the whole corpus of reports, not one at a time, so a theme that shows up in three builds is treated as a signal, not noise.

- **In scope:** improvements to this Foundations baseline and the skills, agents, output style, hooks, and guidance it ships. Every downstream project that downloads the baseline inherits the improvement.
- **Out of scope:** editing other live applications, or deploying anything. Cross-repo propagation into other apps is a separate, larger system with its own per-repo access and gates. This skill proposes; a human-gated Claude Code session executes against this repository only.

## The loop

CAPTURE -> ANALYSE -> PROPOSE -> (human approve) -> GATED EXECUTE -> LEARN.

- **Capture** already happened: the reports live in the Teach Bob tab, tagged by developer name and upload time. Export them (the tab offers a one-click download of the whole set) or gather the `RETROSPECTIVE.md` files.
- **Analyse** the corpus. Read every report. For each of the six lenses (what went well, what did not, improvements, optimisations, waste, missed detail), collect the points across all reports and cluster them by theme. A theme that recurs, or that one report argues sharply, is a candidate. Keep the developer name and upload time against each point: it is the evidence, and the contributor credit.
- **Propose** a prioritised improvement plan. Each item names: the target (a specific skill, agent, output-style rule, hook, or guidance file), the change in one or two sentences, the rationale, the report evidence (quote or paraphrase, with the contributor name and date), and a size estimate. Order by how often the theme recurs and how much it would help. Mark anything that would touch a hard rule or a gate as needing extra care.
- **Human approve.** Present the plan. A human picks which items to apply. Nothing proceeds without this step.
- **Gated execute.** Apply the approved items in one batched change (see `resource-discipline`: batch the edits, gate once). The change then passes the binding `engineering-reviewer` and, if it touches a security surface, `security-reviewer`, exactly as any other change. If the change deploys or publishes, it additionally needs the `deploy-gate` verdict and an explicit human yes. Add the audit row and bump the edition, because the bundle changed.
- **Learn.** Record which items were applied and which were declined, so the next synthesis does not re-propose a rejected idea. Credit the contributors whose reports drove each change (this feeds the credits section).

## When to use

- When a batch of Teach Bob reports has accumulated (a nudge appears in the tab once several new reports are in).
- When the owner asks to review the feedback and fold it back in.
- Not after a single report: one report is a data point, not yet a theme. Wait for a few, or use it only if it names something sharp and specific.

## Prerequisites

- The collected retrospective reports (export them from the Teach Bob tab, or gather the `RETROSPECTIVE.md` files).
- This baseline repository open in Claude Code, so approved items can be applied and gated in place.

## Guardrails (non-negotiable)

- **No autonomous change.** The loop analyses and proposes only. A human approves before any edit, and the binding reviewers plus, for a deploy, the `deploy-gate` and a human confirmation, guard the execution. This mirrors the CLAUDE.md hard rule that nothing deploys or mutates external state without the gate and a human yes.
- **Evidence-led, never invented.** Every proposed change cites a real report and a real contributor. If a theme cannot be tied to a report, it does not go in the plan. Do not fabricate a name, a quote, or a figure; mark an unknown with the explicit unknown marker.
- **Smallest change that helps.** Prefer tightening an existing skill over adding a new one. Batch the approved items and gate once.
- **Prefer an executable guard to a written rule.** A prose rule in a skill helps a little; a script, a test, or a CI step that fails on the defect helps properly. Measured on one project, a set of hard rules added after a painful release took the gate-round count down only modestly, while four executable guards (a pipeline simulation, a theme check, a poller test, a browser probe) each closed a class outright. So for every proposed lesson ask first: can this be a check that fails, not a sentence someone must remember? If yes, ship the check (it is where the fix actually lives). If a lesson genuinely cannot be made executable, fold it as prose but mark it explicitly as an unenforced convention, so no one mistakes an unrun sentence for a guard.
- **Watch the residuals ratio.** A baseline can optimise its own process faster than it closes its own findings, ending with a long backlog of recorded-but-unscheduled residuals. Prefer a fold that closes a real finding (ideally with a check) over one that only adds another rule; when the prose additions outpace the executable ones, that is the signal to stop folding and start enforcing.

## Output

A single `IMPROVEMENT-PLAN.md`: a short summary of the themes found, then the prioritised item list (target, change, rationale, evidence with contributor and date, size), then a credits list of every contributor whose report informed the plan. The owner approves against this file; the applied items become one gated change with an audit row.

## Reuses

`resource-discipline` (batch the change, gate once), `observability-and-audit` (each report is an audit artifact, tagged by user and time; the plan and its outcome are recorded), the binding `engineering-reviewer` and `security-reviewer`, and `project-retrospective` (the generator that produces the reports this skill consumes).
