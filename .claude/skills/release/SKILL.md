---
name: release
description: Cut a release end to end against a stated exit criterion. Version check, full gate, parallel reviews, single artefact with SHA-256, tag, hand-off.
argument-hint: "[version] [exit criterion]"
disable-model-invocation: true
---
# Release $ARGUMENTS

Last tag: !`git describe --tags --abbrev=0 2>/dev/null || echo none`

0. **Exit criterion.** Restate the target version and the exit criterion. If either is missing, ask once, then proceed. After every round print `Exit criterion met: yes/no`. If yes, stop and go to step 6.
1. **Version.** Confirm the version is unused (git tags and App Store if connected). Bump every location and the CHANGELOG in one commit.
2. **Pre-flight.** Run the /preflight steps. Write progress to `.release/verify-progress.md` after each stage so an interruption resumes rather than restarts.
3. **Fix loop (max 3 rounds).** Group all findings by root cause, search for other instances, fix in one change per root cause, re-run the full gate. After round 3, stop and check in with the user.
4. **Independent review.** In parallel, spawn the `security-gate-reviewer` and `engineering-gate-reviewer` agents on the diff since the last tag. Fix blocking findings, then re-run the gate once.
5. **Cold check.** Spawn `cold-verifier` to confirm every claim you intend to report.
6. **Package once.** Build the artefact from tracked source, excluding `.claude/`, `CLAUDE.md`, `.release/`, `.git/`, `node_modules/` and other build output. List the archive contents and confirm none of those paths are present. Then write `<name>-<version>.zip.sha256`, re-verify the checksum, and send the file to the user in this turn with name, size and SHA-256.
7. **Tag.** Commit and tag (the hook re-checks uniqueness and scanners). Do not push tags from a remote session; give the user the exact push command and a verification command.
8. **Record.** Write `.release/<version>.md`: exit criterion, gate table with evidence, reviewer findings and dispositions, SHA-256, user actions outstanding.

Never submit, resubmit or upload to the App Store. Show the user the steps.
