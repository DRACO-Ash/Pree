"""The body cap, exercised at the ASGI layer where its edge cases live."""

from __future__ import annotations

from typing import Any

import pytest

from pree.app import MAX_BODY_BYTES, BodySizeLimit, FrameGuard


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
    middleware: BodySizeLimit | FrameGuard,
    scope: dict[str, Any],
    messages: list[dict[str, Any]],
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


@pytest.mark.anyio
async def test_a_second_read_of_the_body_returns_a_disconnect_not_the_body_again() -> None:
    """The replay-once guard. Without it a second receive() re-delivers the body, which
    hot-loops any disconnect watcher; the earlier test never read twice, so the guard could be
    deleted with the whole suite staying green."""
    seen: list[dict[str, object]] = []

    async def double_reader(scope: object, receive: object, send: object) -> None:
        seen.append(await receive())  # type: ignore[operator]
        seen.append(await receive())  # type: ignore[operator]
        await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]
        await send({"type": "http.response.body", "body": b""})  # type: ignore[operator]

    await _run(
        BodySizeLimit(double_reader), _post_scope(), [{"type": "http.request", "body": b"abc"}]
    )
    assert seen[0]["type"] == "http.request"
    assert seen[0]["body"] == b"abc"
    assert seen[1]["type"] == "http.disconnect"


@pytest.mark.anyio
async def test_a_request_declaring_both_framings_is_refused() -> None:
    """Transfer-Encoding AND Content-Length together is a smuggling primitive.

    RFC 9112 section 6.1 requires the request to be rejected or the connection closed. h11
    frames by Transfer-Encoding and leaves the Content-Length bytes in the buffer, where they
    are served as a pipelined request: against the running server one such request produced a
    401 for the declared body followed by a 200 for a smuggled `GET /healthz`. It is only
    exploitable through a front end that frames by Content-Length where h11 frames by chunks,
    which a modern ingress rejects, so this is a primitive rather than a live path. It is also
    three lines in the middleware that already walks the headers, which makes leaving it a
    choice rather than an oversight.
    """
    scope = _post_scope([(b"transfer-encoding", b"chunked"), (b"content-length", b"6")])
    sent = await _run(FrameGuard(_echo), scope, [{"type": "http.request", "body": b"0"}])
    assert sent[0]["status"] == 400, (
        f"a request declaring both framings reached the application with {sent[0]['status']}"
    )
    headers = dict(sent[0]["headers"])
    assert headers.get(b"connection") == b"close", (
        "the connection stays open after an ambiguous frame, so the trailing bytes can still be "
        "read as a pipelined request"
    )
    assert b"request rejected" in sent[1]["body"]


@pytest.mark.anyio
async def test_a_request_declaring_both_framings_is_refused_on_a_bodyless_method() -> None:
    """The check ran AFTER the bodyless-method early return, so it never ran for GET.

    That is precisely the method class smuggling uses: a front end permits a GET with no body,
    which makes GET the canonical CL.TE carrier. One socket write of `GET /healthz` with both
    framings produced two 200 responses. The refusal was written to close that exact primitive
    and, for five of the six methods it matters most for, did not.
    """
    for method in ("GET", "HEAD", "OPTIONS", "DELETE", "TRACE"):
        scope = {
            "type": "http",
            "method": method,
            "headers": [(b"transfer-encoding", b"chunked"), (b"content-length", b"6")],
        }
        sent = await _run(FrameGuard(_echo), scope, [])
        assert sent[0]["status"] == 400, (
            f"{method} with both framings reached the application with {sent[0]['status']}"
        )
        assert dict(sent[0]["headers"]).get(b"connection") == b"close", (
            f"{method} left the connection open after an ambiguous frame"
        )


@pytest.mark.anyio
async def test_either_framing_header_alone_is_still_accepted() -> None:
    """The refusal is about the PAIR. Refusing either alone would break every normal request."""
    for header in ((b"content-length", b"1"), (b"transfer-encoding", b"chunked")):
        sent = await _run(
            FrameGuard(BodySizeLimit(_echo)),
            _post_scope([header]),
            [{"type": "http.request", "body": b"x"}],
        )
        assert sent[0]["status"] == 200, f"{header!r} alone was refused"
