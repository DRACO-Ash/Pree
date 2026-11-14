---
name: preflight
description: Mirror the remote gates locally and report readiness before any upload, tag or release. Use before release work, before submitting to the App Store, or when asked "is this ready" or "is it gated".
allowed-tools: Read Grep Glob Bash(git *) Bash(df *)
---
# Pre-flight (report only, no edits)

Repo state at invocation:
- Last tag: !`git describe --tags --abbrev=0 2>/dev/null || echo none`
- Uncommitted files: !`git status --porcelain | wc -l`

Run every step. Do not edit files. Do not stop at the first failure; collect everything.

1. **Contract.** Read `.claude/gate.json` and the project CLAUDE.md. If the contract is missing, stop and offer to draft one from the project's real tooling.
2. **Environment.** Disk headroom; registry access (authenticated or mirror, no anonymous Docker Hub pulls); credentials the gates need; actions this environment cannot perform (tag push from a remote session, uploads, App Store submission).
3. **Version.** Compare the intended version with `git tag -l` and, if the App Store MCP is connected, the published versions. Check every version location agrees.
4. **Full gate.** Run every command in the `commit`, `push` and `tag` stages. Record command, exit code and finding count.
5. **Controls bite.** For each control listed under "Controls" in the project CLAUDE.md, neutralise it on a scratch branch or stash, run the suite, confirm at least one failure, restore. Record killed or survived.
6. **Clean start.** Run the app with an empty environment and no `PORT`; confirm `GET /` returns 200 on 8080; rerun with `PORT=9000` and confirm it follows. Check log lines match the required format.
7. **Guard scope.** List what each guard scans and confirm every top-level folder is covered.

## Output
A table: step | command | exit | result | evidence. Then the three-line blocker report. Then a single verdict: READY or NOT READY with the blocking items.
