# Bluestaq operating rules (kit-owned: update through the kit, not by hand)

## Language
- UK English. No em dashes or double dashes in prose. Lead with the point.

## Definition of done
- Done means proven, not claimed. A task is complete only when the evidence is in the reply: command, exit code, and the relevant output lines.
- UI fixes: reproduce first, then prove the fix (headless browser check, test or rendered output). List any cache-busting step. If unproven, say "unproven".
- Data defects: fix or delete at the source. Never annotate a bad value and leave it. Regenerate and grep the output to prove it has gone.
- Releases: build the artefact once, write its SHA-256, and send it to the user in the same turn. Never commit build artefacts. The release package excludes .claude/, CLAUDE.md and .release/: they are development tooling, not product, and the App Store scans every file uploaded. Never upload, submit or resubmit to the App Store; give the user the steps.

## Evidence rule
- Every count, date range, coverage figure or claim about data comes from a command run in this session. Quote the command next to the number.
- Never guess external facts: version tags, action SHAs, key formats, published versions, API shapes. Look them up (gh api, upstream releases, provider docs) and pin actions to a full commit SHA.

## Gates
- Mirror the remote gates locally before any upload: the project contract is .claude/gate.json. Run every stage, not a subset, before calling a build "gated".
- Gates decide on exit codes or structured summaries, never on words such as "failed" or "error".
- Container scans use Grype on the final image: it mirrors the App Store's Anchore scan. Scanner versions are pinned in the cloud setup script; never install tools from an unpinned branch.
- Never weaken, skip or edit a gate to get past it. Never set, unset or reference CLAUDE_GATE_BYPASS.

## Tests must bite
- Every security or compliance control has a test that fails when the control is removed. Prove it: neutralise the control on a scratch branch, run the suite, confirm a failure, restore.
- A mutation that changes no behaviour is not a test. Test credentials use the real format with obviously fake values.

## Stop conditions and loops
- At the start of gate or release work, restate the exit criterion. After each round print "Exit criterion met: yes/no". If yes, stop and report.
- Maximum 3 gate rounds without a check-in with the user.
- The same error twice is a diagnosis problem, not a timing problem: stop and run /prove-cause. Never schedule retries on an unconfirmed cause.
- Run long verification in resumable stages, appending progress to .release/verify-progress.md. Do not rely on long background jobs.

## Blockers
- Report blockers in three lines: what I can fix now; what the user must do (exact steps); what cannot be done in this environment and the workaround.
- Never imply the user can reach something inside Claude's environment (remote disk, sandbox folders, proxy).

## Change discipline
- Before removing or renaming any symbol, search the whole repo, including build scripts, embedded self-checks and packaging config.
- When a pipeline fails, triage the whole report, group by root cause, search for other instances, fix in one change, then release once.
- Check git tags and published versions before choosing a version number. Bump every location in one change.
- Units are explicit at every boundary; SI by default. Every conversion has a test with a known value.

## Sessions
- One goal per session. Before a pause or a context switch, run /handover.
- Close every session with a ledger: fixed and verified; flagged but not fixed; assumptions made.

## Environment (Claude Code on the web)
- Sessions run on a fresh cloud machine cloned from GitHub. Anything not committed does not exist next session.
- Tag pushes are blocked by the Claude Code proxy (HTTP 403). Create the tag, then hand the user the push command and a verification command.
- Push work to a branch and open a pull request. The user merges in GitHub.
- If a gate tool is missing, say so and name it for the cloud environment setup script. Never remove the command from .claude/gate.json.

