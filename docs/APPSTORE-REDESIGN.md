# Redesigning Pree against the real App Store contract

Written after reading the platform documentation (`App Store Docs`, 11 files, 2,653 lines). Until
now this project inferred the platform contract from a compliance skill and from one upload cycle's
worth of experience. The documentation contradicts several of those inferences, and two of the
contradictions are deploy-breaking.

Nothing in this document has been implemented. It is the design, the evidence for it, and the
decisions that are the owner's rather than mine.

## 1. What the documentation changes

### 1.1 The storage add-on cannot support the store we built

`Add-on services.txt` describes file storage as a **mountpoint-s3 CSI mount**, not a block volume,
and lists under Common pitfalls:

> **No file locking** - S3 does not support POSIX advisory locks. Do not use `flock` or similar
> primitives across pods; they will silently fail or behave inconsistently.

`src/pree/store.py` takes `fcntl.flock(handle.fileno(), fcntl.LOCK_EX)` across the whole read,
merge and write. `docs/DEPLOYMENT.md` makes that lock the stated reason for not taking the
PostgreSQL add-on: two gunicorn workers are concurrent writers, "serialised by an exclusive
`flock`... That is why the managed database is not needed yet."

That justification does not survive contact with the platform. The lock does not fail loudly; it
does nothing. Two workers each read the snapshot, each merge correctly against their own copy, and
the second write erases the first. The anti-shrink merge rule cannot help, because each merge is
individually correct.

**A single worker does not fix it either.** `Get started.txt` describes env-var changes as
triggering "a rolling restart", so an old pod and a new pod overlap by design. Durable writes from
one process are not guaranteed even at one replica.

Two further properties of that mount matter and are NOT settled by the documentation:

- `store.py` writes a temporary file and calls `tmp_path.replace(self._path)`. The docs never
  mention whether rename is supported on this mount. If it is unsupported every write fails; if it
  is emulated by copy it is not atomic, and the crash-safety argument goes with it. **UNKNOWN,
  must be confirmed before any design that keeps file storage.**
- Directory listings are eventually consistent. Pree does not list the directory today, so this
  costs nothing now, but it forecloses a future index-by-listing.

### 1.2 The platform already does authentication, and does it better

`Authentication.txt` is unambiguous: "Your app does not need its own login." Every request to
`<app>.appstore.bluestaq.com` carries `X-Auth-Request-Sub` (stable user id),
`X-Auth-Request-Email`, and `X-Auth-Request-Role` (`OWNER` > `ADMIN` > `EDITOR` > `VIEWER`). The
platform **replaces any copy the client sent**, so they cannot be forged, and on a private app a
caller without a grant is refused by the platform and never reaches us.

Pree currently ships a shared `PREE_TEAM_TOKEN`, compared in constant time, plus a
caller-supplied `x-pree-actor` header that is sanitised but **not authenticated**. So today every
audit line attributes an action to whatever string the caller typed.

The platform's headers are strictly better on every axis that matters here: per-user attribution
instead of one shared secret, no secret to distribute or rotate, no token-strength configuration,
and an actor the caller cannot choose.

### 1.3 Errors in our own documents, found by comparison

| Our claim | Where | What the platform documentation says |
|---|---|---|
| Deployment target `pree.apps.bluestaq.com` | `CLAUDE.md`, `docs/DEPLOYMENT.md` twice | `{appname}.appstore.bluestaq.com`, stated in three separate files. Ours appears to be wrong. |
| `PREE_ALLOWED_ORIGIN` = `https://pree.apps.bluestaq.com`, marked **COPY-PASTE EXACT** | `docs/DEPLOYMENT.md` | If the host is wrong, the operator pastes an origin that never matches, and CORS fails closed against the real site. A wrong value in a copy-paste-exact table is worse than a blank one. |
| Listing category unknown | `docs/DEPLOYMENT.md` | The category is a fixed enumeration: `DATA_ANALYTICS`, `MISSION_OPS`, `DEVELOPER_TOOLS`, `SECURITY_COMPLIANCE`, `COMMUNICATION`, `PRODUCTIVITY`, `OTHER`. |
| Short description drafted at ~150 characters | `docs/DEPLOYMENT.md` | Short description is capped at **80 characters**, full description at **500**. Both current drafts are over. |

`docs/DEPLOYMENT.md` bills itself as the single source of the platform settings. Three of its rows
are wrong or unusable. That is the cost of inferring a contract instead of reading it.

### 1.4 One contradiction inside the platform's own documentation

`Submitting an app - overview.txt` describes file storage as "1 Ti capacity"; `Add-on services.txt`
says "Default storage capacity: 1 Gi". **UNKNOWN**, and it only matters under option C below.

### 1.5 The constraint that decides the storage question

`CICD Pipeline.txt`:

> **Tests that need a database** - The test step has no database. Tests that need one cannot run
> there - keep them out of the command in the table above, and cover that code with tests that stub
> the database instead.

The platform generates its own pipeline and we do not maintain it. So any database-backed code is
covered on the platform by stubs only. This project's most expensive repeated lesson is that a test
of a model of the thing is not a test of the thing. The design has to keep the untestable surface
as small as possible.

## 2. The options

| | A. Stateless | B. PostgreSQL | C. Keep file storage |
|---|---|---|---|
| Add-on memory | 0 | 256 Mi | 0 |
| fsGroup blocker | Gone | Gone | Remains |
| `flock` problem | Gone | Gone | **Unsolved** |
| Rename on S3 | N/A | N/A | **UNKNOWN** |
| Survives rolling restart | Yes | Yes | **No** |
| Horizontal scaling | Free | Correct | Corrupts |
| Assessment history | **Lost** | Kept, backed up daily | Kept |
| Untestable on platform | None | Thin adapter | Store |

The per-app cap is 8 Gi memory and 6 CPU, so 256 Mi is about 3% of budget. Cost is not the
deciding factor; correctness and testability are.

**Option C is not viable.** It relies on a lock the platform says does not work, and on a rename
whose behaviour is undocumented. It is the only option that leaves the `fsGroup` request necessary.

## 3. Recommended design

**Take option A if assessment history is not a product requirement; option B if it is.** That is a
product question, not an engineering one, which is why it is section 5.

Everything below applies to both.

### 3.1 Authentication: adopt the platform headers, retire the shared token

- Derive the actor from `X-Auth-Request-Sub`, never from a caller-supplied header. `x-pree-actor`
  is removed; the audit stream gains real attribution for the first time.
- Gate by role rank, not by name, so a role added later cannot inherit write access:
  `VIEWER` reads, `EDITOR` and above write. An unrecognised role is treated as `VIEWER`.
- **Fail closed on an empty header.** `Authentication.txt` says a public app sends all three
  headers empty. Pree must refuse writes when the role is empty rather than treating empty as
  permitted, because that is exactly the posture that would turn a visibility change into an open
  door.
- Keep `PREE_TEAM_TOKEN` for local development only, where no platform sits in front, and make the
  production boot check require the platform path instead. The constant-time compare stays for that
  path; it is cheap and the test that pins it stays meaningful.

This deletes a shared secret from the system, which is a larger security win than anything else in
this document.

### 3.2 Keep the scoring core pure, keep the adapter thin

The scoring core is already pure: standard library only, no I/O, which is why it vendored cleanly
into SPECTRE. Whatever option is chosen, the rule is that validation, merging and scoring stay
pure and fully covered on every runner, and only a thin adapter touches the outside. Under option
B that keeps the platform's stub-only surface to a few dozen statements rather than the store.

### 3.3 Health and readiness

The existing split is already right and the documentation confirms the shape: liveness paths touch
nothing, and the storage proof sits on its own path. Under option B that path probes the database
with a bounded timeout. Under option A it reports no backing store and always passes, and
`/healthz/storage` should be renamed rather than left asserting something that no longer exists.

### 3.4 Resource envelope

The python template defaults to 128 Mi. Pree runs two gunicorn workers and measures in-process
calls at 0 to 2 ms. Proposed: 256 Mi and 500m CPU for the service, plus 256 Mi for PostgreSQL under
option B. Total 512 Mi of 8 Gi.

### 3.5 Listing metadata, now answerable

- **Category**: `MISSION_OPS`. Pree serves Protect and Defend operators; it is not analytics
  tooling. The owner should confirm, but this is the only value that fits the description.
- **Short description**, within 80 characters, drafted: *Explainable, confidence-tiered satellite
  threat scoring for Protect and Defend.* (79 characters.)
- **Full description**: must be cut to 500 characters from the current draft.
- **URL**: `pree.appstore.bluestaq.com`, pending confirmation, with `PREE_ALLOWED_ORIGIN` corrected
  to match.

## 4. What changes, file by file

Under option B. Option A is the same minus the database adapter.

| File | Change |
|---|---|
| `src/pree/store.py` | Replace JSON, `flock` and `replace()` with a thin adapter. `merge_without_shrinking` stays pure and keeps its tests. |
| `src/pree/security.py` | Role-rank check on platform headers; constant-time token compare kept for the development path. |
| `src/pree/app.py` | Actor from `X-Auth-Request-Sub`; `x-pree-actor` removed; write routes gated at `EDITOR`. |
| `src/pree/config.py` | `DATABASE_URL` required in production; `PREE_DATA_DIR` and `STORAGE_MOUNT_PATH` removed; origin corrected. |
| `src/pree/health.py` | Storage probe becomes a bounded database probe, or is retired under option A. |
| `requirements.in` | Adds `psycopg`, with its reason recorded, under option B only. |
| `Dockerfile` | Unchanged. The port, user and flattening contract are all already correct. |
| `docs/DEPLOYMENT.md` | Rewritten against the real contract: URL, origin, category, descriptions, add-ons, resource envelope. |
| `.github/workflows/verify.yml` | Adds a real PostgreSQL service so the adapter is tested against a database somewhere, since the platform's own stage cannot. |

Both gates and the full loop apply, as they do to every change here.

## 5. Decisions that are the owner's

1. **Is assessment history a product requirement?** It decides option A against option B. Pree's
   scoring is a pure function of its inputs; the store exists so an assessment can be read back
   later. Now that the scoring core is vendored into SPECTRE, which has its own database layer,
   standalone persistence may be redundant.
2. **Is the app private?** The whole authentication design rests on it. On a public app the headers
   are always empty and role-based behaviour is impossible.
3. **Confirm the hostname.** Three of our rows are wrong if it is `appstore.bluestaq.com`, and the
   origin value is marked copy-paste-exact.
4. **Confirm `MISSION_OPS`** and sign off the shortened descriptions.

## 6. What this removes

Worth stating plainly, because it is the point of the exercise. Either option retires: the
`fsGroup` operations request that currently blocks the release; the `flock` dependency the platform
says does not work; the rename question; the shared team token; the unauthenticated actor header;
and three wrong rows in the deployment sheet.
