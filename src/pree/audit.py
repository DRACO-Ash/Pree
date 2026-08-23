"""Structured audit logging: one line of JSON per privileged action.

Each record carries the actor, the timings, and the usage, and never a secret. Client-facing
errors stay generic; the detail lands here, server-side.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from collections.abc import Callable
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
# A DISTINCT alarm for a record the guard could not read, because the two say different things: one
# is "a leak was refused", the other is "this line could not be checked, so it was not trusted".
# One marker for both would make a storm of unreadable records indistinguishable from an attack.
UNSCANNABLE_ALARM = '{"kind":"credential_guard","outcome":"refused_a_line_it_could_not_scan"}'
# What a redacted record ATTRIBUTE carries. Not the alarm: an attribute is rendered inline by a
# format string and the alarm is a whole JSON line. Spelt to the project's own convention for a
# withheld value.
REDACTED_ATTRIBUTE = "[REDACTED:credential]"
# The attributes a refusal replaces ITSELF, so the blanket redaction must not touch them. `args` is
# the load-bearing one: the refusal sets it to `()`, and setting it to a string afterwards would
# make `getMessage()` raise on the alarm's own text, which carries no placeholder.
_REPLACED_ATTRIBUTES = frozenset({"msg", "args", "exc_info", "exc_text", "stack_info"})
# The key prefix for a RENDERED part, chosen because no Python attribute name can start with it, so
# a part key and an attribute name can never collide in one dictionary.
_RENDERED_PREFIX = "<"


# The DEFAULT exception renderer, built once rather than per record. `Formatter.formatException` is
# the same code path a handler's own formatter runs, so what the guard scans is what the handler
# will emit rather than an approximation of it. A handler carrying a formatter that renders a
# traceback DIFFERENTLY is the residual, and it is named in `install_credential_guard`.
_EXCEPTION_RENDERER = logging.Formatter()


def _add(parts: dict[str, str], key: str, produce: Callable[[], str]) -> int:
    """Add one part, returning 1 if it could not be produced.

    Every part gets its OWN guard, and that sentence has to be exactly true rather than nearly true.
    A previous version said it and appended `exc_text` and `stack_info` unguarded, so a truthy
    non-`str` in either field made `"\n".join(parts)` raise `TypeError` - out of the filter, out of
    `Handler.handle`, and into the `logger.*` call site, where the same input had previously been
    contained by logging's own `handleError`. A guard that converts a malformed log record into a
    fault in the request handler that logged it is worse than the leak it prevents.

    `Exception` and not a narrower class, deliberately: producing a part runs caller-supplied
    `__str__`, `__format__` and `__repr__` code, which can raise anything.
    """
    try:
        parts[key] = produce()
    except Exception:  # see the docstring: arbitrary caller code, so any exception
        return 1
    return 0


def _scannable_parts(record: logging.LogRecord) -> tuple[dict[str, str], bool]:
    """Every string a formatter can put on the line, keyed by where it came from.

    Two groups, and the difference between them decides what happens when one cannot be produced.

    THE RENDERED PARTS, keyed with `_RENDERED_PREFIX`, are the four strings `logging.Formatter`
    concatenates for every record: the formatted message, the cached exception text, the exception
    rendered from `exc_info`, and the stack text. `getMessage()` alone was the whole scan for a
    round, which is not the rendered record: `logger.error("x", exc_info=ValueError(token))` put the
    credential on the line with the guard armed and no alarm raised, measured on a handler whose
    stream is not a wrapped `sys` stream. A part of this group that cannot be produced means
    the line goes out unscanned, so the caller treats that FAIL-CLOSED.

    THE RECORD'S OWN ATTRIBUTES are the rest, and they exist here because the round that added the
    group above still missed a whole axis. A `Formatter` format string may name any attribute, so
    `extra={"token": ...}` with `%(token)s`, the credential in the LOGGER NAME with `%(name)s`, and
    `funcName` with `%(funcName)s` each reached the line in plaintext with the guard armed. Every
    attribute is scanned as itself when it is a `str` and through `str()` when it is not, because a
    format string renders a non-`str` attribute through exactly that call.

    A failure in the second group is NOT fail-closed, and must not be: an attribute that cannot be
    stringified is not an unscanned emission. A formatter naming it would raise too, and
    `Handler.handleError` then writes the traceback and `record.msg` to stderr rather than the
    attribute. Alarming on it would replace a legitimate line whose rendering never touched the
    thing that failed, which is a denial of service on the log rather than a control on it.

    The COST of the second group is one `str()` per non-`str` attribute per record, which the
    formatter may then pay again. A record carrying an attribute whose `__str__` is expensive or has
    side effects pays for it twice. That is the price of closing the axis and it is recorded rather
    than hidden.
    """
    parts: dict[str, str] = {}
    unrendered = _add(parts, f"{_RENDERED_PREFIX}message>", record.getMessage)
    # Bound to locals before the closures capture them, so what is scanned cannot change between
    # the guard's read and the formatter's.
    exc_text, exc_info, stack_info = record.exc_text, record.exc_info, record.stack_info
    if exc_text:
        # Cached by an earlier handler's formatter, which the next one reuses VERBATIM rather than
        # re-deriving. Scanned alongside the render below, not instead of it: the two can differ,
        # because the formatter that cached this one may not be the default.
        unrendered += _add(parts, f"{_RENDERED_PREFIX}exc_text>", lambda: str(exc_text))
    if exc_info:
        unrendered += _add(
            parts,
            f"{_RENDERED_PREFIX}exc_info>",
            lambda: _EXCEPTION_RENDERER.formatException(exc_info),
        )
    if stack_info:
        unrendered += _add(parts, f"{_RENDERED_PREFIX}stack_info>", lambda: str(stack_info))
    for name, value in list(vars(record).items()):
        if name in _REPLACED_ATTRIBUTES:
            continue
        # SUPPRESSED rather than counted, which is the deliberate asymmetry with the four rendered
        # parts above: a non-stringifiable attribute is not an unscanned emission, so failing closed
        # on it would cost a legitimate line. Held by
        # `test_an_attribute_the_guard_cannot_stringify_does_not_alarm_a_clean_line`.
        with contextlib.suppress(Exception):
            parts[name] = value if isinstance(value, str) else str(value)
    return parts, unrendered > 0


def _refuse(record: logging.LogRecord, alarm: str, carrying: list[str]) -> None:
    """Replace everything a formatter could render, not only the message.

    `Formatter.format` appends the exception and stack text AFTER the formatted message, so
    replacing `msg` alone leaves the credential on the following lines of the same emission.
    `exc_text` is cleared as well as `exc_info` because a formatter caches its rendering there and
    the next handler reuses the cache rather than re-deriving it.

    Then each ATTRIBUTE that carried the credential is redacted individually. Redacting only the
    ones that carried it keeps the rest of the record diagnosable, which is the whole reason the
    audit trail exists. The rendered parts are skipped by their key prefix, because the five fields
    above have already replaced them and `args` in particular must stay a tuple.
    """
    record.msg = alarm
    record.args = ()
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None
    for key in carrying:
        if not key.startswith(_RENDERED_PREFIX):
            setattr(record, key, REDACTED_ATTRIBUTE)


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

    Fail-closed by SUBSTITUTION rather than by raising or dropping, and the reason is a MEASURED
    mechanism rather than the one this docstring asserted for three rounds. It said "a raise inside
    a logging filter is swallowed by the logging module and the line goes out anyway", and both
    halves are wrong: `Handler.handle` calls `self.filter(record)` outside any `try`, so it
    propagates through `Logger.callHandlers` to the `logger.*` call site - into a request handler as
    a 500, or on the boot path as a crash loop - and the record is emitted nowhere. Only a raise
    inside `emit` or `format` is contained, by `Handler.handleError`. Two independent reviews
    measured this and the wrong mechanism was the finding both raised.

    The DECISION is unchanged and the correct reasoning is shorter: raising converts a leak into a
    fault in the caller that was protected, returning False drops the line silently, and a control
    whose success looks like nothing happening is one nobody notices has broken. Substitution keeps
    the caller alive and leaves an audible marker. The record is replaced with a fixed alarm that
    carries no caller input at all.

    The scan is over the whole record, not the message: see `_scannable_parts` for the two bypasses
    that cost a round each.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        expected = _GUARDED_CREDENTIAL
        if not expected:
            return True
        parts, unrendered = _scannable_parts(record)
        carrying = [key for key, text in parts.items() if expected in text]
        if carrying:
            _refuse(record, CREDENTIAL_ALARM, carrying)
        elif unrendered:
            # FAIL CLOSED on a part the formatter will emit and the guard could not read. The
            # previous behaviour fell through to "no match" and emitted the line unscanned, which a
            # review defeated with a `msg` whose `__str__` raised once and then returned the
            # credential: one call for the guard, the next for the formatter.
            _refuse(record, UNSCANNABLE_ALARM, [])
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
        refused = bool(expected and expected in text)
        payload = f"{CREDENTIAL_ALARM}\n" if refused else text
        # A stream whose `write` returns None is legal (a review found `int(None)` raising here),
        # and a guard that raises inside someone else's write is worse than the leak it prevents.
        written = int(self._stream.write(payload) or 0)
        # On a REFUSAL, report the caller's length rather than the alarm's. `write`'s contract lets
        # a caller loop until everything is written, so returning the alarm's length made such a
        # caller re-submit the tail of the refused text. Not reachable through `print` or
        # `StreamHandler`, both of which discard the return, so this was a trap rather than a fault.
        return len(text) if refused else written

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

    ORDER-DEPENDENT, and the dependency is load-bearing rather than incidental: the re-point matches
    a handler's captured stream against `_installed_wrappers()`, so `install_credential_guard` calls
    `_wrap_streams()` BEFORE it walks the handlers. Reverse the two and there are no wrappers to
    match, every handler keeps its pre-wrap stream, and the only remaining cover is the filter.

    The FILTER is attached first, deliberately, and the re-point is allowed to fail. A handler is
    free to expose `stream` as a read-only property, and this runs on the boot path: an
    `AttributeError` here is not a missed guard but a worker that cannot import the app, which the
    platform reports as CrashLoopBackOff. The filter half already covers every record such a handler
    formats; what is lost is a DIRECT write to the stream it captured, which is recorded as a limit
    rather than hidden by taking the process down.
    """
    if not any(isinstance(existing, _CredentialGuard) for existing in handler.filters):
        handler.addFilter(_CredentialGuard())
    stream = getattr(handler, "stream", None)
    if stream is None or isinstance(stream, _GuardedStream):
        return
    for wrapper in _installed_wrappers():
        if stream is wrapper.wrapped:
            # Read-only or slotted `stream`. See the docstring: the filter is already on, and
            # crashing the boot to re-point one stream is the worse trade. `AttributeError` alone,
            # because it is what all three real shapes raise - a property with no setter,
            # `__slots__`, and a frozen dataclass, whose `FrozenInstanceError` subclasses it. A
            # `TypeError` sat here for one commit with nothing that raises it named and no test
            # holding it, which is a speculative catch and this project's own definition of a rule
            # that pins nothing.
            with contextlib.suppress(AttributeError):
                handler.stream = wrapper  # type: ignore[attr-defined]
            return


def install_credential_guard(expected: str | None) -> None:
    """Arm the runtime guard, and be exact about what it does and does not cover.

    WHAT IT COVERS. Every `logging.Handler` that exists when this runs or is constructed after it,
    whatever logger it is attached to and whether the record was emitted directly or propagated from
    a descendant, over the record's WHOLE default rendering including its traceback and stack text;
    and every write through the four `sys` text-stream attributes, including `print`. Handlers that
    captured the pre-wrap stream are re-pointed at the wrapper WHERE `stream` IS ASSIGNABLE; where
    it is not, the filter half alone covers that handler and a direct write to the stream it
    captured is uncovered. That qualification arrived with the `suppress` that made it true, and
    for one commit this sentence and the control register both still stated the re-point flatly.

    The record scan covers the four strings `logging.Formatter` concatenates and every attribute of
    the record, as itself or through `str()`. The residual on that half is a handler whose formatter
    synthesises text neither of those reveals: an override of `format` or `formatException` reading
    a non-`str` attribute in a way `str()` does not show. It is a residual rather than a hole only
    because the second half covers the emission when such a handler's stream is a wrapped one.

    WHAT IT DOES NOT COVER, named because the previous version of this docstring said "every channel
    that leaves this process" and a review then took seven of them:

      ● **Anything reaching file descriptor 1 or 2 without going through a wrapped object.**
        `os.write(1, ...)`, a subprocess inheriting the descriptor, `sys.stdout.buffer.write`,
        `open("/dev/stdout", "w")` and `os.fdopen(1)` are INSTANCES of that class, not the set:
        a new handle on the same descriptor is a new instance, and enumerating them would be the
        same mistake this module's history is made of.
      ● **Any channel that is not a log line.** The reach above is "a write through a wrapped `sys`
        text stream, and a record passing a `logging.Handler`". A credential put in a RESPONSE BODY,
        written to a file on the data volume, used as a FILENAME, or passed in a child process's
        argv leaves without touching either, and none of those is an fd-level write, a re-encoding,
        or a same-privilege disarm. A review measured all four. This bullet was missing for a round
        while the register claimed the uncovered set was stated exactly, and the omission is the
        same shape as everything else in this module's history: the three classes below were the
        ones that had been ATTACKED, so they read as the ones that existed.
      ● **Any encoding but plaintext, and any framing but one write.** The comparison is a substring
        test against the credential as configured, so base64, hex or a reversal passes; and it is a
        test against ONE string, so `write(token[:16])` followed by `write(token[16:])` reassembles
        in the log with no alarm, as do two log records carrying a half each. Both are deliberate
        boundaries - the emitting code chose the encoding and the framing, and matching every
        combination is not possible - but they mean this guard IS an enumeration, over
        (channel x encoding x framing), and all three dimensions belong to the same adversary who
        chose the acquisition route.

      ● **The window before the guard is armed.** `arm_output_guard` needs the credential, so
        `load_config` necessarily runs first and is not covered. Nothing there renders the value -
        every raise path reports the token's length and repetition count only - so the window is
        closed by `config.py` and not by this guard, which is why it belongs in this list rather
        than in the one above.
      ● **An adversary with the same privilege as the code being guarded.**
        `logging.config.dictConfig` with `{".": {"filters": []}}`, a `Handler` subclass overriding
        `handle`, reassigning this module's own state, or restoring `logging.Handler.__init__` each
        removes the control in a few lines from inside the process. That is inherent to ANY
        in-process guard rather than a defect in this one, and it is stated so a reader does not
        have to infer it.

    That last point is the honest correction to how this was described. It was called "the only one
    that does not depend on enumerating the adversary's alphabet". It is not: it swapped an alphabet
    of attribute spellings for a smaller one of channels, encodings and framings. What it is
    worth is that it catches the two channels a leak has actually used, at the point of emission,
    whatever route acquired the value - which is real, and less than was claimed.

    Called once from the boot path, where the credential is legitimately in scope. With no token
    configured there is nothing to guard, and disarming restores the four `sys` attributes.

    Disarming does NOT undo the handler side, and the previous sentence here said "so the wrapper
    is not left installed for the life of the process": true of the streams, false of the handlers.
    A re-pointed handler keeps the WRAPPER as its stream, every attached `_CredentialGuard` stays
    attached, and `logging.Handler.__init__` stays patched. All three go inert, because each reads
    `_GUARDED_CREDENTIAL` and finds it `None`, but inert is not absent: a re-pointed handler's
    writes still pass through `_GuardedStream`, which forwards only the attributes listed on it,
    so something asking that handler's stream for an attribute outside that list gets an
    `AttributeError` where it previously got a value. Reached only in tests and when no token is
    configured, so it is recorded rather than engineered away.
    """
    global _GUARDED_CREDENTIAL  # noqa: PLW0603 - one process-wide value, set at boot
    _GUARDED_CREDENTIAL = expected
    if not expected:
        _restore_streams()
        return
    _wrap_streams()
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


# Every attribute of `sys` that names a text stream leaving this process. `__stdout__` and
# `__stderr__` are in this list because a review measured the consequence of their absence: they
# hold the PRE-WRAP objects, permanently, one underscore from the covered name, and
# `print(token, file=sys.__stdout__)` landed in the pod log in plaintext. That is neither an
# fd-level write nor a re-encoding, so it sat outside both limits this module names while the
# register claimed the covered set was stated exactly.
#
# It is the same shape as the gunicorn hole: a live reference to the pre-wrap object held somewhere
# the guard did not re-point. The standard library guarantees such a reference exists, so it is a
# missed wrap rather than a boundary, and it is wrapped.
# Documentation of the covered attributes, and what a test asserts the code handles. The code
# itself writes them out explicitly rather than looping with `getattr`, because the project's own
# introspection guard refuses a computed attribute name and a control that exempts its own module
# is not a control.
GUARDED_STREAM_ATTRIBUTES = ("stdout", "stderr", "__stdout__", "__stderr__")


def _wrap_streams() -> None:
    """Wrap every `sys` text stream, sharing one wrapper per underlying object.

    SHARED deliberately: `sys.stdout` and `sys.__stdout__` are normally the same object, and two
    wrappers over one stream would mean a handler re-pointed at one of them is not recognised as
    guarded by a check against the other.
    """
    # EXPLICIT attribute access, not `getattr(sys, name)` in a loop. The loop was the obvious way
    # to write this and the project's own guard refused it, correctly: a computed attribute name is
    # exactly what a static rule about attribute names cannot see, and a control that exempts the
    # module implementing it is not a control. Four attributes, written out.
    wrappers: dict[int, _GuardedStream] = {}

    def wrap(stream: Any) -> Any:
        if stream is None or isinstance(stream, _GuardedStream):
            return stream
        # SHARED per underlying object: `sys.stdout` and `sys.__stdout__` are normally the same
        # object, and two wrappers over one stream would mean a handler re-pointed at one is not
        # recognised as guarded by a check against the other.
        return wrappers.setdefault(id(stream), _GuardedStream(stream))

    sys.stdout = wrap(sys.stdout)
    sys.stderr = wrap(sys.stderr)
    # `# type: ignore[misc]` because typeshed marks these Final. That is a declaration of intent,
    # not a runtime restriction, and the intent is worth overriding here for one measured reason:
    # they hold the PRE-WRAP objects permanently, and `print(token, file=sys.__stdout__)` reached
    # the pod log in plaintext while this module claimed its covered set was stated exactly.
    sys.__stdout__ = wrap(sys.__stdout__)  # type: ignore[misc]
    sys.__stderr__ = wrap(sys.__stderr__)  # type: ignore[misc]


def _installed_wrappers() -> list[_GuardedStream]:
    """Every wrapper currently installed on a `sys` stream, for the handler re-point."""
    return [
        stream
        for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__)
        if isinstance(stream, _GuardedStream)
    ]


def _restore_streams() -> None:
    """Put the real streams back, so arming is reversible within one process."""
    if isinstance(sys.stdout, _GuardedStream):
        sys.stdout = sys.stdout.wrapped
    if isinstance(sys.stderr, _GuardedStream):
        sys.stderr = sys.stderr.wrapped
    if isinstance(sys.__stdout__, _GuardedStream):
        sys.__stdout__ = sys.__stdout__.wrapped  # type: ignore[unreachable]
    if isinstance(sys.__stderr__, _GuardedStream):
        sys.__stderr__ = sys.__stderr__.wrapped  # type: ignore[unreachable]


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
