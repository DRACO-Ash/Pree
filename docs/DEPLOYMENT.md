# Pree deployment parameters

The first delivery of a container app ships this table. The operator configures the App Store
from it, so it is the single source of the platform settings rather than prose to interpret.
Every value below is either COPY-PASTE EXACT or explicitly marked to delete. Nothing here is a
descriptive placeholder, because a placeholder near a paste-able field gets pasted as a literal
value and the fail-closed boot then rejects it, costing a deploy cycle per variable.

## Application

| Setting | Value |
|---|---|
| App slug | `pree` |
| Detected template | `python` (quality-gated: a `requirements.txt` and a root `Dockerfile`) |
| Container port | 8080 |
| Port contract | the code reads `PORT` from the environment, defaults to 8080, binds `0.0.0.0` |
| Never set | `ENV PORT=` in the Dockerfile, and never type `PORT` into the console |
| Runtime user | `10001:10001`, non-root, numeric |

## Environment variables tab

Pree carries its defaults in code, and every value the platform injects lives at the pod
level, so most of this tab stays empty. Three variables are the exception and they are set
**together, from the first release**, not deferred.

An earlier version of this table told the operator to leave the token unset for the first
release and called an empty tab correct. That was wrong, and the app now refuses to start in
that state rather than trusting the instruction. With `PREE_ENV=production` and no token the
gate is open, and the server binds `0.0.0.0`, so the assessment store would be readable and
writable by anything reaching the ingress. The store reveals what the operator is watching and
what they judge dangerous, so an open gate is a disclosure, not a convenience.

| Variable | Console action | Provided by |
|---|---|---|
| `PORT` | **delete this variable** | platform, at the pod level |
| `STORAGE_MOUNT_PATH` | **delete this variable** | the FILE_STORAGE add-on, as `/data` |
| `PREE_DATA_DIR` | **delete this variable** | code resolves it from `STORAGE_MOUNT_PATH` |
| `PREE_ENV` | **COPY-PASTE EXACT:** `production` | operator |
| `PREE_TEAM_TOKEN` | the team token, as a **secret** (see below) | operator |
| `PREE_ALLOWED_ORIGIN` | **COPY-PASTE EXACT:** `https://pree.apps.bluestaq.com` | operator |
| `PREE_BUILD_ID` | leave unset, or set to the release tag | release process |

Generate the token with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Production refuses a token shorter than 32 characters, or one built entirely from a repeated sequence, so a doubled word will not start the app.

All three of the operator-set values go in before the first submission. The app refuses to
start on any unsafe combination: production with no token, a token with no origin, or a token
with `*` as the origin. Each failure is a boot error naming the variable, which is cheaper than
serving a credentialed endpoint to any caller. Save the FULL variable set with `save_env_vars`,
which replaces rather than merges, then `apply_env_vars` to make it live.

## Add-ons

| Add-on | Enable | Why |
|---|---|---|
| FILE_STORAGE | yes | the assessment store persists at the injected `/data` |
| POSTGRESQL | no | the snapshot is a single document; concurrent writes are serialised by a file lock (see below) |
| REDIS | no | rate-limit state is per-process by design (see the security policy) |
| CLAMAV | no | Pree accepts no file uploads |

On concurrency: the image runs two gunicorn workers, so there **are** concurrent writers. They
are serialised by an exclusive `flock` held across the whole read, merge and write inside the
store, verified against four real worker processes. That is why the managed database is not
needed yet, rather than there being no contention. An earlier version of this table declined
POSTGRESQL on the stated ground of "no concurrent writers", which contradicted the launch
command directly above it.

## Operations request required

Pree runs as user `10001` and mounts the FILE_STORAGE volume. The mount arrives root-owned, so
**`securityContext.fsGroup` must be set to `10001`** or every write returns `EACCES`. This needs
an operations request; it is not settable from the console. Until it is set, `/healthz/storage`
returns 503 with `"errno_name": "EACCES"` and the resolved directory in the body, so the fault
is a one-screenshot diagnosis rather than a silent pod kill.

## Resource budget

| Resource | Request | Limit |
|---|---|---|
| Memory | 256Mi | 1Gi |
| CPU | 250m | 1 |

Inside the 8Gi and 6 CPU envelope. Two gunicorn workers with a 60 second timeout.

Storage growth is bounded by construction: the assessment collection is capped at 5000
records, dropping the oldest and never the record just written, so the volume cannot fill
through ordinary accumulation.

Measured through the real scoring path on this build: **1339 bytes** for a minimal record and
**1705 bytes** for a maximum-length one, meaning 64-character identifiers, a 64-character
actor and every indicator present. Plan on the maximum, which is **8.1 MiB** of steady state.
An earlier version of this sheet published 1258 bytes as the planning figure; that was a
best-case measurement presented as a worst case, and it understated the volume by a third.

The snapshot is rewritten whole on every upsert and a backup copy sits beside it, so the
directory holds up to three copies at the moment of a write: request a volume of at least
**64 MiB** and the cap can never be the thing that fills it.

The number the cap's size actually trades against is write cost, so it is published here
rather than left to be discovered. Every upsert reads, merges, serialises, fsyncs, copies the
backup and renames the whole snapshot, all under one exclusive lock. Measured on this build:
**101 ms** per write at 1500 records, extrapolating to roughly **340 ms** at the 5000 cap. That
puts the pod's serialised write ceiling near three per second, against a coarse limit of 240
per minute per address, so a handful of distinct addresses writing at the fine limit will queue
on the lock and eventually meet gunicorn's 60 second timeout. It is authenticated traffic only,
so it sits inside the shared-token risk, but it is the reason a larger cap is the wrong answer
to wanting more history: that is the POSTGRESQL add-on.

The read path costs too, and the sheet used to say only that the write did. A single
`GET /v1/assessments/{key}` parses the whole snapshot, with no cache: measured at **81 ms** at
the cap, against a coarse limit of 240 requests a minute per address and a 1 CPU pod limit.
Roughly three busy authenticated callers will saturate the CPU budget. Reads are deliberately
outside the per-actor limiter, because the actor label is caller-supplied and cannot be a
security boundary, so the coarse limiter is the only bound on read cost. If read traffic grows
past a handful of concurrent callers, cache the parsed snapshot behind the store's lock before
raising the pod's CPU limit.

## Health paths

The complete list of unauthenticated paths in production, and nothing else answers without
the token.

| Path | Behaviour |
|---|---|
| `/`, `/healthz`, `/readyz`, `/livez`, `/ping` | 200, touch nothing, cannot hang |
| `/healthz/storage` | the write proof; 200 or 503, see below |

`/openapi.json`, `/docs` and `/redoc` are **not served in production**. They exist in
development only, because they publish the whole route table and the token header name, and
`/docs` loads a third-party script from a content delivery network onto the app origin.

The liveness paths and `/healthz/storage` are exempt from rate limiting, so rejected traffic
cannot drive the container HEALTHCHECK red and restart the pod. Every response, including a
401, a 413 and a 429, carries `Content-Security-Policy: default-src 'none'`,
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and
`Cross-Origin-Opener-Policy: same-origin`.

`GET /` returns 200 and never a 302, because the platform router probes the root.

`/healthz/storage` is the container HEALTHCHECK target and the only path that touches storage.
It performs a real write, races a 1.5 second timeout shorter than the platform probe, and on
failure returns 503 naming the resolved directory and the exact errno.

Concurrency behaves as follows, and this replaces an earlier description in this sheet that no
longer matches the code. Concurrent callers **join the probe already in flight** and receive
its verdict, so one write serves every caller and no verdict is ever synthesised from guesswork
about how busy the pool is. The response is 200 with `"status": "ready"`, or 503 with
`"status": "unready"` and the errno. The only synthesised outcome is `ETIMEDOUT` when the
in-flight probe's 1.5 second budget expires before it answers, which is the correct reading of
a mount that is not responding. A concurrency-induced 503 is therefore possible and intended
for a mount that is genuinely not working; it is not possible for one that is.

A verdict is cached for two seconds, stamped from when the probe started, so the worst-case age
of a `ready` answer is two seconds rather than two seconds plus however long the write took.

`/diagnostics` is a secret-free read-out: every critical input as a boolean and a length, never
a value, plus the resolved identity and storage state, with every field present at once. It is
gated whenever a token is configured, and open before one exists, which is when a first deploy
needs it and has no token to present.

## Rollback

There is no previous release to roll back to. This is the first delivery, stated plainly rather
than implied. The rollback path once a second release exists is `resubmit_app` with the previous
upload archive, which is why dated delivery copies of the archive are kept.
