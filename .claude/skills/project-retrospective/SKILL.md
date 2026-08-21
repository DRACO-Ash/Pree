---
name: project-retrospective
description: Downloadable build retrospective generator (the "Teach Bob" feature). Use at the end of a build, a milestone, or a release, or when someone asks to "run a retrospective", "review how this project went", "capture lessons learned", or "generate a Teach Bob report". Deep-reviews both the code and the process across six lenses (what went well, what did not, improvements, optimisations, waste, and missed detail), captures the developer's name, and writes a single self-contained report you upload into the app's Teach Bob tab, where it is saved tagged by name and time so the lessons can be fed back into the baseline. Works standalone in Claude Code or Claude.ai.
---

# Project retrospective (Teach Bob)

> Teach Bob is the friendly name of the feature and the tab that stores the report. This skill is the generator: drop it into a finished project and it writes the retrospective. The report you produce is uploaded into the Teach Bob tab in Code With Bob, which saves it locally tagged by developer name and upload time so a future reader can retrieve it and fold the lessons back into the standard.

## Purpose and scope

A retrospective is only useful if it is honest, specific, and evidence-led. This skill runs a deep review of a project once the work is done, covering the code and the process that produced it, and writes one portable Markdown report with a header the Teach Bob tab can read. It does not deploy, mutate, or grade the project against the App Store gates (that is `app-store-readiness`); it looks back so the next build goes better.

## When to use

- At the end of a build, a milestone, or a release, while the detail is fresh.
- After a difficult stretch worth learning from, whether it shipped or not.
- When asked to run a retrospective, capture lessons learned, or generate a Teach Bob report.

## Prerequisites

- Run it at the project root so the review can read the real code, the commit history, and the change log. A retrospective written from memory is a guess; read before you assert.
- Know who the report is for: capture the developer's name. If you cannot verify it, ask; never invent one.

## Procedure

1. **Establish the ground truth.** Read the repository: the source, the tests, the change log or audit trail, the README, and the recent commit history. Note the dates and figures you can verify from the tree; mark anything you cannot with `TBC, re-verify` rather than asserting it.
2. **Capture the developer's name.** Ask the person running the skill for the name the report should be filed under, plainly: "Whose retrospective is this? I will file it under that name." Do not fabricate, and do not guess from a git author string without confirming it.
3. **Review across the six lenses**, for both the code and the process. For each finding, cite the evidence (a file, a commit, a decision) so a reader can check it:
   - **What went well** - the choices and habits that paid off and are worth repeating.
   - **What did not** - where the build stalled, broke, or went the wrong way, and why.
   - **Improvements** - concrete changes to how the next project is built.
   - **Optimisations** - where the same result could have come faster, cheaper, or with fewer tokens or steps.
   - **Waste** - effort, rework, or scope that produced no value, and the signal that would have caught it earlier.
   - **Missed detail** - the thing that was overlooked: an edge case, a gate, a requirement, a security or accessibility floor.
4. **Rank the findings.** Lead with the few that would most change the next build. A flat list of twenty equal points is a diary, not a retrospective.
5. **Write the report** to `RETROSPECTIVE.md` at the project root, using the format below.
6. **Hand it off.** Tell the developer the file is written and that the next step is to upload it into the Teach Bob tab in Code With Bob, which saves it tagged by their name and the upload time.

## The report format

Write one self-contained Markdown file. Begin with the header block exactly as below: the Teach Bob tab reads the `Developer` and `Date` lines to tag and sort the saved report, and falls back to the file name and the upload time if they are absent. Keep the labels; fill the values.

```
# Teach Bob retrospective

- Developer: <full name, as it should be filed>
- Project: <project or repository name>
- Date: <YYYY-MM-DD, the day the retrospective was run>
- Scope: <the release, milestone, or window this covers>

## Summary
<three or four sentences: the shape of the project and the single most important lesson>

## What went well
- <finding> (evidence: <file, commit, or decision>)

## What did not
- <finding> (evidence: ...)

## Improvements
- <concrete change for next time>

## Optimisations
- <where the same result could have come faster, cheaper, or leaner>

## Waste
- <effort or scope that produced no value, and the earlier signal>

## Missed detail
- <the overlooked edge case, gate, requirement, or floor>

## Top three to carry forward
1. <the highest-value lesson>
2. <the second>
3. <the third>
```

## Rules

- **Honest and specific, never flattering.** A retrospective that only praises is worthless. Name the real problems, and attribute them to decisions and process, not people.
- **Evidence-led.** Every material claim cites something a reader can verify in the tree or the history. No invented names, dates, figures, or organisations; mark the unverifiable with `TBC, re-verify`.
- **Self-contained and portable.** One Markdown file, readable offline, no links to anything that must be fetched. It has to survive being uploaded, stored, and reread months later.
- **House voice.** UK English, plain language, decision before reasoning, a single dash rather than a long dash, and "and" rather than "+".

## Pairs with

- `working-with-claude` for the review and debugging habits a retrospective tends to reward.
- `resource-discipline` for the token and time waste the optimisation lens looks for.
- `app-store-readiness` when the question is "will it pass", not "how did it go".
