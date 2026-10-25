"""Tests for the provider / data-kind configuration schema."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from trainingpulse_common.catalog import (
    KINDS,
    PROVIDERS,
    DataKind,
    default_precedence,
    providers_supporting,
)
from trainingpulse_common.config import ConfigError, load_config, precedence_warnings

ATHLETE = {"max_hr": 190, "rest_hr": 60, "ftp": 250}
EXAMPLE = Path(__file__).resolve().parents[2] / "config.example.yaml"


@pytest.fixture
def load_with(write_config):
    def load(**sections):
        body = yaml.safe_dump({"athlete": ATHLETE, **sections}, sort_keys=False)
        return load_config(write_config(body), env={})

    return load


@pytest.fixture
def problems_for(load_with):
    def collect(**sections) -> list[tuple[str, str]]:
        with pytest.raises(ConfigError) as info:
            load_with(**sections)
        return info.value.problems

    return collect


# ── Catalog ─────────────────────────────────────────────────────────


def test_every_kind_has_a_spec():
    assert set(KINDS) == set(DataKind)


def test_default_precedence_only_names_supporting_providers():
    for kind in DataKind:
        assert sorted(default_precedence(kind)) == sorted(providers_supporting(kind))


# ── Defaults ────────────────────────────────────────────────────────


def test_minimal_config_fills_every_kind_from_the_catalog(load_with):
    config = load_with()

    assert config.athlete.timezone == "UTC"
    for kind in DataKind:
        section = config.data.kind(kind)
        assert section.precedence == default_precedence(kind)
        assert section.history_days == KINDS[kind].default_history_days
        assert section.refresh_days == KINDS[kind].default_refresh_days


def test_omitted_imports_means_everything_the_provider_supports(load_with):
    config = load_with()
    for name, spec in PROVIDERS.items():
        assert config.providers.imports_of(name) == list(spec.kinds)


def test_refresh_days_zero_is_kept_not_replaced_by_the_default(load_with):
    config = load_with(data={"activities": {"refresh_days": 0}})
    assert config.data.activities.refresh_days == 0


def test_example_file_validates():
    config = load_config(EXAMPLE, env={"INTERVALS_ICU_API_KEY": "key"})

    assert config.active_providers(DataKind.ACTIVITIES) == ["icu"]
    assert config.data.body.precedence == ["withings"]


# ── Imports ─────────────────────────────────────────────────────────


def test_importing_an_unsupported_kind_names_what_is_supported(problems_for):
    problems = problems_for(providers={"strava": {"imports": ["body"]}})

    assert problems[0][0] == "providers.strava"
    assert "Strava cannot provide body" in problems[0][1]
    assert "it provides activities" in problems[0][1]


def test_unknown_kind_is_rejected(problems_for):
    problems = problems_for(providers={"icu": {"imports": ["steps"]}})
    assert problems[0][0].startswith("providers.icu.imports")


def test_empty_imports_is_an_error_not_a_silent_disable(problems_for):
    problems = problems_for(providers={"withings": {"imports": []}})
    assert "imports is empty" in problems[0][1]


def test_imports_accept_a_comma_separated_string(load_with):
    config = load_with(providers={"icu": {"imports": "activities, activities"}})
    assert config.providers.imports_of("icu") == [DataKind.ACTIVITIES]


# ── Precedence ──────────────────────────────────────────────────────


def test_partial_precedence_appends_the_rest_in_default_order(load_with):
    config = load_with(data={"activities": {"precedence": ["hammerhead"]}})
    assert config.data.activities.precedence == [
        "hammerhead",
        "icu",
        "strava",
        "google_health",
    ]


def test_precedence_rejects_a_provider_that_cannot_supply_the_kind(problems_for):
    problems = problems_for(data={"body": {"precedence": "strava"}})

    assert problems[0][0] == "data.body"
    assert "strava cannot provide body" in problems[0][1]
    assert "withings" in problems[0][1]


def test_precedence_rejects_unknown_and_duplicate_providers(problems_for):
    assert "unknown provider" in problems_for(
        data={"activities": {"precedence": ["garmin"]}}
    )[0][1]
    assert "duplicate provider" in problems_for(
        data={"activities": {"precedence": ["icu", "icu"]}}
    )[0][1]


def test_active_providers_respect_enabled_imports_and_order(load_with):
    config = load_with(
        providers={
            "icu": {"enabled": True, "api_key": "k"},
            "strava": {"enabled": True, "client_id": "i", "client_secret": "s"},
            "withings": {"enabled": True, "client_id": "i", "client_secret": "s"},
        },
        data={"activities": {"precedence": ["strava", "icu"]}},
    )

    assert config.active_providers(DataKind.ACTIVITIES) == ["strava", "icu"]
    assert config.active_providers(DataKind.BODY) == ["withings"]
    assert config.active_providers(DataKind.SLEEP) == []


def test_warns_when_an_inactive_provider_outranks_an_active_one(load_with):
    config = load_with(providers={"strava": {"enabled": True, "client_id": "i", "client_secret": "s"}})

    warnings = precedence_warnings(config)

    assert len(warnings) == 1
    assert "data.activities.precedence ranks icu above strava" in warnings[0]


# ── Credentials ─────────────────────────────────────────────────────


def test_enabled_without_credentials_reports_every_provider(problems_for):
    problems = problems_for(
        providers={"withings": {"enabled": True}, "fddb": {"enabled": True, "user": "u"}}
    )

    assert ("providers.withings", "enabled is true but client_id, client_secret is blank") in problems
    assert ("providers.fddb", "enabled is true but password, cookie is blank") in problems


def test_unknown_timezone_is_rejected(problems_for):
    problems = problems_for(athlete={**ATHLETE, "timezone": "Mars/Olympus"})
    assert problems[0][0] == "athlete.timezone"
