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
| Every filesystem refusal surfaces as a handled 503, audited | `src/pree/store.py` | `test_a_real_storage_refusal_returns_503_and_audits_the_action` |
| The rate limiter fails closed when its key table saturates | `src/pree/ratelimit.py` | `test_a_saturated_key_table_fails_closed_rather_than_admitting_everyone` |
| A busy probe pool never reports storage as broken | `src/pree/health.py` | `test_a_busy_pool_is_indeterminate_rather_than_unready` |
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

7. **The diagnostics read-out reports the token length when authenticated.** A boolean and a
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

Each of these now has a named regression test in the control table above.

## Not accepted, and why it is not a risk here

A client-side gate is never a boundary. Pree has no browser-side flag, PIN, or hidden field
standing in for authentication. The token check and the boundary validation are server-side, and
they are the only gates.
