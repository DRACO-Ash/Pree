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

## Submission listing fields

The console asks for these when the app is created. Every row is decided now: the platform
documentation supplies the category enumeration and the length caps, and the owner has signed
off the copy.

| Field | Value |
|---|---|
| App name | `Pree` |
| URL | `pree.appstore.bluestaq.com` (derived from the name; **cannot change after submit**) |
| Detected template | `python` |
| Category | `MISSION_OPS` (Mission Operations) |
| Visibility | private at submission. Every new app starts private; a platform admin publishes it. |
| Short description | below, 79 characters against a cap of 80 |
| Full description | below, 497 characters against a cap of 500 |
| Icon, screenshots, tags | optional, none supplied |

There is no "app type" field. An earlier version of this table carried one marked
`TBC, re-verify`, inferred from the archetype; the upload wizard has no such step, and the
deployment shape follows from the detected template instead.

**Visibility is load-bearing, not cosmetic.** The authorisation design reads the platform's
role header, and on a PUBLIC app all three identity headers are always empty, so role-based
behaviour is impossible there. If this app is ever published publicly the role gate stops
applying and the team token becomes the only gate. See `docs/APPSTORE-REDESIGN.md`.

### The copy

Every clause traces to `README.md` or `CLAUDE.md`; nothing here is a new claim about the
product, a capability, a customer, an organisation or a figure. Both fit the platform's caps,
which an earlier draft did not: it ran to about 150 characters against a short-description
limit of 80, and its full description to 600 against 500.

**Short description** (79 characters)

> Explainable, confidence-tiered satellite threat scoring for Protect and Defend.

**Full description** (497 characters)

> Pree scores how threatening a candidate object is to a protected asset, and shows its working. Approach geometry, relative velocity, manoeuvre cadence against the object's own baseline, photometric departure and radio-frequency activity combine into one score, each with its own reason.
>
> Score and confidence are reported separately: the score says how threatening the evidence is, the tier says how much evidence there was. A missing indicator is excluded and reported as unknown, never defaulted.

Two questions an operator will ask that the copy deliberately does not answer, because neither
is established anywhere in this repository: which UDL instance and classification Pree draws
from, and who the named user community is.

## Environment variables tab

Pree carries its defaults in code, and every value the platform injects lives at the pod
level, so most of this tab stays empty. Three variables are the exception and they are set
**together, from the first release**, not deferred.

An earlier version of this table told the operator to leave the token unset for the first
release and called an empty tab correct. That was wrong, and the app now refuses to start in
that state rather than trusting the instruction. With `PREE_ENV=production` and no token the
gate is open, and the server binds `0.0.0.0`, so anything reaching the ingress could drive the
scoring service. The token remains required at boot even though the platform's role header is
the primary gate, because an empty role header is exactly what a public app sends, and the
token is what stands behind the gate in that case.

| Variable | Console action | Provided by |
|---|---|---|
| `PORT` | **delete this variable** | platform, at the pod level |
| `PREE_ENV` | **COPY-PASTE EXACT:** `production` | operator |
| `PREE_TEAM_TOKEN` | the team token, as a **secret** (see below) | operator |
| `PREE_ALLOWED_ORIGIN` | **COPY-PASTE EXACT:** `https://pree.appstore.bluestaq.com` | operator |
| `PREE_BUILD_ID` | leave unset, or set to the release tag | release process |

`STORAGE_MOUNT_PATH` and `PREE_DATA_DIR` are no longer read at all and are not listed above.
The service holds no state, so neither has an effect; setting either does nothing rather than
misdirecting writes.

Generate the token with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Production refuses a token shorter than 32 characters. It also refuses a token built entirely from a repeated sequence, so a doubled word will not start the app.

All three of the operator-set values go in before the first submission. The app refuses to
start on any unsafe combination: production with no token, a token with no origin, or a token
with `*` as the origin. Each failure is a boot error naming the variable, which is cheaper than
serving a credentialed endpoint to any caller. Save the FULL variable set with `save_env_vars`,
which replaces rather than merges, then `apply_env_vars` to make it live.

## Add-ons

**None.** Every add-on is declined, and the reasons are evidence rather than preference.

| Add-on | Enable | Why |
|---|---|---|
| FILE_STORAGE | no | Pree holds no state. It also could not have held it correctly here: the mount is mountpoint-s3, which has no POSIX advisory locks, so the `flock` the old store relied on does nothing. |
| POSTGRESQL | no | there is nothing to persist. Scoring is a pure function of the request, and assessment history lives in SPECTRE, which has its own database layer. |
| REDIS | no | rate-limit state is per-process by design (see the security policy), and with no shared store there is no mutual exclusion to arrange. |
| CLAMAV | no | Pree accepts no file uploads. |

An earlier version of this table enabled FILE_STORAGE and declined POSTGRESQL on the stated
ground that two gunicorn workers were "serialised by an exclusive `flock`". That reasoning was
wrong about the platform, not merely out of date: the documentation states that `flock` on this
mount "will silently fail or behave inconsistently", so the second worker's write would have
erased the first with nothing reporting it. See `docs/APPSTORE-REDESIGN.md`.

**This removes the operations request this release was blocked on.** With no volume mounted
there is no root-owned mount, so `securityContext.fsGroup` is not needed and the section that
demanded it is gone. Nothing replaces it: there is no pre-deploy action outstanding for
operations.

## Resource budget

| Resource | Request | Limit |
|---|---|---|
| Memory | 256Mi | 1Gi |
| CPU | 250m | 1 |

Inside the platform's per-app envelope of 8Gi memory and 6 CPU, enforced at submit time. Two
gunicorn workers with a 60 second timeout. No add-on is enabled, so the whole budget is this
one service.

The python template defaults to 128Mi in the wizard. 256Mi is requested deliberately: two
workers each load the full application, and a limit that fits one worker produces an OOM kill
under the second, which the platform reports as a deploy failure rather than as a memory
setting.

**No storage budget.** This section used to carry a 5000-record cap, a per-record size and a
10 MiB steady state, all of which existed to bound a volume that is no longer mounted. The
figures were correct and are now simply about nothing; they were removed rather than left to
be read as a live constraint.

## Health paths

The complete list of unauthenticated paths in production. Nothing else answers without either
a platform role or the token.

| Path | Behaviour |
|---|---|
| `/`, `/healthz`, `/readyz`, `/livez`, `/ping` | 200, touch nothing, cannot hang |

Set the wizard's **health check path** to `/healthz`. The container `HEALTHCHECK` probes the
same path.

`/openapi.json`, `/docs` and `/redoc` are **not served in production**. They exist in
development only, because they publish the whole route table and the token header name, and
`/docs` loads a third-party script from a content delivery network onto the app origin.

The liveness paths are exempt from rate limiting, so rejected traffic cannot drive the
container HEALTHCHECK red and restart the pod. Every response, including a 401, a 413 and a
429, carries `Content-Security-Policy: default-src 'none'`, `X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and
`Cross-Origin-Opener-Policy: same-origin`.

`GET /` returns 200 and never a 302, because the platform router probes the root.

**`/healthz/storage` is gone, and so is everything this section used to say about it.** It was
the container HEALTHCHECK target and the only path that touched storage: a real write racing a
1.5 second timeout, with callers joining the probe already in flight, a two-second cached
verdict and a 503 naming the resolved directory and errno. All of it existed to prove a mount
that is no longer attached. Nothing replaces it, because a stateless service has no readiness
condition beyond being able to answer.

That removes a diagnosis, which is worth stating rather than glossing: a storage fault used to
be a one-screenshot answer. There is no storage fault to diagnose now.

`/diagnostics` is a secret-free read-out: every critical input as a boolean and a length, never
a value, plus the resolved identity, with every field present at once. It no longer reports any
storage or data-directory field, and a test asserts their absence so a stale field cannot
invite an operator to diagnose a fault that cannot occur. It needs a VIEWER role or the token.

## Authorisation

The platform signs people in before a request reaches a private app and sets three headers,
replacing any copy the client sent. Pree reads two of them.

| Route | Minimum |
|---|---|
| the five liveness paths | none; unauthenticated by contract |
| `GET /diagnostics` | `VIEWER`, or the team token |
| `POST /v1/assess` | `EDITOR`, or the team token |

Roles are compared by rank, never by name. An unrecognised role ranks as `VIEWER`, so a role
the platform adds later arrives with read access at most. An **empty** role is anonymous and
below every gate, which is what a public app sends on every request.

A present-but-insufficient role is refused outright and never falls back to the token,
otherwise any `VIEWER` holding the shared secret could write.

Only `X-Auth-Request-Role` and `X-Auth-Request-Sub` are read. A header the platform does not
set passes through from the client unchanged, so reading any other `X-Auth-Request-*` header
would be reading attacker-supplied data under an authoritative-looking name.

The audit trail's `actor` is `X-Auth-Request-Sub`. It was a caller-supplied header until this
release, which meant every audit line named whatever string the caller typed.

## Rollback

There is no previous release to roll back to. This is the first delivery, stated plainly rather
than implied. The rollback path once a second release exists is `resubmit_app` with the previous
upload archive, which is why dated delivery copies of the archive are kept.
