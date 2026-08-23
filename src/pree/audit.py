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
    to a helper in another module; and a denylist of language features lost to NINE routes -
    `__getattribute__("__closure__")`, `os.getenv`, `from os import environ`,
    `inspect.getclosurevars`, `operator.attrgetter`, a `str.format` field path, and
    `dataclasses.asdict`/`astuple`/`pickle.dumps`/`__getstate__`/`__reduce__`. Attribute access has
    a method spelling, a string spelling and a library spelling, and no list of them is closed.

    So this checks the BYTES leaving the process against the actual secret, at the last point before
    they leave. It does not care how the value was obtained, which is the whole point: it catches
    the routes nobody enumerated, including the ones not invented yet.

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

    `print` does not go through logging, and the review's decisive attacks all used `print` to
    stdout, which is the pod-log channel the platform aggregates. Wrapping the stream covers that
    without needing to know which call sites exist.

    Only the attributes a stream user actually touches are forwarded, deliberately: `__getattr__`
    delegation would hand back the underlying stream to anything that asked for one attribute,
    which is the same shape of hole as everything else this control replaces.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def write(self, text: str) -> int:
        expected = _GUARDED_CREDENTIAL
        if expected and expected in text:
            return int(self._stream.write(f"{CREDENTIAL_ALARM}\n"))
        return int(self._stream.write(text))

    def flush(self) -> None:
        self._stream.flush()

    def isatty(self) -> bool:
        return bool(getattr(self._stream, "isatty", bool)())

    @property
    def encoding(self) -> str:
        return str(getattr(self._stream, "encoding", "utf-8"))


def install_credential_guard(expected: str | None) -> None:
    """Arm the runtime guard on every channel that leaves this process.

    Called once from the boot path, where the credential is legitimately in scope. With no token
    configured there is nothing to guard and this is a no-op, which is the same open-by-design
    posture the gate itself takes.
    """
    global _GUARDED_CREDENTIAL  # noqa: PLW0603 - one process-wide value, set once at boot
    _GUARDED_CREDENTIAL = expected
    if not expected:
        return
    for name in (_LOGGER_NAME, *ACCESS_LOGGER_NAMES):
        logger = logging.getLogger(name)
        if not any(isinstance(existing, _CredentialGuard) for existing in logger.filters):
            logger.addFilter(_CredentialGuard())
    if not isinstance(sys.stdout, _GuardedStream):
        sys.stdout = _GuardedStream(sys.stdout)
    if not isinstance(sys.stderr, _GuardedStream):
        sys.stderr = _GuardedStream(sys.stderr)
