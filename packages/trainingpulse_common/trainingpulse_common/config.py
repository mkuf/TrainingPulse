"""Structured YAML configuration for TrainingPulse.

Configuration is authored in a single ``config.yaml``. Two independent
environment mechanisms sit on top of it:

1. **Structural overrides** — ``TP_<SECTION>__<KEY>`` (double underscore for
   nesting), applied to the parsed document before validation. Lists accept a
   comma-separated string; booleans accept ``1/true/yes/on`` and their
   negatives.
2. **Value interpolation** — ``${VAR}`` and ``${VAR:-default}`` inside YAML
   string values, expanded textually before validation. These names are chosen
   by the user and carry no ``TP_`` prefix.

Precedence is: env override > YAML value > built-in default.

The file path itself comes from ``TP_CONFIG_FILE``, falling back to
``/config/config.yaml`` and then ``./config.yaml``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, get_args

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

logger = logging.getLogger(__name__)

ENV_PREFIX = "TP_"
CONFIG_PATH_ENV = "TP_CONFIG_FILE"
DEFAULT_CONFIG_PATHS: tuple[str, ...] = ("/config/config.yaml", "config.yaml")

#: Known activity sources, in default precedence order.
SOURCE_NAMES: tuple[str, ...] = ("icu", "strava", "google_health", "hammerhead")

#: Known plugin names.
PLUGIN_NAMES: tuple[str, ...] = ("withings", "fddb")

REDACTED = "***"


# ── Errors ──────────────────────────────────────────────────────────


class ConfigError(Exception):
    """Raised when the configuration file is missing, malformed, or invalid.

    Carries every problem found so the caller can report them all at once
    instead of making the user fix them one restart at a time.
    """

    def __init__(
        self, problems: Sequence[tuple[str, str]], *, path: Path | str | None = None
    ):
        self.problems: list[tuple[str, str]] = list(problems)
        self.path = str(path) if path is not None else None
        super().__init__(format_config_error(self))


def format_config_error(error: ConfigError) -> str:
    """Render a ConfigError as a human-readable, multi-line message."""
    where = f" in {error.path}" if error.path else ""
    lines = [f"Invalid TrainingPulse configuration{where}:"]
    for location, message in error.problems:
        lines.append(f"  - {location}: {message}" if location else f"  - {message}")
    return "\n".join(lines)


# ── Secret marking ──────────────────────────────────────────────────


def secret(default: Any = "", **kwargs: Any) -> Any:
    """Declare a field whose value must never be logged or dumped."""
    return Field(default, json_schema_extra={"secret": True}, **kwargs)


def _model_classes(annotation: Any) -> list[type[BaseModel]]:
    """Return the BaseModel classes reachable from a field annotation."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    found: list[type[BaseModel]] = []
    for arg in get_args(annotation):
        found.extend(_model_classes(arg))
    return found


def secret_paths(model_cls: type[BaseModel]) -> set[tuple[str, ...]]:
    """Every dotted path in ``model_cls`` marked with :func:`secret`."""
    paths: set[tuple[str, ...]] = set()

    def walk(cls: type[BaseModel], prefix: tuple[str, ...]) -> None:
        for name, info in cls.model_fields.items():
            extra = info.json_schema_extra
            if isinstance(extra, dict) and extra.get("secret"):
                paths.add(prefix + (name,))
            for nested in _model_classes(info.annotation):
                walk(nested, prefix + (name,))

    walk(model_cls, ())
    return paths


# ── Schema ──────────────────────────────────────────────────────────


class _Section(BaseModel):
    """Base for every config section: unknown keys are a hard error."""

    model_config = ConfigDict(extra="forbid")


def _split_csv(value: Any) -> Any:
    """Accept a comma-separated string wherever a list of strings is expected."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class AppSection(_Section):
    base_url: str = "http://localhost:8000"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, value: str) -> str:
        return value.strip().rstrip("/")


class DatabaseSection(_Section):
    url: str = "postgresql+asyncpg://trainingpulse:changeme@db:5432/trainingpulse"


class AthleteSection(_Section):
    """Heart rate and power settings. The only source of zone data."""

    max_hr: int = Field(..., gt=0, le=300)
    rest_hr: int = Field(..., gt=0, le=300)
    ftp: int = Field(..., gt=0, le=2000)

    @model_validator(mode="after")
    def _check_hr_order(self) -> "AthleteSection":
        if self.rest_hr >= self.max_hr:
            raise ValueError("rest_hr must be lower than max_hr")
        return self


class IcuSourceSection(_Section):
    enabled: bool = False
    api_key: str = secret("")
    api_base: str = "https://intervals.icu/api/v1"

    def is_configured(self) -> bool:
        return bool(self.api_key.strip())

    def missing_credentials(self) -> list[str]:
        return [] if self.api_key.strip() else ["api_key"]


class StravaSourceSection(_Section):
    enabled: bool = False
    client_id: str = ""
    client_secret: str = secret("")

    def is_configured(self) -> bool:
        return bool(self.client_id.strip() and self.client_secret.strip())

    def missing_credentials(self) -> list[str]:
        missing = []
        if not self.client_id.strip():
            missing.append("client_id")
        if not self.client_secret.strip():
            missing.append("client_secret")
        return missing


class GoogleHealthSourceSection(_Section):
    enabled: bool = False
    client_id: str = ""
    client_secret: str = secret("")
    api_base: str = "https://health.googleapis.com/v4"

    def is_configured(self) -> bool:
        return bool(self.client_id.strip() and self.client_secret.strip())

    def missing_credentials(self) -> list[str]:
        missing = []
        if not self.client_id.strip():
            missing.append("client_id")
        if not self.client_secret.strip():
            missing.append("client_secret")
        return missing


class HammerheadSourceSection(_Section):
    enabled: bool = False
    client_id: str = ""
    client_secret: str = secret("")
    api_base: str = "https://api.hammerhead.io/v1/api"

    def is_configured(self) -> bool:
        return bool(self.client_id.strip() and self.client_secret.strip())

    def missing_credentials(self) -> list[str]:
        missing = []
        if not self.client_id.strip():
            missing.append("client_id")
        if not self.client_secret.strip():
            missing.append("client_secret")
        return missing


class SourcesSection(_Section):
    precedence: list[str] = Field(default_factory=lambda: list(SOURCE_NAMES))
    icu: IcuSourceSection = Field(default_factory=IcuSourceSection)
    strava: StravaSourceSection = Field(default_factory=StravaSourceSection)
    google_health: GoogleHealthSourceSection = Field(
        default_factory=GoogleHealthSourceSection
    )
    hammerhead: HammerheadSourceSection = Field(
        default_factory=HammerheadSourceSection
    )

    _split_precedence = field_validator("precedence", mode="before")(
        classmethod(lambda cls, v: _split_csv(v))
    )

    @field_validator("precedence")
    @classmethod
    def _validate_precedence(cls, value: list[str]) -> list[str]:
        names = [item.strip().lower() for item in value]
        unknown = [name for name in names if name not in SOURCE_NAMES]
        if unknown:
            raise ValueError(
                f"unknown source name(s) {', '.join(unknown)}; "
                f"known sources are {', '.join(SOURCE_NAMES)}"
            )
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate source name(s) {', '.join(duplicates)}")
        # Sources omitted from the list rank last, in declaration order.
        return names + [name for name in SOURCE_NAMES if name not in names]

    def section(self, name: str) -> _Section:
        return getattr(self, name)

    def enabled_names(self) -> list[str]:
        """Enabled source names in precedence order."""
        return [name for name in self.precedence if self.section(name).enabled]


class SyncSection(_Section):
    interval_minutes: int = Field(15, ge=1)
    reconcile_days: int = Field(90, ge=0)
    backfill_years: int = Field(10, ge=1)
    window_days: int = Field(90, ge=1)
    detail_concurrency: int = Field(5, ge=1)
    streams_concurrency: int = Field(5, ge=1)


class McpSection(_Section):
    host: str = "0.0.0.0"
    port: int = Field(8001, ge=1, le=65535)
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["localhost:*", "127.0.0.1:*"]
    )

    _split_hosts = field_validator("allowed_hosts", mode="before")(
        classmethod(lambda cls, v: _split_csv(v))
    )


class WithingsPluginSection(_Section):
    enabled: bool = False
    client_id: str = ""
    client_secret: str = secret("")
    redirect_uri: str = ""
    sync_interval_minutes: int = Field(60, ge=1)
    sync_lookback_days: int = Field(3650, ge=1)

    def is_configured(self) -> bool:
        return bool(self.client_id.strip() and self.client_secret.strip())

    def missing_credentials(self) -> list[str]:
        missing = []
        if not self.client_id.strip():
            missing.append("client_id")
        if not self.client_secret.strip():
            missing.append("client_secret")
        return missing


class FddbPluginSection(_Section):
    enabled: bool = False
    user: str = ""
    password: str = secret("")
    cookie: str = secret("")
    base_url: str = "https://fddb.info"
    lang: str = "de"
    sync_interval_minutes: int = Field(60, ge=1)
    sync_lookback_days: int = Field(365, ge=1)
    request_delay_ms: int = Field(300, ge=0)

    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, value: str) -> str:
        return value.strip().rstrip("/")

    def is_configured(self) -> bool:
        return bool(self.user.strip() and self.password.strip() and self.cookie.strip())

    def missing_credentials(self) -> list[str]:
        return [
            name
            for name, value in (
                ("user", self.user),
                ("password", self.password),
                ("cookie", self.cookie),
            )
            if not value.strip()
        ]


class PluginsSection(_Section):
    withings: WithingsPluginSection = Field(default_factory=WithingsPluginSection)
    fddb: FddbPluginSection = Field(default_factory=FddbPluginSection)

    def section(self, name: str) -> _Section:
        return getattr(self, name)

    def enabled_names(self) -> list[str]:
        return [name for name in PLUGIN_NAMES if self.section(name).enabled]


class Config(_Section):
    """Root of the TrainingPulse configuration."""

    athlete: AthleteSection
    app: AppSection = Field(default_factory=AppSection)
    database: DatabaseSection = Field(default_factory=DatabaseSection)
    sources: SourcesSection = Field(default_factory=SourcesSection)
    sync: SyncSection = Field(default_factory=SyncSection)
    mcp: McpSection = Field(default_factory=McpSection)
    plugins: PluginsSection = Field(default_factory=PluginsSection)


# ── Plugin injection ────────────────────────────────────────────────


@dataclass(frozen=True)
class PluginContext:
    """Everything a plugin needs at construction time."""

    name: str
    database_url: str
    app_base_url: str
    options: Any

    @property
    def prefix(self) -> str:
        return f"/plugins/{self.name}"


def plugin_context(config: Config, name: str) -> PluginContext:
    """Build the context handed to ``build_plugin`` for one plugin."""
    return PluginContext(
        name=name,
        database_url=config.database.url,
        app_base_url=config.app.base_url,
        options=config.plugins.section(name),
    )


# ── Interpolation ───────────────────────────────────────────────────

_INTERPOLATION = re.compile(
    r"""\$\{
        (?P<name>[A-Za-z_][A-Za-z0-9_]*)
        (?::-(?P<default>[^}]*))?
    \}""",
    re.VERBOSE,
)


def _interpolate_str(value: str, env: Mapping[str, str], location: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        fallback = match.group("default")
        if name in env:
            return env[name]
        if fallback is not None:
            return fallback
        # Not an error on its own: an unset credential only matters when the
        # source or plugin is actually enabled, which the semantic checks below
        # report with a far better message.
        logger.warning(
            "config %s references ${%s}, which is not set; using an empty value",
            location,
            name,
        )
        return ""

    return _INTERPOLATION.sub(replace, value)


def _interpolate(node: Any, env: Mapping[str, str], location: str = "") -> Any:
    if isinstance(node, str):
        return _interpolate_str(node, env, location or "value")
    if isinstance(node, dict):
        return {
            key: _interpolate(val, env, f"{location}.{key}" if location else str(key))
            for key, val in node.items()
        }
    if isinstance(node, list):
        return [
            _interpolate(item, env, f"{location}[{index}]")
            for index, item in enumerate(node)
        ]
    return node


# ── Env overrides ───────────────────────────────────────────────────


def _field_path_exists(model_cls: type[BaseModel], parts: Sequence[str]) -> bool:
    """True when ``parts`` addresses a real field, walking nested sections."""
    if not parts:
        return False
    fields = model_cls.model_fields
    info = fields.get(parts[0])
    if info is None:
        return False
    if len(parts) == 1:
        return True
    for nested in _model_classes(info.annotation):
        if _field_path_exists(nested, parts[1:]):
            return True
    return False


def _assign(data: dict[str, Any], parts: Sequence[str], value: str) -> str | None:
    """Set a nested key, returning an error message on conflict."""
    cursor: dict[str, Any] = data
    for part in parts[:-1]:
        existing = cursor.get(part)
        if existing is None:
            existing = {}
            cursor[part] = existing
        elif not isinstance(existing, dict):
            return f"cannot override inside {part}, which is not a mapping"
        cursor = existing
    cursor[parts[-1]] = value
    return None


def _apply_env_overrides(
    data: dict[str, Any], env: Mapping[str, str]
) -> list[tuple[str, str]]:
    """Apply ``TP_*`` overrides in place; return problems found."""
    problems: list[tuple[str, str]] = []
    for name in sorted(env):
        if not name.startswith(ENV_PREFIX) or name == CONFIG_PATH_ENV:
            continue
        suffix = name[len(ENV_PREFIX) :]
        if not suffix:
            continue
        parts = [part.lower() for part in suffix.split("__") if part]
        if not _field_path_exists(Config, parts):
            problems.append(
                (name, f"does not match any config key (tried {'.'.join(parts)})")
            )
            continue
        error = _assign(data, parts, env[name])
        if error is not None:
            problems.append((name, error))
    return problems


# ── Semantic checks ─────────────────────────────────────────────────


def _semantic_problems(config: Config) -> list[tuple[str, str]]:
    """Cross-field rules that need a fully validated model."""
    problems: list[tuple[str, str]] = []

    for name in SOURCE_NAMES:
        section = config.sources.section(name)
        if section.enabled and not section.is_configured():
            missing = ", ".join(section.missing_credentials())
            problems.append(
                (
                    f"sources.{name}",
                    f"enabled is true but {missing} is blank",
                )
            )

    for name in PLUGIN_NAMES:
        section = config.plugins.section(name)
        if section.enabled and not section.is_configured():
            missing = ", ".join(section.missing_credentials())
            problems.append(
                (
                    f"plugins.{name}",
                    f"enabled is true but {missing} is blank",
                )
            )

    return problems


def _warn_precedence(config: Config) -> None:
    """Warn when a disabled source outranks an enabled one.

    A disabled source still participates in supersession, so ranking one above
    an enabled source hides rows that can be enriched behind rows that cannot.
    """
    enabled = set(config.sources.enabled_names())
    for index, name in enumerate(config.sources.precedence):
        if name in enabled:
            continue
        outranked = [
            other for other in config.sources.precedence[index + 1 :] if other in enabled
        ]
        if outranked:
            logger.warning(
                "sources.precedence ranks %s above enabled source(s) %s, but %s is "
                "disabled; its activities will outrank data that can still be "
                "refreshed",
                name,
                ", ".join(outranked),
                name,
            )


# ── Loading ─────────────────────────────────────────────────────────


def _resolve_path(
    path: str | Path | None,
    env: Mapping[str, str],
    search_paths: Sequence[str] = DEFAULT_CONFIG_PATHS,
) -> Path:
    if path is not None:
        candidate = Path(path)
        if not candidate.is_file():
            raise ConfigError([("", f"config file not found: {candidate}")])
        return candidate

    from_env = env.get(CONFIG_PATH_ENV, "").strip()
    if from_env:
        candidate = Path(from_env)
        if not candidate.is_file():
            raise ConfigError(
                [(CONFIG_PATH_ENV, f"config file not found: {candidate}")]
            )
        return candidate

    for default in search_paths:
        candidate = Path(default)
        if candidate.is_file():
            return candidate

    raise ConfigError(
        [
            (
                "",
                "no config file found; set "
                f"{CONFIG_PATH_ENV} or create one of: "
                f"{', '.join(search_paths) or '(no search paths)'}",
            )
        ]
    )


def _validation_problems(error: ValidationError) -> list[tuple[str, str]]:
    problems: list[tuple[str, str]] = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"])
        problems.append((location or "<root>", item["msg"]))
    return problems


def load_config(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    search_paths: Sequence[str] = DEFAULT_CONFIG_PATHS,
) -> Config:
    """Load, interpolate, override, and validate the configuration.

    Raises :class:`ConfigError` with every problem found.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    resolved = _resolve_path(path, environ, search_paths)

    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError([("", f"could not parse YAML: {exc}")], path=resolved) from exc
    except OSError as exc:
        raise ConfigError([("", f"could not read file: {exc}")], path=resolved) from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(
            [("", "the top level of the config file must be a mapping")], path=resolved
        )

    data = _interpolate(raw, environ)
    problems = _apply_env_overrides(data, environ)

    config: Config | None = None
    try:
        config = Config.model_validate(data)
    except ValidationError as exc:
        problems.extend(_validation_problems(exc))

    if config is not None:
        problems.extend(_semantic_problems(config))

    if problems:
        raise ConfigError(problems, path=resolved)

    assert config is not None  # narrowed: no problems means validation succeeded
    _warn_precedence(config)
    return config


def load_config_or_exit(
    path: str | Path | None = None, *, env: Mapping[str, str] | None = None
) -> Config:
    """Load the config, or print every problem and exit non-zero."""
    import sys

    try:
        return load_config(path, env=env)
    except ConfigError as exc:
        print(format_config_error(exc), file=sys.stderr)
        raise SystemExit(1) from exc


# ── Redaction ───────────────────────────────────────────────────────


def redact(config: Config) -> dict[str, Any]:
    """Config as a plain dict with every secret field masked."""
    data = config.model_dump(mode="json")
    for path in secret_paths(type(config)):
        cursor: Any = data
        for part in path[:-1]:
            if not isinstance(cursor, dict):
                cursor = None
                break
            cursor = cursor.get(part)
        if isinstance(cursor, dict):
            value = cursor.get(path[-1])
            if isinstance(value, str) and value:
                cursor[path[-1]] = REDACTED
    return data


def redacted_dump(config: Config) -> str:
    """Effective configuration as YAML, with secrets masked."""
    return yaml.safe_dump(redact(config), sort_keys=False, default_flow_style=False)
