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
● Production accepted a single-character team token. The coarse limiter admits enough attempts
  an hour that a dictionary of common choices sits well inside one, so production now refuses
  any token shorter than 32 characters, or built entirely from a repeated sequence, and the
  sheet names the generation command.

Also corrected: this policy claimed every control was mutation-proven on the strength of a
partial sample, which is the evidence inflation the surrounding paragraph apologises for.

Tenth security review, three guards rebuilt and one document defect of my own:

● Every Dockerfile assertion was a substring search. A BuildKit heredoc body read as a build
  stage, so a decoy `FROM scratch` block satisfied all six resolved-state assertions while the
  real stage ran `USER root` and bound the loopback interface only; an exec-form `ENTRYPOINT`
  turned the gunicorn command line into arguments for something else. The file is now parsed
  into stage, keyword and argument triples, heredocs and unknown keywords are refused rather
  than skipped, and both defeats now turn the suite red.
● The Sonar check read a line, not the resolved property, so `sonar.sources=.` appended below
  the correct line scanned the whole checkout with the assertion green.
● The audit line for a rejected body was bounded only by the body cap. Five 5,000-character
  field names inside a 25 KiB body wrote a 25,422-byte log record, so filling the log volume
  was cheaper than filling the data volume and needed no token. Field names and error counts
  are now truncated and capped.
● The packaging script refused five directory names and nothing by shape. It now fails on any
  archived path ending in a key or certificate extension, or reading like a credential.
● The token floor moved to 32 characters but only `docs/SECURITY.md` followed, while my commit
  message claimed all three places had. `docs/DEPLOYMENT.md` still described a rule the code no
  longer has. Both now state the enforced number, and a guard reads the constant from the
  source so the prose cannot drift from it again.

Eleventh security review, three majors and four overstated claims of mine:

● Round ten bounded the audit line for a rejected body and left the request PATH unbounded, one
  function higher in the same file. A path needs no upload and no valid token: a
  15,000-character request line wrote a 30,074-byte 401 record. All three handlers now truncate
  the path and the reason.
● The suid and sgid sweep was asserted to exist in some build stage, and the shipped layer to
  come from some build stage, with nothing joining the two. Moving the sweep into `build`
  shipped every setuid binary the base image carries with the suite green. The guard now reads
  the shipped COPY's `--from=` and requires every hardening step to run in that stage.
● The ten-error cap on the logged error list was tested nowhere, because the test sent five
  field names. Deleting it wrote a 142,290-byte record. The test now exceeds the cap.
● A `# escape=` parser directive was invisible to the Dockerfile parser and honoured by docker,
  leaving the resolved user as root. Unknown parser directives are refused like heredocs.
● The packaging scan named no certificate extension, followed symlinks so a link could ship a
  private key's contents under a harmless name, and missed `.netrc`, `authorized_keys` and
  `creds.txt`. It now stores links as links, refuses them, and states that it reads names only.
● The token-floor guard caught one number form of five. Fragments are built per line so a table
  row stays whole, and the retired-rule check runs before the floor gate.
● The shipped `MAX_ASSESSMENTS` value was unpinned, because every cap test monkeypatched it.
  It is now asserted against the sheet and the volume footprint.

Twelfth security review, four majors, one of them in the application:

● The rate limiter's eviction pass was not thread-safe, and the fine limiter is concurrent
  because the scoring handler is synchronous and runs in the thread pool. Measured at 375
  exceptions in 4,000 concurrent calls, each a 500 with no audit line and no hardening headers
  in place of a 429. The bookkeeping is serialised and each delete tolerates a missing key.
● `--access-logfile -` reinstated the log-amplification defect through a channel the app does
  not own: 15,046 bytes per unauthenticated request on an unmetered path, and 31 MB written by
  a three-second burst. A truncating filter attached by the factory brings that to 208 bytes,
  measured under the shipped launch command.
● The suid sweep and the pip removal were asserted by substring, so `find /app`, a leading
  `-false`, a trailing `|| true` and a removal aimed at a nonexistent path all passed. The
  resolved commands are now asserted.
● `# escape = ` with spaces around the equals sign reopened the hole the previous round closed.
  The check now uses BuildKit's own directive pattern.
● The logged-path bound was shorter than the longest legitimate path, so a real store key was
  truncated out of every 401 and 503 record.
● The packaging scan anchored its extension list on the whole path, so `deploy.key.txt` and
  `tls.pem/server.bundle` shipped a private key; it now matches on path components and refuses
  hard links and non-ASCII names.
● The token-floor guard was inverted: every figure attached to a size word in a line mentioning
  the token must be the enforced constant, across six files.
● The sheet's per-record figure was a best case published as a planning number. Corrected to
  1705 bytes for a maximum-length record, 8.1 MiB at the cap, with the volume request stated.
● A 5,000-digit integer escaped the app's error contract, and the two rate-limit tiers
  disagreed about their response body. One contract now covers every rejection, each audited.
● Found by coverage rather than by the review: `logging` sets `record.args` to an empty tuple,
  not None, so the access filter's pre-formatted branch was unreachable and gunicorn's own
  access line went out whole.
● Found by re-running the pipeline simulation: the platform runs the suite from the extracted
  archive, which has no `.git`, so the behavioural ignore-rule guard failed there. It now skips
  where nothing can be committed, and no longer asserts a belief about the runner that the
  simulation itself disproves.

Thirteenth security review, two majors, the first defeated live against a running server:

● **Both rate-limit tiers were bypassable with a caller-chosen `X-Forwarded-For`.** uvicorn
  installs its proxy-header middleware unconditionally and gunicorn's trust list defaults to
  loopback plus `FORWARDED_ALLOW_IPS`, so the peer address both tiers key on was replaced before
  the app ran. Measured: 300 requests with a rotating header all admitted where 60 are refused
  once pinned, and 60 of 60 writes accepted against a limit of 20. The launch command now pins
  `--forwarded-allow-ips=255.255.255.255`, and an explicit flag beats the environment default,
  so `FORWARDED_ALLOW_IPS=*` cannot reopen it. Accepted risk 4 claimed no forwarded header was
  trusted; that was false as shipped and is corrected in place rather than quietly edited.
● The suid sweep guard was a denylist against `find`'s open-ended predicate grammar, and four
  more neutering forms passed it. The command is now asserted literally.
● Nothing tied the access-log filter to the app: removing the factory's call left the suite
  green. The filter also did not bound the mapping shape gunicorn's own access logger emits,
  measured at 15,056 bytes untruncated.
● `retry_after_seconds` read the shared deque outside the lock added one function above it.
● The error handler returned a JSON body for statuses that must not carry one.
● Five more credential-shaped paths shipped: the word list had `passwd` but not `password`, and
  `key` only as a dotted extension.
● The store's per-record figure was asserted equal to itself. The test now measures the largest
  record the scorer can emit and requires the published figure to be at least that.

Fourteenth security review, two majors:

● **A pre-auth 307 handed the team token to a caller-named host over cleartext.** Starlette
  redirects a trailing slash before any dependency runs, so `POST /v1/assess/` answered with an
  absolute Location built from the caller's own Host header. A 307 preserves method, body and
  headers, so a client that follows it re-sends the token; measured with `Host: attacker.test`,
  the Location was `http://attacker.test/v1/assess`. Pinning the forwarded trust list last round
  removed the only thing keeping that redirect on TLS. Slash redirects are off.
● The sweep guard was defeated with the guarded line untouched: `SHELL ["/bin/true"]` changes how
  every later RUN executes, and `COPY --from=prep --chmod=0777 --chown=0:0 / /` ships the
  filesystem world-writable and root-owned. SHELL is refused; the copy's every token is asserted.
● The token-floor guard is inverted after four rewrites: in any line pairing the token with a
  size, the enforced constant must appear and no other number may. Twenty fabrications turn it
  red. One changelog sentence was split in two to satisfy it, which is the rule's real cost.
● The packaging scan is now an extension allowlist AND a name denylist. It had been a denylist
  for three rounds and shipped a real private key each time under a name one character outside
  the list; replacing it with an allowlist alone shipped five more.
● The coarse limiter's key space is split by whether a token was presented, so an unauthenticated
  flood cannot consume the operators' budget at the shared platform ingress.
● `_client_key` folds any request carrying a forwarding header into one key, so the control
  survives a launch command the platform supplies rather than living entirely in a flag.
● A request declaring both a Transfer-Encoding and a Content-Length is refused, connection closed.
● Every base image is asserted pinned by digest; "the base digest is pinned" had been a comment.
● The volume figure now searches the indicator value space, finds 1722 bytes, and the sheet
  publishes 2048 as a ceiling with headroom. Both numbers are asserted.
● Corrected: the previous round's rate-limit figures were measured at one worker while the
  shipped command runs two. Restated at two workers with a third arm: 0 of 1,000 refused with
  neither control, 615 with the shipped build.

Fifteenth security review, three majors, two of them controls added in the previous two rounds
that did not do what their commit messages said:

● The Transfer-Encoding plus Content-Length refusal sat AFTER the bodyless-method early return,
  so it never ran for GET, HEAD, OPTIONS, DELETE or TRACE, which is the method class smuggling
  uses. One socket write of `GET /healthz` with both framings produced two 200 responses. It now
  runs first, verified against the running server on every bodyless method.
● The rate-limit key space was caller-selected two ways: the authenticated split was decided on
  the token header's presence rather than its validity, and the forwarding-header fold put a
  request in a different bucket from the peer's own, so a throttled caller escaped by adding a
  header. One peer reached four buckets and 1,920 requests were admitted against a nominal 480.
  The split now uses the constant-time compare, and a request is charged to every key it
  belongs to. Re-measured at two workers across five arms: 480 admitted in total.
● The suid sweep guard was defeated for the fourth consecutive round, by `ENV PATH` with a no-op
  `find` planted, and by one line copying `/bin/true` over `/usr/bin/find`. Pinning what a
  command says cannot establish what it does. The sweep now names both binaries absolutely, a
  write into any system executable directory is refused, and the pipeline asserts the three
  container hard rules against the BUILT IMAGE. Those assertions need a daemon, so the rules
  stay unverified here, which the simulation now says in its own output.
● The token-floor guard was rewritten for the fifth time. Four more forms beat it; fixing the
  number-word table by construction then flagged fourteen true sentences, because this policy
  narrates the guard's own history in measurements. It now covers the instruction-bearing files
  completely and does not cover the policy or the changelog, a gap chosen deliberately.
● The packaging scan lost for a fifth round to names one character outside its list, among them
  `deploykey.txt` and `id-rsa.md`. Any component ending in "key" is now refused.
● The unauthenticated storage probe published the resolved data directory in its 200 body.
● A successful read of the assessment store wrote no audit line, the one privileged action with
  no record. Every read is now audited with its outcome.
● A refused CORS preflight was answered in plain text with no audit line, outside the contract.
● `HEALTHCHECK NONE`, `VOLUME` and `STOPSIGNAL` in the shipped stage all passed silently.
● A `_FLOOR_PHRASES` constant described a rule the guard did not implement; deleted.

Sixteenth security review, seven majors:

● The framing refusal still missed every CORS preflight: Starlette answers one inside the CORS
  middleware without calling down, and CORS sits above the body-size layer where the check
  lived. Measured 200 OK and two responses on one connection with a pipelined GET served. The
  check now lives in `FrameGuard`, registered outermost, with a test asserting the position.
● The layer that normalises a refused preflight rewrote the 400 and dropped its
  `Connection: close`, so the trailing bytes of an ambiguous frame could still be replayed.
● Choosing the rate bucket by the token's validity made refusal an oracle: after saturating the
  unauthenticated bucket, a wrong guess returned 429 and the right token 200. Measured 2,666
  distinguishable guesses in three seconds, about 53,000 a minute, against the 240 a minute the
  token-length floor is calculated from. A peer now gets twenty wrong tokens per window, and once
  spent every token-bearing request is refused whether right or wrong.
● Refused preflights were never metered, because CORS answers them above the coarse limiter:
  902,000 bytes of log in 1.6 seconds, about 32.9 MB a minute per worker, unauthenticated.
● The suid sweep guard was defeated for the fifth round: `WORKDIR /usr/bin` with a relative COPY
  destination, and the JSON-form COPY. Destinations are now resolved against the WORKDIR.
● `ADD https://…` replacing the application source, and `ENV PATH=` in the shipped stage
  hijacking the binaries the pinned CMD resolves, both passed. Both are refused.
● The behavioural image check added last round reported a pass without running: every assertion
  read a failed `docker run` as empty output, and both used `--entrypoint /usr/bin/find`, the
  very binary the mutation replaces. It now exports the filesystem with `docker export`, reads
  modes from the host, checks exit status, and asserts a positive control first.
● `GET /diagnostics?x-pree-token=<token>` wrote the token into the access log in cleartext.
● The read path's comment claimed a path validator that did not exist. It exists and is asserted.
● `docs/.env.example` with a live-looking token shipped: the packaging exemption matched the
  basename anywhere while the placeholder test read only the root path.
● Three packaging nets read a tool failure as a pass, one using `grep -P`, absent from BusyBox.
● The base digest lived in an `ARG`, so `--build-arg` could swap it past the pinning guard.
● Production could run with credentials against an `http://` origin. Non-https is refused.
● The `cors_reject` audit misstated its cause for every 400 on a preflight.

Seventeenth security review, four majors:

● The guessing budget added last round was an unauthenticated denial of service against every
  operator: twenty wrong tokens from anywhere locked out the whole watch floor for the window,
  because behind the ingress they all present one address. The fix is a deletion. There is one
  rate-limit key space, the token plays no part in choosing it, and three rounds of splitting it
  are recorded in accepted risk 4 along with the residual.
● The behavioural image check failed for the second round running: the pip pattern was anchored
  on `(^|/)opt/`, but `docker export` writes member names relative and `tar -tv` puts the mode
  first, so it could not match a single line and printed its success message every run. It now
  reads the last field, covers `site-packages/pip`, and has three positive controls.
● The suid sweep guard fell for the sixth round, to one redundant character: `//usr/bin/find`,
  `/usr//bin/find`, `WORKDIR /usr/./bin`, and `ENV`-substituted destinations. Destinations are
  slash-collapsed and normalised, and a destination containing a variable is refused.
● Preflight metering skipped the six exempt paths while still auditing them: 8.2 MB of log a
  minute per worker, unauthenticated. Every preflight is metered now; probe paths get the
  contract without the audit line.
● The token guard's docstring claimed a property it does not have. The number side needs no
  table; the size-word side is still a denylist, and that is now written down.
● Four packaging checks re-ran `unzip` inside a pipeline whose failure reads as a pass.
● The setuid parse had no positive control; `RateLimiter.spent` grew the table from questions;
  a non-preflight `OPTIONS` was metered twice; and the image listing leaked to the system temp
  directory on failure paths.

Eighteenth security review, three majors, all in controls already repaired once:

● The rate-limit escape returned through one string: the socket key was `socket:<ip>` with a
  forwarding header and a bare `<ip>` without, two different keys, so a saturated caller added
  any forwarding header and got a fresh bucket. Measured 240 then 240 more on the coarse tier
  and 20 authenticated writes then 20 more. The socket key is unconditional now.
● The unmetered-audit amplification returned through the method: the probe-path exemption
  covered any verb, so a DELETE got a router 405 and a full audit line uncounted. 4,000
  requests, none refused, 8.1 MB of log a minute per worker. The exemption is for the probe now,
  not the path, the 405 is not audited, and HEAD is a liveness method rather than a 405.
● The suid sweep guard fell for the seventh round, to the simplest attack nobody had tried:
  `RUN cp /bin/true /usr/bin/find`, which the guard never considered. Also a DIRECTORY
  destination, since normpath strips the trailing slash; `WORKDIR $D` from an ARG; and a local
  tar `ADD`, which docker detects by content. ADD is now refused outright.
● The packaging key rule omitted `/` from its delimiter class, so `docs/keys/prod.txt` shipped.
● The image listing's name scan read a `tar -tv` line's last field, which is the link TARGET,
  so every symlink and hard link was invisible, including to the positive control added one
  commit earlier. The mode count also excluded the hard-link type.
● `_TOKEN_TERMS` omitted "key", after "secret" and "passphrase" had been added for the same
  reason. Both that table and the size-word table are now named as residuals.
● An orphan comment described the guessing budget this project deleted.

Nineteenth security review, two majors:

● The RUN-write guard added one round earlier was gated on a six-verb denylist, and three
  one-line mutations walked through it: shell redirection needs no verb, `tar -C` uses one that
  was not listed, and `python -c open(...)` names the target literally. The denylist is gone: any
  RUN mentioning an executable directory is an offence and the five legitimate ones are pinned by
  exact text. Nine fabrications now turn it red.
● **A claim in `docs/SECURITY.md` was false.** It named `src/pree/keyring/x.py` among paths the
  packaging fix had closed; the rule still required a delimiter after "key", so `keyring` was
  open along with `keystore.json`, `keyfile.txt`, `keychain.py`, `keypair.txt` and
  `sshkeygen.sh`. My verification had reported that path refused because a leftover directory
  from the previous test iteration made a different net fire. The rule is the plain substring
  now, and the paragraph is corrected in place rather than reworded.
● The suppressed 405 dropped `Allow`, which RFC 9110 makes a MUST. The nineteenth-round entry
  recorded this as a non-reproduction and that was WRONG: the measurement removed the argument
  from the other branch, which serves `/diagnostics`, and then measured the probe paths. See the
  twentieth-round entry.
● HEAD was exempt on `/healthz/storage`, which serves only GET, so an unmetered 405 that can
  never be a platform probe: 600 of 600 admitted. The exemption is per path and per method now.
● GET and HEAD on one route gave both the same OpenAPI operation id, so the development document
  was invalid and the loop carried a warning every run. HEAD is its own route.
● The base digest was asserted to exist rather than to be a value: one character shipped a
  different filesystem with the suite green. It is pinned as a literal in both stages.
● The token guard's unit builder used adjacent-line pairs and lost to a three-line split. It
  builds sentences now, and the remaining pronoun-split residual is documented rather than
  chased, because pairing sentences flagged three true statements.

Twentieth security review, one blocker and three majors:

● **A recorded non-reproduction was wrong.** The suppressed 405 DOES drop `Allow` without
  `headers=exc.headers`, on all six probe paths, and the test does turn red. My measurement had
  mutated the adjacent branch. Corrected in the code comment, the test docstring, the policy and
  this file; the test now asserts the exact `Allow` set.
● BLOCKER: BuildKit treats a line ending in an ESCAPED backslash as complete, this parser treated
  any trailing backslash as a continuation, so `LABEL …=pree\` followed by `USER root` swallowed
  the USER and the container ran as root with the suite green. The rule is BuildKit's own now.
● Continuation lines were joined with a space where BuildKit joins with nothing, so a path split
  mid-token across a continuation resolved harmlessly here and to the real target for docker.
● The RUN branch read literals only, so a glob, a shell variable and a command substitution each
  reached `/usr/bin/find`. It refuses `$`, backticks, `?` and brackets now.
● Splitting GET and HEAD into two routes made a liveness path advertise `Allow: GET` while the
  resource serves HEAD. One route with both methods, out of the development schema.
● The sentence splitter cut at `e.g.`; abbreviations are shielded. A sentence introducing a table
  now pairs with the row carrying the number, when it ends in a colon.
● `register_cors` carried a parameter it never called.

Twenty-first security review, one blocker, four majors and five minors, every one of them in the
layer written to prove the application's boundaries hold rather than in a boundary itself:

● BLOCKER: the round-twenty parser fix was reopened one byte to the side. `str.splitlines()`
  breaks a line at VT, FF, 0x1c-0x1e, NEL and U+2028, and a blanket `rstrip()` also strips NBSP
  and the Unicode spaces, where BuildKit splits on `\n` alone and trims `\r\n` alone. So
  `LABEL org.opencontainers.image.title=pree\<0x0b>` above `USER root` was a continuation to
  this parser and two complete instructions to docker: 292 tests green, shipped user root, and a
  0x0b invisible in an editor and in a diff. The parser's line model is BuildKit's now, and ten
  whitespace carriers plus three padded continuations and a CRLF file are asserted directly.
● The opaque-character refusal omitted `*`, the most natural glob of the set:
  `RUN cp /usr/b*n/true /usr/b*n/find` passed 292 of 292 and left every setuid and setgid bit in
  the base image shipped. `/usr/b?n/find` was refused and `/usr/b*n/find` was not, the same
  attack one metacharacter over. `*`, `{` and `}` are refused now, and the comment claiming the
  refusal had a cost was wrong: a vetted RUN never reaches that loop.
● The test added last round to prove that refusal works asserted only that its own four string
  constants contained a refused character, a tautology over literals. Deleting the refusal
  outright left 292 tests green. The guard is a callable helper now, fed synthetic Dockerfiles,
  and thirteen fabrications turn it red.
● The parser-directive guard checked a directive's NAME and never its VALUE, so
  `# syntax=attacker.example/evil-frontend:latest` passed the whole suite. A syntax value is a
  build frontend image: BuildKit pulls it, hands it this file and the whole build context, and it
  may emit any image at all, which would make every assertion in the boot contract a statement
  about a document nothing executes. The shipped `docker/dockerfile:1` was also a floating tag in
  a file that pins its bases by digest. Nothing here needs a BuildKit-only feature, so the
  directive is gone from the Dockerfile and every directive is refused.
● Nothing walked the route table. `@app.post("/v1/debug")` returning the team token,
  unauthenticated, passed 292 of 292 and did not trip the coverage floor: one line to send the
  shared credential to any client on the internet with a green gate. Three tests now walk
  `app.routes` - the gate by introspection, the gate by asking, and the token in no body or log
  line - and the `Allow` expectation is derived from the table instead of a hand-written map over
  five of the nine paths.
● The claim-unit splitter raised `UnboundLocalError` on any document opening with a table row,
  and its colon pairing reached a table row and nothing else, so a wrong token floor written as a
  `●` bullet, a fenced block or a heading-then-row passed. The house style bullets with `●`,
  which made the missed shape the likeliest one. The pairing crosses three units now and all four
  shapes turn it red, with the splitter tested directly on synthetic documents.
● `scripts/package-appstore.sh` wrote the archive before any check ran, so every refusal exited 1
  with the rejected zip sitting at the path the script tells a human to upload. It stages to a
  partial path and moves into place only after the last check. The script had no automated test
  at all after six rounds of findings; it has three now.
● Two comments in `src/pree/app.py` claimed an explicit operation id the code does not pass, and
  two labelled different middleware layers "second-outermost". Only one of those labels was
  wrong, the CORS one; the framing guard is second-outermost and its label was correct.

Twenty-second review, three majors and four minors, all in the round-21 controls themselves:

● The route walk added last round filtered `isinstance(route, APIRoute)`, so the control written
  to make routes visible was blind to every other registration mechanism.
  `app.add_route("/v1/debug", …)` served the team token to an unauthenticated caller with 300 of
  300 green, and so did `app.mount("/admin", …)`; a websocket route carries a path and no methods
  at all. Any route the suite cannot read the gate from is now refused outright, the way the boot
  contract refuses a heredoc rather than parsing it, with the documentation paths exempt by path
  rather than by type and asserted absent in production.
● `UNAUTHENTICATED_PATHS` was derived from the constants it polices, which is the defect the
  pinned liveness literal next to it exists to avoid. Appending `/v1/dump` to `UNMETERED_PATHS`
  with an ungated route on it passed 300 of 300, and the same edit took that path out of the
  coarse rate limiter, so the unauthenticated read was unmetered too. The expected set is a
  literal now, asserted against the shipped constants.
● The write guard omitted `/opt/venv/bin/`, the FIRST entry on the shipped `PATH`: it holds the
  gunicorn the pinned command execs and the python the health check runs.
  `COPY --from=build /bin/true /opt/venv/bin/gunicorn` passed 300 of 300. The RUN spelling was
  caught only by accident, because that path contains the substring `/bin/`, so two branches of
  one guard disagreed about the same file. Every directory on the shipped `PATH` is guarded now,
  and a test derives the list from the Dockerfile's own `ENV` so it cannot drift from the image.
● The colon pairing's three-unit bound is a fourth residual on the token-floor guard, recorded
  now rather than left implied by a comment that said three residuals remain.
● The directive test omitted the two shapes BuildKit honours beyond a leading `#name=` comment, a
  byte-order mark before the comment and the C-style `// syntax=` form. Both are refused, but by
  the unrecognised-keyword assert rather than by the directive guard, so nothing pinned them.
● The packaging probe file was removed in a `finally`, which does not cover a killed process, and
  a survivor would make every later packaging run refuse for a file the suite created. Stale
  probes are swept at fixture entry and their absence asserted after.
● The refused method in the `Allow` test was chosen by `next(iter(set))`, so it differed on every
  run under hash randomisation and a failure could not be reproduced from the seed. Also the
  decay rule in the claim-unit splitter was implemented twice, the `INT` and `TERM` traps in the
  packaging script did not exit, and the same rationale was written out three times across the
  Dockerfile and two docstrings.

Twenty-third security review, three majors and three minors:

● A `@app.middleware("http")` layer answers before the router and appears in no route table, so
  the categorical route refusal added the round before did not refuse it: nine lines returned the
  team token to an unauthenticated caller with the whole loop green. The middleware stack is a
  pinned literal now, class and dispatch, in both environments, with the order part of the pin.
  The claim in the previous round's policy entry that anything unreadable is refused was false as
  written; it described the route table only, and is corrected in place.
● The PATH derivation matched `PATH=` as a SUBSTRING, and `PYTHONPATH` ends in `PATH`. Setting
  both in one ENV made the test read PYTHONPATH and pass while the effective search path began
  with an unguarded directory holding a planted `gunicorn`. Assignments are parsed and matched by
  key equality now, exactly one PATH assignment is required, and the legacy space-separated ENV
  form is refused rather than silently unread.
● The guard covered executable directories and two site-packages leaves, and a venv interpreter
  imports `sitecustomize` from the BASE prefix's standard library. One COPY into
  `/usr/local/lib/python3.12/` was attacker code in the gunicorn master, both workers and the
  health-check interpreter, needing no PATH manipulation. Both importable trees are guarded
  wholesale, and the interpreter version is derived from the base image tag.
● A destination that is an ancestor of a guarded directory was not flagged, so `COPY tree /usr`
  wrote `/usr/bin/*` unseen. The two shipped COPYs that legitimately write over a guarded tree are
  pinned by exact text.
● Two claims in `docs/SECURITY.md` were false, one control row and one sentence of narrative.
  Both restated to what the tests assert.

Twenty-fourth security review, one blocker, two majors and one minor:

● BLOCKER: the middleware pin added the round before asserted on `create_app`'s output, and
  `app.user_middleware` is one of four request-handling surfaces. A layer added in `main.py` after
  the factory returns, a delegating `@app.exception_handler(404)`, a router-level dependency and a
  custom `route_class` each served the team token or granted full access with the whole loop
  green, and the route-class forgery left `require_token` visible in every dependant tree while it
  did. The pin is on the listener now and covers all five surfaces.
● The legacy-ENV refusal tested `"=" in argument`, satisfied by an `=` anywhere in the value, so
  `ENV PATH /opt/tools/exec=1:...` was the legacy form to docker and a parsed assignment here. The
  predicate is the first word, which the ENV PORT guard in the same file already used.
● `sys.path` is wider than the lib directories: a venv interpreter carries
  `{base_prefix}/lib/pythonXY.zip` ahead of the standard library, and the shipped command puts
  `/app/src` on the path with `--pythonpath`. A COPY over either was startup code execution or a
  replaced authentication module. Both are guarded, all four COPYs are vetted by exact text as a
  consequence, and a new test asserts every vetted entry names an instruction that exists.
● Three control rows in `docs/SECURITY.md` overstated their coverage and are restated.

Twenty-fifth security review, one blocker and one minor, both the same mistake:

● BLOCKER: an `APIRoute` SUBCLASS overriding `get_route_handler` is the request handler.
  Registered with `route_class_override`, one returned the team token to any caller sending a
  chosen header, with 307 of 307 green: the router-level pin reads the DEFAULT route class and the
  override is per route, both route walks used `isinstance` which a subclass satisfies, and
  `require_token` stayed visible in the dependant tree while the wrapping handler ignored it.
  Every route's type is now checked exactly, on both surfaces.
● The exception-handler pin compared bare `__name__` strings, and two types can share a name, so a
  decoy handler on a second class called `StoreError` left the pinned set unchanged. Handlers are
  pinned by type identity now and reported module-qualified.

Twenty-sixth security review, one blocker, one major and two minors:

● BLOCKER: round 25 put the pin on the listener and left the GATE asserted only on factory-built
  apps, so one `add_api_route` in `main.py` after the factory returns served the team token to an
  unauthenticated caller in production configuration with 307 of 307 green, confirmed over the
  wire. The listener's route inventory is a pinned literal now, gate included, and a second test
  mounts a client on the listener and asks every non-exempt route without a token.
● Swapping an existing route's `route.app` after registration kept the type, the dependant tree,
  the endpoint and the count intact while the attacker's callable answered. The inventory pins each
  route's ASGI callable as Starlette's own wrapper.
● `PREE_ENV` was missing from the platform-injected set, and the loader defaults it to production,
  so `ENV PREE_ENV=development` in the ship stage passed every test and would turn off the token
  requirement, serve the documentation paths unauthenticated and admit a cleartext origin. It is in
  the set with the token and the origin.
● The pre-write hook's credential rule required a quoted value, so the unquoted Dockerfile
  assignment form walked past the net CLAUDE.md says stops a credential before it lands. Two nets
  now, and the first attempt at the rule was reverted for firing on five legitimate files including
  this changelog.

Twenty-seventh security review, one blocker and two minors:

● BLOCKER: the listener inventory pinned by round 26 recorded LABELS, and four one-region edits in
  `main.py` each served the team token to an unauthenticated caller in production with 310 of 310
  green: a `dependant.call` swap, since FastAPI runs that and the pin read `endpoint.__qualname__`;
  an endpoint with `__qualname__` and `__module__` assigned to match; a plain Route squatting
  `/openapi.json`, which the APIRoute-only inventory missed and the factory-level doc assertion did
  not reach; and a duplicate tuple-identical route inserted first, which the frozenset deduped.
  None needed a gate bypass: all four sat on paths that answer without a token, where every route
  walk skips by design and the literal was the only control. The inventory is an ordered tuple over
  every route now, the executed callable is checked by identity and by source file, and the
  unauthenticated paths have a behavioural half for the first time.
● `passwd` and `pwd` were missing from the boot contract's credential terms while the pre-write
  hook had known them from the start, so `ENV DB_PASSWD=...` was allowed by both nets.
● The hook's credential rule required the term to end the name, so `ENV TEAM_TOKEN_VALUE=` and
  `ENV PREE_TOKEN_2=` walked past, and its eight-character value floor let `abc123` through. The
  term may sit anywhere now and the floor is four, measured to cost no false positive.

Twenty-eighth security review, three blockers and one major:

● BLOCKER: no test in the repository read `response.headers`. One `setdefault` in the hardening
  middleware served the production token to an unauthenticated caller on all six exempt paths with
  the whole loop green. Header names are pinned to a list now and no header value may carry the
  credential, on every path and in the authenticated, wrong-token and no-token cases.
● BLOCKER: a pinned key set is not a pinned body. `service = "pree-" + base64(token)` disclosed the
  credential on all five liveness paths, invisible to a key-set check and to a raw substring search
  at once. The liveness body is pinned exactly. This also settles the worth of the identity and
  provenance checks: a leak compiled with a forged `co_filename` satisfied five assertions at once,
  so the comment now says `co_filename` is a `compile()` argument rather than provenance.
● BLOCKER: the storage 503 branch had never been reached, because storage is writable under test,
  so one added key returned the token to any unauthenticated caller with zero statement misses. A
  second listener is built over an unwritable directory and the 503 body is pinned exactly.
● MAJOR: both credential nets were name denylists, so `ENV PREE_AUTH=<value>` was allowed by both.
  Seventh time a term table here has been one entry short. The check is an allowlist of the five
  environment names this image sets, failing closed on anything else.
● Every probe now uses `follow_redirects=False` and asserts no Location header, because
  `TestClient` follows by default and a 307 carrying the token in Location read as a 200; and both
  GET and HEAD are asked, since HEAD is served on all five liveness paths.

Twenty-ninth security review, five blockers and one major:

● BLOCKER, and my error: `leaked_headers` was declared and asserted and never appended to. My edit
  last round failed to match its anchor, only the declaration landed, and I reported the header
  channel closed and put a control row in the policy naming a dead assertion. A token header on
  `/v1/*` and `/diagnostics` reached every unauthenticated 401 with the loop green. Wired in now,
  across every route, every header case, every error shape and the CORS preflight.
● A permitted-name list plus a substring search is not a pin: `vary: base64(token)` passed both
  halves at once. The ten unauthenticated paths assert the header mapping exactly.
● The storage 200 and 503 bodies were key sets while the liveness body beside them was exact, so
  `errno_name = base64(token)` disclosed the credential on an unauthenticated path. Both exact now.
● No test pinned an audit record's field set, and the token walk never made a successful gated
  call, so only rejection lines were grepped: the token in the success audit record reached the pod
  log store on every write. Record kinds and fields are pinned, and an unknown kind fails.
● The boot line was three substring checks with no negative assertion, so appending the token to it
  passed. It is pinned exactly, with the token asserted absent.
● MAJOR: the ENV allowlist inverted names and not values, so `ENV PYTHONUNBUFFERED="<credential>"`
  shipped. The flag variables take exactly `1`, and PATH is guarded in every stage.

Thirtieth security review, three blockers, one major and three minors:

● BLOCKER: the header control was a permitted-name list plus a substring search, so
  `vary: base64(token)` served the credential to unauthenticated 401s and 404s on every non-probe
  path. Every header VALUE is pinned now: an exact literal, a bounded pattern, or a validated method
  list, with anything left over equal to the hardening set. The pin found two real exemptions on its
  first run, the documentation pages' CSP exemption and the fact that `/docs/oauth2-redirect` is not
  in `DOC_PATHS` and keeps the full set.
● BLOCKER: `EXPECTED_AUDIT_KEYS` pinned field names, not values, so `key + "#" + base64(token)` put
  the credential in the pod log on every write. Every string-valued field is an exact set member or
  a bounded pattern.
● BLOCKER: two of the six pinned record kinds were never produced by the test that owns the pin, so
  their field lists were literals nothing compared. The walk now configures an origin and refuses a
  preflight, corrupts both snapshots to force a store error, and issues a conditional read to reach
  the 304 branch whose headers were outside every pin. An unobserved pinned kind now fails.
● The `/diagnostics` mapping, the documentation pages and the `content-length` exclusion are all
  closed or honestly recorded; the exclusion is not a disclosure, because h11 refuses a bad length
  on the wire, but it would hide an unservable probe response from an in-process client.
● Two of five fabrication runs reported zero failures because the plant had not landed. Both were
  checked rather than counted, and both were red once applied.

Engineering review of rounds twenty-two to thirty-one, one blocker and two majors, plus a
deliberate subtraction:

● BLOCKER: `EXPECTED_AUDIT_KEYS` asserted completeness on record KINDS and not on field NAMES, so a
  surplus name was an exemption rather than a pin. `cors_reject` listed `reason`, which that handler
  never emits, and the value rule permits 512 printable characters there: base64 of the token on a
  record any unauthenticated caller triggers, 314 tests green. The field check is two-directional
  now and turned three dead literals red on its first run.
● The audit `key` rule was a hand-written charset admitting roughly 113 appended characters, so the
  token's hex appended to the key passed. It reads `STORE_KEY_PATTERN` from the application now.
● Two ENV tests were strictly subsumed, proved by mutation: one planted credential failed all three,
  so the two denylists asserted nothing the allowlist did not. Both deleted, history folded into the
  allowlist docstring. Also removed: a pass-through alias, a subsumed route walk, two duplicated
  assertions, a duplicated table parse, the suid sweep written twice, the interpreter version
  written four times, a self-contradicting comment pair, and a dead `delenv`. Net 143 lines out.
● The four FastAPI documentation routes were pinned by closure qualnames from inside a dependency;
  they are asserted structurally now, and every fabrication that beat the previous form is still
  red. What those paths serve is still pinned exactly.

Security review of the subtraction, one blocker, two majors and two minors:

● BLOCKER, pre-existing: BuildKit permits a QUOTED ENV or ARG key and strips the quotes later, so
  `ENV "PREE_ENV"=development` sets it exactly as the bare form does. The assignment pattern
  required the key at a word boundary, so a quoted key matched nothing and the line was read and
  asserted about nothing; the pre-write hook allowed the same forms. Six spellings passed with the
  suite green, each defeating a hard rule. The pattern accepts a quoted key, both hook rules do
  too, and the parser now fails closed when it reads fewer assignments than the line carries.
● The structural doc-route assertion classified rows by `endpoint.__module__`, an assignable
  string, so a forged /redoc with a spoofed module served the store as HTML to an unauthenticated
  development caller. Classification is by code-object filename now, and development is asserted to
  carry exactly four framework routes.
● The audit value scan skipped every non-string, and a number carries the credential:
  `duration_ms=int.from_bytes(token.encode(), "big")` decodes back to the token on every write.
  Every numeric field is bounded and the scan recurses to any depth, which pinned two legitimate
  nested fields it found on the first run.
● A deletion was WRONG and is reverted: folding the suid sweep's two byte-identical copies into one
  removed a second witness, so a two-edit neutered sweep passed. Both copies are back, deliberately.
● The claim that documentation-path bodies are pinned exactly was false; they are checked for the
  header channel and token absence only, and serve HTML by design. Corrected in place.

Thirty-fourth security review, one blocker, two majors and four minors, answered by changing the
shape of three controls rather than widening them:

● BLOCKER: the fail-closed assignment count compared TOTALS, so a token yielding two assignments
  paid for a token yielding none, and `ENV PYTHONUNBUFFERED="1"PYTHONDONTWRITEBYTECODE="1"
  ""PREE_TEAM_TOKEN""=...` parsed three of three with every name allowlisted while BuildKit set the
  credential into the shipped image config. The reader now REFUSES every word that is not a plain
  KEY=value rather than trying to recognise what docker accepts. Nine spellings from three rounds
  are red, including that one.
● `_endpoint_origin` was computed and never asserted, and two of the three properties that were
  checked asserted nothing, so a Route defined in main.py served the whole store as HTML to an
  unauthenticated development caller. The origin is asserted, and every route's ASGI callable must
  be Starlette's own wrapper rather than only an APIRoute's.
● A bound of 300,000 on `duration_ms` left about eighteen bits a record, enough to leak the token
  two bytes at a time. The ceiling is the exercise's own measured elapsed time now, two bounds for
  fields never emitted are deleted, and every value rule must be exercised by an emitted record.
● The validation record's `loc` parts are caller-supplied and were capped but never scrubbed; they
  go through the same sanitiser as the actor label now. Two hand-written charsets in the value table
  are replaced by idempotence under that sanitiser, which cannot drift from the code it describes.
● My claim that both hook rules accept a quoted key was false: it was two of three, and
  `ENV "PORT"=8080` was not blocked. Fixed, with repeated quotes accepted. Widening the rules to
  catch a continuation line was tried and reverted for firing on this repository's own source.

Thirty-fifth security review, one major regression, one major, and six minors:

● REGRESSION of mine: `_ScrubIdempotent` was a duck-typed stand-in for `re.Pattern`, and the
  dispatch tested two isinstance arms with no else, so every value it governed was accepted
  UNCHECKED. It had replaced two working regexes. Poisoning its `match` left all 311 tests green.
  One interface for every rule now, a fail-closed else, and a canary that hands each rule a value it
  must reject and the dispatch a rule type it must complain about.
● `co_filename` is whatever string was handed to `compile()`, so a forged endpoint compiled with
  FastAPI's filename satisfied the origin assertion and the callable check. It is a code-object
  IDENTITY comparison against a reference app now.
● The correlation narrowed the timing channel to about 7.5 bits rather than closing it. It is
  bounded by the slowest single request now, and the residual is stated in bits.
● The value parser post-stripped quotes, so `PREE_ENV="'development'"` was read as `development`
  where docker sets `'development'`. The inner group is captured.
● Two forms docker honours were falsely refused, a quoted value with whitespace and a bare
  `ARG NAME` declaration. An exact splitter and a declaration branch.
● A rejected field name scrubbing to empty was logged as `anonymous`, the sentinel for no actor.
  Log parts have their own marker.
● The hook's rules saw only the first assignment on a line, while this Dockerfile writes
  multi-assignment ENV lines. Any assignment on a keyword-anchored line now.
● Two false claims of mine in `docs/SECURITY.md` are corrected: "zero application findings" was
  contradicted by the same section, and a conditional recommendation to freeze the pins was recorded
  as an unconditional endorsement ahead of the review that would decide it.

Thirty-sixth security review, one major and five minors, all closed:

● The previous round's `sanitise_log_part` fix was unverified and self-contradictory: reverting the
  application to `sanitise_actor` left 312 tests green, and the marker the application emits was a
  value the `loc` rule rejected. Both closed, and the load-bearing half is the CALL SITE assertion,
  since a unit test on the function passes whichever one the application calls.
● Demoting the origin string to a partition key lost an attack identity does not cover: poisoning
  `FastAPI.setup` at import makes the reference and the app share one forged code object. Both
  checks are asserted now; the previous commit had traded rather than added.
● Every `_Pattern` rule used `re.match` with `$`, which matches before a trailing newline, so each
  admitted the one character it excludes. `fullmatch` now.
● The timing correlation was derived from a measurement the leak inflates: a handler that sleeps for
  the secret and reports its true duration raises its own ceiling. An absolute ceiling sits beside
  the correlated one.
● A backslash inside an accepted bare ENV value was read literally where docker strips it, so the
  guarded-directory derivation would have guarded a path docker never creates. Refused.
● CLAUDE.md gave a false mechanism for a hard rule: an image `ENV PORT` does NOT defeat platform
  injection, because a runtime value overrides image ENV. The rule stands for two other reasons and
  the clause is repaired here and in the Dockerfile comment that repeated it. An overclaim in
  docs/SECURITY.md is struck.

Thirty-seventh security review: PASS. Five minors closed on the way:

● The audited request path was not scrubbed while `loc` beside it was, so `GET /%1b%5b2J` put a
  control character in the rejection record. It goes through the scrub with its OWN length bound,
  because the actor's 64-character cap would have truncated a real store key out of every record.
  The scrub also removes the byte amplification the truncation used to bound.
● The `fullmatch` fix had no canary, so reverting `_Pattern` entirely left the suite green. Every
  pattern rule now has a trailing-newline canary, and the canary set must cover the rule set.
● The `ENV PORT` mechanism is split per variable, in all four places the claim appeared: a baked
  PORT shadows the code default and asserts a port the platform may not use; a baked PREE_DATA_DIR
  genuinely defeats the injection, because load_config prefers it over STORAGE_MOUNT_PATH, so every
  write would land on the ephemeral layer. My first repair was right for one and wrong for the
  other, in a clause governing both.
● The backslash refusal said docker "strips" a backslash where it un-escapes one.
● Three residuals are recorded rather than implied closed: duration_ms as a 5.7-bit channel, the
  combined route-check bypass needing import-time execution, and a doubled backslash refused.

Engineering review after the security PASS, three majors and six minors:

● A removal the engineering gate itself had ordered cost a control: the ENV allowlist's subsumption
  claim lived only in a docstring, so two lines shipped `ENV PREE_ENV=development` with the suite
  green. The walk stays deleted, the two tables are back as data, and the disjointness is asserted.
● `origin_allowed` was pinned by name and nothing else, and the value scan returned early on every
  boolean, so one token bit per refused preflight shipped green. Booleans are pinned by name, and the
  handler test asserts the field's value in both directions.
● The path scrub used the actor charset, which deletes `/` and `%`, so `/v1/assess` became
  `v1assess` and two requests produced an identical record. There is a path charset, the pin matches
  what the application can emit, and the separator is asserted present.
● The suid sweep property test is strengthened and the claim recorded for it withdrawn. It asserted
  the predicate set and not the START PATH, so `find /opt/venv -xdev -perm /6000` satisfied it in
  ONE edit and cleared nothing outside the virtual environment; nine forms satisfied it against a
  fixture of 4755, 2755 and 6755 files. It now pins the start path and the exact predicate set, and
  the single-edit neutering turns four tests red. The claim that a property "is not satisfiable by
  any number of coordinated edits" was false and was an argument for deleting the literal copies;
  property and literals are complementary, and both are kept.
● Minors: a stale byte-cost comment in app.py that cited a test now asserting the opposite; an
  orphaned comment fragment; one incident narrated four times and one channel three times, cut to
  the telling nearest each assertion; sixteen stale test-count literals replaced with "the whole
  suite green", which the changelog already said was the rule; and the `record` seam typed with a
  Protocol so a caller that drops `docs=` is a type error.

Security re-review, four majors and three minors, and the round's own corrections:

● The audit scrub is split by data PROVENANCE. The actor label stays Unicode-aware, because an
  operator's name may legitimately be non-Latin and the 64-character cap bounds it; caller-supplied
  data is ASCII-only. A single Unicode charset let `%F0%9D%90%80` (U+1D400, an astral LETTER that
  `\w` keeps) through, at twelve bytes each as a surrogate escape: a 160-character path wrote 1,802
  bytes where the test asserted 416. Astral inputs were added to the path bound test; the claim
  made here that they reached both was false, and the next review is recorded below.
● `error_count` was bounded by MAX_VALIDATION_ERRORS_LOGGED, and the application reports the TRUE
  total while capping only the `errors` list. The bound was wrong, not the application.
● The ENV allowlist is pinned as an exact literal frozenset, which closes the
  `ENV PYTHONPATH=/app/plugins` class without enumerating it.
● All four fail-closed arms of the audit value check are canaried: unpinned string, unbounded
  number, unnamed boolean, unhandled type. Four of the five could be deleted with the suite green.
● Three claims of mine are corrected in place rather than reworded, because they are the sentences a
  human would rely on: the app.py comment claiming the test asserts one byte per character (it
  asserts `isascii() and isprintable()` plus a whole-line ceiling, and the ratio is a consequence of
  those two); the security register's "not satisfiable by any number of coordinated edits", which
  was false and was an argument for deleting the literals; and the single neutering-cost figure
  quoted for every container rule, which varies by rule.
● The two version stamps "must agree" per CLAUDE.md, and nothing checked that they did. There is now
  a test, and it also pins the changelog heading the stamp will ship as. The stamp does NOT move per
  pre-release round: V0.1 is unreleased, every round hardens the same undelivered artefact, and
  `0.1.1` would assert a patch to a release that never happened. That reading is recorded in the
  test rather than left implicit. The stamp moves on delivery; the changelog row moves every round.

Security re-review of the round above, one major and three minors:

● The astral hole was closed in the CODE and asserted in only one of the two places it appeared.
  Reverting `sanitise_log_part` to the Unicode charset, one line, wrote a 6,684-byte audit record
  against 548 shipped with the whole suite green: the astral probe reached the body path only
  through a helper feeding no byte or charset assertion, and the `loc` rule is derived from the
  shipped scrub so it moves with any mutation of it. The astral shape is now first in body order,
  because only the first ten errors reach a record, and each logged field name is asserted ASCII and
  printable. The property, not a byte count, so it fires whatever the payload's slot arithmetic.
● The path scrub DELETED refused characters, which is not injective, and the collisions landed on
  legitimate routes: `GET /v1/,assess` was audited as `/v1/assess` and `GET /v1/assessments/a:b,c`
  as `/v1/assessments/a:bc`, so an unauthenticated caller could put a route or a store key they
  never requested into the audit trail. Refused characters are now percent-escaped, `%` is the
  introducer and no longer passes through, and injectivity is asserted as a property over the
  colliding inputs. Honest limit stated rather than implied: truncation cannot be injective, so the
  guarantee holds up to the cap and no further.
● `error_count` was bounded by MAX_BODY_BYTES, a BYTE count used as if it were a field count, so
  `len(exc.errors()) + 20000` passed. The ceiling is now derived, `MAX_BODY_BYTES // 6`, with the
  six-byte minimum field and the arithmetic in the open. The `path` value pin was Unicode-aware for
  the same reason and is now ASCII, so it could catch the charset regression rather than admit it.
● The suid sweep property pinned which predicates appear and nothing about their ARGUMENTS, so
  `\( -type l -o -type l \)` with both literal copies brought into line left every test green while
  the sweep cleared nothing: a symlink cannot carry a setuid bit. The argument to each `-type` is
  pinned as `["f", "d"]`. The register's claim that the property "catches a change that keeps the
  text plausible" was an over-claim about exactly this change, and is corrected where it was made.

Third security review of the audit layer, one major and five minors:

● The audited `path` field now takes the RAW request target off the wire, not the decoded path, and
  escapes every byte outside a permitted ASCII set. Two earlier fixes were defeated for the same
  underlying reason: space was the one whitespace character the charset permitted, so it survived
  the escape and was then removed by a `.strip()` two functions away, and
  `GET /v1/assessments/a:b%20` was audited byte-identically to `GET /v1/assessments/a:b`; and
  decoding aliases whatever runs afterwards, so `/v1/%assess` shared a record with
  `/v1/%25assess` and `/v1/assessments/a%2Fb:c` read as `/v1/assessments/a/b:c`. Injective by
  construction up to the cap, with no strip anywhere and the truncation limit stated.
● A test of mine was named for absent code rather than an invariant: reintroducing the `.strip()`
  left the suite green, correctly, because with space escaped the strip is a behaviour-preserving
  no-op. The name claimed what the body could not check. It now asserts the load-bearing fact, that
  no permitted path byte is whitespace, and permitting space again turns it red.
● The probe set is GENERATED, every byte in three positions, because the hand-picked list is what
  let the space through while claiming to cover every refused character. And the property is
  asserted end to end on real records, because for three rounds the sanitiser was injective in
  isolation while the application handed it an aliased input.
● `VOLUME` and `STOPSIGNAL` are refused in every stage, not only the shipped one, and `USER` before
  the shipped stage. `VOLUME /usr/bin` one line above the suid sweep left the whole suite green: the
  classic builder mounts a VOLUME'd directory for later RUNs, so `-xdev` skips it and every setuid
  binary in the base image survives; BuildKit makes it a no-op instead. Builder-dependent, which is
  why the refusal belongs in the text.
● The astral shape reaches the logged error window only because pydantic reports declared fields
  before extras. Asserted rather than assumed, so a pydantic bump cannot silently re-open the
  previous round's major.
● Corrected: "all three shapes sit inside the logged window" (the tiny flood is counted, not
  logged); the path value pin admitted a space it can no longer emit; the measured error-count
  maximum was 4,696, not 4,402.
● Recorded rather than fixed: the actor label and the rejected field name still alias, deliberately,
  because both are read by a human and neither names a route.

Fourth security review of the audit layer, one major and three minors:

● "The request target as bytes off the wire" was FALSE, and a real request found it. uvicorn's h11
  implementation partitions the target on `?` before the scope exists, so `raw_path` is the raw PATH
  and never the full target: `GET /v1/assessments/a:b?x=1`, `?x=2` and the bare path wrote one
  identical record, unauthenticated and well under the cap. The accessor was named `_raw_target`, a
  control row claimed "two distinct request TARGETS", and the end-to-end test asserted that
  universal while all eight of its probes differed in the path, so its body could not see it.
● The query is NOT recovered, deliberately. `audit.py` records that a query string once carried the
  team token into the pod log store, which is why the access log drops every one; putting it into an
  audited field would re-open that in the forensic channel. The scope is corrected instead: the
  accessor is `_raw_path`, the guarantee is over paths rather than targets, the test name says
  "whose paths differ", and a `had_query` bit records that a query was present without recording
  what it said. Two different queries still share a record, and the bit does not claim otherwise.
  The value is asserted absent from the stream.
● The truncation threshold was in the wrong unit. The escape expands 3:1, so truncation starts at 54
  raw bytes, not 160. No truncated record can read as a real route, since a truncated one is exactly
  160 characters and the longest legitimate path is 145.
● The astral proxy used `any`, which a list index or a numerically named field would satisfy with no
  astral name present; it asserts the count of five. The fallback test covered the absent-key branch
  only, and the `isinstance(raw, bytes)` guard is load-bearing rather than defensive: weakening it
  to `raw is not None` left the whole suite green while a `str` would make every audited rejection a
  500. Three cases now: absent, `str`, `bytearray`.

Fifth security review of the audit layer, one major and five minors:

● The previous round's fix reintroduced the defect it cited. `had_query` was added to five record
  kinds, its value pinned on ONE, and a comment written claiming it was asserted on every kind
  across the two-token axis. The value scan returns after checking a boolean's name, so
  `bool(config.team_token and ord(config.team_token[0]) & 1)` at the CORS site left the whole suite
  green while handing an unauthenticated caller one token bit per refused preflight. Now asserted on
  every kind, both directions, two tokens.
● Three places named TypeError where the scrub raises ValueError. The consequence was right, the
  mechanism was not.
● `GET /path?` records `had_query=false`, since an empty query is indistinguishable from none in the
  ASGI scope. The bit is described as a NON-EMPTY query string.
● `_raw_path` now partitions on `?` itself. The exclusion held under h11, httptools and the
  TestClient, but it rested on their convention, and a server placing the full target in `raw_path`
  would write query values into the field that once held the team token in cleartext. My own canary
  then showed the partition was unobservable under the shipped stack, so the hostile server is
  simulated by a middleware and the query and token are asserted absent from the record. The same
  canary showed the fallback arm's partition could never fire, since Starlette's parser splits the
  query before `.path` exists, so that one is removed: a check that cannot fail reads as a control
  and is not one.
● The truncation claims had no canary: the test probed permitted bytes only, which truncate 1:1, so
  the wrong unit could be restored with nothing red. The 54-byte threshold and
  `MAX_LOGGED_PATH > 16 + STORE_KEY_MAX_LENGTH` are asserted, the second being what keeps truncation
  aliasing a diagnosis cost rather than a forgery.
● The `confidence` pin permitted `medium`, which the enum has never had, and omitted `moderate` and
  `insufficient`, which the application emits. A meta-assertion now checks the literal pin and the
  enum agree, so the literal can stay a literal.

Sixth security review of the audit layer, two majors and three minors:

● The `path` field was pinned by CHARSET, and hex is inside that charset, so appending
  `config.team_token.encode().hex()` at the CORS site put the WHOLE credential into a record any
  unauthenticated caller can trigger, 240 a minute, with the suite green and the "token not in log"
  assertion still true because the value was hex. Third form of one defect: `key + base64(token)`
  and `duration_ms = int.from_bytes(token)` were the first two. The rule that comes out of it: a
  value that can be RECOMPUTED must be recomputed, not shape-checked. Each logged path is now
  asserted equal to the scrub of the target that produced it, and the token is asserted absent both
  verbatim and hex-encoded.
● "The two-token axis catches the class rather than the member" was false, at three places. Both
  fixture tokens contain a hyphen, so `... and "-" in token` on `had_query` was green, and the same
  conjunct on `origin_allowed` was green too: two real bits of the credential per refused preflight.
  A two-sample axis catches only a predicate that disagrees between those two samples. The
  accompanying claim that a single-token version would pass against the `ord(token[0]) & 1`
  expression was also false, since that expression is request-independent. The axis is now five
  fixed tokens that the obvious character classes each split, plus one per run from
  `secrets.token_urlsafe`, and the limit is stated where the set is defined: a constant predicate can
  survive by luck, so this raises the channel's cost rather than closing it.
● The pin-drift guard covered `confidence` alone and now covers the class. `action` and `outcome` are
  checked against the literals `app.py` actually passes, read from the source so the check stays
  independent of the code it constrains. Two earlier versions of that walk found a subset and drew a
  wrong conclusion: dict-only found nothing, dict-plus-keyword called `read_assessment` unemitted
  when it is passed positionally. Positional arguments bind to `audit`'s real signature.
● The `path` pin refused `UNPRINTABLE_MARKER`, which the scrub emits for an empty target, so it is
  wrapped in `_MarkerOr` as `loc` was. Wrapping it revealed a second defect in one edit: the
  newline sweep and the check that every pattern is swept both asked `isinstance` of the wrapper, so
  wrapping removed `path` from both at once. Both now reach through it.
● The changelog carried "Fourth security review of the audit layer" twice, for two different
  reviews, and six control-register rows for recent controls were missing. Both corrected.

Seventh security review of the audit layer, two majors and four minors, and the round where the
approach changed rather than the code:

● Four rounds sampled the token axis and each was defeated. The last round's set left predicates
  that survive a `token_urlsafe` draw with probability one, and an operator token like
  `Bluestaq-2026!MissionCritical#42` boots and leaks a real bit per record to `"!" in token`. A
  sample raises the cost of a channel; it cannot close one. So the channel is closed by SCOPE: every
  expression that can reach an audit record is enumerated by AST across both emitting modules, and
  exactly one may read the config object, the `origin_allowed` comparison. The credential is
  reachable only through `config`, so an expression that cannot read `config` cannot encode it in
  any encoding. A second rule closes the indirect route, since the HTTP layer never reads
  `config.team_token` at all, and its whole config read set is pinned rather than one name refused.
● All eight leaks the gate demonstrated go through `config` and are refused: hex on a `path`, hex
  split across a `key`, 64 hex characters as an `actor`, base64 in a `reason`, four token bytes in a
  `score` mantissa, an attacker-indexed bit in a boolean, a conditional `outcome` inside a closed
  set, and a whole new boolean. Several were gated on `config.is_production` so a development-only
  exercise could not see them; the gate is itself a config read.
● Defence in depth, since a scope rule is static: every caller-influenced value is now recomputed
  from the request rather than shape-checked - `path` against the target, `key` against the validated
  ids, `actor` against the scrub of the header, `score` and `evidence_coverage` against the response,
  `reason` against the handler's literal. Verified with four leaks that read no config at all. The
  exercise runs in both environments now, and the encoding sweep covers six forms with its limit
  stated: enumerating encodings will always be one short, which is why scope is the control.
● The drift walk proved which literals exist, not which a record carries: the gate widened the
  `outcome` pin, added two decoys, and emitted a conditional carrying a token bit, green. It now
  collects only from emission sites and requires a closed-set field to be a literal, permitting a
  pass-through of an enclosing parameter. `kind` joins `action` and `outcome`.
● `AUDIT_BOOLEAN_FIELDS` is derived from a registry naming each field's correlating test, so a third
  boolean cannot be added with two table edits. It asserts the named test exists, not that it does
  what its name says, and that limit is recorded where it lives.
● Four defects in my own AST walk, found by running it rather than reading it: a dict-only version
  found nothing; adding keywords called `read_assessment` unemitted when it is passed positionally; a
  call-argument-only version missed the `audit` record because `audit.py` binds it to a name first;
  and it counted `addHandler` as an unresolvable payload, refusing the tree it measured. An error
  message hardcoded one module while the walk covered two. The accessor now asserts it resolved
  something and refuses a payload it cannot follow.
● The claim that ordinals were "gone from" the security register was false: fourteen remain from the
  earlier series. They have never collided, so the sweep is scoped to the three audit-layer headings
  and the entry says so.

Eighth security review: a blocker, a major and two minors, and the architecture changed:

● The scope closure was defeated TWICE in nine lines with the whole loop green: a helper in
  `security.py` called as `rejection_reason(config, exc)`, and a helper in `app.py` whose parameter
  was named `cfg`. Either recovered the deployed token verbatim from the pod log on an
  unauthenticated 401. Both rules checked the SPELLING of a name rather than the flow of a value,
  and the attacker chooses the names.
● **The credential now leaves the HTTP layer entirely.** `create_app` takes a `ServiceConfig` with
  no token field plus a `verify_token` callable closed over the credential, and `main.py` takes that
  boundary before it writes the boot line, so even the logged length comes from the token-free view.
  No attribute `ServiceConfig` exposes carries the credential. This row originally claimed no
  helper, parameter name, module or encoding in `app.py` could reach it, and that absolute was
  false; see the nine-route entry below. Two allowlist tests keep it true: the credential is read in exactly three functions
  across the whole package under any spelling, and the type the HTTP layer receives has no token
  field and does not import `Config`.
● Four audited values were shape-checked only, so the claim that every caller-influenced value is
  recomputed was false. `status` was bounded to 400-599, so a record could disagree with the
  response it describes at about 7.6 bits per 404; `error_count` admitted about 12.4; and
  `errors[].type` was pattern-checked, where unpadded lowercase base32 fits the charset, is not one
  of the six encodings swept, and carried a whole credential. All four are recomputed now.
● The `reason` recomputation was a prefix match, leaving 498 free printable characters on
  `auth_reject`, which is the channel the blocker used. Equality now, with `store_error`'s path
  recomputed from the data directory.
● The unresolvable-payload self-assertion was bypassable by a decoy dict literal in the same call.
  It resolves the argument actually passed, through one level of binding, and counts what it cannot
  follow.

Ninth security review: one major, two minors, and a claim of mine that was the finding:

● The commit that took the credential out of the HTTP layer also asserted in FOUR places that "no
  helper, parameter name, module, or encoding in `app.py` can reach it". False. The review took it
  in four lines twice: `verify_token.__closure__[0].cell_contents` with a computed
  `getattr("team" + "_token")`, and a direct `os.environ` read. Either wrote the whole credential to
  the pod log with 337 tests green and all four scope tests passing. Needs a source change, so it is
  a durability and claim-accuracy failure rather than a live vulnerability, but the claim is what a
  reviewer relies on to stop looking, on the same channel as the previous blocker.
● The claims are corrected where they were made, and a guard now refuses LANGUAGE FEATURES rather
  than more spellings of a name: `__closure__`, `cell_contents`, `__globals__`, `__wrapped__`,
  `__dict__`, `__code__` and neighbours; `globals()`, `vars()`, `locals()`, `eval`, `exec`,
  `compile`; `getattr` with a computed name; and an `os.environ` read outside `load_config`. That
  alphabet is Python's and fixed rather than the author's and chosen, which is why it cannot be one
  short the way the previous denylist was.
● `Config.team_token` is `field(repr=False)`. `repr()` of a Config printed the token verbatim, so any
  f-string, print, format or exception carrying one disclosed it without spelling a token-shaped
  attribute. One keyword closes the class; a test drives all five real renderings.
● `token_verifier` closes over the expected string rather than the whole Config, so introspecting the
  cell yields one value instead of every setting beside it.
● Minors: the reader allowlist walked `glob` while claiming "every module", so a subpackage would
  have been outside it silently, and is `rglob` now; and three citations in source docstrings did not
  resolve - a test name that never existed, "two readers" where the allowlist names four, and a
  `split()` that is `for_service()`. The register-row guard covers the control table, not source
  docstrings.

Tenth security review: two majors, three minors, and the enumeration approach abandoned:

● The feature denylist was defeated NINE ways with the loop green, decisively
  `verify_token.__getattribute__("__closure__")[0].__getattribute__("cell_contents")`: the whole
  dunder list falls to spelling attribute access as a method call, and my classifier returned None
  for any call whose func was an attribute. Also a `str.format` field path (a string constant, so no
  AST attribute node exists and the reader allowlist is blind too), `os.getenv`,
  `from os import environ`, `inspect.getclosurevars`, `operator.attrgetter`, and
  `dataclasses.asdict`/`astuple`/`pickle.dumps`/`__getstate__`/`__reduce__`. `slots=True` had made
  three denylist entries inert rather than protective. My claim that the alphabet "cannot be one
  short" was wrong: attribute access has a method spelling, a string spelling and a library
  spelling.
● **The control is now a RUNTIME check on the bytes leaving the process.**
  `audit.install_credential_guard`, armed at boot by `security.arm_output_guard`, refuses any line
  containing the credential on the audit logger, stdout and stderr. It does not care how a leak
  obtained the value. Fail-closed by substitution, not by raising (which propagates to the `logger.*`
  call site and emits the record nowhere - this row said "swallowed by logging", which a later round
  measured as false; see the ninth security review below) or dropping (a control whose success looks
  like nothing happening). Measured over twelve leak routes; benign
  lines pass untouched, because a guard that suppresses clean lines is a denial of service on the log.
● Both allowlists are keyed on (module, function) rather than a bare name, which had given any module
  a free credential read via a helper called `for_service` and a free environment read via one called
  `load_config`. A per-module IMPORT allowlist closes the library spelling: that set genuinely is
  small and fixed, and `inspect`, `pickle`, `operator`, `copy`, `gc` have no business in a module
  serving a request.
● `authorise` was a second copy of the compare that no served request reached, so an edit to it could
  not affect a request while every test of it stayed green. It is a thin caller of the closure now.
● **The worst finding was not in the code: three of the four claim sites I reported as corrected were
  not corrected, and `main.py` had no hunk at all.** I said "the claims are corrected where they were
  made"; it was true in one place of four. All four are corrected, the register's absolute is amended
  in place rather than answered thirty lines below, the `repr=False` claim is narrowed to the default
  dataclass `__repr__`, and two stale reader counts are fixed.

Eleventh security review: two majors, three minors, and my framing was the deeper error:

● The runtime guard was bypassed SEVEN ways. A logger-level filter does not run for a record
  propagated from a descendant, only the ancestor's handlers do; and gunicorn builds its handlers
  before the worker imports the factory, so they hold the pre-wrap stream - measured,
  `gunicorn.error`'s handler had `guarded=False, filters=[]`. Three lines put the credential in the
  pod log on an unauthenticated 401 with everything green, acquiring it via
  `__import__("os").getenv(...)`, which no `ast.Import` node contains.
● Fixed at HANDLER level: `Handler.handle` runs handler filters for every record reaching it,
  whatever logger emitted it. Existing handlers found through two sources, later ones covered by
  patching `Handler.__init__`, and a handler holding the pre-wrap stream re-pointed at the wrapper.
  Measured against the exact plant under the real launch command: 0 credential occurrences, 1 alarm.
● **The framing was the deeper error and the reviewer was right.** I called the guard "the only one
  that does not depend on enumerating the adversary's alphabet". A plaintext substring test over a
  set of channels IS an enumeration, over (channel x encoding), and both dimensions belong to the
  same adversary. The covered set is now stated exactly and the uncovered set named: fd-level writes
  (`os.write(1, ...)`, a subprocess inheriting descriptor 1) and any encoding but plaintext.
● `_GuardedStream` was irreversible and incomplete: `writelines`, `fileno` and `buffer` forwarded
  now, `int(None)` no longer raises, and disarming restores the real streams. `buffer` and `fileno`
  are forwarded unguarded, deliberately, since withholding them moves the breakage not the exposure.
● The guard's edge-path test passed only because an earlier test left `propagate=False` on the
  process-wide logger; it failed in isolation. It manages `propagate` itself now.
● Two more copies of the `repr=False` absolute corrected, and the over-claiming test name changed to
  what it asserts.

Twelfth security review: two majors, four minors:

● `sys.__stdout__` holds the PRE-WRAP object, permanently, one underscore from the covered name, so
  `print(token, file=sys.__stdout__)` reached the pod log in plaintext. Neither fd-level nor
  re-encoded, so it sat outside both stated limits while the register claimed the covered set was
  stated exactly. All four `sys` stream attributes are wrapped now, sharing one wrapper per
  underlying object so a handler re-pointed at one is recognised via the other.
● Writing that taught its own lesson: the obvious `for name in (...): setattr(sys, name, ...)` was
  refused by this project's OWN introspection guard, correctly, because a computed attribute name is
  what a static rule about names cannot see. A control that exempts its own module is not a control,
  so the four attributes are written out explicitly.
● Two halves of the guard could be deleted with the suite green: the stream re-point, which its own
  docstring calls necessary, and the second source of the handler walk, whose docstring says "either
  alone has a gap". Both claims true, neither asserted - the recurring shape of this project's
  defects. Each is now driven through the path where it is the only defence: the re-point via
  `handler.emit` directly rather than a logger, and the walk with `_handlerList` cleared as
  `dictConfig` clears it.
● A THIRD limit was missing and is now stated: the guard cannot survive an adversary with the same
  privilege as the code it guards. `dictConfig` with `{".": {"filters": []}}`, a Handler subclass
  overriding `handle`, or restoring `logging.Handler.__init__` each removes it in a few lines.
  Inherent to any in-process guard, not a defect in this one.
● The fd bullet named two instances where it meant a class; `sys.stdout.buffer.write`,
  `open("/dev/stdout")` and `os.fdopen(1)` are further instances, and enumerating them would repeat
  the mistake this module's history is made of.
● On a refused line the wrapper returned the alarm's length rather than the caller's, so a caller
  looping until everything is written would re-submit the tail. Unreachable through `print` or
  `StreamHandler`, both of which discard the return, so a trap rather than a fault.
● Correction to the commit that landed this round: its message says "345 passed" and the real count
  is 344. Recorded here rather than rewritten out of the pushed history, because a record that says
  what was claimed and what was true is worth more than a tidy one. Second miscount in a commit
  message this range; the first was caught before pushing.

Continuous integration, which closes the standing container gap:

● `.github/workflows/verify.yml` runs the verification loop and the pipeline simulation on a runner
  that has a Docker daemon, so the three container hard rules - no setuid or setgid bits, the
  non-root numeric user, no pip in the shipped filesystem - execute for the first time on any
  machine. Exit 2, the script's own deferral signal, is treated as a failure.
● The daemon is proved reachable BEFORE the pipeline runs, so the exit-2 check means what its
  comment says. My first draft claimed exit 2 "means the detection broke", which is false on a
  runner with no daemon; with the proof first it is true.
● The actions are pinned to version tags rather than commit digests, and the residual is named
  rather than implied: this session cannot resolve a digest for a repository outside its scope, and
  a guessed digest is worse than an honest tag. The job holds no deploy secret because it deploys
  nothing.
● The workflow is not in the upload archive. `package-appstore.sh` builds from an allowlist, so
  `.github` is absent by construction, and `.dockerignore` keeps it out of the build context.
● **The run happened and it is green.** CI run 32630383551 on `fbefd3f` built the image and executed
  all three assertions: `no setuid or setgid bits`, `no pip or setuptools in the shipped filesystem`,
  `runs as 10001:10001`. The evidence is the output, not the exit code, and the load-bearing line is
  `exported 5618 entries, 5618 with a parseable mode` - proof the sweep read real modes rather than
  silently reading nothing, which is the failure mode an earlier version of these checks had.
  **The standing gap of this whole project is closed: the container hard rules are verified in fact
  and no longer only in text.**
● Three residuals recorded rather than implied away: the single flattened layer is still a text
  assertion plus a successful build, and the platform-side history scan is not measured here; the
  workflow's own exit-2 branch is unexercised, because the pipeline exited 0; and `useradd` warns
  that uid 10001 exceeds SYS_UID_MAX 999, which is a warning and not a failure, since the third
  assertion measured the running identity directly.

### Eighth security review: PASS, and the six minors closed on the way past

The thirteenth security round returned **VERDICT: PASS** - six minors, no major, no blocker - and
verified the guard under the shipped launch command. Its figures are quoted as the REVIEWER's
measurements, not as project facts, because nothing in the repository evidences them and an
engineering review rightly asked which they were: 5 of 5 handlers wrapped and filtered, 0 occurrences
of the token in 609 live log lines, both of the previous round's majors closed and held, 7 of 8
mutations red. What is reproducible here is the suite and the loop. All six minors are closed in this
release, and the two that were more than a sentence:

● **The filter scanned the message, not the rendered record.** `Formatter.format` appends the
  exception and stack text after the message, so `exc_info` carrying the credential went out with the
  guard armed and no alarm raised, on any handler whose stream is not a wrapped `sys` stream. The scan
  now covers `getMessage()`, `exc_text`, `exc_info` and `stack_info`, using
  `Formatter.formatException` so what is scanned is what the handler will emit; a refusal clears all
  three traceback fields rather than alarming over `msg` alone. A traceback carrying no credential is
  left intact, asserted, because a guard that strips every traceback costs every diagnosis.
● **The uncovered set had a fourth class and a fifth dimension.** A credential leaving by a response
  body, a file on the data volume, a filename, or a child's argv touches neither half of the guard,
  and a credential split across two writes reassembles in the log with no alarm. Both are now named
  in the module and in the control register, which had claimed the set was stated exactly.

The other four: the boot-path stream re-point can no longer crash a worker on a handler whose
`stream` is read-only, and the ordering it depends on is written down; the claim that disarming leaves
nothing installed is corrected to the truth, that the handler side persists inert; and the refused
write's return value - the one item of the six that nothing held, revertible with 347 tests green -
is now driven through the caller's own write loop, which must terminate in one round.

Three new regression tests, 350 passing, coverage 99% against the gate's 80%. No version bump: V0.1
is unreleased, so a stamp move would assert a patch to a release that never happened.

### Ninth security review and the engineering round on the same commit: one finding, reached twice

The security round returned PASS with five minors; the engineering round on the same commit returned
FAIL with three majors. Their top finding was the same, reached independently, and it was not a leak:
**the guard's own design rationale asserted a mechanism the runtime does not have.** For three rounds
the module said a raise inside a logging filter is swallowed and the line goes out anyway, and used
that to justify failing closed by substitution. `Handler.handle` calls the filter outside any `try`,
so it propagates to the `logger.*` call site and the record is emitted nowhere. The decision was
right and the reason was wrong, which is worse: an engineer trusting the stated reason removes the
suppressions and turns a malformed log record into a 500 in the handler that logged it.

Everything else closed in this round:

● **A whole axis of the scan.** A format string may name any record attribute, so `extra={"token":
  ...}` with `%(token)s`, the credential in the logger name, and `funcName` all reached the line in
  plaintext with the guard armed. Every attribute is now scanned, as itself or through `str()`, and
  each one carrying the credential is redacted individually so the rest of the record stays
  diagnosable.
● **The scan failed open.** A `msg` whose `__str__` raised once then returned the credential went out
  unscanned. A part of the default rendering the guard cannot read is now refused with a distinct
  alarm. The attribute walk deliberately does NOT fail closed, because an attribute the formatter
  never renders is not an unscanned emission and alarming on it would cost a legitimate line; the
  asymmetry has its own test.
● **This project's introspection guard refused the fix**, since the renderable set is
  `record.__dict__`. Resolved by one exemption keyed on (module, function, call) and pinned by a test
  asserting it has exactly one member reading its own `logging.LogRecord` parameter - not by
  exempting the module, which would be a control exempting its own implementation.
● **"Each part is produced under its own `try`"** was false for two of four: a truthy non-`str`
  `exc_text` or `stack_info` made the join raise out of the filter. **The suppression around
  `formatException` was unheld.** **A `TypeError` in the re-point's catch was speculative and
  unheld**, and is gone; `AttributeError` covers all three real shapes. **"ARMED FIRST" preceded
  itself**, since the guard needs the credential `load_config` resolves; the window is closed by
  `config.py` and is now in the uncovered set.

Four new regression tests, 354 passing, coverage 99% against the gate's 80%. Still no version bump:
V0.1 is unreleased.

Twelve mutations were run against these fixes and two came back green first time, which is the
argument for running them: the test holding the `formatException` guard used an input the standard
library renders defensively rather than raising, and a two-field fix had only one field driven. Both
are red now.

### Both gates FAIL: the guard emitted what its own absence would have contained

The security round found a BLOCKER and three majors; the engineering round found three majors on the
same commit, one of them the same finding reached independently. Six of the seven reported channels
reproduced before anything changed, and so did the BLOCKER.

● **The BLOCKER.** An attribute whose `__str__` raises on the first call and returns the credential
  on the second. Disarmed, the formatter's first call raises and `handleError` discards the emission,
  so nothing is written; armed, the record scan absorbed the raising call and the formatter's second
  call succeeded. Arming the control was what put the credential in the log.
● **The cause, and the fix.** Four rounds had widened a MODEL of the emitted line and each lost to a
  part of the real one: `%(args)s` the message never consumed, a `repr` conversion, a formatter
  default that never touches the record, a lying `str` subclass, a filter running after the guard's.
  The guarantee moved to `logging.Handler.format`, where a stock handler turns a record into the
  string it emits, so what is scanned is the line rather than a prediction of it. All seven channels
  refuse, and "arming never emits what the disarmed process would not" is asserted as an invariant.
● The record scan is kept for early refusal and field-level redaction. `msg` and `args` are scanned
  now: one constant had meant both "not redacted" and "not scanned", which is where the two-name gap
  both reviewers found came from.
● Two deletions. `_refuse`'s stated reason for its key-prefix skip was false and the line pinned
  nothing; the real reason is collision avoidance and an attribute named `message` holds it. And a
  `try/except TypeError` around `vars()` was dead code, because `LogRecord` declares no `__slots__`
  and a subclass cannot shed the dictionary; the mechanism is asserted instead.
● The fourth and fifth copies of the inverted filter-raise claim, in this file and in the security
  policy. The commit that said it had corrected that claim everywhere had found three of five.

Seven new regression tests, 360 passing, coverage 99% against the gate's 80%, no missed statements.
Seventeen mutations run: sixteen red. Four were green first time and three were real gaps, all in the
lying-`str` coercion, which was held at only one of its three comparison points. The fourth green is
recorded rather than fixed: adding dead code back cannot be caught by mutation testing, because dead
code has no observable behaviour, which is why coverage is a separate gate.

### Both gates FAIL again: ten majors, and the two answered by deleting code

Five majors from the security round and five from the engineering round on the same commit. All real,
none reachable from the unauthenticated edge: every one needs a formatter, a filter, or a handler
choice inside the process.

● **The coercion was applied to a copy while the original was emitted.** `str()` dispatches to
  `type(value).__str__`, so a `str` subclass can return clean text while holding the credential; and
  `StreamHandler.emit` writes `msg + terminator`, so a hostile `__add__` produced the credential
  after the scan passed. `str.__str__` reads the underlying object and cannot be intercepted, and the
  coerced value is now what gets written and returned.
● **`Handler.format` is not the universal chokepoint the previous round claimed.** `HTTPHandler`
  urlencodes `record.__dict__`; `SocketHandler` and `DatagramHandler` pickle it. No subclassing
  needed, and `HTTPHandler` had been named as covered. For those three the record scan is the only
  layer, which is now what the uncovered set says. This project's handlers are `StreamHandler`s on
  stdout, so none of it is live here.
● **The "arming is never worse" invariant was false.** The walk called `str()` on every non-`str`
  attribute, so a `__str__` with a side effect ran zero times disarmed and once armed - measured
  writing to a raw file descriptor. The walk reads `str` attributes only now and renders nothing,
  which also halves the guard's cost.
● **Two deletions, each of which was the finding.** The fail-closed refusal of an unreadable record
  destroyed clean operational lines for no containment once the finished-line scan existed, so it and
  its second alarm are gone. And a third dead defensive branch went the way of the previous two: a
  defensive line needs a named reachable input or it goes.
● **Two reasons that were wrong twice**, on the rendered-key skip and on the redaction's suppression.
  The first was a self-inflicted scan bypass described as cosmetic; the second guarded a line the
  named shape cannot reach while the shape that can raise faulted five lines above it.
● **Five unpinned load-bearing lines** now have tests, each asserted on the mechanism because a
  behavioural assertion was being satisfied by a different layer: both stdlib-patch idempotence
  guards, the construction patch, the rendered-key skip, the redaction suppression - and the SHIPPED
  rate limits, which every rate-limit test had bypassed by injecting its own limiter.

Six new regression tests, 365 passing, coverage 99% against the gate's 80%, no missed statements.

### The record-scanning layer removed, on the owner's decision

The credential output guard had three layers. The one that scanned the RECORD at filter time is gone;
what remains scans text that is actually leaving - the finished line at `logging.Handler.format`, and
writes through the four `sys` text streams.

● **Why.** It scanned a model of the emitted line and could not be made into a guarantee: seven
  rounds of widening the model each ended in a measured bypass. It also produced most of the recent
  defects, all self-inflicted and none reachable from the unauthenticated edge - the side-effect
  amplification that made arming worse than not arming, a redaction that minted a key the scan used,
  an unguarded write that faulted its caller, a two-name scan gap, and a fail-closed branch that
  destroyed clean lines.
● **What it cost.** Field-level redaction: a refused line reads as one alarm rather than naming the
  field. And `HTTPHandler`, `SocketHandler` and `DatagramHandler` lose their only layer, since none
  emits `Handler.format`'s return - acceptable here only because this app builds nothing but
  `StreamHandler`s on stdout.
● **What it bought.** `audit.py` from 212 statements to 152; the suite from 366 tests to 356; the
  guard's per-line overhead reduced to the finished-line scan, measured at effectively zero against
  the baseline. A "37 seconds to 27" claim in the first version of this row is WITHDRAWN: it did not
  reproduce, because the slower reading was taken while two reviewer subagents were running on the
  same machine and measured contention rather than the change. And the introspection guard is back to NO exemptions, the one it carried
  having existed only for the removed layer's `vars(record)`.

`securityContext.fsGroup=10001` is CONFIRMED by the owner and recorded in `docs/DEPLOYMENT.md` as a
required deployment parameter rather than an open decision.

356 passing, coverage 99% against the gate's 80%, no missed statements.

### The stderr half of the byte channel, and seven sentences describing a deleted layer

The engineering gate failed the reduction. One real hole, one false figure, seven stale sentences.

● **The register claimed four `sys` streams and the suite drove two.** Deleting the `sys.stderr`
  wrap, the `sys.__stderr__` wrap, or the stderr restore each left the suite green - and it is live
  in the shipped image, since the `Dockerfile` passes `--error-logfile -` and gunicorn builds that as
  a `StreamHandler` on the pre-wrap `sys.stderr`. All four are now driven by iterating
  `GUARDED_STREAM_ATTRIBUTES`, which also gives that published constant its first reader.
● **Two tests went vacuous as a side effect of the reduction**, each satisfied by a surviving layer
  rather than by the thing it named: the idempotence test asserted a patch that had been deleted, and
  the forgotten-handler test passed whether or not the handler walk found anything. Both re-pointed
  at what they are for.
● **A figure is WITHDRAWN.** "37 seconds to 27" did not reproduce; the slower reading was taken with
  two reviewer subagents on the same machine and measured contention.
● **Seven sentences described the removed layer as live**, across the module, the register and the
  test prose, plus an orphaned comment fragment ending in "and".

Two new tests, one rewritten, 357 passing, coverage 99%. Eleven mutations re-run including every one
the review found green: eleven red.

### A fourth serialising handler, and three unpinned guarantees outside the guard

● **The boundary held.** The security round re-attacked auth, the token compare, route gating,
  boundary validation, the body cap, the framing guard, CSP, CORS fail-closed, both limiter tiers,
  the sanitisers, the store's merge rules and the container contract, and defeated none of them.
  Seventy-five mutations, seventy-one red, quoted as the reviewer's count rather than re-run here.
  No secret in any body, header, log line, the tree, or 75 revisions of history.
● **The reduction's cost was wrong by one handler.** The uncovered set was enumerated by "does not
  emit `Handler.format`'s return value", and `QueueHandler` DOES emit it and pickles
  `record.__dict__` anyway, so it read as covered while carrying an `extra=` attribute across an IPC
  queue. The criterion is now "serialises `record.__dict__`", which makes it four, with
  `QueueHandler`'s split status stated: message half covered, every other attribute not.
● **Three guarantees outside the credential guard were held by nothing.** `_first_refused`'s
  charge-every-key invariant - measured here as the shared bucket losing one charge where it should
  lose two, with the reviewer's stronger "admits what the code refuses" figure recorded as theirs
  because I could not reproduce it in three attempts; both CORS allowlists; and the audit `reason`
  cap. All three now driven, the last on the second attempt after my own
  canary caught the first version entering no code path.

Four new regression tests, 360 passing, coverage 99%.

### One register row, and the difference between a narrative and a live claim

The security round found the code sound - 49 of 50 mutations red, every boundary attack fail-closed,
no secret reachable anywhere - and failed the commit on one table row.

● **The `QueueHandler` criterion was fixed in the module and not in the register.** I had reported it
  fixed in both. The live row still said three handlers, by the criterion under which `QueueHandler`
  reads as covered because it DOES emit `format`'s return - so an engineer adding a queue for async
  logging would read the register and ship a leak. Rewritten to the property that decides membership.
  The dated round write-ups keep the old criterion because they are history; the register is a live
  claim table and cannot.
● **`QueueHandler` "pickles it after formatting"** attributed the pickling to the handler.
  `enqueue` calls `put_nowait(record)`; the queue pickles, so an in-process `queue.Queue` leaks
  nothing and a multiprocessing queue does. Over-stated exposure, corrected mechanism.
● **"A non-`str` formatter return is already broken for `StreamHandler.emit`" was false**, and it was
  the stated reason for deleting a defensive branch. Unarmed it emits fine; under the permanent
  `Handler.format` patch it raises and the line is lost. The deletion stands on coverage; the missing
  part was that this is a third standing cost of arming, now listed.
● **The arming-twice test caught only a re-add carrying this module's marker.** It asserts the stock
  function now, and an unmarked wrapper turns it red.

360 passing, coverage 99%.

### The premise under the removed layer, and a false claim about a commit's own diff

● **`install_credential_guard`'s cost argument rests on "this app builds nothing but
  `StreamHandler`s", and nothing held it.** True by grep, and it justified deleting a security layer.
  Now asserted over the real source against every handler class `logging` and `logging.handlers`
  offer; a `QueueHandler` or `SocketHandler` construction turns it red, so whoever adds one revisits
  the cost statement rather than editing a list.
● **A stale-prose sweep was declared complete for the third time.** Six more sites named the deleted
  filter or constructor patch as the live mechanism, and one was FALSE rather than stale: a docstring
  claiming a malformed record is refused "with a distinct alarm", when that alarm no longer exists and
  the record is emitted nowhere.
● **A commit message claimed its own diff wrongly.** The previous commit said an attribution now
  appeared in "both documents"; `git show --stat` shows it never touched `docs/SECURITY.md`. Applied
  here, along with the rule: a completeness claim needs evidence, which for a commit message means
  `git show --stat` before writing it.
● **Two comment corrections**: three handlers log a `reason`, not two; and two of the three cannot
  reach the cap, which does not make those slices dead code, because a slice on a short string
  executes where a dead branch does not.
● The `_first_refused` divergence direction I could not reproduce was reproduced by a second review at
  78 of 78, so it is recorded as measured rather than as an open caveat.

One new regression test, 361 passing, coverage 99%.

### A BLOCKER on the tripwire written to protect the premise

● **The premise tripwire walked the top level only.** `glob("*.py")` instead of `rglob`, so a
  `QueueHandler` in a subpackage left the whole suite green - and this project had already recorded
  fixing the identical defect in the reader allowlist a few tests away. A recorded lesson is not a
  control.
● **The fix needed a synthetic tree to be holdable at all**, because `glob` and `rglob` are the same
  function on a flat package. The walker is extracted and proved against nested offenders at two
  depths; reverting to `glob` now turns that test red.
● **The scan's own detection branch was unheld** - deleting the half that sees
  `logging.handlers.X(...)` left the suite green - and now a positive canary proves the scan sees the
  one construction this package makes before it refuses anything.
● **A no-secret assertion was a tautology**: the malformed-record test never put the credential on
  the record, so it passed with the guard un-armed. The credential is the argument now.
● **A test docstring contradicted the security policy** about the same control's evidence. A third
  independent harness measured the limiter divergence at 46 of 46 in the admitting direction, so the
  direction is confirmed by three harnesses and my structural conjecture is withdrawn.

Two new tests, 362 passing, coverage 99%. Five mutations re-run: five red.
