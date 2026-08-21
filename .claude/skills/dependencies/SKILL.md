---
name: dependencies
description: Dependency hygiene. Use when adding, pinning, or auditing dependencies. Covers exact pinning, the committed lockfile, npm ci, the CVE scan, the static zero-runtime-dependency rule, and the server policy of no new runtime dependency without a recorded reason.
---

# Dependencies

> Stack note: the commands shown here are the Node example. For Python, Java, Go, Rust, or .NET, run the equivalent canonical step from `toolchain-adapters`. The principle in this skill is what binds; the command is illustrative.

## Purpose and scope

How dependencies are managed: exact-pinned with a committed lockfile, installed with `npm ci`, scanned for known vulnerabilities. The static artifact has zero runtime dependencies; the server app adds a runtime dependency only with a recorded reason. Scope is the manifest, the lockfile, and supply-chain hygiene. It does not cover install steps (`environment-setup`) or the App Store dependency scan stage (`app-store-deployment`).

## When to use

- Adding or upgrading a dependency.
- Auditing what the project depends on.

## Prerequisites

- `environment-setup` complete (`node`, `npm`).

## Procedure

1. **Static: confirm the runtime has zero dependencies.**
   ```
   grep -nE "require\(|import .* from|src=\"http" ${SOURCE_PATH}   # expect no runtime imports/remote src
   ```
2. **Pin exactly** (no `^`/`~`).
   ```json
   "devDependencies": { "<tool>": "X.Y.Z" }
   ```
3. **Regenerate the lockfile deliberately** when you change a dependency, and commit it.
   ```
   npm install --package-lock-only    # update lockfile without installing
   git add package-lock.json
   ```
4. **Install reproducibly** everywhere with `npm ci` (never `npm install` in CI).
5. **Audit for known vulnerabilities, and do not trust the exit code alone.**
   ```
   npm audit --omit=dev   # runtime
   npm audit              # tooling; review and address High/Critical
   ```
   When the advisory endpoint is unreachable, `npm audit --json` on npm 8 and later returns an error-shaped object on stdout with **exit code 0** (a `message` or `error` field, no `metadata`). Naive parsing reads that as a clean, zero-advisory report, so a real High advisory scans as clean. Detect the error shape first (JSON present, `metadata` absent) and treat it as a failure to check, never a pass: an honest skip on an explicitly offline runner, a hard fail on the authoritative networked runner (`appstore-gate-compliance`, `security-hardening`).

## Decision rules

- **Runtime dependency requested (static)?** Refuse; the artifact is single-file and offline. Re-scope.
- **Runtime dependency requested (server)?** Allowed only with a recorded reason in the change; prefer the standard library; pin exact.
- **Range vs exact?** Always exact, for reproducibility and supply-chain safety.
- **Lockfile conflict in a merge?** Do not hand-edit. Re-run `npm install --package-lock-only` on the merged manifest and commit.
- **Bumping the release version?** Surgical, never a repo-wide search-and-replace of the old version string. A blind replace has twice corrupted `package-lock.json` (stamping a transitive dependency's record, then fabricating a version for a path-keyed record), tolerated by npm until an install resolves the poisoned record and caught only by the binding gate. A bump touches exactly three fields: the root `version` and `packages[""].version` in the lockfile, plus `package.json`. Edit by path, confirm `git diff package-lock.json` shows exactly two changed lines, and never let a bump touch a line containing `node_modules/` (`appstore-gate-compliance`).
- **`npm audit` flags a High?** Upgrade to a fixed version, re-pin, regenerate the lockfile, re-run the loop. If transitive and uncontrollable, document the suppression with justification (`security-hardening`).
- **`npm audit` exited 0?** Confirm it actually ran. A report with no `metadata` and a `message`/`error` field is an outage, not a clean tree; distinguish "clean" from "could not check" before trusting it (`appstore-gate-compliance`).

## Standards (checkable assertions)

- Static: zero runtime dependencies (no `require`/`import`/remote `src` in the artifact).
- All dependencies are exact-pinned (no `^`/`~` in the manifest).
- `package-lock.json` is committed and unchanged after `npm ci`.
- `npm audit` shows no unaddressed High/Critical, and a run that could not reach the advisory endpoint is treated as a failure to check, never a pass.

## Failure modes and remedies

- **`npm ci` says lockfile out of sync.** Manifest changed without regenerating the lockfile. Fix: `npm install --package-lock-only`, commit.
- **A transitive CVE appears.** Detect: `npm audit`. Fix: upgrade the parent to a patched version; else document the exposure and constrain usage.
- **Someone adds a caret range.** Detect: grep the manifest for `^`/`~`. Fix: pin exact, regenerate the lockfile.

## Verification

`git status --porcelain package-lock.json` empty after `npm ci`; the manifest has only exact versions; `npm audit` clean for runtime and no unaddressed High/Critical for tooling.

## Worked example

Upgrading the browser driver: set `"playwright": "1.56.1"` exactly, `npm install --package-lock-only`, `git add package-lock.json`, `npm ci` (lockfile unchanged), run the loop green. No runtime dependency was touched (static has none; server recorded the reason if it were a runtime change).

## Glossary

- **Lockfile:** `package-lock.json`, the exact resolved dependency tree.
- **`npm ci`:** clean install from the lockfile; fails if manifest and lockfile disagree.
- **Pinned/exact version:** `X.Y.Z`, not a range.
- Other terms: `glossary`.

## Provenance

Merged from both source bundles' dependency manifests, the committed lockfiles, the CI `npm ci` steps, and the no-runtime-dependency (static) and recorded-reason (server) policies.
