"""Runtime configuration, read once from the environment and validated.

The production values are fixed by the deployment contract (version 1) and
are handed to the process by systemd from ``/etc/cowhill/apps/lightsail-demo.env``::

    HOST=127.0.0.1
    PORT=8101
    SERVE_STATIC=0
    ALLOWED_ORIGINS=https://lightsail-demo.cowhill.dev

Everything else has a documented default suitable for the public demo. An
invalid value is a startup failure, never a silent fallback: the process must
not bind a different interface or port than it was told to.

Development mode
----------------
``ALLOWED_ORIGINS`` unset means *development mode*: WebSocket upgrades are
accepted from loopback ``http://`` origins (``localhost``, ``127.0.0.1``,
``[::1]``, any port) and a missing ``REVISION`` file is reported as the
revision ``development``. Setting ``ALLOWED_ORIGINS`` to anything (as the
production environment file does) switches both behaviours off.
"""

from __future__ import annotations

import ipaddress
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

SERVICE_NAME = "lightsail-demo"
DEVELOPMENT_REVISION = "development"

# Contract environment variable names (do not rename).
ENV_HOST = "HOST"
ENV_PORT = "PORT"
ENV_SERVE_STATIC = "SERVE_STATIC"
ENV_ALLOWED_ORIGINS = "ALLOWED_ORIGINS"

# Additional, optional knobs with defaults. Documented in README.md.
ENV_LOG_LEVEL = "LOG_LEVEL"
ENV_WS_MAX_MESSAGE_BYTES = "WS_MAX_MESSAGE_BYTES"
ENV_WS_MAX_CONNECTIONS = "WS_MAX_CONNECTIONS"
ENV_WS_RATE_LIMIT_PER_SECOND = "WS_RATE_LIMIT_PER_SECOND"
ENV_WS_RATE_LIMIT_BURST = "WS_RATE_LIMIT_BURST"
ENV_WS_SEND_QUEUE_LIMIT = "WS_SEND_QUEUE_LIMIT"
ENV_WS_HEARTBEAT_SECONDS = "WS_HEARTBEAT_SECONDS"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_SERVE_STATIC = True
DEFAULT_LOG_LEVEL = "INFO"
# A single chat, draw or game message is well under 1 KiB; 4 KiB leaves room
# for the largest legitimate chat message while bounding parsing work.
DEFAULT_WS_MAX_MESSAGE_BYTES = 4096
# Simultaneous WebSocket connections accepted per applet (chat, draw, game).
DEFAULT_WS_MAX_CONNECTIONS = 64
# Per-connection token bucket. Drawing sends one message per pointer event
# (up to the display refresh rate); the game sends on velocity change and
# while eating. 120/s sustained with a 240 burst is generous for one person.
DEFAULT_WS_RATE_LIMIT_PER_SECOND = 120.0
DEFAULT_WS_RATE_LIMIT_BURST = 240
# Outbound messages buffered per connection before a slow receiver is dropped.
DEFAULT_WS_SEND_QUEUE_LIMIT = 256
# Server-initiated ping interval; a client that does not answer is closed.
DEFAULT_WS_HEARTBEAT_SECONDS = 30.0

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


class ConfigError(ValueError):
    """Raised for an invalid or missing configuration value."""


def _parse_bool(name: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be 0 or 1, got {raw!r}")


def _parse_int(name: str, raw: str, *, minimum: int, maximum: int) -> int:
    try:
        value = int(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _parse_float(name: str, raw: str, *, minimum: float, maximum: float) -> float:
    try:
        value = float(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if math.isnan(value) or not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {raw!r}")
    return value


def _parse_host(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise ConfigError(f"{ENV_HOST} must not be empty")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        # Host names are deliberately not accepted: the contract binds an
        # address, and a name could resolve to a wildcard or public interface.
        raise ConfigError(f"{ENV_HOST} must be an IP address, got {value!r}") from None
    if address.is_unspecified:
        raise ConfigError(
            f"{ENV_HOST} must not be a wildcard address ({value}); bind a specific "
            "interface such as 127.0.0.1"
        )
    return str(address)


def normalize_origin(raw: str) -> str:
    """Return ``scheme://host[:port]`` in lower case, or raise ConfigError.

    An origin is exactly a scheme and an authority: no path, query, fragment,
    credentials or trailing slash. ``https://example.com/`` is rejected so an
    allowlist entry can never accidentally fail to match a browser's Origin.
    """
    value = raw.strip()
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise ConfigError(f"origin {value!r} must start with http:// or https://")
    if not parts.hostname:
        raise ConfigError(f"origin {value!r} has no host")
    if parts.path or parts.query or parts.fragment or parts.username or parts.password:
        raise ConfigError(f"origin {value!r} must be scheme://host[:port] only")
    try:
        port = parts.port
    except ValueError:
        raise ConfigError(f"origin {value!r} has an invalid port") from None
    host = parts.hostname.lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    default_port = 443 if parts.scheme == "https" else 80
    if port is None or port == default_port:
        return f"{parts.scheme}://{host}"
    return f"{parts.scheme}://{host}:{port}"


def _parse_origins(raw: str) -> tuple[str, ...]:
    entries = [item for item in raw.replace(",", " ").split() if item]
    if not entries:
        raise ConfigError(f"{ENV_ALLOWED_ORIGINS} is set but contains no origins")
    seen: dict[str, None] = {}
    for entry in entries:
        seen.setdefault(normalize_origin(entry), None)
    return tuple(seen)


@dataclass(frozen=True)
class Settings:
    """Validated runtime settings. Construct with :func:`load_settings`."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    serve_static: bool = DEFAULT_SERVE_STATIC
    # ``None`` means development mode (loopback origins accepted).
    allowed_origins: tuple[str, ...] | None = None
    log_level: str = DEFAULT_LOG_LEVEL
    ws_max_message_bytes: int = DEFAULT_WS_MAX_MESSAGE_BYTES
    ws_max_connections: int = DEFAULT_WS_MAX_CONNECTIONS
    ws_rate_limit_per_second: float = DEFAULT_WS_RATE_LIMIT_PER_SECOND
    ws_rate_limit_burst: int = DEFAULT_WS_RATE_LIMIT_BURST
    ws_send_queue_limit: int = DEFAULT_WS_SEND_QUEUE_LIMIT
    ws_heartbeat_seconds: float | None = DEFAULT_WS_HEARTBEAT_SECONDS
    # Where the application is installed; ``public/`` and ``REVISION`` live here.
    app_dir: Path = field(default_factory=lambda: default_app_dir())  # noqa: PLW0108 (defined below)

    @property
    def development_mode(self) -> bool:
        return self.allowed_origins is None

    @property
    def public_dir(self) -> Path:
        return self.app_dir / "public"

    @property
    def revision_file(self) -> Path:
        return self.app_dir / "REVISION"

    def origin_allowed(self, origin: str | None) -> bool:
        """Decide whether a WebSocket upgrade with this Origin may proceed.

        This is a browser cross-site guard, not authentication: a non-browser
        client can send any Origin it likes. A missing header is refused
        because every browser sends one for WebSocket handshakes.
        """
        if not origin:
            return False
        try:
            normalized = normalize_origin(origin)
        except ConfigError:
            return False
        if self.allowed_origins is not None:
            return normalized in self.allowed_origins
        scheme, _, authority = normalized.partition("://")
        host = authority.rsplit(":", 1)[0] if not authority.endswith("]") else authority
        return scheme == "http" and host in _LOOPBACK_HOSTS


def default_app_dir() -> Path:
    """The directory containing ``main.py``, resolved through symlinks.

    In production ``main.py`` is started through ``.../current/main.py`` where
    ``current`` is a symlink to a release directory. ``resolve()`` follows it,
    so this points at the *real* release even after ``current`` is switched to
    a newer release later on.
    """
    return Path(__file__).resolve().parent.parent


def load_settings(env: Mapping[str, str] | None = None, *, app_dir: Path | None = None) -> Settings:
    """Read and validate settings from ``env`` (default: ``os.environ``)."""
    if env is None:
        env = os.environ
    values: dict[str, object] = {}

    values["host"] = _parse_host(env.get(ENV_HOST, DEFAULT_HOST))
    values["port"] = _parse_int(ENV_PORT, env.get(ENV_PORT, str(DEFAULT_PORT)), minimum=1, maximum=65535)
    values["serve_static"] = _parse_bool(
        ENV_SERVE_STATIC, env.get(ENV_SERVE_STATIC, "1" if DEFAULT_SERVE_STATIC else "0")
    )
    raw_origins = env.get(ENV_ALLOWED_ORIGINS)
    values["allowed_origins"] = None if raw_origins is None else _parse_origins(raw_origins)

    log_level = env.get(ENV_LOG_LEVEL, DEFAULT_LOG_LEVEL).strip().upper()
    if log_level not in _LOG_LEVELS:
        raise ConfigError(f"{ENV_LOG_LEVEL} must be one of {sorted(_LOG_LEVELS)}, got {log_level!r}")
    values["log_level"] = log_level

    values["ws_max_message_bytes"] = _parse_int(
        ENV_WS_MAX_MESSAGE_BYTES,
        env.get(ENV_WS_MAX_MESSAGE_BYTES, str(DEFAULT_WS_MAX_MESSAGE_BYTES)),
        minimum=256,
        maximum=1024 * 1024,
    )
    values["ws_max_connections"] = _parse_int(
        ENV_WS_MAX_CONNECTIONS,
        env.get(ENV_WS_MAX_CONNECTIONS, str(DEFAULT_WS_MAX_CONNECTIONS)),
        minimum=1,
        maximum=10000,
    )
    values["ws_rate_limit_per_second"] = _parse_float(
        ENV_WS_RATE_LIMIT_PER_SECOND,
        env.get(ENV_WS_RATE_LIMIT_PER_SECOND, str(DEFAULT_WS_RATE_LIMIT_PER_SECOND)),
        minimum=1.0,
        maximum=100000.0,
    )
    values["ws_rate_limit_burst"] = _parse_int(
        ENV_WS_RATE_LIMIT_BURST,
        env.get(ENV_WS_RATE_LIMIT_BURST, str(DEFAULT_WS_RATE_LIMIT_BURST)),
        minimum=1,
        maximum=1000000,
    )
    values["ws_send_queue_limit"] = _parse_int(
        ENV_WS_SEND_QUEUE_LIMIT,
        env.get(ENV_WS_SEND_QUEUE_LIMIT, str(DEFAULT_WS_SEND_QUEUE_LIMIT)),
        minimum=1,
        maximum=1000000,
    )
    heartbeat = _parse_float(
        ENV_WS_HEARTBEAT_SECONDS,
        env.get(ENV_WS_HEARTBEAT_SECONDS, str(DEFAULT_WS_HEARTBEAT_SECONDS)),
        minimum=0.0,
        maximum=3600.0,
    )
    values["ws_heartbeat_seconds"] = heartbeat if heartbeat > 0 else None

    if app_dir is not None:
        values["app_dir"] = Path(app_dir).resolve()

    return Settings(**values)  # type: ignore[arg-type]
