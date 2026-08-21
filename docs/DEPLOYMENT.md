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
| `PREE_TEAM_TOKEN` | the team token, as a **secret** | operator |
| `PREE_ALLOWED_ORIGIN` | **COPY-PASTE EXACT:** `https://pree.apps.bluestaq.com` | operator |
| `PREE_BUILD_ID` | leave unset, or set to the release tag | release process |

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

On concurrency: the image runs two gunicorn workers, so there **are** concurrent writers. They
are serialised by an exclusive `flock` held across the whole read, merge and write inside the
store, which is why the managed database is not needed yet rather than why there is no
contention. An earlier version of this table declined POSTGRESQL on the stated ground of "no
concurrent writers", which contradicted the two-worker launch command.
| CLAMAV | no | Pree accepts no file uploads |

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

## Health paths

All of these return HTTP 200, unauthenticated, and touch nothing, so none can hang:
`/`, `/healthz`, `/readyz`, `/livez`, `/ping`. They are exempt from rate limiting, along with
`/healthz/storage`, so rejected traffic cannot drive the container HEALTHCHECK red and restart
the pod.

`GET /` returns 200 and never a 302, because the platform router probes the root.

`/healthz/storage` is the container HEALTHCHECK target and the only path that touches storage.
It performs a real write, races a 1.5 second timeout shorter than the platform probe, and on
failure returns 503 naming the resolved directory and the exact errno.

`/diagnostics` is a secret-free read-out: every critical input as a boolean and a length, never
a value, plus the resolved identity and storage state, with every field present at once. It is
gated whenever a token is configured, and open before one exists, which is when a first deploy
needs it and has no token to present.

## Rollback

There is no previous release to roll back to. This is the first delivery, stated plainly rather
than implied. The rollback path once a second release exists is `resubmit_app` with the previous
upload archive, which is why dated delivery copies of the archive are kept.
