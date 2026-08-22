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
| Team token compared in constant time | `src/pree/security.py` | `test_token_compare_invokes_the_constant_time_primitive`, `test_the_constant_time_primitive_is_actually_reached_at_runtime` |
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
| Concurrent probe callers join the probe in flight rather than guessing | `src/pree/health.py` | `test_a_concurrent_caller_joins_the_probe_rather_than_guessing` |
| A write that misses its budget is never reported as ready | `src/pree/health.py` | `test_a_write_that_took_longer_than_its_budget_is_reported_unready` |
| An observer's own delay is never charged to the mount | `src/pree/health.py` | `test_an_observers_own_delay_is_not_charged_to_the_mount` |
| A malformed stored value is dropped rather than crashing the handler | `src/pree/store.py` | `test_a_non_object_assessment_value_is_dropped_rather_than_crashing` |
| A snapshot too deep to parse fails closed, not into an unhandled 500 | `src/pree/store.py` | `test_a_deeply_nested_snapshot_fails_closed_rather_than_crashing` |
| The liveness handlers never occupy the shared request threadpool | `src/pree/app.py` | `test_the_liveness_routes_never_occupy_the_shared_request_threadpool` |
| The five documented liveness paths exist and stay unmetered | `src/pree/app.py` | `test_the_liveness_contract_is_exactly_the_five_documented_paths`, `test_every_documented_liveness_path_is_exempt_from_rate_limiting` |
| Production refuses a short or wholly repeated team token | `src/pree/config.py` | `test_production_refuses_a_short_token`, `test_production_refuses_a_token_built_entirely_from_repetition` |
| The security policy has exactly one controls section | `docs/SECURITY.md` | `test_the_security_policy_has_exactly_one_controls_section` |
| The documented liveness paths match the pinned literals | `docs/DEPLOYMENT.md` | `test_the_documented_liveness_paths_match_the_paths_the_code_pins` |
| Every control row here cites a test that exists | `docs/SECURITY.md` | `test_every_control_row_cites_a_test_that_exists`, `test_the_where_column_of_every_control_row_points_at_a_real_file` |
| The deployment sheet documents only probe behaviour the code can produce | `docs/DEPLOYMENT.md` | `test_the_deployment_sheet_documents_only_probe_behaviour_the_code_can_produce` |
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
| Hash-locked dependencies, scanned for vulnerabilities | `requirements.txt` | `scripts/verify.sh` |
| The Dockerfile contract is asserted by parsing, not by substring | `Dockerfile` | `tests/test_boot_contract.py` |
| A rejected body cannot write an unbounded audit line | `src/pree/app.py` | `test_a_rejected_body_cannot_write_an_unbounded_audit_line` |
| The body cap is bounded against the granted memory | `src/pree/app.py` | `test_the_body_cap_is_derived_from_the_memory_the_platform_grants` |
| No document states a token size but the enforced one | `docs/` | `test_no_document_states_a_token_size_but_the_enforced_one` |
| Sonar scans `src` only, read as a resolved property | `sonar-project.properties` | `test_the_sonar_configuration_scopes_sources_to_src` |
| The upload archive carries no credential-shaped path | `scripts/package-appstore.sh` | `scripts/package-appstore.sh` |
| No handler writes an audit line an unauthenticated caller can size | `src/pree/app.py` | `test_a_long_request_path_cannot_write_an_unbounded_audit_line` |
| Every hardening step runs in the stage that actually ships | `Dockerfile` | `test_every_hardening_step_runs_in_the_stage_that_actually_ships` |
| The shipped collection cap matches the sheet and the volume | `src/pree/store.py` | `test_the_shipped_collection_cap_matches_the_sheet_and_the_write_budget` |
| The access log cannot be sized by an unauthenticated caller | `src/pree/audit.py` | `test_the_access_log_filter_bounds_the_request_line` |
| The suid sweep is exactly the vetted command | `Dockerfile` | `test_the_suid_sweep_is_exactly_the_command_that_clears_every_bit` |
| No forwarded header can rewrite the rate-limit key | `Dockerfile` | `test_the_launch_command_refuses_to_trust_a_forwarded_client_address` |
| The pip removal targets the venv the build creates | `Dockerfile` | `test_the_pip_removal_targets_the_venv_the_build_actually_creates` |
| The runtime user is created unprivileged | `Dockerfile` | `test_the_numeric_user_is_created_unprivileged` |
| Every rejection uses one error contract and is audited | `src/pree/app.py` | `test_every_rejection_uses_one_error_contract_and_is_audited` |
| Both rate-limit tiers answer identically and keep Retry-After | `src/pree/app.py` | `test_both_rate_limit_tiers_answer_identically_and_keep_retry_after` |
| The limiter never turns a 429 into a 500 under concurrency | `src/pree/ratelimit.py` | `test_concurrent_callers_never_turn_a_429_into_a_500` |
| The access-log filter is installed by the factory, not by its own test | `src/pree/app.py` | `test_the_factory_installs_the_access_log_filter` |
| The access log is bounded in the mapping shape gunicorn emits | `src/pree/audit.py` | `test_the_access_log_filter_bounds_the_mapping_shape_gunicorn_emits` |
| Retry-After is read under the same lock as the count | `src/pree/ratelimit.py` | `test_the_retry_after_read_happens_under_the_same_lock_as_the_count` |
| A bodiless status never carries a body | `src/pree/app.py` | `test_a_bodiless_status_stays_bodiless` |
| No pre-auth redirect hands the token to a caller-named host | `src/pree/app.py` | `test_a_trailing_slash_is_a_404_not_a_redirect` |
| A forwarding header collapses the rate-limit key rather than steering it | `src/pree/app.py` | `test_a_forwarding_header_cannot_widen_the_rate_limit_key_space` |
| Unauthenticated traffic cannot consume the operators' rate budget | `src/pree/app.py` | `test_an_unauthenticated_flood_does_not_exhaust_the_authenticated_budget` |
| An ambiguously framed request is refused and the connection closed | `src/pree/app.py` | `test_a_request_declaring_both_framings_is_refused` |
| SHELL is refused, so a RUN's text is its command | `Dockerfile` | `tests/test_boot_contract.py` |
| The shipped COPY carries no flags that undo the hardening | `Dockerfile` | `test_the_shipped_stage_is_exactly_one_copied_layer` |
| Every base image is pinned by digest | `Dockerfile` | `test_every_base_image_is_pinned_by_digest` |
| The upload archive ships only allowlisted extensions | `scripts/package-appstore.sh` | `scripts/package-appstore.sh` |
| The authenticated rate space needs a valid token, not a present one | `src/pree/app.py` | `test_the_authenticated_key_space_needs_a_valid_token_not_a_present_one` |
| An ambiguous frame is refused on every method, bodyless included | `src/pree/app.py` | `test_a_request_declaring_both_framings_is_refused_on_a_bodyless_method` |
| Nothing writes over a binary the hardening steps name | `Dockerfile` | `test_nothing_writes_over_a_binary_the_hardening_steps_depend_on` |
| The shipped stage declares nothing that undoes a control | `Dockerfile` | `test_the_shipped_stage_declares_no_instruction_that_undoes_a_control` |
| The storage probe publishes the data directory only on failure | `src/pree/health.py` | `test_the_storage_probe_publishes_the_data_directory_only_on_failure` |
| Every read of the assessment store is audited | `src/pree/app.py` | `test_every_read_of_the_store_is_audited` |
| A refused CORS preflight uses one contract and is audited | `src/pree/app.py` | `test_a_refused_cors_preflight_uses_the_same_contract_and_is_audited` |

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

   That third sentence was FALSE as shipped for two rounds, and it is worth saying so rather
   than editing it quietly. uvicorn installs its proxy-header middleware unconditionally and
   gunicorn's trust list defaults to loopback plus whatever `FORWARDED_ALLOW_IPS` holds, so the
   peer address the limiter keys on was being replaced by a caller-supplied `X-Forwarded-For`
   before the application ran. A sidecar ingress forwarding over loopback is trusted by that
   default, and `FORWARDED_ALLOW_IPS=*` is a common platform value, so the environment alone
   could have turned both tiers off.

   There are now two independent controls. The launch command pins
   `--forwarded-allow-ips=255.255.255.255`, the limited broadcast address, which can never be
   the source of a TCP connection, and an explicit flag beats the environment default so this
   cannot be widened from outside the image. And `_limit_keys` charges any request that carries a
   forwarding header at all into one shared key, so the control survives a launch command the
   platform supplies rather than living entirely in a flag, which is how the original defect
   stayed invisible.

   Measured at the shipped two workers, 1,000 requests with a rotating header each time:
   **0 refused** with neither control, **615 refused** with the shipped build, and **671
   refused** with the flag removed but the in-app fold present. The middle figure is the point:
   either control alone closes it.

   An earlier version of this entry reported "0 of 300 refused before and 60 of 300 after". Both
   numbers were taken at one worker while the shipped command runs two, where the effective
   limit doubles, so the pair could not discriminate and contradicted this entry's own first
   sentence about worker count. The reviewer caught that, not I, and the figures above replace
   it at the shipped configuration.

   If operations later needs real client addresses, set the flag to the ingress address
   specifically, never to `*`, and remove the in-app fold deliberately at the same time.
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

Fifth review: two majors of evidence rather than code. A test passed with the control it was
named after deleted, and the register cited it as that control's evidence while the test which
actually pins the control went uncited. And the deployment sheet still described a busy probe
pool returning 200 with status "unknown" and errno EBUSY for the endpoint that gates pod
restarts, two commits after that behaviour was removed, with a register row asserting the same
retired control and citing a test deleted alongside it. Five residual defects came with them,
two of them fail-closed by luck rather than construction: the budget judged on the observer's
clock rather than the write's own duration, and the cache stamped at observation time.

Sixth review: making the liveness handlers synchronous again left the entire suite green while
measuring a 500-fold liveness latency regression under an unauthenticated flood of the unmetered
probe path, enough to restart a healthy pod. A RecursionError from a deeply nested snapshot
escaped the store's error contract into an unhandled 500 with no hardening headers and no audit
line. And both guards added in the fifth round were themselves defeatable: the register check
matched bare test names only, and the deployment-sheet check was a denylist wearing the name of
a property.

Four times across those two rounds a fix shipped with nothing distinguishing it from the
behaviour it replaced. That is the recurring failure of this work, and it is a failure of
testing rather than of code: the reviewer named two instances, mutation testing found the third,
and the reviewer found the fourth. Every control named in those reviews was mutation-tested and each mutation was caught. That is a statement about the controls the reviews examined, not a universal claim over every row in the table above: an earlier version of this sentence asserted the universal on the strength of a partial sample, which is the same evidence inflation the paragraph above it apologises for.

Seventh review: three majors, all of them in the guards added by the previous two rounds
rather than in the code they protect, which held every attack that round ran. The liveness
guard derived its expectation from the very constant it was policing, so deleting a path from
that constant left the whole suite green while the route returned 404 and silently lost its
rate-limit exemption. The register guard was defeated six ways, including a four-cell row, an
indented row, a row whose first cell was the word "Control", an existing but unrelated
citation, backticked prose, and a non-existent test name with trailing parentheses. The
deployment-sheet guard was defeated six ways, including an uppercase status, a hyphenated one,
an unquoted one, an HTTP code stated in prose, a fabricated response header, and a 204 on
readiness. All three are rebuilt to fail on anything they cannot check rather than skip it, and
every one of those twelve fabrications is now caught. The rebuilt register guard immediately
found real drift: two rows citing test names that had been renamed without the register
following. One security fix came with them: production accepted a one-character team token,
and now refuses anything shorter than 32 characters or built entirely from a repeated sequence.

Tenth review: two majors, both in the Dockerfile guard rather than in the image. Every
assertion about the build was a substring search over the file text, and the reviewer defeated
the set two ways docker itself resolves differently. A BuildKit heredoc body was read as a
build stage, so a decoy `FROM scratch` block satisfied all six resolved-state assertions while
the real final stage ran `USER root` and bound the loopback interface only. An exec-form
`ENTRYPOINT` alongside the `CMD` meant docker passed the gunicorn command line as arguments to
something else, so the server never started, and the suite stayed green through both. The file
is now parsed into stage, keyword and argument triples, unknown keywords and heredocs are
refused outright rather than skipped, and the launch command is read from the resolved final
stage. Both defeats were re-applied afterwards and both now turn the suite red.

Three guards were rebuilt alongside them. The Sonar check read a line rather than the resolved
property, so appending `sonar.sources=.` below the correct line scanned the whole checkout with
the assertion still green. The audit line for a rejected body was bounded only by the body cap,
and five 5,000-character field names inside a 25 KiB body produced a 25,422-byte log record, so
filling the log volume was cheaper than filling the data volume and needed no token. The
packaging script refused five directory names and nothing by shape, so a `.pem` or a file whose
name reads like a credential shipped to the App Store unremarked.

One documentation defect is worth naming in its own right, because it was mine and not the
reviewer's to find. The token floor was raised to 32 characters and the character-variety rule
was deleted, and my commit message for that change claimed the wording had been narrowed "in
the comment, the register row and the deployment sheet". Only `docs/SECURITY.md` was changed.
`docs/DEPLOYMENT.md` still told the operator about a character-variety rule the code no longer
has, alongside the superseded floor. Both documents now state the enforced number, and a guard reads
`MIN_PRODUCTION_TOKEN_LENGTH` from the source and fails on any sentence about the token that
states a different figure or names the retired rule, so the prose cannot drift from the
constant again.

Eleventh review: three majors, and the first of them is the same defect as round ten's own
headline, one function higher up the file. Bounding the audit line for a rejected body left the
request PATH unbounded, and a path is the cheaper attack: no upload, no valid token. A
15,000-character request line wrote a 30,074-byte 401 record, because JSON escaping of control
characters doubles the bytes on the way in, and h11 admits roughly 16 KiB of request line. All
three handlers now truncate the path, and the reason string with it.

The second was a hard rule with no guard behind it. The suid and sgid sweep was asserted to
exist in some stage, and the shipped layer was asserted to be copied from some stage, and
nothing joined the two. Moving the sweep into the `build` stage shipped every setuid binary the
base image carries, `su`, `mount`, `passwd` and `newgrp` among them, with the whole suite green;
repointing the shipped COPY at `build` shipped the unswept stage with pip in it. The guard now
reads the `--from=` of the single shipped COPY and requires the sweep, the pip removal and the
numeric user creation all to run in that stage, and refuses a positional stage reference.

The third was this round's own headline left unasserted. The ten-error cap on the logged error
list was correct in code and tested nowhere: the test sent five field names, so the cap was
never reached. Deleting it wrote a 142,290-byte record from a 31,944-byte body, 5.6 times worse
than the defect round ten fixed. The test now exceeds the cap and asserts the exact count.

Four minors came with them, each an overstated claim of mine rather than a defect in the app.
The token-floor guard caught one number form of five: a hyphenated figure, a spelled-out one, a
sentence naming the credential but not the token, and a figure inside a table cell all passed,
and a reworded variety rule was never examined because the floor gate ran first. Fragments are
now built per line so a table row stays whole, the retired-rule check runs before the floor
gate, and all eight fabrications are caught. The packaging scan named no certificate extension
despite a comment claiming it did, followed symlinks so a link named `notes-appendix.md`
shipped a private key's contents, and missed `.netrc`, `authorized_keys` and `creds.txt`; it
now stores links as links, refuses them outright, and says plainly that it reads names and
never bytes. A `# escape=` parser directive was invisible to the Dockerfile parser and honoured
by docker, which split a continuation the parser had swallowed and left the resolved user as
root: unknown parser directives are now refused the way heredocs are. And the shipped
`MAX_ASSESSMENTS` value was unpinned, because every cap test monkeypatched it: raising it to
10^9 left the suite green.

Nine of nine mutations in the reviewer's own list now turn the suite red, verified one at a
time. The pattern across the last four rounds is worth stating plainly, because it is not
flattering: the application code has held every attack these rounds have run, and every defect
found has been in a guard, in a document, or in a claim I made about one of them.

Twelfth review: four majors, and for the first time in five rounds one of them is in the
application rather than in a guard.

The rate limiter's eviction pass was not thread-safe, and the fine limiter is genuinely
concurrent: the scoring handler is synchronous, so Starlette runs it in the thread pool. Three
failures lived in one function. `sorted(...)` iterated the key table while another thread
inserted into it; two threads selected the same eviction candidate, so the second delete raised
KeyError; and a deque emptied by one thread raised IndexError in another. Reproduced at 375
exceptions in 4,000 concurrent calls, and every one of them was a 500 with no audit line and
none of the hardening headers, in place of the 429 the limiter exists to return. The
bookkeeping is now serialised and each delete tolerates an already-gone key.

The second major was the log-amplification defect again, through a channel the application does
not own. Round eleven bounded every audit line the handlers write; the shipped launch command
passes `--access-logfile -`, so uvicorn writes the raw request line for every request, and
`/healthz` is deliberately exempt from the limiter. Measured under the real launch command:
15,046 bytes of log for one unauthenticated request, and a three-second burst wrote 31 MB,
about 620 MB a minute per worker, with every request answered 200. That both fills the log
volume and buries the audit trail the bounded handlers exist to produce. A truncating filter is
now attached to both access loggers by the factory, so it applies in every worker; the same
request now writes 208 bytes, measured the same way.

The remaining two majors were the Dockerfile guard again, one layer deeper than last round.
Round eleven tied each hardening step to the stage that ships; nothing checked what the steps
actually did. `find /app` in place of `find /`, a leading `-false`, and a trailing `|| true`
each left the suite green while every setuid binary the base image carries shipped, and the pip
removal repointed at a path that does not exist passed the same way. The resolved command is
now asserted: root, `-xdev`, no narrowing predicate, no tolerated failure, and a removal tied
to the venv the build stage creates. A `# escape = ` directive with one space around the equals
sign also reopened the exact hole the previous round closed, because the check skipped any
fragment with a space before the `=`; the pattern is now BuildKit's own.

Six minors came with them. The path bound was 96 characters against a longest legitimate path
of 145, so a real store key was truncated out of every 401 and 503 record; and the test's
1,024-byte ceiling was falsified by an input it never tried, because an astral code point costs
twelve JSON bytes rather than one. The packaging scan anchored its extension list on the end of
the whole path, so `deploy.key.txt` and `tls.pem/server.bundle` both shipped a real private
key, and it saw neither hard links nor a Cyrillic homoglyph. The token-floor guard admitted four
more forms, so it was inverted: rather than matching floor phrasings, it now requires every
figure attached to a size word in any line mentioning the token to be the enforced constant,
across six files. The per-record figure in the deployment sheet was a best case published as a
planning number, understating the volume by a third, and the volume size it was checked against
appeared in no document. And a 5,000-digit integer left the application's own error contract
entirely, answering with the framework's message shape and writing no audit line, while the two
tiers of the rate limiter disagreed about their own response body.

Two defects in this round were found by neither the reviewer nor a test, and both are worth
recording because of how they surfaced.

The pipeline simulation, re-run for the first time since the ignore-rule guard was rebuilt from
a text check into a behavioural one, failed immediately: the platform runs the suite from the
extracted archive, which carries .gitignore and no .git, and `git check-ignore` outside a work
tree fails for every path. The behavioural version had never run anywhere but a developer
checkout. Worse, my first fix asserted that the platform runner must always have a work tree,
on the reasoning that it commits its own generated pipeline file into a checkout. The simulation
disproved that assertion on the next run. The belief was mine and it was wrong, so the guard now
claims only what it can support: the ignore rules protect the environment where someone commits,
that environment is a work tree by definition, and the local loop always runs in one.

One defect in this round was found by neither the reviewer nor a test. Coverage reported two
unreachable statements in the new access-log filter, and the reason was that `logging` sets
`record.args` to an empty tuple rather than to None when a caller logs an already-formatted
string. The filter's guard tested the type and not the emptiness, so the pre-formatted branch
could never run, and gunicorn's own access logger formats its line before logging it. A
coverage figure is not a test, and this is the case for reading it anyway.

Thirteenth review: two majors, and the first was defeated live against a running server rather
than reasoned about, which is why it is the most serious finding in this project's history.

Both rate-limit tiers key on the peer address, and the peer address was caller-controlled.
uvicorn installs ProxyHeadersMiddleware unconditionally; gunicorn's trust list defaults to
loopback plus whatever `FORWARDED_ALLOW_IPS` holds; and the middleware rewrites the client
address before the application sees the request, so nothing in the application could tell. 300
requests with a rotating `X-Forwarded-For` were all admitted where 60 should have been refused,
and 60 of 60 writes to the scoring path were accepted against a limit of 20. Accepted risk 4
asserted that no forwarded header was trusted; it was false as shipped, and it stayed false for
two rounds while three separate reviews looked at the limiter. The fix is in the launch command,
because the application cannot recover a peer address that was overwritten above it: the trust
list is pinned to the limited broadcast address, an explicit flag beats the environment default,
and a test asserts the flag on the resolved CMD. Verified the same way it was broken, under the
real launch command, including with `FORWARDED_ALLOW_IPS=*` set: 0 of 300 refused before, 60 of
300 after, and the environment variable could not reopen it.

The second major was the sweep guard, for the third round running. It refused eight neutering
tokens and the reviewer found four more, each leaving every test green while the sweep cleared
nothing: `-not -perm /6000`, `-newer /etc/hostname`, `-regex ".*/no-match"` and `-uid 4242`, two
of them confirmed against a real fixture carrying 4755 and 2755 files. `find`'s predicate
grammar is open-ended, so a denylist was always going to lose. The command is now asserted
literally, as an allowlist of one, and changing the sweep means changing the literal too.

Six minors. Nothing tied the access-log filter to the app, so replacing the factory's call with
`pass` left the whole suite green, which is the same shape as the sweep defect a round earlier.
The filter did not bound the record shape gunicorn's own access logger actually emits, a format
string plus a mapping, measured at 15,056 bytes untruncated, while a docstring of mine claimed
both shapes were covered. `retry_after_seconds` read the shared deque outside the lock added one
function above it. The handler returned a JSON body for statuses that must not carry one. The
packaging word list had "passwd" but not "password" and "key" only as a dotted extension, so
five more credential-shaped paths shipped. And the store's per-record figure was asserted equal
to itself: the comment said "measured through the real scoring path" and the test measured
nothing, so any schema growth would have left the sheet, the test and the volume request
agreeing while all three understated reality. It now measures, and asserts the published figure
is never below the measured one.

Two of my own guard fixes this round were wrong before they were right, and both are recorded
because the pattern matters more than the individual errors. The token-floor guard was rewritten
to check every integer in a line mentioning the token, on the reviewer's own suggestion, and
that read a seventy-row control table as a single claim and produced pages of noise: a guard
that cries wolf gets relaxed, so it is worse than the hole it closes. It now bounds a unit to a
line or a pair of adjacent table rows and allows up to two words between the figure and the size
word, which catches all eighteen fabrications tried against it. The exemption list that came
with it was itself the hole in the next attempt: 20 is the per-actor rate limit and also a
plausible wrong floor, so exempting it admitted "no fewer than twenty printable characters". The
list is gone rather than trimmed. And the first measurement helper for the per-record figure
invented a record larger than the scorer can emit, 2,354 bytes against a real maximum of 1,679,
which would have forced the sheet to publish a number 38% above anything real.

Fourteenth review: two majors, and the more serious was not something the last round broke but
something the last round made worse. Starlette redirects a trailing slash by default, and it
does so before any dependency runs, so `POST /v1/assess/` answered 307 with an absolute Location
built from the caller's own Host header, unauthenticated. A 307 preserves the method, the body
and the headers, so a client that follows it re-sends the team token; measured live with
`Host: attacker.test`, the Location was `http://attacker.test/v1/assess`. Pinning the forwarded
trust list in the previous commit removed the only thing that had been keeping that redirect on
TLS, because the scheme is now unconditionally http. An operator who typed a trailing slash and
followed redirects would have put the token on the wire in cleartext. The redirect is off; a
trailing slash is a 404.

The second major was the sweep guard again, and this time with the guarded line untouched.
`SHELL ["/bin/true"]` above the sweep changes how every later RUN is executed without changing
a character of it, so an exact-match allowlist became a statement about a string docker never
runs, and the whole suite stayed green while nothing was swept. `COPY --from=prep --chmod=0777
--chown=0:0 / /` did the same job from the other end, shipping the filesystem world-writable and
root-owned past a test that only checked the copy's source. SHELL is now refused outright, and
the shipped copy's every token is asserted. Three rounds of this guard have now failed in three
different places, which is what it looks like when a hard rule is verified by text because no
daemon is available to verify it by building.

Eight minors. Four deserve naming.

The token-floor guard has been rewritten four times and lost four times, and the reason was
always the same: it asked whether a sentence looked like a floor claim, which needs a complete
table of number words, of size words, of token synonyms and of adjacency, and each round the
reviewer found the missing entry. It is now inverted. In any unit that mentions the token and
mentions a size, the enforced constant must appear and no other number may. That needs no
complete table of anything, because a wrong floor is wrong for being a number that is not 32,
whatever surrounds it. The cost is real and worth stating: a legitimate sentence pairing the
token with any other figure now fails, and two in this repository did, which is why a changelog
entry was split in two. Twenty fabrications now turn it red.

The packaging scan was a denylist for three rounds and shipped a real private key each time
under a name one character outside the list: `.crt` was listed and `.cert` was not, `.gpg` and
not `.pgp`, `.kdbx` and not `.kdb`, `authorized_keys` and not `authorized_keys2`, `deploy_key`
and not `deploy_key.txt`. Enumerating every name a credential might have is not a finite task.
The archive is now checked against an allowlist of the extensions this project actually ships,
so an unknown extension fails by default, AND against the name denylist, because five of the ten
paths that beat the old scan wear an extension this project genuinely ships. Replacing one net
with the other was my first attempt and it shipped five of them; both nets are needed.

The per-record volume figure has been wrong three times, each time in a way that flattered it:
1258 bytes was a best case presented as a worst case, and 1705 was one tidy set of indicator
values presented as a maximum, 17 bytes under the reachable one. The record's size is dominated
by float representation rather than by the schema, so it moves with a scoring change that alters
no field. The suite now searches every present-or-absent indicator combination with the
longest-serialising values, finds 1722, and the sheet publishes 2048 as a ceiling with headroom
while asserting both numbers, so the two cannot drift apart in the flattering direction.

And a measurement I reported was wrong. The previous round's entry said "0 of 300 refused before,
60 of 300 after". Both figures came from a one-worker server while the shipped command runs two,
where the effective limit doubles, so the pair could not discriminate and contradicted the same
entry's own first sentence about worker count. The reviewer caught it, not I. Restated at the
shipped configuration, with a third arm the original lacked: at two workers, 1,000 requests with
a rotating header, 0 are refused with neither control and 615 with the shipped build.

The remaining four minors are smaller and each closed with a test: the coarse tier runs before
authentication, so its key space is now split by whether a token was presented, and an
unauthenticated flood can no longer consume the operators' budget; the forwarded-header control
no longer lives only in the launch command, because `_limit_keys` charges any request carrying such
a header into one key; the base digest, which a comment claimed was pinned, is now asserted; and
a request declaring both a Transfer-Encoding and a Content-Length is refused with the connection
closed rather than framed by one parser and re-read by another.

Two of my own tests this round were worthless before they were useful, in the same way. The
forwarded-header test rotated the header through the test client and asserted a 429, which it got
with the control removed as well, because the test client's peer address never varies: the
limiter was refusing for the wrong reason. Varying the peer needs the ASGI scope built by hand.
And the register guard could not see an `async def`, so any row citing an async test read as
citing a nonexistent one, which had quietly pushed the register towards citing file paths instead
of test names, weaker evidence for no reason at all.

Fifteenth review: three majors, and two of them were controls added in the previous two rounds
that did not do what their own commit messages said.

The Transfer-Encoding plus Content-Length refusal was placed AFTER the middleware's
bodyless-method early return, so it never ran for GET, HEAD, OPTIONS, DELETE or TRACE. That is
precisely the method class smuggling uses, because a front end permits a GET with no body, which
makes GET the canonical carrier. One socket write of `GET /healthz` with both framings produced
two 200 responses. The refusal was written to close that exact primitive and, for the five
methods it matters most for, did not. It now runs first, and every bodyless method is refused
with the connection closed, verified against the running server.

The rate-limit key space was caller-selected. The split between authenticated and
unauthenticated traffic was decided on the PRESENCE of the token header rather than its
validity, so an unauthenticated caller reached the operators' space with
`X-Pree-Token: anything`; and the fold that stopped a rotating forwarding header from minting
fresh buckets put the request in a DIFFERENT bucket from the peer's own, so a caller already at
its limit escaped by adding a header. Together one peer reached four buckets by toggling two
headers it fully controls, and the reviewer measured 1,920 requests admitted against a nominal
480. The space is now decided by the constant-time compare that was already available, and a
request is charged to every key it belongs to and refused if any is over, so a header can only
ever reduce an allowance. Measured the same way after the fix, at two workers, 720 requests per
arm across five arms: 480 admitted in total, exactly the nominal two-worker budget.

The third major was the suid sweep guard, for the fourth consecutive round, and it is worth
being blunt about the pattern rather than reporting another repair. The command's text is pinned
literally, `SHELL` is refused, and the guard was still defeated twice: `ENV PATH="/neutered:$PATH"`
above the sweep with a no-op `find` planted on that path, and the single line
`COPY --from=build /bin/true /usr/bin/find`. Both left the whole suite green, and the reviewer
confirmed against a real fixture that files at 4755 and 2755 survived. Pinning what a command
SAYS can never establish what it DOES. The sweep now names both binaries absolutely, which
removes PATH from the question entirely, and an instruction writing into any system executable
directory is refused. More importantly, the pipeline's containerize stage now asserts the three
container hard rules against the BUILT IMAGE: no setuid or setgid bits anywhere on the
filesystem, a runtime identity of exactly 10001:10001, and no pip. Those assertions need a
Docker daemon, so they do not run here, and until Continuous Integration builds the image those
three rules are claims and not evidence. That is stated in the simulation's own output now,
rather than left for a reviewer to notice for a fifth time.

Eight minors, four of which are corrections to guards or claims of mine.

The token-floor guard was rewritten for the fifth time, and the honest outcome is a narrower
scope rather than a cleverer rule. Four fabrications beat the previous version: a floor wrapped
over two prose lines, "eighteen" and "twenty-eight" as words absent from a hand-written table,
and "under 16" with no unit. Fixing the word table by construction rather than by enumeration
fixed those, and then flagged fourteen true sentences, because the policy and the changelog
narrate this guard's own history in measurements: "five 5,000-character field names", "a
30,074-byte record", each in a sentence that also says "token". No text rule can separate a
measurement from a floor claim without understanding the sentence. So the guard now covers the
instruction-bearing files only, the deployment sheet, the README, this baseline and the example
environment file, completely, digits and words alike; and it does not cover this policy or the
changelog, where a wrong floor asserted in prose would pass. That is a real gap, deliberately
chosen, on the reasoning that the sheet is where a wrong number becomes a wrong deployment.
Twelve fabrications now turn it red, and lowering the code's own floor turns two tests red.

The packaging scan lost for a fifth round, each time to a name one character outside its list:
`.crt` but not `.cert`, `deploy_key` but not `deploy_key.txt`, then `deploy_key.txt` fixed and
`deploykey.txt` shipping because deleting the delimiter beat the delimited-word rule, and
`id-rsa.md` shipping because the pattern spelled `id_rsa` with a literal underscore while its
neighbour on the same line used a wildcard for exactly this reason. `vault` and `jwt` were two
ordinary credential names in no list at all. The key rule is now the loosest thing that still
means something, any component ending in "key" or "keys", and the closing message says the scan
matched no name on a list rather than claiming nothing reads like a credential.

The unauthenticated storage probe published the resolved absolute data directory in its 200
body, while `/diagnostics` gated the same field on the reasoning that configuration detail
narrows an attacker's search space for free. The deployment sheet described the directory as
appearing in the 503 body, which was true of the sheet and not of the code. It now appears only
on failure, where it earns its place: a screenshot of the 503 is the whole diagnosis.

A successful read of the assessment store wrote no audit line. A refused request was audited, a
rejected body was audited, a store failure was audited, and a successful DISCLOSURE of a record
was not. This policy names that store as one of the two assets worth protecting, because it
reveals what the operator is watching and what they judge dangerous, and under the shared-token
model "who read what" is the only forensic question the trail could answer about a stolen token.
Every read is now audited with its outcome: disclosed, not modified, or not found.

The remaining minors: a refused CORS preflight was answered by Starlette in plain text with no
audit line, outside the single error contract this policy claims; `HEALTHCHECK NONE`, `VOLUME`
and `STOPSIGNAL` in the shipped stage all passed silently, and `HEALTHCHECK NONE` disables the
storage proof three earlier rounds treated as the control keeping a broken mount out of service;
a `_FLOOR_PHRASES` constant sat in the test module with a comment describing a rule the guard did
not implement, which is exactly the evidence inflation this document apologises for elsewhere,
and it is deleted; and the per-record volume figure, wrong three times before, is now searched
across all 64 indicator combinations rather than measured at one set of values.

Each of these now has a named regression test in the control table above.

## Not accepted, and why it is not a risk here

A client-side gate is never a boundary. Pree has no browser-side flag, PIN, or hidden field
standing in for authentication. The token check and the boundary validation are server-side, and
they are the only gates.
