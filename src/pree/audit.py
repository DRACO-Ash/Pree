"""Structured audit logging: one line of JSON per privileged action.

Each record carries the actor, the timings, and the usage, and never a secret. Client-facing
errors stay generic; the detail lands here, server-side.
"""

from __future__ import annotations

import contextlib
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


def _exact_text(value: str) -> str:
    """The characters a value actually holds, as a `str` with builtin behaviour.

    `str(value)` is NOT enough and the difference was two findings. `str()` dispatches to
    `type(value).__str__`, so a `str` SUBCLASS can return "harmless line" while the characters it
    holds are the credential - the same trick as a lying `__contains__`, one dunder over. `value[:]`
    fails the same way through `__getitem__`. `str.__str__(value)` reads the underlying object, so
    it cannot be intercepted, and it returns an exact `str`. Measured: `str()` gave "harmless",
    `str.__str__` gave the real characters.

    Callers must USE the return value, not merely scan it. Scanning a coerced copy and then emitting
    the caller's object is how the second finding worked: `StreamHandler.emit` writes
    `msg + self.terminator`, so a subclass with clean characters and a hostile `__add__` put the
    credential in the sink after passing the scan. What is scanned has to be what is emitted.

    Takes a `str` and nothing wider. A branch for non-`str` input sat here for a draft and coverage
    reported it as dead: both callers are typed `str`, and a formatter that returns something else
    is already broken for `StreamHandler.emit`, which concatenates the result. Third dead defensive
    branch removed on that argument in this module, which is starting to look like the useful rule
    rather than an incident.
    """
    return str.__str__(value)


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
        # COERCED, and the coerced value is what gets written. `in` dispatches to
        # `type(text).__contains__` and `str()` dispatches to `type(text).__str__`, so a `str`
        # subclass can deny holding the credential twice over - measured on this channel, which is
        # `print` and the pod log. Passing `text` through after scanning `_exact_text(text)` would
        # scan one thing and emit another.
        exact = _exact_text(text)
        refused = bool(expected and expected in exact)
        payload = f"{CREDENTIAL_ALARM}\n" if refused else exact
        # A stream whose `write` returns None is legal (a review found `int(None)` raising here),
        # and a guard that raises inside someone else's write is worse than the leak it prevents.
        written = int(self._stream.write(payload) or 0)
        # On a REFUSAL, report the caller's length rather than the alarm's. `write`'s contract lets
        # a caller loop until everything is written, so returning the alarm's length made such a
        # caller re-submit the tail of the refused text. Not reachable through `print` or
        # `StreamHandler`, both of which discard the return, so this was a trap rather than a fault.
        return len(exact) if refused else written

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
    """Re-point a stream this handler captured before arming.

    The re-point is the half that a review's measurement made necessary. gunicorn builds its
    handlers in `Arbiter.setup`, before the worker imports the app factory, so by the time this runs
    they already hold the PRE-WRAP `TextIOWrapper`. Measured under the shipped launch command:
    `gunicorn.error`'s handler had `guarded=False` and `filters=[]`. Replacing `sys.stdout` does
    nothing for an object that captured the old one.

    ORDER-DEPENDENT, and the dependency is load-bearing rather than incidental: the re-point matches
    a handler's captured stream against `_installed_wrappers()`, so `install_credential_guard` calls
    `_wrap_streams()` BEFORE it walks the handlers. Reverse the two and there are no wrappers to
    match and every handler keeps its pre-wrap stream.

    THIS FUNCTION ONLY RE-POINTS NOW. It used to attach a per-handler filter as well, and that whole
    layer is gone - see `install_credential_guard` for what it did, what removing it cost, and why
    the owner chose to remove it. What is left is worth keeping on its own terms: a handler holding
    a pre-wrap stream writes past the byte-channel wrapper, and re-pointing it is a two-line fix for
    a measured hole.

    The re-point is allowed to FAIL. A handler is free to expose `stream` as a read-only property,
    and this runs on the boot path: an `AttributeError` here is not a missed guard but a worker that
    cannot import the app, which the platform reports as CrashLoopBackOff. The content such a
    handler emits is still scanned by `_patch_handler_format`; what is lost is a DIRECT write to the
    stream it captured, recorded as a limit rather than hidden by taking the process down.
    """
    stream = getattr(handler, "stream", None)
    if stream is None or isinstance(stream, _GuardedStream):
        return
    for wrapper in _installed_wrappers():
        if stream is wrapper.wrapped:
            # Read-only or slotted `stream`. See the docstring: crashing the boot to re-point one
            # stream is the worse trade. `AttributeError` alone, because it is what all three real
            # shapes raise - a property with no setter, `__slots__`, and a frozen dataclass, whose
            # `FrozenInstanceError` subclasses it. A `TypeError` sat here for one commit with
            # nothing that raises it named and no test holding it, which is a speculative catch and
            # this project's own definition of a rule that pins nothing.
            with contextlib.suppress(AttributeError):
                handler.stream = wrapper  # type: ignore[attr-defined]
            return


def install_credential_guard(expected: str | None) -> None:
    """Arm the runtime guard, and be exact about what it does and does not cover.

    TWO LAYERS, both of which scan text that is actually leaving. There were three; the third is
    gone, deliberately, and the paragraph after these two says what that cost.

    ● **The finished line, at `logging.Handler.format`.** Whatever the record held and whatever
      rendered it - any attribute, any conversion (`str`, `repr`, `ascii`), a formatter default that
      never touched the record, a filter that ran after this one, an unstable `__str__` - the text a
      stock handler emits is scanned before it leaves. This replaced four rounds of scanning a MODEL
      of that line, each of which lost to a part of the real one the model did not have. See
      `_patch_handler_format` for the six measured bypasses that forced it.
    ● **The byte channel, at the four `sys` text-stream attributes**, including `print`. Handlers
      that captured a pre-wrap stream are re-pointed at the wrapper WHERE `stream` IS ASSIGNABLE;
      where it is not, the layer above still scans what that handler emits, and only a DIRECT write
      to the stream it captured is uncovered.

    THE LAYER THAT WAS REMOVED, and what removing it cost. A per-handler `logging.Filter` scanned
    the RECORD - the four strings `Formatter` concatenates, plus every `str` attribute - and
    replaced the individual field that carried the credential, so a refused record kept the rest of
    its fields readable. Two things made it worth deleting rather than fixing:

      ● It was not a guarantee and could not be made into one. It scanned a MODEL of the line, and
        seven rounds of widening that model each ended with a measured bypass: a traceback, then
        `%(args)s` the message never consumed, then a `repr` conversion, then a formatter default
        that never touched the record, then a filter running after it, then an unstable `__str__`
        that rendered differently for the guard and for the formatter. Once the finished line itself
        was scanned, the model added no containment.
      ● It generated most of the defects. The side-effect amplification that made ARMING the guard
        emit what disarming would not; the redaction minting an attribute the scan used as a key;
        the unguarded write that faulted the caller; the two-name scan gap; and a fail-closed branch
        that destroyed clean operational lines. Every one of those lived in this layer, and none of
        them was reachable from the unauthenticated edge - they were self-inflicted.

    What is lost is real and is named here rather than in a footnote. FIELD-LEVEL REDACTION: a
    refused line now reads as one alarm instead of naming the field that carried the credential, so
    diagnosis of a refusal is coarser. And COVERAGE OF THREE STOCK HANDLERS: `HTTPHandler`
    urlencodes `record.__dict__`, `SocketHandler` and `DatagramHandler` pickle it, and none of the
    three emits `Handler.format`'s return value, so the removed filter was their only layer. They
    are now wholly uncovered. This application constructs `StreamHandler`s on `sys.stdout` and
    nothing else, which is why that trade is acceptable HERE and would not be in a service that
    ships logs over a socket.

    WHAT IT DOES NOT COVER, named because the previous version of this docstring said "every channel
    that leaves this process" and a review then took seven of them:

      ● **Anything reaching file descriptor 1 or 2 without going through a wrapped object.**
        `os.write(1, ...)`, a subprocess inheriting the descriptor, `sys.stdout.buffer.write`,
        `open("/dev/stdout", "w")` and `os.fdopen(1)` are INSTANCES of that class, not the set:
        a new handle on the same descriptor is a new instance, and enumerating them would be the
        same mistake this module's history is made of.
      ● **A handler that does not emit `Handler.format`'s return value.** THREE STOCK HANDLERS do
        this - `HTTPHandler` urlencodes `record.__dict__`, `SocketHandler` and `DatagramHandler`
        pickle it - and a `Handler` subclass overriding `format` is a fourth way. No subclassing is
        required for the first three, which is why this is listed here and not folded into the
        same-privilege class: a review put the credential on the wire from a stock handler plus an
        ordinary context-enricher filter. NOTHING covers those three now: the record-scanning
        filter that was their only layer has been removed, so they are wholly uncovered rather than
        partly. This project's own handlers are `StreamHandler`s on `sys.stdout` - the only handler
        construction anywhere in `src/` - so none of it is live here, and in a service that ships
        logs over a socket this trade would be the wrong one.
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
      ● **An adversary with the same privilege as the code being guarded.** A `Handler` subclass
        overriding `format`, reassigning this module's own state, or restoring
        `logging.Handler.format` each removes the control in a few lines from inside the process.
        That is inherent to ANY in-process guard rather than a defect in this one, and it is stated
        so a reader does not have to infer it.

    That last point is the honest correction to how this was described. It was called "the only one
    that does not depend on enumerating the adversary's alphabet". It is not: it swapped an alphabet
    of attribute spellings for a smaller one of channels, encodings and framings. What it is
    worth is that it catches the two channels a leak has actually used, at the point of emission,
    whatever route acquired the value - which is real, and less than was claimed.

    Called once from the boot path, where the credential is legitimately in scope. With no token
    configured there is nothing to guard, and disarming restores the four `sys` attributes.

    Disarming does NOT undo the handler side, and the previous sentence here said "so the wrapper
    is not left installed for the life of the process": true of the streams, false of the handlers.
    A re-pointed handler keeps the WRAPPER as its stream, and `logging.Handler.format` stays
    patched. TWO now: this said three, then four, and the count fell to two when the filter layer
    and the handler-construction patch that existed to attach it were removed. Both go inert,
    because each reads `_GUARDED_CREDENTIAL` and finds it `None`, but inert is not absent, and the
    cost is worth naming rather than implying:

      ● Every `Handler.format` call in the process, for the life of the process, routes through a
        closure in this module. It cannot be removed, only made inert. Any handler anywhere in the
        program pays that indirection.
      ● A re-pointed handler's writes still pass through `_GuardedStream`, which forwards only the
        attributes listed on it, so something asking that handler's stream for an attribute outside
        that list gets an `AttributeError` where it previously got a value.

    Reached only in tests and when no token is configured, so it is recorded rather than engineered
    away - but "irreversible mutation of three stdlib attributes" is what arming actually buys, and
    a reader deciding whether to arm it deserves that in one place.
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
    # The GUARANTEE, as opposed to a further layer: everything above scans a model of the line, this
    # scans the line. Written last for reading order only - unlike `_wrap_streams` before the
    # handler walk, which is load-bearing, this call commutes with everything around it and a review
    # confirmed that moving it changes nothing.
    _patch_handler_format()


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


def _patch_handler_format() -> None:
    """Scan the FINISHED LINE, which is the only text a handler actually emits.

    THE GUARANTEE for the log channel, and the reason it exists is that four rounds of scanning a
    MODEL of the line each lost to a part of the real one the model did not have. The record scan
    reached: the message, then the traceback, then every attribute - and a plain `logging.Formatter`
    still put the credential on the line six ways, every one measured on the commit before this:

      ● `%(args)s` where the `%` substitution does not consume the argument, by a mapping key the
        message never names or by a `%.4s` that truncates it.
      ● `%(obj)r` and `{obj!r}` and `{obj!a}`, because a format string chooses the CONVERSION and
        `repr` is not `str`. An object hiding the credential in `__repr__` renders it.
      ● `Formatter(defaults={...})`, where the value never touches the record, so no record scan can
        see it however complete.
      ● A `str` subclass whose `__contains__` lies, kept verbatim by an `isinstance` check.
      ● A filter added AFTER the record-scanning one, which is the ordinary context-enricher
        pattern: the patched constructor MADE that guard the first filter, so every later one ran
        after it and whatever it put on the record was unscanned. Both the constructor patch and the
        filter are gone; this bullet is one of the six historical bypasses that forced the move to
        scanning the finished line, not a live description.
      ● An attribute whose `__str__` raises on the first call and returns the credential on the
        second. This was the worst of the six, because it made the guard actively harmful: DISARMED,
        the formatter's first call raises and `handleError` discards the emission, so nothing is
        written; ARMED, the record scan absorbs the raising call and the formatter's second call
        succeeds, so the credential is emitted. A control that leaks what its absence would have
        contained is worse than no control.

    None of those is an fd-level write, a re-encoding, a split write, or a same-privilege disarm.
    They are all the same defect: a scan of what the line was PREDICTED to contain, not of what it
    does. So this stops predicting. `Handler.format` is where MOST stock handlers turn a record into
    the string they emit - `StreamHandler`, `FileHandler`, `WatchedFileHandler`, `SysLogHandler`,
    `SMTPHandler`, `QueueHandler.prepare` and `MemoryHandler`'s target all call it - and scanning
    its return value is indifferent to which attribute, conversion, formatter default, filter, or
    `__str__` produced the text. It is the same principle `_GuardedStream` already applied to the
    byte channel, applied to the log channel.

    THREE STOCK HANDLERS DO NOT, and the first version of this paragraph named one of them as
    covered. `HTTPHandler.emit` sends `urlencode(self.mapLogRecord(record))`, which is
    `record.__dict__`; `SocketHandler.makePickle` and `DatagramHandler` call `format` only for its
    `exc_text` side effect, throw the return away, and pickle `record.__dict__`. A review measured a
    675-byte pickle and a 533-byte POST body carrying the credential from stock handlers with no
    subclassing at all. For those three the finished-line scan does not apply and the RECORD scan is
    their only layer, and that layer has since been removed, so those three are WHOLLY uncovered.
    Named in the uncovered set below, where the residual was once described as needing a `Handler`
    subclass - which understated it, since none of this needs one.

    What this does NOT cover, named because being exact about it is the whole discipline here: a
    `Handler` subclass that overrides `format`, or that emits without calling it. That is the
    same-privilege class already recorded, not a new one.
    """
    if getattr(logging.Handler.format, "_pree_guarded", False):
        return
    original = logging.Handler.format

    def guarded_format(self: logging.Handler, record: logging.LogRecord) -> str:
        # COERCED, and the coerced value is what is returned. A formatter may return a `str`
        # subclass, and returning the caller's object after scanning a copy is how the second
        # finding worked: `StreamHandler.emit` writes `msg + self.terminator`, so a hostile
        # `__add__` produced the credential from characters that scanned clean. An exact `str` has
        # the builtin `+`.
        text = _exact_text(original(self, record))
        expected = _GUARDED_CREDENTIAL
        if expected and expected in text:
            return CREDENTIAL_ALARM
        return text

    guarded_format._pree_guarded = True  # type: ignore[attr-defined]
    logging.Handler.format = guarded_format  # type: ignore[method-assign]
