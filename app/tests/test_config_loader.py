"""Tests for the structured YAML configuration loader."""

from __future__ import annotations

import logging
import textwrap

import pytest
from pydantic import BaseModel
from trainingpulse_common.catalog import DataKind, default_precedence
from trainingpulse_common.config import (
    REDACTED,
    Config,
    ConfigError,
    load_config,
    redact,
    secret_paths,
)

# ── Basic loading ───────────────────────────────────────────────────


def test_minimal_config_applies_defaults(minimal_config):
    config = load_config(minimal_config, env={})

    assert config.athlete.max_hr == 190
    assert config.athlete.rest_hr == 60
    assert config.athlete.ftp == 250
    assert config.app.base_url == "http://localhost:8000"
    assert config.app.log_level == "INFO"
    assert config.sync.interval_minutes == 15
    assert config.sync.window_days == 90
    assert config.mcp.port == 8001
    assert config.mcp.allowed_hosts == ["localhost:*", "127.0.0.1:*"]
    assert config.athlete.timezone == "UTC"
    assert config.data.activities.precedence == default_precedence(DataKind.ACTIVITIES)
    assert config.providers.enabled_names() == []


def test_base_url_trailing_slash_is_stripped(write_config):
    path = write_config(
        """
        app:
          base_url: https://pulse.example.com/
        athlete:
          max_hr: 190
          rest_hr: 60
          ftp: 250
        """
    )

    config = load_config(path, env={})

    assert config.app.base_url == "https://pulse.example.com"


def test_empty_file_still_requires_athlete(write_config):
    path = write_config("")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})

    assert [location for location, _ in excinfo.value.problems] == ["athlete"]


def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "nope.yaml", env={})

    assert "not found" in excinfo.value.problems[0][1]


def test_no_file_anywhere_lists_search_path(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={}, search_paths=[str(tmp_path / "absent.yaml")])

    message = excinfo.value.problems[0][1]
    assert "TP_CONFIG_FILE" in message
    assert "absent.yaml" in message


def test_config_path_env_var_is_honoured(minimal_config):
    config = load_config(env={"TP_CONFIG_FILE": str(minimal_config)})

    assert config.athlete.ftp == 250


def test_config_path_env_var_pointing_nowhere_is_an_error(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={"TP_CONFIG_FILE": str(tmp_path / "missing.yaml")})

    assert excinfo.value.problems[0][0] == "TP_CONFIG_FILE"


def test_malformed_yaml_is_reported(write_config):
    path = write_config("athlete: [unclosed\n")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})

    assert "could not parse YAML" in excinfo.value.problems[0][1]


def test_non_mapping_top_level_is_rejected(write_config):
    path = write_config("- just\n- a\n- list\n")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})

    assert "must be a mapping" in excinfo.value.problems[0][1]


# ── Validation strictness ───────────────────────────────────────────


def test_unknown_key_is_rejected(write_config):
    path = write_config(
        """
        athlete:
          max_hr: 190
          rest_hr: 60
          ftp: 250
        sync:
          reconile_days: 30
        """
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})

    assert ("sync.reconile_days", "Extra inputs are not permitted") in [
        (location, message) for location, message in excinfo.value.problems
    ]


def test_wrong_type_is_rejected(write_config):
    path = write_config(
        """
        athlete:
          max_hr: not-a-number
          rest_hr: 60
          ftp: 250
        """
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})

    assert excinfo.value.problems[0][0] == "athlete.max_hr"


def test_all_errors_are_aggregated(write_config):
    path = write_config(
        """
        athlete:
          max_hr: 190
          rest_hr: 60
        sync:
          interval_minutes: zero
        mcp:
          bogus: 1
        """
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})

    locations = {location for location, _ in excinfo.value.problems}
    assert locations == {"athlete.ftp", "sync.interval_minutes", "mcp.bogus"}


def test_rest_hr_must_be_below_max_hr(write_config):
    path = write_config(
        """
        athlete:
          max_hr: 150
          rest_hr: 160
          ftp: 250
        """
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})

    assert "rest_hr must be lower than max_hr" in excinfo.value.problems[0][1]


def test_error_message_lists_every_problem(write_config):
    path = write_config("sync:\n  interval_minutes: nope\n")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})

    rendered = str(excinfo.value)
    assert "Invalid TrainingPulse configuration" in rendered
    assert str(path) in rendered
    assert "athlete" in rendered
    assert "sync.interval_minutes" in rendered


# ── Env overrides ───────────────────────────────────────────────────


def test_scalar_override(minimal_config):
    config = load_config(
        minimal_config, env={"TP_DATA__ACTIVITIES__REFRESH_DAYS": "7"}
    )

    assert config.data.activities.refresh_days == 7


def test_deeply_nested_override(minimal_config):
    config = load_config(
        minimal_config,
        env={
            "TP_PROVIDERS__STRAVA__ENABLED": "true",
            "TP_PROVIDERS__STRAVA__CLIENT_ID": "123",
            "TP_PROVIDERS__STRAVA__CLIENT_SECRET": "shh",
        },
    )

    assert config.providers.strava.enabled is True
    assert config.providers.strava.client_secret == "shh"


def test_list_override_is_comma_split(minimal_config):
    config = load_config(
        minimal_config,
        env={
            "TP_DATA__ACTIVITIES__PRECEDENCE": "strava, icu",
            "TP_MCP__ALLOWED_HOSTS": "a:*,b:*",
        },
    )

    assert config.data.activities.precedence == [
        "strava",
        "icu",
        "google_health",
        "hammerhead",
    ]
    assert config.mcp.allowed_hosts == ["a:*", "b:*"]


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_bool_override_truthy_forms(minimal_config, raw):
    config = load_config(
        minimal_config,
        env={
            "TP_PROVIDERS__FDDB__ENABLED": raw,
            "TP_PROVIDERS__FDDB__USER": "u",
            "TP_PROVIDERS__FDDB__PASSWORD": "p",
            "TP_PROVIDERS__FDDB__COOKIE": "c",
        },
    )

    assert config.providers.fddb.enabled is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
def test_bool_override_falsy_forms(minimal_config, raw):
    config = load_config(minimal_config, env={"TP_PROVIDERS__FDDB__ENABLED": raw})

    assert config.providers.fddb.enabled is False


def test_override_beats_file_value(write_config):
    path = write_config(
        """
        athlete:
          max_hr: 190
          rest_hr: 60
          ftp: 250
        sync:
          window_days: 30
        """
    )

    config = load_config(path, env={"TP_SYNC__WINDOW_DAYS": "45"})

    assert config.sync.window_days == 45


def test_unknown_override_names_the_variable(minimal_config):
    with pytest.raises(ConfigError) as excinfo:
        load_config(minimal_config, env={"TP_SYNC__NOPE": "1"})

    assert excinfo.value.problems[0][0] == "TP_SYNC__NOPE"
    assert "does not match any config key" in excinfo.value.problems[0][1]


def test_config_path_var_is_not_treated_as_an_override(minimal_config):
    config = load_config(env={"TP_CONFIG_FILE": str(minimal_config)})

    assert config.athlete.max_hr == 190


def test_override_into_non_mapping_is_reported(write_config):
    path = write_config(
        """
        athlete:
          max_hr: 190
          rest_hr: 60
          ftp: 250
        sync: 5
        """
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={"TP_SYNC__WINDOW_DAYS": "45"})

    assert excinfo.value.problems[0][0] == "TP_SYNC__WINDOW_DAYS"
    assert "not a mapping" in excinfo.value.problems[0][1]


# ── Interpolation ───────────────────────────────────────────────────


def test_interpolation_uses_environment(write_config):
    path = write_config(
        """
        athlete:
          max_hr: 190
          rest_hr: 60
          ftp: 250
        providers:
          icu:
            enabled: true
            api_key: ${MY_ICU_KEY}
        """
    )

    config = load_config(path, env={"MY_ICU_KEY": "abc123"})

    assert config.providers.icu.api_key == "abc123"


def test_interpolation_default_is_used_when_unset(write_config):
    path = write_config(
        """
        app:
          base_url: ${PUBLIC_URL:-http://fallback:9000}
        athlete:
          max_hr: 190
          rest_hr: 60
          ftp: 250
        """
    )

    config = load_config(path, env={})

    assert config.app.base_url == "http://fallback:9000"


def test_interpolation_unset_without_default_warns_and_empties(write_config, caplog):
    path = write_config(
        """
        athlete:
          max_hr: 190
          rest_hr: 60
          ftp: 250
        providers:
          icu:
            api_key: ${NOT_SET_ANYWHERE}
        """
    )

    with caplog.at_level(logging.WARNING):
        config = load_config(path, env={})

    assert config.providers.icu.api_key == ""
    assert "NOT_SET_ANYWHERE" in caplog.text
    assert "providers.icu.api_key" in caplog.text


def test_literal_dollar_is_preserved(write_config):
    path = write_config(
        """
        athlete:
          max_hr: 190
          rest_hr: 60
          ftp: 250
        providers:
          strava:
            client_id: "costs $5 and 100% $ of nothing"
        """
    )

    config = load_config(path, env={})

    assert config.providers.strava.client_id == "costs $5 and 100% $ of nothing"


def test_interpolation_happens_inside_lists(write_config):
    path = write_config(
        """
        athlete:
          max_hr: 190
          rest_hr: 60
          ftp: 250
        mcp:
          allowed_hosts:
            - ${MCP_HOST_PATTERN}
            - static:*
        """
    )

    config = load_config(path, env={"MCP_HOST_PATTERN": "dynamic:*"})

    assert config.mcp.allowed_hosts == ["dynamic:*", "static:*"]


def test_env_override_value_is_not_interpolated(minimal_config):
    config = load_config(
        minimal_config,
        env={
            "TP_PROVIDERS__STRAVA__CLIENT_ID": "${NOT_EXPANDED}",
            "NOT_EXPANDED": "boom",
        },
    )

    assert config.providers.strava.client_id == "${NOT_EXPANDED}"


# ── Precedence ──────────────────────────────────────────────────────


def test_precedence_subset_appends_missing_sources(write_config):
    path = write_config(
        """
        athlete:
          max_hr: 190
          rest_hr: 60
          ftp: 250
        data:
          activities:
            precedence: [strava]
        """
    )

    config = load_config(path, env={})

    assert config.data.activities.precedence == [
        "strava",
        "icu",
        "google_health",
        "hammerhead",
    ]


def test_precedence_rejects_unknown_source(write_config):
    path = write_config(
        """
        athlete:
          max_hr: 190
          rest_hr: 60
          ftp: 250
        data:
          activities:
            precedence: [icu, garmin]
        """
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})

    assert excinfo.value.problems[0][0] == "data.activities"
    assert "garmin" in excinfo.value.problems[0][1]


def test_precedence_rejects_duplicates(write_config):
    path = write_config(
        """
        athlete:
          max_hr: 190
          rest_hr: 60
          ftp: 250
        data:
          activities:
            precedence: [icu, icu]
        """
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})

    assert "duplicate" in excinfo.value.problems[0][1]


def test_active_providers_follow_precedence_order(write_config):
    path = write_config(
        """
        athlete:
          max_hr: 190
          rest_hr: 60
          ftp: 250
        data:
          activities:
            precedence: [google_health, strava, icu]
        providers:
          icu:
            enabled: true
            api_key: k
          strava:
            enabled: true
            client_id: i
            client_secret: s
        """
    )

    config = load_config(path, env={})

    assert config.active_providers(DataKind.ACTIVITIES) == ["strava", "icu"]


def test_disabled_source_outranking_enabled_one_warns(write_config, caplog):
    path = write_config(
        """
        athlete:
          max_hr: 190
          rest_hr: 60
          ftp: 250
        data:
          activities:
            precedence: [strava, icu, google_health]
        providers:
          icu:
            enabled: true
            api_key: k
        """
    )

    with caplog.at_level(logging.WARNING):
        load_config(path, env={})

    assert "data.activities.precedence ranks strava above icu" in caplog.text


# ── enabled requires credentials ────────────────────────────────────


def test_enabled_source_without_credentials_is_an_error(write_config):
    path = write_config(
        """
        athlete:
          max_hr: 190
          rest_hr: 60
          ftp: 250
        providers:
          strava:
            enabled: true
            client_id: "1234"
        """
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})

    assert excinfo.value.problems == [
        ("providers.strava", "enabled is true but client_secret is blank")
    ]


def test_enabled_session_provider_without_credentials_is_an_error(write_config):
    path = write_config(
        """
        athlete:
          max_hr: 190
          rest_hr: 60
          ftp: 250
        providers:
          fddb:
            enabled: true
            user: someone
        """
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})

    assert excinfo.value.problems == [
        ("providers.fddb", "enabled is true but password, cookie is blank")
    ]


def test_disabled_source_without_credentials_is_fine(write_config):
    path = write_config(
        """
        athlete:
          max_hr: 190
          rest_hr: 60
          ftp: 250
        providers:
          google_health:
            enabled: false
        """
    )

    config = load_config(path, env={})

    assert config.providers.google_health.is_configured() is False


def test_blank_numeric_override_is_a_validation_error_not_a_crash(minimal_config):
    with pytest.raises(ConfigError) as excinfo:
        load_config(minimal_config, env={"TP_PROVIDERS__FDDB__REQUEST_DELAY_MS": ""})

    assert excinfo.value.problems[0][0] == "providers.fddb.request_delay_ms"


# ── Redaction ───────────────────────────────────────────────────────


def _all_field_paths(
    model_cls: type[BaseModel], prefix: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], str]]:
    """Every (path, field_name) pair in a model tree."""
    found: list[tuple[tuple[str, ...], str]] = []
    for name, info in model_cls.model_fields.items():
        path = prefix + (name,)
        found.append((path, name))
        annotation = info.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            found.extend(_all_field_paths(annotation, path))
    return found


SECRET_NAME_HINTS = ("secret", "password", "cookie", "api_key", "token", "_pw")


def test_every_sensitive_looking_field_is_marked_secret():
    marked = secret_paths(Config)
    unmarked = [
        ".".join(path)
        for path, name in _all_field_paths(Config)
        if any(hint in name.lower() for hint in SECRET_NAME_HINTS)
        and path not in marked
    ]

    assert unmarked == [], (
        "these fields look sensitive but are not declared with secret(): "
        f"{unmarked}"
    )


def test_known_secret_paths_are_marked():
    marked = secret_paths(Config)

    assert ("providers", "icu", "api_key") in marked
    assert ("providers", "strava", "client_secret") in marked
    assert ("providers", "google_health", "client_secret") in marked
    assert ("providers", "hammerhead", "client_secret") in marked
    assert ("providers", "withings", "client_secret") in marked
    assert ("providers", "fddb", "password") in marked
    assert ("providers", "fddb", "cookie") in marked


def test_redact_masks_secrets_and_keeps_the_rest(write_config):
    path = write_config(
        """
        athlete:
          max_hr: 190
          rest_hr: 60
          ftp: 250
        providers:
          icu:
            enabled: true
            api_key: super-secret-key
          strava:
            enabled: true
            client_id: "42"
            client_secret: also-secret
        """
    )

    config = load_config(path, env={})
    dumped = redact(config)

    assert dumped["providers"]["icu"]["api_key"] == REDACTED
    assert dumped["providers"]["strava"]["client_secret"] == REDACTED
    assert dumped["providers"]["strava"]["client_id"] == "42"
    assert dumped["athlete"]["ftp"] == 250
    assert "super-secret-key" not in str(dumped)
    assert "also-secret" not in str(dumped)


def test_redact_leaves_empty_secrets_visible_as_empty(minimal_config):
    config = load_config(minimal_config, env={})
    dumped = redact(config)

    assert dumped["providers"]["icu"]["api_key"] == ""


# ── Keys from the sources/plugins era ──────────────────────────────


@pytest.mark.parametrize(
    "body, location, hint",
    [
        (
            """
            sources:
              precedence: [icu]
            """,
            "sources",
            "data.activities.precedence",
        ),
        (
            """
            plugins:
              withings:
                enabled: false
            """,
            "plugins",
            "providers.withings",
        ),
        (
            """
            sync:
              backfill_years: 5
            """,
            "sync.backfill_years",
            "history_days",
        ),
        (
            """
            sync:
              reconcile_days: 30
            """,
            "sync.reconcile_days",
            "data.activities.refresh_days",
        ),
    ],
)
def test_moved_keys_say_where_they_went(write_config, body, location, hint):
    athlete = "athlete:\n  max_hr: 190\n  rest_hr: 60\n  ftp: 250\n"
    path = write_config(athlete + textwrap.dedent(body).lstrip())

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={})

    assert len(excinfo.value.problems) == 1
    problem_location, message = excinfo.value.problems[0]
    assert problem_location == location
    assert hint in message
