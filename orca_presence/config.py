"""Typed configuration for the orca-presence daemon.

Configuration is never hardcoded at the call sites: :class:`Config` resolves
every value from, in order of precedence:

1. environment variables,
2. a TOML file,
3. the documented defaults below.

The Discord application id has no default at all, so it can only come from the
environment, the ``.env`` file or the TOML file and never from the source tree.

Defaults
--------
* ``client_id`` -- required, no default. Read from ``ORCA_DISCORD_CLIENT_ID``
  or the ``client_id`` TOML key; the daemon refuses to start without it.
* ``poll_interval_seconds`` -- ``15.0`` seconds between Orca CLI polls, also
  used as the Discord publish throttle interval.
* ``orca_executable`` -- ``"orca"``, the Orca CLI command or path, resolved
  through ``PATH`` when it is a bare command name. A leading ``~`` is expanded.
* ``orca_timeout_seconds`` -- ``5.0`` seconds per Orca CLI invocation, matching
  :data:`orca_presence.orca_client.DEFAULT_TIMEOUT_SECONDS`.
* ``show_usage`` -- ``False``; enables the OpenCode token part of the Discord
  state. The usage database is only queried while this toggle is on.
* ``show_cost`` -- ``False``; appends the estimated USD cost to the usage part
  of the state. Only meaningful when ``show_usage`` is also on.
* ``usage_interval_seconds`` -- ``300.0`` seconds between usage database reads,
  independent from the Orca poll interval.
* ``usage_db_path`` -- ``~/.local/share/opencode/opencode.db`` with the home
  directory expanded; the local OpenCode database read in read-only mode.
* ``timezone`` -- ``"America/Argentina/Cordoba"``; an IANA zone name used only
  to render times for presentation.

Configuration file
------------------
The TOML file is selected by the ``ORCA_PRESENCE_CONFIG_FILE`` environment
variable or, when that variable is unset, by a ``orca-presence.toml`` in the
current working directory. A missing default file is fine; a file requested
explicitly (by argument or environment variable) must exist and be valid.

.. code-block:: toml

    client_id = "123456789012345678"
    poll_interval_seconds = 15.0
    orca_executable = "orca"
    orca_timeout_seconds = 5.0

Numeric values may be TOML numbers or numeric strings (environment variables
are always strings). Every value is validated by :meth:`Config.load` and by
direct construction, so an unusable configuration fails fast with
:class:`ConfigError` instead of surfacing later as a mysterious runtime error.
"""

from __future__ import annotations

import math
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "CLIENT_ID_ENV",
    "CONFIG_FILE_ENV",
    "DEFAULT_CONFIG_FILE_NAME",
    "DEFAULT_ORCA_EXECUTABLE",
    "DEFAULT_ORCA_TIMEOUT_SECONDS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_SHOW_COST",
    "DEFAULT_SHOW_USAGE",
    "DEFAULT_TIMEZONE",
    "DEFAULT_USAGE_DB_PATH",
    "DEFAULT_USAGE_INTERVAL_SECONDS",
    "ORCA_EXECUTABLE_ENV",
    "ORCA_TIMEOUT_ENV",
    "POLL_INTERVAL_ENV",
    "SHOW_COST_ENV",
    "SHOW_USAGE_ENV",
    "TIMEZONE_ENV",
    "USAGE_DB_PATH_ENV",
    "USAGE_INTERVAL_ENV",
    "Config",
    "ConfigError",
    "find_config_file",
]

CLIENT_ID_ENV = "ORCA_DISCORD_CLIENT_ID"
POLL_INTERVAL_ENV = "ORCA_PRESENCE_POLL_INTERVAL_SECONDS"
ORCA_EXECUTABLE_ENV = "ORCA_PRESENCE_ORCA_EXECUTABLE"
ORCA_TIMEOUT_ENV = "ORCA_PRESENCE_ORCA_TIMEOUT_SECONDS"
SHOW_USAGE_ENV = "ORCA_PRESENCE_SHOW_USAGE"
SHOW_COST_ENV = "ORCA_PRESENCE_SHOW_COST"
USAGE_INTERVAL_ENV = "ORCA_PRESENCE_USAGE_INTERVAL_SECONDS"
USAGE_DB_PATH_ENV = "ORCA_PRESENCE_USAGE_DB_PATH"
TIMEZONE_ENV = "ORCA_PRESENCE_TIMEZONE"
CONFIG_FILE_ENV = "ORCA_PRESENCE_CONFIG_FILE"

DEFAULT_POLL_INTERVAL_SECONDS: float = 15.0
DEFAULT_ORCA_EXECUTABLE = "orca"
DEFAULT_ORCA_TIMEOUT_SECONDS: float = 5.0
DEFAULT_SHOW_USAGE: bool = False
DEFAULT_SHOW_COST: bool = False
DEFAULT_USAGE_INTERVAL_SECONDS: float = 300.0
DEFAULT_USAGE_DB_PATH = str(Path("~/.local/share/opencode/opencode.db").expanduser())
DEFAULT_TIMEZONE = "America/Argentina/Cordoba"
DEFAULT_CONFIG_FILE_NAME = "orca-presence.toml"

_TOML_KEYS: frozenset[str] = frozenset(
    {
        "client_id",
        "poll_interval_seconds",
        "orca_executable",
        "orca_timeout_seconds",
        "show_usage",
        "show_cost",
        "usage_interval_seconds",
        "usage_db_path",
        "timezone",
    }
)

_TRUE_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES: frozenset[str] = frozenset({"0", "false", "no", "off"})


class ConfigError(ValueError):
    """The configuration is missing or invalid; the daemon cannot start."""


@dataclass(frozen=True, slots=True)
class Config:
    """Resolved orca-presence settings.

    Direct construction validates and normalizes every field, so callers that
    build a configuration programmatically get the same guarantees as
    :meth:`load`. ``client_id`` and ``orca_executable`` are stripped of
    surrounding whitespace and ``orca_executable`` has a leading ``~``
    expanded; the numeric fields are stored as finite floats greater than zero;
    the two toggles accept real booleans or the usual ``true``/``false``
    spellings; ``usage_db_path`` has a leading ``~`` expanded and ``timezone``
    must name a known IANA zone.
    """

    client_id: str
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    orca_executable: str = DEFAULT_ORCA_EXECUTABLE
    orca_timeout_seconds: float = DEFAULT_ORCA_TIMEOUT_SECONDS
    show_usage: bool = DEFAULT_SHOW_USAGE
    show_cost: bool = DEFAULT_SHOW_COST
    usage_interval_seconds: float = DEFAULT_USAGE_INTERVAL_SECONDS
    usage_db_path: str = DEFAULT_USAGE_DB_PATH
    timezone: str = DEFAULT_TIMEZONE

    def __post_init__(self) -> None:
        client_id = _parse_str(self.client_id, "client_id")
        object.__setattr__(self, "client_id", client_id)

        executable = _parse_str(self.orca_executable, "orca_executable")
        if executable.startswith("~"):
            executable = str(Path(executable).expanduser())
        object.__setattr__(self, "orca_executable", executable)

        interval = _parse_positive_float(
            self.poll_interval_seconds, "poll_interval_seconds"
        )
        object.__setattr__(self, "poll_interval_seconds", interval)

        timeout = _parse_positive_float(
            self.orca_timeout_seconds, "orca_timeout_seconds"
        )
        object.__setattr__(self, "orca_timeout_seconds", timeout)

        show_usage = _parse_bool(self.show_usage, "show_usage")
        object.__setattr__(self, "show_usage", show_usage)

        show_cost = _parse_bool(self.show_cost, "show_cost")
        object.__setattr__(self, "show_cost", show_cost)

        usage_interval = _parse_positive_float(
            self.usage_interval_seconds, "usage_interval_seconds"
        )
        object.__setattr__(self, "usage_interval_seconds", usage_interval)

        db_path = _parse_str(self.usage_db_path, "usage_db_path")
        if db_path.startswith("~"):
            db_path = str(Path(db_path).expanduser())
        object.__setattr__(self, "usage_db_path", db_path)

        zone = _parse_timezone(self.timezone, "timezone")
        object.__setattr__(self, "timezone", zone)

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> Config:
        """Resolve the configuration from a TOML file, the environment and defaults.

        ``path`` selects the TOML file explicitly; when it is omitted the file
        is located with :func:`find_config_file` (``ORCA_PRESENCE_CONFIG_FILE``
        or ``./orca-presence.toml``). Environment variables override the file,
        which overrides the documented defaults. ``env`` defaults to
        :data:`os.environ` and exists so tests can inject a mapping.

        Raises :class:`ConfigError` when a requested file is missing or
        unreadable, when it contains unknown keys or invalid values, or when
        ``client_id`` is not configured at all.
        """

        environment: Mapping[str, str] = os.environ if env is None else env
        config_path = (
            find_config_file(environment)
            if path is None
            else _require_file(Path(path), "path argument")
        )
        file_values = {} if config_path is None else _read_values(config_path)

        client_id = _resolve_client_id(file_values, config_path, environment)
        poll_interval = _resolve_positive_float(
            "poll_interval_seconds",
            POLL_INTERVAL_ENV,
            DEFAULT_POLL_INTERVAL_SECONDS,
            file_values,
            config_path,
            environment,
        )
        executable = _resolve_str(
            "orca_executable",
            ORCA_EXECUTABLE_ENV,
            DEFAULT_ORCA_EXECUTABLE,
            file_values,
            config_path,
            environment,
        )
        timeout = _resolve_positive_float(
            "orca_timeout_seconds",
            ORCA_TIMEOUT_ENV,
            DEFAULT_ORCA_TIMEOUT_SECONDS,
            file_values,
            config_path,
            environment,
        )
        show_usage = _resolve_bool(
            "show_usage",
            SHOW_USAGE_ENV,
            DEFAULT_SHOW_USAGE,
            file_values,
            config_path,
            environment,
        )
        show_cost = _resolve_bool(
            "show_cost",
            SHOW_COST_ENV,
            DEFAULT_SHOW_COST,
            file_values,
            config_path,
            environment,
        )
        usage_interval = _resolve_positive_float(
            "usage_interval_seconds",
            USAGE_INTERVAL_ENV,
            DEFAULT_USAGE_INTERVAL_SECONDS,
            file_values,
            config_path,
            environment,
        )
        usage_db_path = _resolve_str(
            "usage_db_path",
            USAGE_DB_PATH_ENV,
            DEFAULT_USAGE_DB_PATH,
            file_values,
            config_path,
            environment,
        )
        timezone = _resolve_str(
            "timezone",
            TIMEZONE_ENV,
            DEFAULT_TIMEZONE,
            file_values,
            config_path,
            environment,
        )

        return cls(
            client_id=client_id,
            poll_interval_seconds=poll_interval,
            orca_executable=executable,
            orca_timeout_seconds=timeout,
            show_usage=show_usage,
            show_cost=show_cost,
            usage_interval_seconds=usage_interval,
            usage_db_path=usage_db_path,
            timezone=timezone,
        )


def find_config_file(env: Mapping[str, str] | None = None) -> Path | None:
    """Locate the TOML configuration file, or return ``None`` when there is none.

    ``ORCA_PRESENCE_CONFIG_FILE`` takes precedence and must point to an
    existing file; otherwise ``orca-presence.toml`` is used when present in the
    current working directory. A missing default file is not an error.
    """

    environment: Mapping[str, str] = os.environ if env is None else env
    explicit = environment.get(CONFIG_FILE_ENV)
    if explicit is not None:
        return _require_file(Path(explicit), CONFIG_FILE_ENV)
    default = Path(DEFAULT_CONFIG_FILE_NAME)
    return default if default.is_file() else None


def _require_file(path: Path, source: str) -> Path:
    """Return ``path`` expanded, raising :class:`ConfigError` when it is not a file."""

    if not str(path).strip():
        raise ConfigError(f"{source} must not be blank")
    expanded = path.expanduser()
    if not expanded.is_file():
        raise ConfigError(f"{source} does not point to an existing file: {expanded}")
    return expanded


def _read_values(path: Path) -> dict[str, object]:
    """Parse ``path`` as TOML and reject keys the configuration does not know."""

    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path} could not be read: {exc}") from exc

    unknown = sorted(set(data) - _TOML_KEYS)
    if unknown:
        raise ConfigError(
            f"{path} contains unknown configuration key(s): {', '.join(unknown)}; "
            f"valid keys are: {', '.join(sorted(_TOML_KEYS))}"
        )
    return data


def _resolve_client_id(
    file_values: Mapping[str, object],
    config_path: Path | None,
    environment: Mapping[str, str],
) -> str:
    """Resolve the required ``client_id`` from the environment or the TOML file."""

    if CLIENT_ID_ENV in environment:
        return _parse_str(environment[CLIENT_ID_ENV], CLIENT_ID_ENV)
    if "client_id" in file_values:
        return _parse_str(file_values["client_id"], _source(config_path, "client_id"))
    location = f"{config_path}" if config_path is not None else DEFAULT_CONFIG_FILE_NAME
    raise ConfigError(
        f"client_id is not configured; set the {CLIENT_ID_ENV} environment "
        f"variable or the client_id key in {location}"
    )


def _resolve_str(
    field: str,
    env_var: str,
    default: str,
    file_values: Mapping[str, object],
    config_path: Path | None,
    environment: Mapping[str, str],
) -> str:
    """Resolve a string setting from the environment, the TOML file or ``default``."""

    if env_var in environment:
        return _parse_str(environment[env_var], env_var)
    if field in file_values:
        return _parse_str(file_values[field], _source(config_path, field))
    return default


def _resolve_positive_float(
    field: str,
    env_var: str,
    default: float,
    file_values: Mapping[str, object],
    config_path: Path | None,
    environment: Mapping[str, str],
) -> float:
    """Resolve a positive float setting from environment, TOML file or ``default``."""

    if env_var in environment:
        return _parse_positive_float(environment[env_var], env_var)
    if field in file_values:
        return _parse_positive_float(file_values[field], _source(config_path, field))
    return default


def _resolve_bool(
    field: str,
    env_var: str,
    default: bool,
    file_values: Mapping[str, object],
    config_path: Path | None,
    environment: Mapping[str, str],
) -> bool:
    """Resolve a boolean setting from the environment, the TOML file or ``default``."""

    if env_var in environment:
        return _parse_bool(environment[env_var], env_var)
    if field in file_values:
        return _parse_bool(file_values[field], _source(config_path, field))
    return default


def _source(config_path: Path | None, field: str) -> str:
    """Describe where a TOML value came from for error messages."""

    if config_path is None:
        return field
    return f"{config_path}: {field}"


def _parse_str(raw: object, source: str) -> str:
    """Validate a string setting, stripping surrounding whitespace."""

    if not isinstance(raw, str):
        raise ConfigError(f"{source} must be a string, got {type(raw).__name__}")
    value = raw.strip()
    if not value:
        raise ConfigError(f"{source} must not be blank")
    return value


def _parse_positive_float(raw: object, source: str) -> float:
    """Validate a setting as a positive finite number (numeric strings allowed)."""

    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ConfigError(f"{source} must be a number, got {type(raw).__name__}")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{source} must be a number, got {raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{source} must be a positive finite number, got {raw!r}")
    return value


def _parse_bool(raw: object, source: str) -> bool:
    """Validate a boolean setting (real booleans or the common spellings)."""

    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in _TRUE_VALUES:
            return True
        if value in _FALSE_VALUES:
            return False
    raise ConfigError(f"{source} must be a boolean, got {raw!r}")


def _parse_timezone(raw: object, source: str) -> str:
    """Validate a setting as a known IANA time zone name."""

    value = _parse_str(raw, source)
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(
            f"{source} must be a known IANA time zone, got {value!r}"
        ) from exc
    return value
