---
name: data-layer
description: Data layer and persistence. Use when shaping the data the app holds, reading or writing stored state, or choosing a store. Covers static data-as-literals with a build-pass normalisation, the server atomic JSON store and optional database add-on, the anti-shrink merge rule, boundary validation on every write, and seeding.
---

# Data layer

## Purpose and scope

How the application holds and persists data. The static artifact carries its data as inline literals, normalised once in a build pass. The server app persists to an atomic JSON store on the file-storage add-on, or to a database add-on when relational integrity is needed, with boundary validation on every write and an anti-shrink rule on merges. Scope is the shape, validation, and durability of data. It does not cover the HTTP routes that expose it (`api-and-integration`) or in-memory client state (`state-management`).

## Real data is verbatim law

When a real dataset is supplied (a delivered file, an export, a system of record), its controlled values are IMMUTABLE by default. Do not fold a vocabulary, rename a grade, canonicalise a spelling, or tidy a schema on your own judgement. The instinct to normalise is right for hand-authored data and exactly wrong for a delivered dataset that other systems already mirror: a tidier schema silently breaks every consumer that expected the original.

Mirror the file verbatim first. Then, if a normalisation genuinely helps (a controlled value set, empty-source handling, seed trust, a country-spelling convention), take each deviation to the owner as an explicit question and apply it only on a yes. Ask before transforming, never after. Mark any value you cannot confirm with the explicit unknown marker; never invent one to fill a gap.

## When to use

- Defining or changing the shape of stored data.
- Adding a read or write path to a store.
- Choosing between the JSON store and a database add-on.

## Prerequisites

- `code-architecture` (archetype and the createApp factory for server).
- `security-hardening` (store path or database URL come from the environment).

## Procedure

1. **Static: data as normalised literals.** Author data inline, then run a single build pass that validates shape, fills defaults, and rejects malformed records. The shipped artifact contains only normalised data; no fetch occurs at runtime.
2. **Server: choose the store.** Default to an atomic JSON store on the file-storage add-on (`STORAGE_MOUNT_PATH=/data`). Use a database add-on (Postgres) only when you need transactions, concurrent writers, or relational queries.
3. **Write atomically.** Write to a temp file in the same directory, then rename over the target, so a crash never leaves a half-written file.
   ```js
   await fs.writeFile(tmp, JSON.stringify(next));
   await fs.rename(tmp, target);   // atomic on the same filesystem
   ```
4. **Validate at the boundary, fail closed.** Every incoming record is validated before it touches the store. Unknown or malformed input is rejected, not coerced.
5. **Merge without shrinking.** When merging an update into stored state, never let a partial payload delete fields the client did not send. Apply the anti-shrink rule: merge keys, keep existing values absent from the update.
6. **Seed deterministically.** Provide a seed routine that creates the initial store if absent and is idempotent on re-run.

## Migrations, backups, and retention

1. **Version the data shape.** Stamp stored state with a schema version. On read, migrate an older snapshot forward through small, idempotent, additive steps; never silently drop fields you do not recognise. Prefer additive changes (new optional fields) over renames or removals.
2. **Migrate forward, deliberately.** For the database add-on, apply forward-only migrations (reversible where practical) at a controlled step (startup or a migration job), recorded in version control; never hand-edit schema in production. For the JSON store, migrate in the read path or a one-off script, guarded so it runs once.
3. **Back up before a destructive or migrating write.** Take a backup first and log that you did (`observability-and-audit`). JSON store: copy to a timestamped file before the rename. Database: a snapshot or dump before the migration. `deploy-gate` requires a tested rollback to exist before an irreversible step.
4. **Bound growth and prune.** Cap collections (keep the newest, never silently lose a fresh entry), and prune old backups on a retention window so storage does not grow without limit.

## Decision rules

- **JSON store vs database?** JSON for simple, low-concurrency state; database when you need transactions or concurrent writers.
- **Partial update received?** Merge, do not replace; never drop unsent fields (anti-shrink).
- **A real dataset was supplied?** Its controlled values are law: mirror them verbatim; folding, renaming, or canonicalising any of them is a change the owner must confirm first.
- **Malformed record?** Reject at the boundary; never store coerced junk.
- **Two writers race?** JSON store is single-writer per process; serialise writes, or move to the database add-on.
- **Add-on path empty at boot?** Read `STORAGE_MOUNT_PATH` at request time, not module load.
- **Changing the data shape?** Bump the schema version and add a forward, idempotent migration with a backup first; never drop unknown fields.

## Standards (checkable assertions)

- Static data is normalised in a build pass; the artifact holds no runtime fetch.
- Server writes are atomic (temp-write then rename).
- Every write validates input at the boundary and fails closed.
- Merges never shrink stored state (anti-shrink rule holds under test).
- The seed routine is idempotent.
- Stored data carries a schema version; shape changes ship a forward migration and a pre-migration backup; collections are capped and backups pruned.

## Failure modes and remedies

- **Half-written store after a crash.** Cause: direct overwrite. Fix: temp-write then rename.
- **A field disappears after a partial save.** Cause: replace instead of merge. Fix: anti-shrink merge; add a regression test that saves a partial and asserts retained fields.
- **Junk record stored.** Cause: no boundary validation. Fix: validate and reject before write.
- **Seed double-applies.** Cause: non-idempotent seed. Fix: create-if-absent guard.

## Verification

A parity test writes a partial update and asserts no field was dropped; a crash-simulation test asserts the store is never half-written; boundary tests assert malformed input is rejected; the seed runs twice with identical result.

## Worked example

A server app stores user preferences as JSON on `/data`. A client sends `{theme:"dark"}` only. The write path validates the record, merges it into the stored object keeping `language` and `notifications` intact (anti-shrink), writes to `/data/prefs.json.tmp`, then renames over `/data/prefs.json`. A test sends the partial and asserts all three keys survive.

## Superset merges for an AI scan

The anti-shrink rule generalises to an in-app AI update scan, where the incoming data is a model's research result and the goal is to grow the dataset safely. There: match an incoming record by stable id or, failing that, by a fingerprint (normalised name plus an edition discriminator) so a re-scan that changes the id still updates in place; update only fields carrying a real confirmed value; merge multi-dimensional scores dimension by dimension; ARCHIVE rather than delete a record whose lifecycle has ended; and supersede a premature estimate only when a later scan confirms the real one. Snapshot before the scan so a bad result rolls back. The end-to-end pattern is `ai-update-scan`.

## Glossary

- **Atomic write:** temp-write then rename, so readers never see a partial file.
- **Anti-shrink merge:** a merge that never deletes fields absent from the incoming update.
- **Boundary validation:** validating input at the entry point and rejecting malformed data.
- **File-storage add-on:** App Store persistent volume mounted at `STORAGE_MOUNT_PATH=/data`.
- Other terms: `glossary`.

## Provenance

Merged from the static data-as-literals plus build-pass normalisation, the server atomic JSON store, the anti-shrink merge and boundary-validation rules, and the App Store file-storage and database add-on contracts in `appstore.md`.
