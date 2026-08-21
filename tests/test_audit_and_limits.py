"""Audit records and the rate limiter, tested with an injected clock rather than sleeping."""

from __future__ import annotations

import io
import json
import logging

from pree.audit import audit
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
