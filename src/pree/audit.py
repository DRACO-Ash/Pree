"""Structured audit logging: one line of JSON per privileged action.

Each record carries the actor, the timings, and the usage, and never a secret. Client-facing
errors stay generic; the detail lands here, server-side.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_LOGGER_NAME = "pree.audit"
# The access log is a separate channel with a separate hole. The application's own handlers
# truncate the request path, but gunicorn's `--access-logfile -` hands uvicorn the raw request
# line, and `/healthz` is deliberately exempt from the rate limiter, so an unauthenticated
# caller could write 15 KB of log per request with nothing counting the requests: measured at
# 2,069 requests in three seconds, 31 MB of log, roughly 620 MB a minute per worker. That both
# fills the log volume and buries the audit trail the bounded handlers exist to produce.
ACCESS_LOGGER_NAMES = ("uvicorn.access", "gunicorn.access")
MAX_ACCESS_PATH = 160


def _clip(value: object) -> object:
    """Clip one field of an access record, and redact any query string in it.

    The redaction is not tidiness. `GET /diagnostics?x-pree-token=<the real token>` is refused
    for authentication, correctly, and then the access log wrote the token verbatim into the pod
    log store: the one channel in this application that ever held it in cleartext. No route
    here takes a query parameter, so nothing is lost by dropping every query string, and the
    filter that already rewrites this field is the cheapest place to do it.
    """
    if not isinstance(value, str):
        return value
    marked = value
    if "?" in marked:
        head, _, _ = marked.partition("?")
        marked = f"{head}?[redacted]"
    if len(marked) > MAX_ACCESS_PATH:
        marked = marked[:MAX_ACCESS_PATH] + "[truncated]"
    return marked


class _TruncateRequestPath(logging.Filter):
    """Truncate the caller-sized part of an access record, in place, before formatting.

    uvicorn's access record carries (client_addr, method, full_path, http_version, status), so
    the path is one positional argument and can be replaced without touching the format string.
    A filter is used rather than a formatter because a filter survives whatever handler and
    formatter the worker installs, and this has to hold under gunicorn's own logging setup.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # Three record shapes, because three producers exist and which one reaches the logger
        # depends on the worker class and on gunicorn's configuration, neither of which a test
        # can pin from inside the app:
        #
        #   tuple   uvicorn's access logger: (client_addr, method, path, http_version, status).
        #   dict    gunicorn's own access logger, glogging.py: a format string plus a mapping of
        #           atoms. `isinstance(args, tuple)` is False for a mapping and record.msg is
        #           only the short format string, so this shape went out WHOLE: measured at
        #           15,056 bytes with no truncation marker. UvicornWorker does not use it today,
        #           which made it latent rather than live, and a test docstring claimed it was
        #           covered when it was not.
        #   neither an already-formatted string. logging sets record.args to an EMPTY TUPLE in
        #           that case, not to None, so the guard must test emptiness and not type.
        #
        # Every string is clipped, not only the path: the client address is caller-sized the
        # moment a forwarded header is trusted, and the method comes off the wire too.
        if args and isinstance(args, dict):
            record.args = {key: _clip(value) for key, value in args.items()}
        elif args and isinstance(args, tuple):
            record.args = tuple(_clip(value) for value in args)
        elif isinstance(record.msg, str) and len(record.msg) > MAX_ACCESS_PATH * 4:
            record.msg = record.msg[: MAX_ACCESS_PATH * 4] + "[truncated]"
        return True


def bound_access_log() -> None:
    """Attach the truncating filter to every access logger, idempotently.

    Called from the app factory, so it runs inside each gunicorn worker on the same import
    that builds the app. Attaching to the logger rather than to a handler means it applies
    however the worker chooses to emit the record, including handlers added after this call.
    """
    for name in ACCESS_LOGGER_NAMES:
        logger = logging.getLogger(name)
        if not any(isinstance(existing, _TruncateRequestPath) for existing in logger.filters):
            logger.addFilter(_TruncateRequestPath())


def build_logger(stream: Any = None) -> logging.Logger:
    """Return the audit logger, wired to a single-line formatter.

    An explicitly supplied stream gets its own logger instance, because handlers are cached
    per logger name: sharing one name meant the first caller's stream won and every later
    injection was silently ignored.
    """
    if stream is not None:
        # Built outside the global registry, which never reclaims a logger, so an injected
        # stream cannot leak one per call.
        logger = logging.Logger(_LOGGER_NAME)
        logger.addHandler(_handler_for(stream))
        logger.setLevel(logging.INFO)
        logger.propagate = False
        return logger
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        logger.addHandler(_handler_for(sys.stdout))
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def _handler_for(stream: Any) -> logging.Handler:
    """One line per record, no prefix: the record is already structured JSON."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def audit(
    logger: logging.Logger,
    action: str,
    actor: str,
    duration_ms: int,
    outcome: str,
    **usage: Any,
) -> None:
    """Emit one structured audit line. Never pass a secret in usage."""
    record: dict[str, Any] = {
        "kind": "audit",
        "action": action,
        "actor": actor,
        "duration_ms": duration_ms,
        "outcome": outcome,
    }
    record.update(usage)
    logger.info(json.dumps(record, separators=(",", ":"), sort_keys=True))


# The credential the guard below refuses to emit, and the marker that replaces a line carrying it.
# Module-level rather than passed around, because the guard has to be reachable from a logging
# filter and a stream wrapper that the caller does not own.
_GUARDED_CREDENTIAL: str | None = None
CREDENTIAL_ALARM = '{"kind":"credential_guard","outcome":"refused_a_line_carrying_the_credential"}'


class _CredentialGuard(logging.Filter):
    """Refuse to emit any log record whose rendered text contains the credential.

    A RUNTIME control, and the reason it exists is that every static one before it was an
    enumeration the adversary could step outside. Three rounds ran the same way: sampling tokens
    lost to a predicate constant across the sample; a denylist of name spellings lost to `cfg` and
    to a helper in another module; and a denylist of language features lost to NINE routes,
    `__getattribute__("__closure__")` among them, because attribute access has a method spelling, a
    string spelling and a library spelling.

    So this checks the bytes against the actual secret, and does not care how the value was
    obtained. What it DOES still depend on is being attached where the bytes pass, which is the
    limit stated honestly in `install_credential_guard`.

    Attached to HANDLERS, not loggers. A logger-level filter does not run for a record propagated
    from a descendant - only the ancestor's handlers do - and it covers only the logger names it was
    given. A review measured both: a child of `pree.audit` propagated straight past the filter on
    its parent, and `gunicorn.error`, `uvicorn.error` and root were never filtered at all.
    `Handler.handle` runs handler filters for every record that reaches it, whatever logger emitted
    it, which removes the name enumeration entirely.

    Fail-closed by SUBSTITUTION rather than by raising. A raise inside a logging filter is
    swallowed by the logging module and the line goes out anyway; returning False would drop the
    line silently, and a control whose success looks like nothing happening is one nobody notices
    has broken. The record is replaced with a fixed alarm that carries no caller input at all.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        expected = _GUARDED_CREDENTIAL
        if not expected:
            return True
        try:
            rendered = record.getMessage()
        except Exception:
            return True
        if expected in rendered:
            record.msg = CREDENTIAL_ALARM
            record.args = ()
        return True


class _GuardedStream:
    """A write-through wrapper that refuses to pass the credential to a stream.

    `print` does not go through logging, and a review's decisive attacks used `print` to stdout,
    which is the pod-log channel the platform aggregates. Wrapping the stream covers that without
    needing to know which call sites exist.

    Only the attributes a stream user actually touches are forwarded, deliberately: `__getattr__`
    delegation would hand back the underlying stream to anything that asked for one attribute, which
    is the same shape of hole as everything else this control replaces. `writelines`, `fileno` and
    `buffer` are forwarded because a review measured their absence breaking
    `subprocess(stdout=sys.stdout)`, `faulthandler.enable()` and `print(file=sys.stdout.buffer)` -
    and a control that breaks the process is one an operator removes.

    `buffer` and `fileno` are forwarded WITHOUT guarding, and that is a named limit rather than an
    oversight: bytes written to fd 1 do not pass through this object at all, so `os.write(1, ...)`
    and a subprocess inheriting the descriptor are outside what this can see. Hiding that by
    withholding the attribute would only move the breakage, not the exposure.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    @property
    def wrapped(self) -> Any:
        """The stream underneath, so arming can be undone and a handler can be re-pointed."""
        return self._stream

    def write(self, text: str) -> int:
        expected = _GUARDED_CREDENTIAL
        payload = f"{CREDENTIAL_ALARM}\n" if expected and expected in text else text
        # A stream whose `write` returns None is legal (a review found `int(None)` raising here),
        # and a guard that raises inside someone else's write is worse than the leak it prevents.
        return int(self._stream.write(payload) or 0)

    def writelines(self, lines: Any) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        self._stream.flush()

    def fileno(self) -> int:
        return int(self._stream.fileno())

    @property
    def buffer(self) -> Any:
        return self._stream.buffer

    def isatty(self) -> bool:
        return bool(getattr(self._stream, "isatty", bool)())

    @property
    def encoding(self) -> str:
        return str(getattr(self._stream, "encoding", "utf-8"))


def _guard_handler(handler: logging.Handler) -> None:
    """Attach the filter to one handler, and re-point a stream it captured before arming.

    The re-point is the half that a review's measurement made necessary. gunicorn builds its
    handlers in `Arbiter.setup`, before the worker imports the app factory, so by the time this runs
    they already hold the PRE-WRAP `TextIOWrapper`. Measured under the shipped launch command:
    `gunicorn.error`'s handler had `guarded=False` and `filters=[]`. Replacing `sys.stdout` does
    nothing for an object that captured the old one.
    """
    if not any(isinstance(existing, _CredentialGuard) for existing in handler.filters):
        handler.addFilter(_CredentialGuard())
    stream = getattr(handler, "stream", None)
    if stream is None or isinstance(stream, _GuardedStream):
        return
    for wrapper in (sys.stdout, sys.stderr):
        if isinstance(wrapper, _GuardedStream) and stream is wrapper.wrapped:
            handler.stream = wrapper  # type: ignore[attr-defined]
            return


def install_credential_guard(expected: str | None) -> None:
    """Arm the runtime guard, and be exact about what it does and does not cover.

    WHAT IT COVERS. Every `logging.Handler` that exists when this runs or is constructed after it,
    whatever logger it is attached to and whether the record was emitted directly or propagated from
    a descendant; and every write through the `sys.stdout` / `sys.stderr` objects, including
    `print`. Handlers that captured the pre-wrap stream are re-pointed at the wrapper.

    WHAT IT DOES NOT COVER, named because the previous version of this docstring said "every channel
    that leaves this process" and a review then took seven of them:

      ● **fd-level writes.** `os.write(1, ...)` and a subprocess inheriting file descriptor 1 do not
        pass through any Python object this touches.
      ● **Any encoding but plaintext.** The comparison is a substring test against the credential as
        configured, so base64, hex or a reversal passes. This is a deliberate boundary - the
        emitting code chose the encoding, and matching every encoding is not possible - but it means
        this guard IS an enumeration, over (channel x encoding), and both dimensions belong to the
        same adversary who chose the acquisition route.

    That last point is the honest correction to how this was described. It was called "the only one
    that does not depend on enumerating the adversary's alphabet". It is not: it swapped an alphabet
    of attribute spellings for a smaller one of channels and encodings. What it is worth is that it
    catches the two channels a leak has actually used, at the point of emission, whatever route
    acquired the value - which is real, and less than was claimed.

    Called once from the boot path, where the credential is legitimately in scope. With no token
    configured there is nothing to guard, and disarming restores the streams so the wrapper is not
    left installed for the life of the process.
    """
    global _GUARDED_CREDENTIAL  # noqa: PLW0603 - one process-wide value, set at boot
    _GUARDED_CREDENTIAL = expected
    if not expected:
        _restore_streams()
        return
    if not isinstance(sys.stdout, _GuardedStream):
        sys.stdout = _GuardedStream(sys.stdout)
    if not isinstance(sys.stderr, _GuardedStream):
        sys.stderr = _GuardedStream(sys.stderr)
    # Every handler that already exists, including gunicorn's and uvicorn's, which are built before
    # the worker imports this module.
    for handler in _existing_handlers():
        _guard_handler(handler)
    _patch_handler_construction()


def _existing_handlers() -> list[logging.Handler]:
    """Every handler the logging module currently knows about.

    TWO sources, because either alone has a gap. `logging._handlerList` is private and holds weak
    references, and it is the only registry of handlers that exist but may not be attached to a
    logger this module can name - which matters because naming loggers is exactly what failed:
    gunicorn builds `gunicorn.error`'s handler before the worker imports this module. Read through
    `getattr` so a future Python that drops it degrades to the manager walk rather than raising at
    boot. The manager walk then covers anything the private list misses.
    """
    found: list[logging.Handler] = []
    for reference in list(getattr(logging, "_handlerList", [])):
        handler = reference() if callable(reference) else reference
        if isinstance(handler, logging.Handler):
            found.append(handler)
    candidates: list[Any] = [logging.getLogger()]
    candidates.extend(logging.getLogger().manager.loggerDict.values())
    for logger in candidates:
        for handler in getattr(logger, "handlers", []):
            if isinstance(handler, logging.Handler) and handler not in found:
                found.append(handler)
    return found


def _restore_streams() -> None:
    """Put the real streams back, so arming is reversible within one process."""
    if isinstance(sys.stdout, _GuardedStream):
        sys.stdout = sys.stdout.wrapped
    if isinstance(sys.stderr, _GuardedStream):
        sys.stderr = sys.stderr.wrapped


def _patch_handler_construction() -> None:
    """Cover handlers built AFTER arming, once, by wrapping `Handler.__init__`.

    A handler created later - by a library, by a reload, by a logging reconfiguration - would
    otherwise be unguarded, and the review's bypass used exactly a handler the guard never saw.
    Patching construction rather than `addHandler` covers a handler that is filtered before it is
    attached to anything.
    """
    if getattr(logging.Handler.__init__, "_pree_guarded", False):
        return
    original = logging.Handler.__init__

    def guarded_init(self: logging.Handler, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        if _GUARDED_CREDENTIAL:
            self.addFilter(_CredentialGuard())

    guarded_init._pree_guarded = True  # type: ignore[attr-defined]
    logging.Handler.__init__ = guarded_init  # type: ignore[method-assign]
