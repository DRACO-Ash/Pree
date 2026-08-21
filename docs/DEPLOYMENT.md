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

**The correct console state for the first release is an EMPTY environment tab.** Pree carries
its defaults in code, and every value the platform provides is injected at the pod level. Any
variable typed into the tab is an override and a liability.

| Variable | Console action | Provided by |
|---|---|---|
| `PORT` | **delete this variable** | platform, at the pod level |
| `STORAGE_MOUNT_PATH` | **delete this variable** | the FILE_STORAGE add-on, as `/data` |
| `PREE_ENV` | leave unset for the first release | code default `development` |
| `PREE_TEAM_TOKEN` | leave unset until the team is onboarded | operator, as a secret |
| `PREE_ALLOWED_ORIGIN` | leave unset until the token is set | operator |
| `PREE_DATA_DIR` | **delete this variable** | code resolves it from `STORAGE_MOUNT_PATH` |
| `PREE_BUILD_ID` | leave unset, or set to the release tag | release process |

When the team is onboarded, `PREE_ENV=production`, `PREE_TEAM_TOKEN` and `PREE_ALLOWED_ORIGIN`
are set **together**. A token with no origin, or a token with `*` as the origin, makes the app
refuse to start. That is deliberate: it fails at boot rather than serving a credentialed
endpoint to any caller. Save the FULL variable set with `save_env_vars`, which replaces rather
than merges, then `apply_env_vars` to make it live.

## Add-ons

| Add-on | Enable | Why |
|---|---|---|
| FILE_STORAGE | yes | the assessment store persists at the injected `/data` |
| POSTGRESQL | no | no transactions or concurrent writers in this release |
| REDIS | no | rate-limit state is per-process by design (see the security policy) |
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
`/`, `/healthz`, `/readyz`, `/livez`, `/ping`.

`GET /` returns 200 and never a 302, because the platform router probes the root.

`/healthz/storage` is the container HEALTHCHECK target and the only path that touches storage.
It performs a real write, races a 1.5 second timeout shorter than the platform probe, and on
failure returns 503 naming the resolved directory and the exact errno.

`/diagnostics` is a secret-free read-out: every critical input as a boolean and a length, never
a value, plus the resolved identity and storage state, with every field present at once.

## Rollback

There is no previous release to roll back to. This is the first delivery, stated plainly rather
than implied. The rollback path once a second release exists is `resubmit_app` with the previous
upload archive, which is why dated delivery copies of the archive are kept.
