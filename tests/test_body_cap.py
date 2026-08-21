"""The body cap, exercised at the ASGI layer where its edge cases live."""

from __future__ import annotations

from typing import Any

import pytest

from pree.app import MAX_BODY_BYTES, BodySizeLimit


async def _echo(scope: Any, receive: Any, send: Any) -> None:
    """A minimal downstream app that drains the body and reports how much it saw."""
    seen = 0
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            break
        seen += len(message.get("body") or b"")
        if not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": str(seen).encode("ascii")})


async def _run(
    middleware: BodySizeLimit, scope: dict[str, Any], messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    queue = list(messages)
    sent: list[dict[str, Any]] = []

    async def receive() -> Any:
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message: Any) -> None:
        sent.append(message)

    await middleware(scope, receive, send)
    return sent


def _post_scope(headers: list[tuple[bytes, bytes]] | None = None) -> dict[str, Any]:
    return {"type": "http", "method": "POST", "headers": headers or []}


@pytest.mark.anyio
async def test_a_non_http_scope_passes_straight_through() -> None:
    """A lifespan or websocket scope has no body to cap and must not be touched."""
    sent = await _run(BodySizeLimit(_echo), {"type": "lifespan"}, [])
    assert sent[0]["status"] == 200


@pytest.mark.anyio
async def test_a_bodyless_method_is_not_drained() -> None:
    scope = {"type": "http", "method": "GET", "headers": []}
    sent = await _run(BodySizeLimit(_echo), scope, [])
    assert sent[0]["status"] == 200


@pytest.mark.anyio
async def test_a_malformed_content_length_is_not_trusted_as_a_pass_or_a_reject() -> None:
    """An unparseable header falls through to the streaming count, which is authoritative."""
    scope = _post_scope([(b"content-length", b"not-a-number")])
    sent = await _run(BodySizeLimit(_echo), scope, [{"type": "http.request", "body": b"tiny"}])
    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b"4"


@pytest.mark.anyio
async def test_a_declared_length_over_the_cap_is_refused_without_reading_the_body() -> None:
    scope = _post_scope([(b"content-length", str(MAX_BODY_BYTES + 1).encode("ascii"))])
    sent = await _run(BodySizeLimit(_echo), scope, [])
    assert sent[0]["status"] == 413


@pytest.mark.anyio
async def test_a_disconnect_mid_stream_does_not_hang_or_crash() -> None:
    sent = await _run(
        BodySizeLimit(_echo),
        _post_scope(),
        [
            {"type": "http.request", "body": b"partial", "more_body": True},
            {"type": "http.disconnect"},
        ],
    )
    assert sent[0]["status"] == 200


@pytest.mark.anyio
async def test_the_body_is_replayed_exactly_once() -> None:
    """A second read must see a disconnect, not the body again."""
    sent = await _run(
        BodySizeLimit(_echo), _post_scope(), [{"type": "http.request", "body": b"abcdef"}]
    )
    assert sent[1]["body"] == b"6"
