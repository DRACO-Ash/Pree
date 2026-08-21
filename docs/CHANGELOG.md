# Changelog

## V0.1 (unreleased)

First delivery. Scaffolds Pree as a Bluestaq App Store `python` container app against the
Foundations baseline.

● The scoring core: an explainable, confidence-tiered assessment over five weighted
  indicators, where an absent indicator is excluded from the weighting and marked
  `TBC, re-verify` rather than defaulted to a number.
● The HTTP surface: a `create_app` factory, the five conventional health paths, a separate
  storage proof, a secret-free diagnostics read-out, and the gated scoring and read routes.
● The security posture: constant-time token compare, fail-closed boot on an unsafe token and
  origin pairing, boundary validation, two-tier rate limiting, and sanitised audit lines.
● The store: atomic writes with a backup, forward migration, and merges that never shrink.
● The container: hash-locked install in a build stage, a prep stage whose last mutation is the
  suid and sgid sweep, and a flattened `FROM scratch` ship stage.
● The loop: `ruff`, `mypy` over source and tests, `pytest` with a Cobertura report, and
  `pip-audit`, at 99% coverage against a gate bar of 80%. The test count moves with every
  round, so it is reported by the loop rather than pinned here where it goes stale.

Ten defects found by the binding engineering and security gates on first review are fixed in
this release, each with a named regression test:

● Production booted with the authentication gate open, and the deployment table instructed the
  operator into exactly that state. Production now refuses to start without a token.
● A refused write moved the live snapshot aside before writing, so a full volume destroyed the
  dataset and it read back as empty. The replacement is now written first and swapped in
  atomically, with a backup fallback on read.
● Two workers lost writes to an unsynchronised read-modify-write. Serialised by an exclusive
  lock across the whole operation.
● The packaging script matched banned entries as substrings, so `.env` hit `.env.example` and
  no archive could be built. The pipeline simulation had therefore never run to completion.
● `.gitignore` was absent from the archive while a test asserted on it, which would have failed
  the platform's test stage and killed the deploy.
● The container HEALTHCHECK path was rate limited, so rejected traffic could restart the pod.
● Request bodies had no cap, and the framework buffers the whole body before the token gate.
● A non-finite number turned a boundary rejection into a 500 that echoed the caller's input.
● The fine rate-limit tier was keyed on a caller-supplied header, so a fresh label per request
  bypassed it. Both tiers now key on the peer address.
● A corrupt snapshot raised through module import, so the pod never bound and left no
  diagnosable surface at all.

### Second gate round

Both binding gates failed the first fix pass. Three of their findings were regressions the
fixes themselves introduced, which is recorded rather than smoothed over:

● The storage probe never released its worker slot after a timeout, so a volume that was slow
  but healthy pinned the pool at capacity and the container restarted in a loop. The slot is
  now freed by the worker whenever it finishes, and results are cached briefly so concurrent
  callers cost one write.
● The new rate-limit eviction policy failed open: once the key table saturated, the key being
  counted was the only evictable bucket, so it evicted itself and was admitted without bound.
  It now excludes that key and denies when nothing is evictable.
● The production auth refusal was conditional on `PREE_ENV`, which defaulted to `development`.
  The default is now `production`, so one forgotten console variable cannot reopen the gate.

Two controls had never existed and were added:

● The interactive documentation was served in production, publishing the route table and the
  token header name, and `/docs` loaded a floating-tag script from a content delivery network
  onto the app origin. It is development-only now.
● No response carried a Content-Security-Policy or any hardening header. Every response now
  carries a locked policy plus nosniff, frame denial, no-referrer and an opener policy.

Also fixed: four store call sites still raised bare `OSError`, so the documented first-deploy
mount failure returned a framework 500 with no audit line; an unreadable primary snapshot now
recovers from the backup; the allowed origin must be a concrete origin in any environment,
rejecting `null` and lists as well as `*`; `apt` and `dpkg` are stripped from the runtime
image; and boot is no longer an import side effect, which means a configuration error is a
logged refusal rather than a worker that dies before it can say why.

Verified without a Docker daemon: the exact launch command boots under gunicorn with two
uvicorn workers, binds `0.0.0.0`, answers 200 at `/`, hides OpenAPI in production and emits
every hardening header; and the store's file lock holds across four real worker processes,
with 30 of 30 concurrent writes surviving.

### Third gate round

Two further regressions from the second round of fixes, and three holes the tests had left:

● Reporting a saturated probe pool as busy rather than unready meant a permanently wedged
  mount answered 200 from its third probe onward, forever. The container health check never
  reached three consecutive failures, so a pod with completely unavailable storage stayed in
  service. A pool whose every slot has already overrun its timeout is now reported as a wedged
  mount; only slots still inside their timeout count as busy.
● The unreadable-primary recovery destroyed its own safety net: the next write copied the
  corrupt primary over the good backup, so a second corruption was unrecoverable. The backup is
  now refreshed only from a primary that parses.
● `fcntl.flock` was the one filesystem call still outside the store's error wrap, so a refused
  lock produced a framework 500 with no audit line.
● The cache-expiry check was asserted nowhere; deleting it froze the readiness signal for the
  life of the worker. The clock is injected now and two tests cover expiry.
● The Dockerfile launch target and the module factory were not tied together by any test, so
  reverting the target left the suite green while the container could not start at all.

### Fifth gate round

The engineering gate passed the change with four minor items, fixed here rather than banked:
the masking test asserted only that some `StoreError` surfaced, so inverting which error won
left it green; the middleware count in a comment ignored that the CORS registration is
conditional; the test count above was stale at 184; and this file gained no row for the
previous commit, which the project's own convention requires. No behavioural change.

### Third security review

One required control that had never existed, and one fail-open the previous round narrowed
rather than closed:

● The assessment collection had no cap and no pruning. Every upsert rewrites the whole
  snapshot, so retention set both the volume ceiling and the per-write cost, and a token
  holder at the per-address rate limit added tens of megabytes a day until the volume filled
  and the pod went out of service. Capped at 5000, oldest dropped first, the record just
  written never dropped, ordered by an explicit write-order list because the snapshot is
  serialised with sorted keys and object order does not survive the round trip.
● The busy-versus-wedged rule required every held slot to have overrun, so an unauthenticated
  flood of the unmetered probe path kept the newest slot always fresh and a mount whose writes
  overran the probe budget reported ready indefinitely. Measured at one 503 in twelve probes
  under an eight-way flood, against three consecutive needed. A busy verdict now requires
  positive evidence of a completed probe, verified in all four directions so the fix does not
  reintroduce a false unready.

Also: a configured data directory must be absolute and is checked before resolution, because
resolve() makes every value absolute and the check could therefore never fail; a quote-wrapped
pasted path is normalised rather than becoming a literal directory inside the container; a
control character in any value is refused; the boot line reports auth state and token length
so a stale token is visible without a deploy cycle; the CSP exemption for the development docs
is now test-enforced against production; and Retry-After never returns zero.

### Fourth security review

The probe fix from the previous round was wrong in the opposite direction, and the review
caught it at the shipped parameters:

● Making a busy verdict depend on a recent successful probe left a window, because the result
  cache expired one instant before that grace did. An unauthenticated flood of the unmetered
  probe path could hold it open and drive a healthy mount to 503: four false 503s in
  twenty-four probes at 300 ms write latency, seven at 1.2 s, with four consecutive, which is
  enough to restart the pod. The guard test used a grace fifteen times the shipped value, so it
  never exercised the real configuration, and the claim recorded in the security policy that
  all four directions had been verified was sampling luck rather than verification.
● The heuristic is gone. Concurrent callers join the probe already in flight and report what it
  reports, so a joiner cannot be wrong about the volume and one write serves every caller.
● Measuring that fix surfaced a further defect it would otherwise have shipped with: a joiner
  arriving just after a slow write finally landed reported ready and cached it, so an
  over-budget mount answered ready in five of twenty-four probes. A write that misses its
  budget is now unready for every caller.
● The retention cap's bound held only while the snapshot's write order covered every stored key
  exactly once. Pruning stale names enforced one half; a snapshot whose order omitted keys
  retained far more than the cap and then evicted each new write instead of the oldest record.
  Both directions are enforced on read now, and duplicates are collapsed.
● The register now states that the cap is shared across everyone holding the team token rather
  than per operator, and the deployment sheet publishes the measured per-write cost beside it.

### Fifth security review

Two majors, both failures of evidence rather than exploitable code, plus five residual defects:

● A test passed with the control it was named after deleted. Its second call started a fresh
  over-budget write whose timeout came through the ordinary path, so an implementation with no
  over-budget guard satisfied it identically. Deleted; the register now cites the stub-driven
  test that actually kills that mutation.
● The deployment sheet still told the operator that a busy probe pool returns 200 with status
  "unknown" and errno EBUSY, "never 503", for the endpoint that gates pod restarts, two commits
  after that behaviour was deleted. A register row asserted the same retired control and cited
  a test deleted with it.
● The budget was measured against the observer's clock rather than the write's own duration, so
  a descheduled request charged its delay to the mount. The worker reports its duration now.
● The cache was stamped at observation time, making worst-case staleness the window plus the
  write latency. It is stamped from the probe start, and the real bound is published.
● A non-object stored value raised ValueError past the store's error contract into a framework
  500 with no audit line.
● The liveness routes were sync handlers sharing the request threadpool with probe callers.
● A bare Any on the executor silenced type checking on every call to it.

Mutation testing then found a third instance of the same shape the review had named twice:
reverting the cache stamp left the whole health suite green. That fix now has a test that
distinguishes it.

### Sixth security review

One major and four minors. The reviewer could not make the probe lie in either direction under
a 40-way flood across six mount conditions, could not make a joiner disagree with its owner,
and confirmed single-flight holds at one concurrent write under load. It also accepted, with
measurements, the decision to leave the storage-probe route synchronous rather than duplicate
the verdict logic in an async path.

● The major was the fourth instance of the recurring shape: making the liveness handlers sync
  again left all 217 tests green, while measuring a 500x liveness latency regression and a 269x
  throughput collapse under a 120-way flood, which is enough to restart a healthy pod. A
  structural test now asserts those handlers are coroutines.
● RecursionError is a RuntimeError, so a deeply nested snapshot escaped the store's error
  contract entirely: a framework 500 with no hardening headers and no audit line, falsifying
  three register rows at once.
● The register-citation guard matched bare test names only, so a row citing a test FILE went
  unchecked and a row citing nothing at all was invisible. Three fabricated rows passed it.
● The deployment-sheet guard was a three-token denylist wearing the name of a property, so the
  sheet could drift in any new direction. The allowed states are derived from the code now.
● This changelog reported a stale test count and had recorded neither of the last two reviews.

### Seventh security review

Three majors, all in the guards the previous two rounds added rather than in the code they
protect, which held every attack the round ran:

● The liveness guard derived its expectation from the constant it was meant to police. Deleting
  "/readyz" from that constant left the whole suite green while the route returned 404, and the
  path silently left the rate-limit exemption with nothing turning red. The five documented
  paths are pinned as literals now, each required to exist and to be served by a coroutine.
● The register guard was defeated six ways and the deployment-sheet guard six more, including
  fabrications that render as ordinary rows and prose a reader would believe. Both are rebuilt
  to fail on anything they cannot check rather than skip it, and all twelve fabrications are
  now caught. The rebuilt register guard immediately found two rows citing test names that had
  been renamed without the register following.
● Production accepted a one-character team token. At 240 attempts a minute per address per
  worker a dictionary of common choices sits well inside an hour, so production now refuses
  anything shorter than 24 characters and the sheet names the generation command.

Also corrected: this policy claimed every control was mutation-proven on the strength of a
partial sample, which is the evidence inflation the surrounding paragraph apologises for.
