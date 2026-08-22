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
