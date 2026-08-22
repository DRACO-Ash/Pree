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
| An ambiguously framed request is refused and the connection closed | `src/pree/app.py` | `test_a_request_declaring_both_framings_is_refused` |
| SHELL is refused, so a RUN's text is its command | `Dockerfile` | `tests/test_boot_contract.py` |
| The shipped COPY carries no flags that undo the hardening | `Dockerfile` | `test_the_shipped_stage_is_exactly_one_copied_layer` |
| Every base image is pinned by digest | `Dockerfile` | `test_every_base_image_is_pinned_by_digest` |
| The upload archive ships only allowlisted extensions | `scripts/package-appstore.sh` | `scripts/package-appstore.sh` |
| An ambiguous frame is refused on every method, bodyless included | `src/pree/app.py` | `test_a_request_declaring_both_framings_is_refused_on_a_bodyless_method` |
| Nothing writes over a binary the hardening steps name | `Dockerfile` | `test_nothing_writes_over_a_binary_the_hardening_steps_depend_on` |
| The shipped stage declares nothing that undoes a control | `Dockerfile` | `test_the_shipped_stage_declares_no_instruction_that_undoes_a_control` |
| The storage probe publishes the data directory only on failure | `src/pree/health.py` | `test_the_storage_probe_publishes_the_data_directory_only_on_failure` |
| Every read of the assessment store is audited | `src/pree/app.py` | `test_every_read_of_the_store_is_audited` |
| A refused CORS preflight uses one contract and is audited | `src/pree/app.py` | `test_a_refused_cors_preflight_uses_the_same_contract_and_is_audited` |
| The framing guard is the outermost middleware | `src/pree/app.py` | `test_the_frame_guard_is_the_outermost_middleware` |
| A refused preflight is metered | `src/pree/app.py` | `test_a_refused_preflight_is_metered` |
| A guessing run is bounded by the ordinary limiter, with no oracle | `src/pree/app.py` | `test_a_guessing_run_is_bounded_by_the_ordinary_limiter` |
| An unauthenticated flood cannot refuse a request the limit would admit | `src/pree/app.py` | `test_an_unauthenticated_flood_cannot_refuse_a_request_the_limit_would_admit` |
| A preflight to a probe path is refused without an audit line | `src/pree/app.py` | `test_a_preflight_to_a_probe_path_is_refused_without_an_audit_line` |
| A wrong method on a probe path is metered and not audited | `src/pree/app.py` | `test_a_wrong_method_on_a_probe_path_is_metered_and_not_audited` |
| Every liveness path answers HEAD as well as GET | `src/pree/app.py` | `test_every_liveness_path_answers_head_as_well_as_get` |
| Only vetted RUN commands may touch an executable directory | `Dockerfile` | `test_nothing_writes_over_a_binary_the_hardening_steps_depend_on` |
| The base image is pinned to the vetted digest in both stages | `Dockerfile` | `test_every_base_image_is_pinned_by_digest` |
| The probe exemption is per path and per method | `src/pree/app.py` | `test_the_probe_exemption_is_per_path_and_per_method` |
| The Dockerfile parser follows BuildKit's continuation rule | `tests/test_boot_contract.py` | `test_the_parser_follows_buildkit_continuation_semantics` |
| The sentence splitter does not break at an abbreviation | `tests/test_boot_contract.py` | `test_the_sentence_splitter_does_not_break_at_an_abbreviation` |
| Production refuses a cleartext allowed origin | `src/pree/config.py` | `test_production_refuses_a_cleartext_allowed_origin` |
| The access log redacts a query string | `src/pree/audit.py` | `test_the_access_log_filter_redacts_a_query_string` |
| A malformed store key is refused at the boundary | `src/pree/app.py` | `test_a_malformed_store_key_is_refused_at_the_boundary` |
| Nothing writes over a binary a hardening step names, at any WORKDIR | `Dockerfile` | `test_nothing_writes_over_a_binary_the_hardening_steps_depend_on` |
| No instruction fetches from the network or rewrites the shipped PATH | `Dockerfile` | `test_no_instruction_fetches_from_the_network_or_rewrites_the_shipped_path` |
| Exactly one environment file ships, the root example | `scripts/package-appstore.sh` | `test_the_example_environment_file_carries_no_real_value` |

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

   There is ONE key space, and the token plays no part in choosing it. Three rounds were spent
   splitting it and each split was worse than the last: by the header's presence, an
   unauthenticated caller reached the operators' budget; by the token's validity, refusal became
   a guessing oracle worth about 53,000 attempts a minute; bounding wrong guesses per peer let
   twenty requests from anywhere on the internet deny the whole watch floor, because behind the
   ingress every operator presents one address. The residual of one space is that an
   unauthenticated caller at the shared ingress address consumes the same 240-per-window budget
   the operators draw on. That is a real cost and it is accepted, on the reasoning that it is the
   ordinary limit rather than a cheaper one, that it was accepted before any of the three splits,
   and that every attempt to do better produced a worse defect. A per-operator budget needs
   per-operator identity, which is the single-sign-on seam named in risk 1, not a header.

   An earlier version of this entry reported "0 of 300 refused before and 60 of 300 after". Both
   numbers were taken at one worker while the shipped command runs two, where the effective
   limit doubles, so the pair could not discriminate and contradicted this entry's own first
   sentence about worker count. The reviewer caught that, not I, and the figures above replace
   it at the shipped configuration.

   One residual is not in the limiter at all and belongs here. The six probe paths are exempt
   from the coarse limit for GET and HEAD, so an unauthenticated caller can drive the ACCESS log
   without bound on those methods: measured at about 3.2 MB a minute per worker. The audit
   channel is bounded (those paths write no record) and the request line is truncated to 160
   characters, so what is unbounded is the volume of short lines rather than their size. It is
   accepted because the alternative is metering the platform's own probes, which turns an
   infrastructure fault into a pod restart, and because a log volume filling is a degradation
   rather than a disclosure. If the platform's log store has a hard quota, set a retention policy
   on it rather than a limit here.

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

Sixteenth review: seven majors. Two of them defeated this round's own headline control with one
attacker-chosen header, and two more were guards that reported a pass without running.

The framing refusal moved from the innermost middleware to above the bodyless-method return
last round, and still missed every CORS preflight, because Starlette answers a preflight inside
the CORS middleware without calling down and CORS sits above the body-size layer. Measured: 200
OK and two responses on one connection, with a pipelined GET served. A second shape got through
even where the refusal did fire: the layer that normalises a refused preflight rewrote the 400
into a fresh response and dropped the `Connection: close` that the refusal sets precisely so the
trailing bytes cannot be replayed. Position is the whole control, and it took three attempts to
find the one position nothing can answer above. `FrameGuard` now holds it, a test asserts it
holds it, and the unreachable close-check that would have been the second line is deleted rather
than left reading as a control.

Choosing the rate-limit bucket by the token's validity fixed one defect and created a worse one:
refusal itself became a free oracle. Once a peer saturated its unauthenticated bucket with wrong
guesses, a wrong guess landed in the saturated bucket and returned 429 while the RIGHT token
landed in a fresh bucket and returned 200, so the caller kept guessing at full speed and read the
answer off the status code. Measured: 2,666 distinguishable guesses in three seconds, about
53,000 a minute, against the 240 a minute this project's own token-length floor is calculated
from. A peer now gets twenty wrong tokens per window, counted separately, and once that budget is
spent every token-bearing request from it is refused whether the token is right or wrong.

Refused preflights were never counted, because CORS answers them above the coarse limiter.
Measured: 902,000 bytes of log in 1.6 seconds, about 32.9 MB a minute per worker, from an
unauthenticated caller. That is the exact amplification class the bounded audit lines exist to
close, on the one path nothing metered, and this round had just added a new audit line to it.

The suid sweep guard was defeated for the FIFTH consecutive round, and both new ways were
one line. `WORKDIR /usr/bin` with `COPY --from=build /bin/true find` writes `/usr/bin/find`
while the destination token is just `find`, and the JSON form `COPY --from=build ["/bin/true",
"/usr/bin/find"]` yields a token with a bracket on the end. The destination is now resolved
against the WORKDIR in force and stripped of quoting. And `ADD https://…` replacing the
application source, plus an `ENV PATH=` in the shipped stage hijacking the `sh` and `gunicorn`
the pinned CMD resolves, both passed and are both refused now.

The worst of the seven was the behavioural image check this project added last round precisely
because text guards keep failing. Every assertion was `VALUE=$(docker run … || true)` followed by
a test for emptiness, so a failed run read as a pass; and both used `--entrypoint /usr/bin/find`,
so the mutation they exist to catch, a no-op binary copied over that path, made them print "no
setuid or setgid bits" and pass. The one behavioural verification of the container hard rules was
defeated by the very mutation that motivated it. It now exports the filesystem to the host with
`docker export` and reads the modes from the tar listing, trusts nothing inside the image, checks
the exit status of every command rather than the emptiness of its output, and asserts a positive
control first so silence cannot be a pass.

Eight minors. `GET /diagnostics?x-pree-token=<the token>` wrote the team token verbatim into the
access log, the one channel in this application that ever held it in cleartext; query strings are
now redacted and no route here takes a parameter. The read path's comment claimed a path
validator that did not exist, in a round whose own commit message called that pattern out; the
validator exists now and is asserted. The packaging script's `.env.example` exemption matched the
basename anywhere, so `docs/.env.example` carrying a live-looking token shipped while the test
that checks placeholders read only the root path; the exemption is anchored, the test reads the
whole tree, and the script refuses a second environment file. Three more packaging nets read a
tool failure as a pass, one of them using `grep -P`, which is absent from BusyBox: all three now
check exit status, and the pattern is POSIX. The base digest lived in an `ARG`, so `--build-arg`
swapped it while the guard resolved the default only; it is inline in both `FROM` lines and an
`ARG` referenced by a `FROM` is refused. Production could run with credentials against an
`http://` origin, putting the token on the wire; a non-https origin is refused in production.
And the `cors_reject` audit recorded `origin_allowed: false` for every 400 on a preflight,
including one raised for the allowed origin by a different control, so the only record of the
event misstated its cause.

Two things in this round were mine to notice rather than the reviewer's. The guessing budget's
first implementation charged every token-bearing request, which would have locked out an operator
who simply made more than twenty ordinary requests in a window: a denial of service dressed as a
control. Separating the question from the charge needed a new `spent` method on the limiter, and
that is what the code does now. And the container restarted mid-mutation-test leaving one
mutation applied in the working tree; the snapshot discipline caught it on the next diff, which
is the only reason it is not in this commit.

Seventeenth review: four majors, and the first was a control this project introduced one round
earlier that turned out to be an unauthenticated denial of service against every operator.

Twenty wrong tokens from anywhere on the internet locked out the whole watch floor for the rest
of the window, at about a third of a request a second, twelve times cheaper than the coarse
limit beside it. The guessing budget keyed on the socket peer, and behind the platform ingress
every operator presents one address, so an attacker spent a budget that was not theirs. The
previous round's own narrative had rejected the self-inflicted version of exactly this, "a denial
of service dressed as a control", and then shipped the attacker-driven version, which is worse.
The test that was supposed to protect it flooded ten times against a budget of twenty and passed.

The fix is a deletion, not another control. The key space is one bucket per peer and the token
plays no part in choosing it. Three rounds were spent splitting that space and each split was
worse than the last: by the header's presence, an unauthenticated caller reached the operators'
budget; by validity, refusal became a guessing oracle worth about 53,000 attempts a minute;
bounding the guesses produced this. A saturated peer is now refused identically whichever token
it holds, which is the property the token-length floor is calculated from, and the residual is
recorded in accepted risk 4 rather than papered over. A control that has to be repaired twice
and is worse each time is a control that should not exist.

The second major was the behavioural image check, for the second consecutive round. Last round it
read a failed command as a pass; this round the pip assertion could not match anything at all,
because `docker export` writes tar member names RELATIVE and `tar -tv` puts the mode first, so
`opt/venv/bin/pip3` is never preceded by `/` or start-of-line and the anchored pattern was dead.
The check printed its success message on every run. Proven offline: the old pattern found zero
matches in a listing containing both `opt/venv/bin/pip3` and `site-packages/pip/__init__.py`.
It now reads the last field, unanchored, covers the library directory as well as the entry
points, and there are three positive controls rather than one: the listing must be long, the mode
column must parse, and the pattern's own path prefix must be present. A check that cannot fire is
worse than no check, because it reports a pass.

The suid sweep guard fell for the SIXTH consecutive round, and this time to one redundant
character. `//usr/bin/find`, `/usr//bin/find` and `WORKDIR /usr/./bin` with a relative
destination all name the same file as `/usr/bin/find`, and all three walked past a prefix test on
the raw string; `ENV TGT=/usr/bin/find` with `COPY … $TGT` walked past it too. Destinations are
now slash-collapsed and normalised before the test, and a destination containing a variable is
refused rather than resolved. `posixpath.normpath` alone was not enough, because POSIX makes a
leading double slash implementation-defined and returns it unchanged.

The fourth major was the preflight metering added one round earlier: it called a helper that
returns early for the six exempt paths, so preflights on those paths were still uncounted while
the audit line below them was written anyway. Measured: 1,200 refused preflights across
`/healthz`, `/` and `/healthz/storage` returned 400 each with nothing counting them, and eight
threads grew the log by 435,200 bytes in 3.18 seconds, about 8.2 MB a minute per worker, 45% of
it on the channel the forensic trail lives in. The exemption exists so the platform's probes are
never throttled and the platform probes with GET, so a preflight is now metered on every path,
and a preflight to a probe path gets the contract without the audit line. Re-measured on a fresh
server per path, 400 requests each: 240 admitted and 160 refused on `/healthz`,
`/healthz/storage` and `/v1/assess` alike.

Seven minors, and four are corrections to claims. The token guard's docstring said the inverted
rule "needs no complete table of anything"; it needs a complete table of SIZE words, because
`size.search` gates the whole check, and "16 random units" states a wrong floor with no size word
in it. The number side needs no table, which is where five rounds of defeats came from, and the
docstring now says exactly that and names the residual. The packaging script's comment claimed
every check read the captured listing while four re-ran `unzip` inside a pipeline whose failure
would read as a pass; they read the variable now. The setuid parse had no positive control, so a
dead producer would have reported a pass. And the outermost-middleware assertion tested only the
POSITION, so any layer at position 0 satisfied it, while its own comment claimed only the
hardening headers may sit there.

Three smaller ones: `RateLimiter.spent` created a table entry for every key it was merely asked
about, so the table could grow from questions rather than traffic; it is deleted along with the
guessing budget that needed it. A non-preflight `OPTIONS` was charged to the coarse limiter twice,
halving its allowance. And the image listing leaked into the system temp directory on every
failure path, because it was created outside the working directory the exit trap removes.

One thing in this round was mine to notice. The middleware-position assertion I wrote to fix the
minor did not type-check as an identity comparison, and the version that did type-check asserted
nothing, because mypy read the branch as unreachable. It took three attempts to write an
assertion that both compiles and fires.

Eighteenth review: three majors, and all three were in controls this project had already
repaired at least once.

The rate-limit escape came back through one string. The socket key was spelled `socket:<ip>`
when a forwarding header was present and a bare `<ip>` when it was not, which are two different
keys, so a saturated caller added any forwarding header and got a fresh bucket: measured, 240
admitted then 240 more on the coarse tier, and 20 authenticated writes then 20 more on the
expensive path, twice the documented budget per peer and four times it at two workers. The
docstring said a header "can only ever reduce a caller's allowance" and it doubled it. The
socket key is unconditional now and the fold is additional, and the test that missed this
compared the folded key across peers without ever comparing one peer's key with and without the
header.

The unmetered-audit amplification came back through the method. `_refuse_over_limit` exempted
the six probe paths whatever the verb, so any method other than GET got a router 405 and a full
audit line with nothing counting it: 4,000 requests across eight threads, none refused, 624,000
bytes in 4.60 seconds, about 8.1 MB a minute per worker on the channel the forensic trail lives
in. That is the same amplification, at the same measured rate, that the previous commit closed
for preflights while claiming to have closed the last uncounted unauthenticated path. HEAD was
the cheapest trigger, because FastAPI does not add HEAD for a GET route. The exemption is now
for the probe rather than for the path, the 405 on a probe path is not audited, and HEAD is a
liveness method.

The suid sweep guard fell for the SEVENTH consecutive round, and this time the simplest form of
the attack was the one nobody had tried: `RUN cp /bin/true /usr/bin/find`. The guard only ever
considered COPY and ADD. Three more forms were also invisible: `normpath` strips a trailing
slash, so a DIRECTORY destination (`/usr/bin`, `/usr/bin/`, `/usr/bin/.`) never matched a prefix
test for `/usr/bin/` while docker still writes `/usr/bin/<basename>`; the variable refusal
inspected only the destination token, so `ARG D=/usr/bin` with `WORKDIR $D` reached the same
place; and `ADD <local tar> /` extracts over anything, with docker detecting the archive by
content, so a tar named `.txt` is unreadable from the file. The directory itself is now flagged,
a variable anywhere in the resolved path is refused, RUN commands that write into those
directories are checked, and ADD is refused outright rather than inspected, because this project
uses COPY everywhere and an extension list will always be one short.

Five minors. The packaging key rule omitted `/` from its delimiter class, so "key" as a whole
path COMPONENT was not a delimited word and `docs/keys/prod.txt` and `docs/key/prod.txt` shipped
a real private key past a scan whose own comment claimed a component-wide match.

That paragraph originally named `src/pree/keyring/x.py` in the same list, as fixed. It was not:
adding "/" to the delimiter class closed the directory case and left `keyring` open, because the
rule still required a delimiter AFTER "key". The next review ran the script's own pipeline and
found it, along with `keystore.json`, `keyfile.txt`, `keychain.py`, `keypair.txt` and
`sshkeygen.sh`. My own verification had shown that path refused, and it was wrong: a leftover
directory from the previous iteration of the same test loop was still present, so a different
net fired and I read it as this one. Asserting a fix that does not exist is the one failure mode
worse than the hole, because nobody looks again. The rule is now the plain substring "key",
false positives and all. The image listing's name scan read the last field of a `tar -tv` line,
which is the link TARGET for a symlink or a hard link, so every link member was invisible: a
listing containing `opt/venv/bin/pip3 -> python3.12` matched nothing, and the positive control
added one commit earlier was blind in exactly the same way. The mode-column count excluded `h`,
the hard-link type. An orphan comment still described the guessing budget this project deleted.
And `_TOKEN_TERMS` omitted "key", so "the shared access key as 16 random characters" stated a
wrong floor with a full size word in it and passed, after "secret" and "passphrase" had already
been added to that same table for the same reason. The docstring now names the token-synonym
table as a residual alongside the size-word one, because two tables that have each been one
entry short are not a solved problem.

The pattern across the last four rounds is worth stating without softening it. Every major in
this round, and five of seven in the round before, were defects in controls this project had
already fixed once: the same escape through a different spelling, the same amplification through
a different method, the same guard through a different path form. The application's own
boundaries, the constant-time compare, the boundary validation, the CSP and CORS lock, the
generic-error contract, the secret trace, have held throughout. What keeps failing is the layer
written to prove they hold, and it keeps failing in the direction of reporting a pass.

Nineteenth review: two majors, and one of them was a claim in this document rather than a defect
in the code.

The RUN-write guard added one round earlier was gated on a six-verb denylist, and three one-line
mutations walked straight through it: `RUN /bin/cat /bin/true > /usr/bin/find` needs no verb at
all, `RUN tar -xf … -C /usr/bin/` uses one that was not listed, and
`RUN python -c "open('/usr/bin/find','w')…"` names the target literally. Two mutations that were
caught were caught by accident, matching `/bin/` inside the SOURCE path rather than the
destination. Enumerating the ways a shell can write a file is the same losing game as
enumerating the ways a name can look like a credential, so the denylist is gone: any RUN
mentioning an executable directory is an offence, and the five legitimate ones are pinned by
exact text in `_VETTED_RUNS` the way the sweep itself is. Adding a RUN that touches `/usr/bin`
now means editing that literal, which is a decision a reviewer sees in the diff. Nine
fabrications turn it red, including shell redirection, `tar -C`, `sed -i`, `busybox cp`, a
`cd`-relative write and a `chmod u+s`.

The second major is worse than a hole. The paragraph above about the packaging key rule named
`src/pree/keyring/x.py` as one of three paths the fix had closed. It had not: adding "/" to the
delimiter class fixed the directory case and left `keyring` open, because the rule still required
a delimiter after "key". Five more names were open with it. My own verification had reported that
path refused, and the reason it did was a leftover directory from the previous iteration of the
same test loop, so a different net fired and I recorded it as this one. That is the second time
in this project a measurement of mine was wrong because the harness was contaminated, and the
first time the wrong result was written into this document as a completed fix. The rule is now
the plain substring, with the false positives that implies, and the paragraph is corrected in
place rather than quietly reworded.

Five minors. Four are below; the fifth I recorded as a non-reproduction and I was wrong about
it, which the next review proved. That correction is in the twentieth-round entry.
`HEAD` was exempted on `/healthz/storage`, which serves only GET, so a 405 that can never be a
platform probe was unmetered: 600 of 600 admitted, 33,000 bytes of access log in 0.62 seconds.
The exemption is now per path and per method, which is what "the exemption is for the probe, not
the path" actually means. Registering GET and HEAD on one route made FastAPI derive one operation
id for both, so the development OpenAPI document was invalid and the verify loop carried a
duplicate-operation-id warning on every run; HEAD is its own route now. The base digest was
asserted to EXIST rather than to be a particular value, so changing one character shipped a
different filesystem with the suite green: it is pinned as a literal and both stages must carry
it. And the token guard's unit builder used adjacent-line pairs, which lost to a floor split
across three lines; it builds sentences now.

That last one has a residual, and it is the third on this guard. A floor split so that the
sentence naming the token carries no number and the next carries the number without naming the
token still passes. Pairing adjacent sentences catches it and flagged three true statements in
this repository immediately, "a verdict is cached for two seconds" among them. A guard that cries
wolf gets relaxed rather than obeyed, so the pairing is not done and the boundary is written into
the test's own docstring. Widening this guard has now cost more than it bought twice running,
which is the point at which the right answer is to stop widening it and say where it ends.

Twentieth review: one blocker, three majors, three minors. And the most important item is not a
defect in the app: it is that the previous round's write-up recorded a real finding as a
non-reproduction, and a maintainer reading it would have deleted a load-bearing argument.

The nineteenth review reported that the audit-suppressed 405 dropped the `Allow` header. I
reported that I could not reproduce it, in four places: the code comment, the test docstring,
this policy and the changelog. All four were wrong. Starlette raises the 405 with `Allow` on the
EXCEPTION and sets nothing on the response, so removing `headers=exc.headers` from the suppressed
branch drops the header on all six probe paths and turns the test red. My measurement had removed
the argument from the OTHER branch, the one that serves `/diagnostics`, and then measured the
probe paths, which the suppressed branch serves. The mutation was adjacent to the control rather
than on it, which is precisely the shape of the contaminated-directory error one round earlier.
Two rounds running, a measurement of mine was wrong in the direction of reporting a control
sound, and the second time it licensed deleting one. The four places are corrected, and the test
now asserts the exact `Allow` set rather than a substring.

The blocker was a Dockerfile parser defect, and it is the fourth distinct one in this file.
BuildKit's continuation rule is `([^\\])\\[ \t]*$`: a line ending in an ESCAPED backslash is not
a continuation. This parser treated any trailing backslash as one, so
`LABEL org.opencontainers.image.title=pree\\` followed by `USER root` swallowed the USER into
the LABEL and it vanished from every assertion, while docker resolved the shipped stage's user to
root with the boot contract green. The joiner was wrong in the same place: BuildKit concatenates
continuation lines with nothing and this inserted a space, so `/usr/bi` + backslash + newline +
`n/find` read as `/app/n/find` here and as `/usr/bin/find` to docker. Both are now BuildKit's own
rule, taken from its parser source rather than approximated, and four fabrications turn them red.

The third major was the RUN branch reading literals only while the COPY branch has refused `$`
since round sixteen on the stated reasoning that chasing substitution forms is a losing game.
Three forms walked through: `/usr/b?n/find` as a glob, `D=/usr/b; … ${D}in/find`, and a command
substitution. The RUN branch now refuses `$`, a backtick, `?` and brackets, which the five vetted
RUNs contain none of.

Three minors. Splitting GET and HEAD into two routes to fix the duplicate-operation-id warning
broke something quieter: Starlette builds a 405's `Allow` from the matched route's own methods, so
a liveness path advertised `GET` alone while the resource serves HEAD, and the test could not see
it because it asserted a substring. One route carries both methods now and the liveness paths are
out of the development schema entirely, which is a trade stated in the code: FastAPI gives every
method on one route the same operation id however that id is chosen, so a two-method route in the
schema is always a duplicate. A correct `Allow` beats a dev-only schema entry for a path whose
whole contract is "200, touches nothing", and the deployment sheet documents those five paths with
a test pinning them.

The sentence splitter cut at `e.g.`, `i.e.` and `min.`, so a floor written "e.g. 16 random
characters" lost its subject to the previous half and passed; abbreviations are shielded now. A
sentence introducing a table was never paired with the row carrying the number; it is now, but
only when it ends in a colon, because pairing every trailing sentence flagged a true statement
immediately. And `register_cors` carried a parameter it never called, which read as though the
CORS layer consulted the metered-path exemption and was invisible to the coverage figure.

One structural change came out of this round rather than out of a finding. Mutating the parser's
continuation rule, its joiner and the abbreviation shielding each left all 289 tests green: the
guards were load-bearing against the shipped Dockerfile and the parser underneath them was
load-bearing against nothing. Every defect this file has had was a parser defect, and four of them
were found by a reviewer rather than by the suite for exactly that reason. The parser now takes its
text as a parameter and is tested directly on synthetic input, so the continuation rule, the
joiner and the splitter each have a test that fails when they change. That is the difference
between a guard and a guard that can be trusted, and it should have been there fifteen rounds ago.

Each of these now has a named regression test in the control table above.

## Not accepted, and why it is not a risk here

A client-side gate is never a boundary. Pree has no browser-side flag, PIN, or hidden field
standing in for authentication. The token check and the boundary validation are server-side, and
they are the only gates.
