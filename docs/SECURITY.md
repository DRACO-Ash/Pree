# Pree security policy

The threat model, the controls, and every deliberately accepted risk. A control that cannot be
verified is treated as failed.

## Threat model

Pree holds no personal data. What it holds is an assessment of which objects threaten which
protected assets, which is itself sensitive: it reveals what the operator is watching and what
they judge dangerous. The two assets worth protecting are therefore the shared team token and
the assessment store.

## Controls

| Control | Where | Verified by |
|---|---|---|
| Team token compared in constant time | `src/pree/security.py` | `test_token_compare_uses_a_constant_time_primitive` |
| Every state-changing route gated on the token | `src/pree/app.py` | `test_assess_requires_the_token` |
| Generic client errors, detail server-side only | `src/pree/app.py` | `test_a_wrong_token_gets_the_same_generic_error` |
| CORS fail-closed: named origin only, never a wildcard with a token | `src/pree/config.py` | `test_cors_allows_only_the_configured_origin` |
| Refuses to start on an unsafe token and origin pairing | `src/pree/config.py` | `test_wildcard_origin_with_a_token_refuses_to_start_in_production` |
| Two-tier rate limiting, both keyed on the peer address | `src/pree/ratelimit.py` | `test_a_spoofed_actor_header_cannot_reset_the_rate_limit` |
| Platform probes exempt from rate limiting | `src/pree/app.py` | `test_the_coarse_limit_never_touches_a_platform_probe` |
| Request bodies capped before the token gate runs | `src/pree/app.py` | `tests/test_body_cap.py` |
| Production refuses to start with the gate open | `src/pree/config.py` | `test_production_refuses_to_start_with_no_token_at_all` |
| A refused write never loses a stored record, and leaves the primary in place | `src/pree/store.py` | `test_a_refused_write_leaves_every_prior_record_readable` |
| An unreadable primary snapshot recovers from the backup | `src/pree/store.py` | `test_a_corrupt_primary_recovers_from_the_backup` |
| Concurrent writers serialised by an exclusive lock | `src/pree/store.py` | `test_two_concurrent_upserts_both_survive`, and verified against four real worker processes |
| A failed privileged action is still audited | `src/pree/app.py` | `test_a_failing_store_returns_a_generic_503_and_still_audits_the_action` |
| Validation errors never echo caller input | `src/pree/app.py` | `test_the_validation_error_never_echoes_the_callers_input_back` |
| Boundary validation, strict, extras forbidden, non-finite numbers refused | `src/pree/api_models.py` | `test_out_of_range_unknown_or_coercible_input_is_rejected_at_the_boundary` |
| Actor labels sanitised and length-capped against log forging | `src/pree/security.py` | `test_actor_sanitisation_strips_log_forging_characters` |
| No secret in any log, audit line, health body, or error | `src/pree/health.py` | `test_diagnostics_reports_secrets_as_a_boolean_and_a_length_only` |
| Locked Content-Security-Policy and hardening headers on every response | `src/pree/app.py` | `test_every_response_carries_the_hardening_headers` |
| Interactive documentation not served in production | `src/pree/app.py` | `test_the_interactive_docs_are_not_served_in_production` |
| Every filesystem refusal surfaces as a handled 503, audited | `src/pree/store.py` | `test_a_real_storage_refusal_returns_503_and_audits_the_action`, `test_a_failed_lock_release_surfaces_as_a_store_error` |
| The rate limiter fails closed when its key table saturates | `src/pree/ratelimit.py` | `test_a_saturated_key_table_fails_closed_rather_than_admitting_everyone` |
| A busy probe pool never reports storage as broken | `src/pree/health.py` | `test_a_pool_busy_with_probes_still_inside_their_timeout_is_indeterminate` |
| Concurrent probe callers join the probe in flight rather than guessing | `src/pree/health.py` | `test_a_concurrent_caller_joins_the_probe_rather_than_guessing` |
| A write that misses its budget is never reported as ready | `src/pree/health.py` | `test_a_write_that_misses_its_budget_is_never_reported_as_ready` |
| The retention cap holds even on a snapshot with a partial write order | `src/pree/store.py` | `test_a_partial_write_order_still_trims_to_the_cap` |
| The assessment collection is capped, newest kept | `src/pree/store.py` | `test_the_collection_is_capped_and_the_newest_record_always_survives` |
| A configured data directory must be absolute, and a pasted value is normalised | `src/pree/config.py` | `test_a_relative_data_directory_is_refused`, `test_a_quote_wrapped_path_is_normalised_not_taken_literally` |
| A control character in any config value is refused | `src/pree/config.py` | `test_a_control_character_in_a_value_is_refused` |
| The CSP exemption for the dev docs cannot reach production | `src/pree/app.py` | `test_the_csp_exemption_cannot_reach_production` |
| Retry-After never tells a refused caller to retry immediately | `src/pree/ratelimit.py` | `test_retry_after_is_never_zero_even_for_a_key_with_no_history` |
| A wedged mount keeps reporting unready, and never goes quiet | `src/pree/health.py` | `test_a_wedged_mount_keeps_answering_unready_rather_than_going_quiet` |
| The readiness signal cannot freeze on a stale cached result | `src/pree/health.py` | `test_the_cached_result_expires` |
| A recovery never destroys the backup it recovered from | `src/pree/store.py` | `test_a_recovery_does_not_destroy_the_backup_it_recovered_from` |
| The launch command targets a factory that exists | `Dockerfile` | `test_the_launch_command_targets_the_factory_that_actually_exists` |
| The allowed origin must be a concrete origin, in any environment | `src/pree/config.py` | `test_an_origin_that_is_not_a_concrete_origin_is_refused_in_any_environment` |
| Atomic writes; a failed write never becomes the snapshot | `src/pree/store.py` | `test_a_failed_write_fails_closed_and_leaves_no_temporary_file` |
| Merges never shrink the stored dataset | `src/pree/store.py` | `test_merge_never_deletes_a_key_the_update_omitted` |
| Non-root numeric user, no suid or sgid bits, one flattened layer | `Dockerfile` | `tests/test_boot_contract.py` |
| Hash-locked dependencies, scanned for vulnerabilities | `requirements.txt` | `pip-audit` in `scripts/verify.sh` |

## Deliberately accepted risks

Each of these is a decision, not an oversight. Recorded here rather than left implicit.

1. **Shared-token model, no per-user identity.** Every operator presents the same team token, so
   the audit trail records a self-declared actor label, not an authenticated identity. A
   compromised token is a compromise of the whole team's access. Accepted for the first release
   because Pree serves a single watch floor. Single-sign-on is the seam to close it: the token
   check is one function, `security.authorise`, and nothing else reads the token.
2. **Assessment confidentiality is not cryptographic at rest.** The store is a JSON file on the
   platform volume, protected by the platform's own access controls rather than by application
   encryption. Accepted because the platform volume is not multi-tenant.
3. **The actor label is caller-supplied.** It is sanitised and capped, so it cannot forge a log
   line, but it is not proof of who acted. This follows directly from risk 1.
4. **Rate-limit state is per-process and in-memory, and keyed on the peer address.** Three
   distinct limitations, stated separately because an earlier version of this entry recorded
   only the mildest of them and read as an accepted limitation while concealing a control
   failure. First, with two workers the effective limit is the configured limit times the
   worker count, and it resets on restart: accepted, this is process protection rather than a
   quota, and a shared limiter needs the Redis add-on. Second, both tiers key on the peer
   address. That is not caller-controlled, which was the actual defect in the original design
   (the fine tier keyed on the caller-supplied actor header, so a fresh label per request
   bypassed it entirely), but behind a shared platform proxy many operators may present one
   address, so they share a bucket. Accepted for a single watch floor. Third, no forwarded
   header is trusted, because the proxy chain is not verified; keying on a spoofable header
   would be worse than sharing a bucket.
5. **Scoring weights and thresholds are operational judgements, not calibrated constants.** Each
   is named and documented at its definition in `src/pree/scoring.py`. They are a starting point
   for the watch floor to tune against real outcomes, and they are stated as such rather than
   presented as validated.

6. **The file lock is advisory and local to the volume.** `flock` serialises the two workers
   in one pod, which is the scope that has the problem, and it was verified against four real
   worker processes. It is not a distributed lock: on a network filesystem, or across two pods
   sharing one volume, `flock` semantics are not guaranteed. Accepted because the platform
   schedules one pod against this volume; a second replica needs the database add-on, not a
   tighter lock.

7. **The assessment collection is capped at 5000 records rather than retained in full, and
   the cap is shared.** The store is a single whole-file document, so retention costs both
   volume and per-write time. Above the cap the oldest assessment is dropped, and the record
   just written is never the one dropped. Two consequences an operator needs stated plainly
   rather than inferred. The cap is not "your last 5000": it is the app's last 5000 across
   everyone sharing the team token, so a busy colleague can age out assessments you are still
   watching. That follows from risk 1, but nobody should have to derive it. And the bound holds
   only while the snapshot's write order covers every stored key exactly once, which the read
   path now enforces in both directions; a snapshot whose order omitted keys retained far more
   than the cap and then evicted each new write instead of the oldest record. Accepted because
   Pree scores a live watch picture rather than serving as an archive; if the watch floor needs
   longer history, that is the database add-on, not a larger file.

8. **The diagnostics read-out reports the token length when authenticated.** A boolean and a
   length, never a value, and gated whenever a token is configured. Before a token exists the
   read-out is open, which is deliberate: a first deploy needs it and has no token to present,
   and in that state there is no length to disclose.

## Corrected, not accepted

The binding gates ran twice over this scaffold and failed it both times. Every failure they
found is fixed rather than written off, and each is named here, because a fix is only
trustworthy if the failure it addresses is stated plainly.

First review: production booting with the auth gate open while this document claimed every
route was gated; a refused write destroying the whole snapshot and reading back as empty; the
fine rate-limit tier keyed on a caller-supplied header; no cap on a request body that the
framework buffers before the token gate; a non-finite number turning a boundary rejection into
a 500 that echoed caller input; and two workers losing writes to an unsynchronised
read-modify-write.

Second review, which caught three regressions introduced by the first round of fixes: the
storage probe never releasing its slot after a timeout, so a slow-but-healthy volume pinned the
pod unready and restarted it in a loop; the new rate-limit eviction policy failing open once
its key table saturated, admitting a fresh peer without bound; and the production auth refusal
being conditional on `PREE_ENV`, which defaulted to the permissive value. It also found two
controls that had never existed: the interactive documentation was served in production, and
no response carried a Content-Security-Policy or any hardening header. And it found four store
call sites still raising bare `OSError`, so the documented first-deploy mount failure produced
a framework 500 with no audit line at all.

Third review, which caught two more regressions from the second round of fixes plus three
holes the tests had left open: reporting a saturated probe pool as merely busy meant a
permanently wedged mount answered 200 from its third probe onward, so the container health
check never saw three consecutive failures and a pod with unavailable storage stayed in
service; and the new unreadable-primary recovery copied the corrupt primary over its own good
backup on the next write, making a second corruption unrecoverable. It also found `flock`
still raising bare `OSError` past the store's error contract, the cache-expiry check asserted
nowhere (deleting it froze the readiness signal for the life of the worker), and the coupling
between the Dockerfile launch target and the module factory untested, so reverting the target
left the suite green while the container could not start at all.

Third security review, which found one required control that had never existed and one
residual fail-open narrowed rather than closed by the previous round. The store had no cap and
no pruning, so a token holder writing distinct pairs at the per-address rate limit added tens
of megabytes a day to a volume that only grows, with per-write cost rising linearly because
every upsert rewrites the whole snapshot; the end state is a full volume, `ENOSPC`, a
permanent 503 on the storage proof and a pod out of service with no application-level way
back. And the busy-versus-wedged rule required EVERY held slot to have overrun, so an
unauthenticated flood of the unmetered probe path kept the newest slot always fresh: a mount
whose writes overran the probe budget reported ready indefinitely. Measured before the fix at
one 503 in twelve probes under an eight-way flood, against three consecutive needed to fail
the health check.

The cap now keeps the newest and never drops the record just written, ordered by an explicit
write-order list because the snapshot is serialised with sorted keys and object order does not
survive the round trip.

The first attempt at the probe fix was wrong, and the fourth security review caught it. It made
a busy verdict depend on a recent successful probe, and with the shipped parameters the result
cache expired one instant before that grace did, leaving a window in which a saturated pool had
no positive evidence and called a healthy volume unready. An unauthenticated flood of this
unmetered path could hold the window open: measured at four false 503s in twenty-four probes at
300 ms write latency and seven at 1.2 s, with four consecutive, which is enough to restart the
pod. The claim recorded here that it had been verified in all four directions was sampling
luck, not verification, and the guard test used a grace fifteen times the shipped value so it
never exercised the real configuration.

The heuristic is gone. Concurrent callers now JOIN the probe already in flight and report what
it reports, so a joiner cannot be wrong about the volume, and one write serves every caller.
Measured at the shipped parameters under an eight-way flood, sampling every 0.37 s: healthy at
50 ms, 300 ms and 1.2 s latency each give zero false 503s in twenty-four probes; an overrunning
mount, a refused mount and a wedged mount each give twenty-four out of twenty-four. One further
defect surfaced only in that measurement: a joiner arriving just after a slow write finally
landed reported ready and cached it, so an over-budget mount answered ready in five of
twenty-four probes. A write that misses its budget is now unready for every caller.

Each of these now has a named regression test in the control table above.

## Not accepted, and why it is not a risk here

A client-side gate is never a boundary. Pree has no browser-side flag, PIN, or hidden field
standing in for authentication. The token check and the boundary validation are server-side, and
they are the only gates.
