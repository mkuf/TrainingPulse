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
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from trainingpulse_common.catalog import (
    KINDS,
    PROVIDER_NAMES,
    PROVIDERS,
    DataKind,
    default_precedence,
    providers_supporting,
)

logger = logging.getLogger(__name__)

ENV_PREFIX = "TP_"
CONFIG_PATH_ENV = "TP_CONFIG_FILE"
DEFAULT_CONFIG_PATHS: tuple[str, ...] = ("/config/config.yaml", "config.yaml")

REDACTED = "***"

#: Top-level and nested keys from the sources/plugins era, with where they went.
#: Reported instead of a bare "extra inputs are not permitted".
_MOVED_KEYS: dict[tuple[str, ...], str] = {
    ("sources",): "moved to providers.<name>, and sources.precedence to "
    "data.activities.precedence",
    ("plugins",): "moved to providers.withings and providers.fddb",
    ("sync", "backfill_years"): "moved to data.<kind>.history_days (in days)",
    ("sync", "reconcile_days"): "moved to data.activities.refresh_days",
}


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
    #: Where the status page links to the dashboards. Empty means base_url's
    #: host on Grafana's default port 3000.
    grafana_url: str = ""
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("base_url", "grafana_url")
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
    #: Fallback body weight for calorie estimates, used only when no weigh-in
    #: (e.g. from Withings) has been imported.
    weight_kg: float | None = Field(None, gt=0, le=400)
    #: IANA zone used to assign timestamps to calendar days. Body measurements
    #: are deduplicated per day, and a 00:30 weigh-in in Berlin belongs to the
    #: previous day in UTC.
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _check_hr_order(self) -> "AthleteSection":
        if self.rest_hr >= self.max_hr:
            raise ValueError("rest_hr must be lower than max_hr")
        return self


# ── Providers ───────────────────────────────────────────────────────


class _ProviderSection(_Section):
    enabled: bool = False
    #: Data kinds to import. Omitted means every kind the provider supports.
    #: Named ``imports`` because ``import`` cannot be a field name and the
    #: ``TP_*`` override resolver matches field names, not aliases.
    imports: list[DataKind] | None = None

    _split_imports = field_validator("imports", mode="before")(
        classmethod(lambda cls, v: _split_csv(v))
    )

    def missing_credentials(self) -> list[str]:
        return []

    def is_configured(self) -> bool:
        return not self.missing_credentials()


class _OAuthProviderSection(_ProviderSection):
    client_id: str = ""
    client_secret: str = secret("")

    def missing_credentials(self) -> list[str]:
        return [
            name
            for name, value in (
                ("client_id", self.client_id),
                ("client_secret", self.client_secret),
            )
            if not value.strip()
        ]


class IcuProvider(_ProviderSection):
    api_key: str = secret("")
    api_base: str = "https://intervals.icu/api/v1"

    def missing_credentials(self) -> list[str]:
        return [] if self.api_key.strip() else ["api_key"]


class StravaProvider(_OAuthProviderSection):
    pass


class GoogleHealthProvider(_OAuthProviderSection):
    api_base: str = "https://health.googleapis.com/v4"


class HammerheadProvider(_OAuthProviderSection):
    api_base: str = "https://api.hammerhead.io/v1/api"


class WithingsProvider(_OAuthProviderSection):
    #: Blank uses {app.base_url}/plugins/withings/get_token, the callback
    #: earlier releases registered, so existing Partner Hub apps keep working.
    redirect_uri: str = ""


class FddbProvider(_ProviderSection):
    user: str = ""
    password: str = secret("")
    cookie: str = secret("")
    base_url: str = "https://fddb.info"
    lang: str = "de"
    #: Politeness delay between scraped diary pages.
    request_delay_ms: int = Field(300, ge=0)

    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, value: str) -> str:
        return value.strip().rstrip("/")

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


class ProvidersSection(_Section):
    icu: IcuProvider = Field(default_factory=IcuProvider)
    strava: StravaProvider = Field(default_factory=StravaProvider)
    google_health: GoogleHealthProvider = Field(default_factory=GoogleHealthProvider)
    hammerhead: HammerheadProvider = Field(default_factory=HammerheadProvider)
    withings: WithingsProvider = Field(default_factory=WithingsProvider)
    fddb: FddbProvider = Field(default_factory=FddbProvider)

    @field_validator(*PROVIDER_NAMES)
    @classmethod
    def _validate_imports(
        cls, section: _ProviderSection, info: ValidationInfo
    ) -> _ProviderSection:
        if section.imports is None:
            return section
        spec = PROVIDERS[info.field_name]
        if not section.imports:
            raise ValueError(
                "imports is empty; set enabled: false instead, or omit imports "
                "to take everything this provider supports"
            )
        unsupported = [kind.value for kind in section.imports if kind not in spec.kinds]
        if unsupported:
            raise ValueError(
                f"{spec.label} cannot provide {', '.join(unsupported)}; it provides "
                f"{', '.join(kind.value for kind in spec.kinds)}"
            )
        # Dedupe, keeping the user's order.
        return section.model_copy(
            update={"imports": list(dict.fromkeys(section.imports))}
        )

    def section(self, name: str) -> _ProviderSection:
        return getattr(self, name)

    def imports_of(self, name: str) -> list[DataKind]:
        """Kinds ``name`` is configured to import, whether or not it is enabled."""
        section = self.section(name)
        if section.imports is None:
            return list(PROVIDERS[name].kinds)
        return list(section.imports)

    def is_active(self, name: str, kind: DataKind) -> bool:
        """True when ``name`` is enabled and imports ``kind``."""
        return self.section(name).enabled and kind in self.imports_of(name)

    def enabled_names(self) -> list[str]:
        """Enabled provider names, in declaration order."""
        return [name for name in PROVIDER_NAMES if self.section(name).enabled]


# ── Data kinds ──────────────────────────────────────────────────────


class DataKindSection(_Section):
    #: Highest precedence first. Supporting providers left out rank last.
    #: Positional and independent of ``enabled``: disabling a provider must not
    #: promote another provider's rows over its stored history.
    precedence: list[str] = Field(default_factory=list)
    #: How far back the first sync lists. Filled from the kind's default.
    history_days: int | None = Field(None, ge=1)
    #: How far back every later sync re-lists to pick up upstream edits.
    #: 0 disables re-listing. Filled from the kind's default.
    refresh_days: int | None = Field(None, ge=0)

    _split_precedence = field_validator("precedence", mode="before")(
        classmethod(lambda cls, v: _split_csv(v))
    )


class DataSection(_Section):
    # validate_default so an omitted block still gets its kind's defaults.
    activities: DataKindSection = Field(
        default_factory=DataKindSection, validate_default=True
    )
    body: DataKindSection = Field(default_factory=DataKindSection, validate_default=True)
    nutrition: DataKindSection = Field(
        default_factory=DataKindSection, validate_default=True
    )
    sleep: DataKindSection = Field(default_factory=DataKindSection, validate_default=True)

    @field_validator(*(kind.value for kind in DataKind), mode="after")
    @classmethod
    def _resolve(cls, section: DataKindSection, info: ValidationInfo) -> DataKindSection:
        kind = DataKind(info.field_name)
        spec = KINDS[kind]
        supporting = providers_supporting(kind)

        names = [name.strip().lower() for name in section.precedence]
        unknown = [name for name in names if name not in PROVIDERS]
        if unknown:
            raise ValueError(
                f"unknown provider(s) {', '.join(unknown)} in precedence; known "
                f"providers are {', '.join(PROVIDER_NAMES)}"
            )
        cannot = [name for name in names if name not in supporting]
        if cannot:
            raise ValueError(
                f"{', '.join(cannot)} cannot provide {kind.value}; providers that "
                f"can are {', '.join(supporting) or 'none yet'}"
            )
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(
                f"duplicate provider(s) {', '.join(duplicates)} in precedence"
            )

        resolved = names + [
            name for name in default_precedence(kind) if name not in names
        ]
        return section.model_copy(
            update={
                "precedence": resolved,
                "history_days": section.history_days or spec.default_history_days,
                "refresh_days": (
                    spec.default_refresh_days
                    if section.refresh_days is None
                    else section.refresh_days
                ),
            }
        )

    def kind(self, kind: DataKind) -> DataKindSection:
        return getattr(self, kind.value)


# ── Sync and MCP ────────────────────────────────────────────────────


class SyncSection(_Section):
    """Scheduling and throughput. History windows live in ``data.<kind>``."""

    interval_minutes: int = Field(15, ge=1)
    #: Chunk size for backfill and refresh listing.
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


class Config(_Section):
    """Root of the TrainingPulse configuration."""

    athlete: AthleteSection
    app: AppSection = Field(default_factory=AppSection)
    database: DatabaseSection = Field(default_factory=DatabaseSection)
    providers: ProvidersSection = Field(default_factory=ProvidersSection)
    data: DataSection = Field(default_factory=DataSection)
    sync: SyncSection = Field(default_factory=SyncSection)
    mcp: McpSection = Field(default_factory=McpSection)

    def active_providers(self, kind: DataKind) -> list[str]:
        """Providers that sync ``kind``, in that kind's precedence order."""
        return [
            name
            for name in self.data.kind(kind).precedence
            if self.providers.is_active(name, kind)
        ]


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
        # provider is actually enabled, which the semantic checks below
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


def _moved_key_problems(data: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Point keys from the sources/plugins era at their new home."""
    problems: list[tuple[str, str]] = []
    for path, message in _MOVED_KEYS.items():
        cursor: Any = data
        for part in path:
            if not isinstance(cursor, Mapping) or part not in cursor:
                break
            cursor = cursor[part]
        else:
            problems.append((".".join(path), message))
    return problems


def _semantic_problems(config: Config) -> list[tuple[str, str]]:
    """Cross-field rules that need a fully validated model."""
    problems: list[tuple[str, str]] = []
    for name in PROVIDER_NAMES:
        section = config.providers.section(name)
        if section.enabled and not section.is_configured():
            problems.append(
                (
                    f"providers.{name}",
                    f"enabled is true but {', '.join(section.missing_credentials())} "
                    "is blank",
                )
            )
    return problems


def precedence_warnings(config: Config) -> list[str]:
    """Inactive providers ranked above active ones, per kind.

    Not an error: a provider you stopped using keeps its stored rows, and
    ranking it first is how you say "keep its history visible". But those rows
    can no longer be refreshed, which is usually not what was meant.
    """
    warnings: list[str] = []
    for kind in DataKind:
        precedence = config.data.kind(kind).precedence
        for index, name in enumerate(precedence):
            if config.providers.is_active(name, kind):
                continue
            outranked = [
                other
                for other in precedence[index + 1 :]
                if config.providers.is_active(other, kind)
            ]
            if outranked:
                warnings.append(
                    f"data.{kind.value}.precedence ranks {name} above "
                    f"{', '.join(outranked)}, but {name} is not importing "
                    f"{kind.value}; its stored rows will hide data that can "
                    "still be refreshed"
                )
    return warnings


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
    moved = _moved_key_problems(data)
    if moved:
        # Validating would only add a less helpful "extra inputs" error for
        # the same keys, plus knock-on errors from sections that moved.
        problems.extend(moved)
    else:
        try:
            config = Config.model_validate(data)
        except ValidationError as exc:
            problems.extend(_validation_problems(exc))

    if config is not None:
        problems.extend(_semantic_problems(config))

    if problems:
        raise ConfigError(problems, path=resolved)

    assert config is not None  # narrowed: no problems means validation succeeded
    for warning in precedence_warnings(config):
        logger.warning(warning)
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
