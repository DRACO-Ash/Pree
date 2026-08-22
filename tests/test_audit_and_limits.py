"""Audit records and the rate limiter, tested with an injected clock rather than sleeping."""

from __future__ import annotations

import io
import itertools
import json
import logging
import threading

from pree.audit import MAX_ACCESS_PATH, audit, bound_access_log
from pree.ratelimit import RateLimiter


def _logger_to_buffer() -> tuple[logging.Logger, io.StringIO]:
    buffer = io.StringIO()
    logger = logging.getLogger("pree.audit.buffer")
    logger.handlers = [logging.StreamHandler(buffer)]
    logger.handlers[0].setFormatter(logging.Formatter("%(message)s"))
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger, buffer


def test_an_audit_record_is_one_line_of_parseable_json() -> None:
    logger, buffer = _logger_to_buffer()
    audit(logger, action="assess", actor="ops", duration_ms=7, outcome="ok", score=91.2)
    lines = buffer.getvalue().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record == {
        "kind": "audit",
        "action": "assess",
        "actor": "ops",
        "duration_ms": 7,
        "outcome": "ok",
        "score": 91.2,
    }


def test_the_limiter_admits_up_to_the_limit_then_refuses() -> None:
    now = [0.0]
    limiter = RateLimiter(3, 60.0, clock=lambda: now[0])
    assert [limiter.allow("k") for _ in range(4)] == [True, True, True, False]


def test_the_window_reopens_once_the_oldest_hit_expires() -> None:
    now = [0.0]
    limiter = RateLimiter(1, 10.0, clock=lambda: now[0])
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False
    now[0] = 11.0
    assert limiter.allow("k") is True


def test_keys_are_limited_independently() -> None:
    now = [0.0]
    limiter = RateLimiter(1, 60.0, clock=lambda: now[0])
    assert limiter.allow("a") is True
    assert limiter.allow("b") is True
    assert limiter.allow("a") is False


def test_retry_after_is_never_zero_even_for_a_key_with_no_history() -> None:
    """A refused key whose bucket the fail-closed branch evicted has no history.

    Answering 0 there tells a compliant client to retry immediately, in a tight loop, for as
    long as the table stays saturated, so the floor is one second in every case.
    """
    now = [0.0]
    limiter = RateLimiter(1, 30.0, clock=lambda: now[0])
    assert limiter.retry_after_seconds("unseen") == 1
    limiter.allow("k")
    assert limiter.retry_after_seconds("k") >= 1


def test_tracked_keys_are_bounded_so_a_key_spray_cannot_grow_memory_without_limit() -> None:
    now = [0.0]
    limiter = RateLimiter(5, 60.0, clock=lambda: now[0], max_keys=4)
    for index in range(50):
        limiter.allow(f"actor-{index}")
    assert len(limiter._hits) <= 4


def test_eviction_never_resets_a_bucket_that_is_over_its_limit() -> None:
    """Insertion-order eviction let a spray of fresh keys reopen a throttled key's window."""
    now = [0.0]
    limiter = RateLimiter(1, 60.0, clock=lambda: now[0], max_keys=3)
    assert limiter.allow("victim") is True
    assert limiter.allow("victim") is False
    for index in range(20):
        limiter.allow(f"spray-{index}")
    assert limiter.allow("victim") is False, "the throttled bucket was evicted and reset"


def test_expired_buckets_are_reclaimed_before_active_ones() -> None:
    now = [0.0]
    limiter = RateLimiter(5, 10.0, clock=lambda: now[0], max_keys=2)
    limiter.allow("stale-one")
    limiter.allow("stale-two")
    now[0] = 100.0
    limiter.allow("fresh")
    assert "fresh" in limiter._hits
    assert len(limiter._hits) <= 2


def test_a_saturated_key_table_fails_closed_rather_than_admitting_everyone() -> None:
    """The eviction fix had made the limiter fail open.

    "Never evict a key at its limit" left the key currently being counted as the only
    evictable bucket once the table filled with saturated ones, so it evicted itself on every
    request and was admitted without bound.
    """
    now = [0.0]
    limiter = RateLimiter(2, 60.0, clock=lambda: now[0], max_keys=8)
    for index in range(8):
        assert limiter.allow(f"full-{index}") is True
        assert limiter.allow(f"full-{index}") is True
    assert all(len(b) >= 2 for b in limiter._hits.values())
    assert [limiter.allow("fresh-peer") for _ in range(5)] == [False] * 5


def test_ordinary_use_is_unaffected_by_the_fail_closed_eviction() -> None:
    now = [0.0]
    limiter = RateLimiter(3, 60.0, clock=lambda: now[0], max_keys=4)
    assert [limiter.allow("a") for _ in range(5)] == [True, True, True, False, False]


def test_concurrent_callers_never_turn_a_429_into_a_500() -> None:
    """The eviction pass was not thread-safe, and the fine limiter is genuinely concurrent.

    `create_assessment` is a sync handler, so Starlette runs it in the anyio threadpool and
    several threads call `allow` at once. Three failures lived in `_evict_if_needed`:
    `sorted(...)` iterated the key table while another thread's `setdefault` inserted into it,
    two threads selected the same candidate so the second delete raised KeyError, and a deque
    emptied by one thread raised IndexError in another. Measured before the lock: 9,571
    exceptions across 24,000 calls. Each was a 500 with no audit line and none of the hardening
    headers, in place of the 429 the limiter exists to return.

    The table must be large for this to bite: the window in which another thread can insert is
    the length of one pass over it, so a small table hides the race entirely. These parameters
    are calibrated, not guessed: reverting the lock and running this shape three times gave
    365, 382 and 373 exceptions, a wide enough margin to be reliable while costing the suite
    four thousand calls rather than the twenty-four thousand the first version ran.
    """
    failures: list[str] = []
    counter = itertools.count()
    limiter = RateLimiter(limit=3, window_seconds=60.0, max_keys=2000)
    for index in range(2700):
        limiter.allow(f"seed-{index}")

    def hammer() -> None:
        for _ in range(500):
            try:
                limiter.allow(f"t{next(counter)}")
            # Bare Exception on purpose: the assertion IS that nothing escapes.
            except Exception as exc:
                failures.append(type(exc).__name__)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures, (
        f"the limiter raised under concurrency, which the caller sees as a 500 rather than a "
        f"429: {len(failures)} exceptions, {sorted(set(failures))}"
    )


def test_the_access_log_filter_bounds_the_request_line() -> None:
    """The access log was the same amplification as the audit line, 500 times larger.

    gunicorn's `--access-logfile -` hands uvicorn the raw request line, and `/healthz` is
    deliberately exempt from the rate limiter, so an unauthenticated caller wrote 15 KB of log
    per request with nothing counting the requests. Measured under the shipped launch command:
    15,046 bytes for one request before the filter, 208 bytes after, and 31 MB written by a
    three-second burst that returned 200 to every request.
    """
    bound_access_log()
    bound_access_log()  # idempotent: the factory runs in every worker, and may run twice
    access = logging.getLogger("uvicorn.access")
    installed = [f for f in access.filters if type(f).__name__ == "_TruncateRequestPath"]
    assert len(installed) == 1, f"expected exactly one truncating filter, got {len(installed)}"

    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(message)s"))
    access.handlers = [handler]
    access.setLevel(logging.INFO)
    access.propagate = False
    long_path = "/healthz?" + "x" * 15_000
    access.info('%s - "%s %s HTTP/%s" %d', "10.0.0.1", "GET", long_path, "1.1", 200)

    written = buffer.getvalue()
    assert "[truncated]" in written, "the filter did not fire"
    assert len(written) < MAX_ACCESS_PATH + 200, f"the access line is {len(written)} bytes"
    assert "/healthz" in written, "the filter destroyed the part an operator needs"


def test_the_access_log_filter_leaves_a_normal_request_line_alone() -> None:
    """A control that mangles ordinary traffic gets removed, so it must not."""
    bound_access_log()
    access = logging.getLogger("uvicorn.access")
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(message)s"))
    access.handlers = [handler]
    access.setLevel(logging.INFO)
    access.propagate = False
    # The longest legitimate path: /v1/assessments/ plus a 129-character store key.
    real = "/v1/assessments/" + "a" * 64 + ":" + "b" * 64
    access.info('%s - "%s %s HTTP/%s" %d', "10.0.0.1", "GET", real, "1.1", 200)
    written = buffer.getvalue()
    assert "[truncated]" not in written, "a legitimate longest-path request was truncated"
    assert real in written


def test_the_access_log_filter_also_bounds_a_pre_formatted_line() -> None:
    """gunicorn's own access logger formats the line before logging it, so there are no args.

    Truncating only the positional arguments would leave that path unbounded, and which of the
    two shapes reaches the logger depends on the worker class and on gunicorn's configuration,
    neither of which this test can pin. Both shapes are bounded.
    """
    bound_access_log()
    access = logging.getLogger("gunicorn.access")
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(message)s"))
    access.handlers = [handler]
    access.setLevel(logging.INFO)
    access.propagate = False
    access.info('10.0.0.1 - "GET /healthz?%s HTTP/1.1" 200' % ("x" * 15_000))
    written = buffer.getvalue()
    assert "[truncated]" in written, "the pre-formatted path was not truncated"
    assert len(written) < MAX_ACCESS_PATH * 4 + 200, f"the line is {len(written)} bytes"
    assert "/healthz" in written


def test_the_access_log_filter_bounds_the_mapping_shape_gunicorn_emits() -> None:
    """gunicorn logs a format string plus a MAPPING of atoms, never a formatted string.

    glogging.py calls `access_log.info(access_log_format, safe_atoms)`. A dict is not a tuple,
    and record.msg is only the short format string, so this shape went out whole: measured at
    15,056 bytes with no truncation marker. UvicornWorker does not use that path today, so it
    was latent rather than live, and a docstring of mine claimed it was covered when it was not.
    """
    bound_access_log()
    access = logging.getLogger("gunicorn.access")
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(message)s"))
    access.handlers = [handler]
    access.setLevel(logging.INFO)
    access.propagate = False
    atoms = {"h": "10.0.0.1", "r": "GET /healthz?" + "x" * 15_000 + " HTTP/1.1", "s": "200"}
    access.info('%(h)s "%(r)s" %(s)s', atoms)

    written = buffer.getvalue()
    assert "[truncated]" in written, "the mapping shape was not truncated"
    assert len(written) < MAX_ACCESS_PATH + 200, f"the access line is {len(written)} bytes"
    assert "/healthz" in written and "200" in written


def test_the_retry_after_read_happens_under_the_same_lock_as_the_count() -> None:
    """A structural assertion, because this race cannot be forced deterministically.

    `retry_after_seconds` is called from the thread pool immediately after `allow` returns
    False, on the same key, and it reads the same shared deque: between the emptiness test and
    `bucket[0]` another thread's prune could empty it, giving IndexError where a 429 with a
    Retry-After belongs. Forcing it needs a whole window to elapse inside a two-bytecode gap,
    so 160,000 concurrent calls produced nothing. The control is the critical section, so the
    critical section is what gets asserted: an unguarded read is the defect, whether or not this
    machine is fast enough to show it.
    """
    limiter = RateLimiter(limit=1, window_seconds=60.0)
    acquisitions: list[str] = []
    real = limiter._guard

    class SpyLock:
        def __enter__(self) -> None:
            acquisitions.append("in")
            real.acquire()

        def __exit__(self, *_: object) -> None:
            real.release()

    limiter._guard = SpyLock()  # type: ignore[assignment]
    assert limiter.allow("peer") is True
    assert limiter.allow("peer") is False
    before = len(acquisitions)
    assert limiter.retry_after_seconds("peer") >= 1
    assert len(acquisitions) > before, (
        "retry_after_seconds read the shared key table without taking the lock that allow() "
        "takes, so a concurrent prune can empty the deque between its two reads"
    )
