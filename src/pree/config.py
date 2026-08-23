"""Environment-only configuration with fail-closed validation.

Every value is resolved from the environment at boot. Nothing is hard-coded and nothing is baked
into the image. Not because an image default always beats an injected value, which is false: a
runtime value overrides image ENV. It is because a baked value shadows the fallback below it, and
for the data directory the resolution order here means a baked PREE_DATA_DIR is preferred over the
platform's STORAGE_MOUNT_PATH, which would send every write to the ephemeral layer.

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
MIN_PRODUCTION_TOKEN_LENGTH = 32
# A repeated word cleared the length floor, so repetition is refused directly. A count of
# distinct characters was tried first and was worse than nothing: it refused a real
# secrets.token_hex(16) about 1.7% of the time, which teaches an operator to weaken a
# credential until the checker stops complaining, while still admitting
# "Bluestaq2026!Bluestaq2026". These two rules are properties of the string, not guesses
# about its entropy, and a 32-character floor clears token_hex(16) and token_urlsafe(24).
_ORIGIN_PATTERN = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?$")

DEFAULT_PORT = 8080
MIN_PORT = 1
MAX_PORT = 65535
LOCAL_DATA_DIR = "./data"


class ConfigError(RuntimeError):
    """Raised when the environment cannot be resolved into a safe configuration."""


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """Everything the HTTP layer needs, and DELIBERATELY not the team token.

    This type exists because a security review defeated the previous protection twice in nine
    lines. That protection was a static rule refusing `config.<attr>` inside an expression that
    reaches an audit record, plus a pin on which `config` attributes the HTTP layer reads. Both
    checked the SPELLING of a name rather than the flow of a value, so two variants walked
    straight past them: a helper in another module called as `helper(config, exc)`, and a helper
    in the same module whose parameter was named `cfg` rather than `config`. Either recovered the
    deployed credential verbatim from the pod log on every unauthenticated 401.

    A name-based rule will always lose that race, because the attacker chooses the names. So the
    credential is not in this object. `create_app` is handed a `ServiceConfig` and a callable, and
    nothing in the HTTP layer's object graph carries the token under ANY spelling: there is no
    attribute to reach, so no helper, parameter name, module, or encoding can reach it.

    `auth_enabled` and `token_length` are carried as precomputed FACTS rather than derived from the
    value, because `/diagnostics` needs both and neither discloses the credential. Without them a
    stale token is invisible on a deployed pod: every client gets 401, and the read-out that would
    show it sits behind the very token that is wrong.
    """

    port: int
    data_dir: Path
    allowed_origin: str | None
    environment: str
    build_id: str
    data_dir_was_configured: bool
    auth_enabled: bool
    token_length: int

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@dataclass(frozen=True, slots=True)
class Config:
    """The resolved runtime contract, including the credential. Immutable once boot validated it.

    Held by the boot path only. `split()` is the boundary: past it, the credential exists solely
    inside one closure and the HTTP layer holds a callable instead of a secret.
    """

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

    def for_service(self) -> ServiceConfig:
        """The token-free view the HTTP layer receives."""
        return ServiceConfig(
            port=self.port,
            data_dir=self.data_dir,
            allowed_origin=self.allowed_origin,
            environment=self.environment,
            build_id=self.build_id,
            data_dir_was_configured=self.data_dir_was_configured,
            auth_enabled=self.auth_enabled,
            token_length=len(self.team_token or ""),
        )


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


def _smallest_repeating_unit(value: str) -> str:
    """The shortest prefix whose repetition reproduces the whole string.

    Returns the value itself when it is not an exact repetition, so a token that merely
    contains a repeated fragment is not refused; only one built entirely from repetition is.
    """
    for size in range(1, len(value) // 2 + 1):
        if len(value) % size == 0 and value[:size] * (len(value) // size) == value:
            return value[:size]
    return value


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
    unit = _smallest_repeating_unit(token)
    if len(unit) * 2 <= len(token):
        raise ConfigError(
            f"Refusing to start: PREE_TEAM_TOKEN is {len(token) // len(unit)} repetitions of "
            f"a {len(unit)}-character sequence. Generate one with "
            f'python -c "import secrets; print(secrets.token_urlsafe(32))".'
        )
    if origin is None:
        raise ConfigError(
            "Refusing to start: PREE_TEAM_TOKEN is set with no PREE_ALLOWED_ORIGIN. "
            "Set the allowed origin to the app's real origin."
        )
    if not origin.startswith("https://"):
        # CORS is configured with allow_credentials=True, so the browser will attach the team
        # token to a cross-origin request to this origin. Over http that is the token in
        # cleartext on the wire, and the origin pattern admitted http:// in any environment.
        # Development still allows it, because localhost has no certificate.
        raise ConfigError(
            f"Refusing to start: PREE_ALLOWED_ORIGIN is {origin!r}, which is not https. "
            "Credentialed requests to a cleartext origin put the team token on the wire. "
            "Set the app's https origin."
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
