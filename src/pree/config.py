"""Environment-only configuration with fail-closed validation.

Every value is resolved from the environment at boot. Nothing is hard-coded and nothing is
baked into the image, because an image-level default always beats a code fallback chain and
would silently defeat the value the platform injects.

Resolution order for the data directory is explicit variable, then platform-injected
variable, then a local default. A control that cannot be verified is treated as failed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# The shortest string that can be a quoted value: the two quotes themselves.
_QUOTED_MINIMUM = 2
_CONTROL_CHARS = frozenset(chr(code) for code in [*range(0, 32), 127])
# The single credential guarding the whole assessment store. Wrong-token attempts are rate
# limited per address, but 240 a minute per address per worker puts a dictionary of common
# choices well inside an hour, so a short or guessable token gets the same fail-closed boot
# treatment as an unsafe origin rather than a warning nobody reads.
MIN_PRODUCTION_TOKEN_LENGTH = 24
# A 24-character repetition of one dictionary word cleared the length floor while the
# control was described as rejecting a guessable token. secrets.token_urlsafe(32) yields 43
# characters with far more variety than this, so the floor costs a real token nothing.
MIN_PRODUCTION_TOKEN_VARIETY = 12
_ORIGIN_PATTERN = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?$")

DEFAULT_PORT = 8080
MIN_PORT = 1
MAX_PORT = 65535
LOCAL_DATA_DIR = "./data"


class ConfigError(RuntimeError):
    """Raised when the environment cannot be resolved into a safe configuration."""


@dataclass(frozen=True, slots=True)
class Config:
    """The resolved runtime contract. Immutable once boot has validated it."""

    port: int
    data_dir: Path
    team_token: str | None
    allowed_origin: str | None
    environment: str
    build_id: str
    data_dir_was_configured: bool

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def auth_enabled(self) -> bool:
        """Authentication is on exactly when a team token is configured."""
        return self.team_token is not None


def _read(env: dict[str, str], name: str) -> str | None:
    """Read a variable, normalising what a console paste actually delivers.

    Three things, all of them observed failure modes rather than theory. A cleared console
    field leaves an empty string, not a missing key, so blank counts as absent and a blank
    token never counts as a configured token. A pasted value often arrives wrapped in the
    quotes that surrounded it in a document, and `"/data"` used as a path resolves to a
    literal directory named `"/data"` inside the working directory, which is writable, so the
    fault presents as silent data loss rather than an error. And a control character in a
    value would carry into a log line or a header.
    """
    value = env.get(name, "").strip()
    if len(value) >= _QUOTED_MINIMUM and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    if any(ch in value for ch in _CONTROL_CHARS):
        raise ConfigError(f"{name} contains a control character")
    return value or None


def _resolve_port(env: dict[str, str]) -> int:
    """Resolve the listen port. The platform injects PORT; the code defaults to 8080."""
    raw = _read(env, "PORT")
    if raw is None:
        return DEFAULT_PORT
    if not raw.isdigit():
        raise ConfigError(f"PORT must be a positive integer, got {raw!r}")
    port = int(raw)
    if not MIN_PORT <= port <= MAX_PORT:
        raise ConfigError(f"PORT must be within {MIN_PORT} to {MAX_PORT}, got {port}")
    return port


def _resolve_data_dir(env: dict[str, str]) -> Path:
    """Resolve the data directory: explicit, then platform-injected, then local default.

    The resolved path must be absolute and must not be the filesystem root. Both are
    rejected at boot rather than discovered on the first write.
    """
    explicit = _read(env, "PREE_DATA_DIR") or _read(env, "STORAGE_MOUNT_PATH")
    raw = explicit or LOCAL_DATA_DIR
    expanded = Path(raw).expanduser()
    if explicit is not None and not expanded.is_absolute():
        # Checked BEFORE resolving, or the check cannot fail: resolve() makes every value
        # absolute against the working directory, so a relative or quote-wrapped path became a
        # writable directory inside the container and the store silently missed the volume.
        raise ConfigError(
            f"the data directory must be an absolute path, got {raw!r}. A relative value "
            f"resolves inside the container and is lost on every restart."
        )
    path = expanded.resolve()
    if path == Path(path.anchor):
        raise ConfigError(f"data directory must not be the filesystem root, got {path}")
    return path


def _validate_origin_shape(origin: str | None) -> None:
    """Reject an origin that is not a real origin, in every environment.

    The wildcard check used to live inside the production branch and match `*` exactly, so
    development accepted `*` with credentials, and `null` (which allows every opaque origin:
    a sandboxed iframe, a `data:` document) was accepted anywhere. An origin is either a
    concrete scheme and host or it is not usable.
    """
    if origin is None:
        return
    if origin in {"*", "null"} or "," in origin:
        raise ConfigError(
            f"Refusing to start: PREE_ALLOWED_ORIGIN must be a single concrete origin, "
            f"got {origin!r}. A wildcard, 'null', or a list is never an allowed origin."
        )
    if not _ORIGIN_PATTERN.match(origin):
        raise ConfigError(
            f"Refusing to start: PREE_ALLOWED_ORIGIN must look like "
            f"scheme://host[:port], got {origin!r}."
        )


def _validate_production_auth(token: str | None, origin: str | None, environment: str) -> None:
    """Fail closed on every unsafe production posture.

    Three postures are refused, not two. The absent token is the most dangerous of them and
    was the one originally missed: with no token the gate is open, so production would serve
    read and write of the assessment store to any caller that can reach the ingress. The
    store reveals what the operator is watching and what they judge dangerous, so an open
    gate is a disclosure, not a convenience. Development is the only place the open mode is
    reachable.
    """
    if environment != "production":
        return
    if token is None:
        raise ConfigError(
            "Refusing to start: PREE_ENV is 'production' with no PREE_TEAM_TOKEN. "
            "Production must never serve the assessment store unauthenticated. "
            "Set PREE_TEAM_TOKEN and PREE_ALLOWED_ORIGIN together."
        )
    if len(token) < MIN_PRODUCTION_TOKEN_LENGTH:
        raise ConfigError(
            f"Refusing to start: PREE_TEAM_TOKEN is {len(token)} characters, below the "
            f"{MIN_PRODUCTION_TOKEN_LENGTH} required in production. Generate one with "
            f'python -c "import secrets; print(secrets.token_urlsafe(32))".'
        )
    if len(set(token)) < MIN_PRODUCTION_TOKEN_VARIETY:
        raise ConfigError(
            f"Refusing to start: PREE_TEAM_TOKEN uses only {len(set(token))} distinct "
            f"characters, below the {MIN_PRODUCTION_TOKEN_VARIETY} required in production. "
            f'Generate one with python -c "import secrets; '
            f'print(secrets.token_urlsafe(32))".'
        )
    if origin is None:
        raise ConfigError(
            "Refusing to start: PREE_TEAM_TOKEN is set with no PREE_ALLOWED_ORIGIN. "
            "Set the allowed origin to the app's real origin."
        )


def load_config(env: dict[str, str] | None = None) -> Config:
    """Resolve and validate the configuration, or raise ConfigError.

    The environment is injected so boot behaviour is testable without mutating the process.
    """
    source = dict(os.environ if env is None else env)
    environment = (_read(source, "PREE_ENV") or "production").lower()
    if environment not in {"development", "production"}:
        raise ConfigError(f"PREE_ENV must be 'development' or 'production', got {environment!r}")

    token = _read(source, "PREE_TEAM_TOKEN")
    origin = _read(source, "PREE_ALLOWED_ORIGIN")
    _validate_origin_shape(origin)
    _validate_production_auth(token, origin, environment)

    return Config(
        port=_resolve_port(source),
        data_dir=_resolve_data_dir(source),
        data_dir_was_configured=(
            _read(source, "PREE_DATA_DIR") or _read(source, "STORAGE_MOUNT_PATH")
        )
        is not None,
        team_token=token,
        allowed_origin=origin,
        environment=environment,
        build_id=_read(source, "PREE_BUILD_ID") or "unknown",
    )
