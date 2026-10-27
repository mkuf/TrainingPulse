"""Tests for providers, their feeds, and the per-day record engine."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from catalog import PROVIDER_NAMES, PROVIDERS, DataKind
from config_loader import load_config
from fddb_client import (
    FddbAuthenticationError,
    FddbNoDataError,
    parse_diary_html,
)
from providers.base import Provider
from providers.fddb import local_days, map_day
from providers.feeds import FEED_CONTRACTS, BodyRecord, NutritionRecord
from providers.records import (
    _body_values,
    choose_daily_winners,
    first_day_to_list,
    local_day_window,
    map_records,
)
from providers.registry import build_all_providers
from providers.withings import WithingsBodyFeed, map_measure_group

FDDB_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fddb"
BERLIN = ZoneInfo("Europe/Berlin")


# ── Every provider honours the contract ─────────────────────────────


@pytest.fixture
def all_providers(minimal_config):
    return build_all_providers(load_config(minimal_config, env={}), None)


def test_registry_builds_every_catalog_provider(all_providers):
    assert [provider.name for provider in all_providers] == list(PROVIDER_NAMES)


def test_providers_satisfy_the_protocol(all_providers):
    for provider in all_providers:
        assert isinstance(provider, Provider), provider.name
        assert provider.auth == PROVIDERS[provider.name].auth, provider.name


def test_feeds_match_the_catalog_and_their_contracts(all_providers):
    for provider in all_providers:
        feeds = provider.feeds()
        assert set(feeds) == set(PROVIDERS[provider.name].kinds), provider.name
        for kind, feed in feeds.items():
            assert feed.kind == kind
            assert isinstance(feed, FEED_CONTRACTS[kind]), (provider.name, kind)


def test_oauth_providers_have_connect_routes(all_providers):
    for provider in all_providers:
        is_oauth = PROVIDERS[provider.name].auth == "oauth"
        assert bool(provider.connect_path) == is_oauth, provider.name
        assert bool(provider.disconnect_path) == is_oauth, provider.name


# ── Daily deduplication ─────────────────────────────────────────────


DAY_1, DAY_2, DAY_3 = date(2026, 3, 1), date(2026, 3, 2), date(2026, 3, 3)


def test_highest_precedence_provider_wins_each_day():
    rows = [(DAY_1, "google_health"), (DAY_1, "withings"), (DAY_2, "google_health")]

    winners = choose_daily_winners(rows, ["withings", "google_health"])

    assert winners == {DAY_1: "withings", DAY_2: "google_health"}


def test_daily_winner_does_not_depend_on_row_order():
    rows = [(DAY_3, "b"), (DAY_3, "a"), (DAY_3, "withings")]

    assert choose_daily_winners(rows, ["withings"]) == {DAY_3: "withings"}
    assert choose_daily_winners(list(reversed(rows)), []) == {DAY_3: "a"}


def test_unranked_providers_lose_to_ranked_ones():
    rows = [(DAY_1, "zzz"), (DAY_1, "fddb")]
    assert choose_daily_winners(rows, ["fddb"]) == {DAY_1: "fddb"}


# ── Windows ─────────────────────────────────────────────────────────


def test_first_sync_walks_the_full_history():
    today = date(2026, 10, 25)
    assert first_day_to_list(
        today=today, latest_stored=None, history_days=365, refresh_days=7
    ) == today - timedelta(days=365)


def test_later_syncs_relist_the_refresh_window():
    today = date(2026, 10, 25)
    assert first_day_to_list(
        today=today, latest_stored=today, history_days=365, refresh_days=7
    ) == date(2026, 10, 18)


def test_a_stale_provider_catches_up_from_its_newest_day():
    today = date(2026, 10, 25)
    assert first_day_to_list(
        today=today,
        latest_stored=date(2026, 9, 1),
        history_days=365,
        refresh_days=7,
    ) == date(2026, 9, 1)


def test_days_without_data_are_not_relisted_once_synced():
    today = date(2026, 10, 25)
    assert first_day_to_list(
        today=today,
        latest_stored=date(2026, 7, 1),
        history_days=365,
        refresh_days=7,
        synced_through=date(2026, 10, 24),
    ) == date(2026, 10, 18)


def test_a_provider_with_no_data_yet_does_not_rewalk_history():
    today = date(2026, 10, 25)
    assert first_day_to_list(
        today=today,
        latest_stored=None,
        history_days=365,
        refresh_days=7,
        synced_through=date(2026, 10, 24),
    ) == date(2026, 10, 18)


def test_a_cursor_behind_the_newest_stored_day_does_not_rewind():
    today = date(2026, 10, 25)
    assert first_day_to_list(
        today=today,
        latest_stored=date(2026, 10, 1),
        history_days=365,
        refresh_days=7,
        synced_through=date(2026, 9, 1),
    ) == date(2026, 10, 1)


def test_refresh_zero_still_relists_the_newest_stored_day():
    today = date(2026, 10, 25)
    assert first_day_to_list(
        today=today, latest_stored=today, history_days=365, refresh_days=0
    ) == today


def test_forced_sync_walks_the_full_history_again():
    today = date(2026, 10, 25)
    assert first_day_to_list(
        today=today, latest_stored=today, history_days=30, refresh_days=7, force=True
    ) == date(2026, 9, 25)


def test_local_day_window_covers_whole_local_days():
    start, end = local_day_window(date(2026, 7, 1), date(2026, 7, 2), BERLIN)

    assert start == datetime(2026, 6, 30, 22, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 7, 2, 22, 0, tzinfo=timezone.utc)


def test_local_days_round_trips_the_window():
    start, end = local_day_window(date(2026, 7, 1), date(2026, 7, 2), BERLIN)
    assert local_days(start, end, BERLIN) == (date(2026, 7, 1), date(2026, 7, 2))


def test_body_rows_get_their_local_day():
    record = BodyRecord(
        external_id="1",
        measured_at=datetime(2026, 7, 1, 22, 30, tzinfo=timezone.utc),
        weight_kg=72.0,
    )

    assert _body_values(record, "withings", BERLIN)["day"] == date(2026, 7, 2)
    assert _body_values(record, "withings", ZoneInfo("UTC"))["day"] == date(2026, 7, 1)


def test_map_records_skips_unusable_and_broken_payloads():
    class Feed:
        kind = DataKind.BODY

        def map_record(self, payload):
            if payload == "boom":
                raise ValueError("bad payload")
            return payload

    assert map_records(Feed(), "withings", ["a", None, "boom", "b"]) == ["a", "b"]


# ── Withings ────────────────────────────────────────────────────────


WITHINGS_GROUP = {
    "grpid": 5_123_456_789,
    "date": 1_751_408_400,
    "category": 1,
    "deviceid": "abc123",
    "measures": [
        {"value": 72450, "type": 1, "unit": -3},
        {"value": 165, "type": 6, "unit": -1},
        {"value": 5810, "type": 76, "unit": -2},
    ],
}


def test_withings_group_maps_weight_fat_and_muscle():
    record = map_measure_group(WITHINGS_GROUP)

    assert record == BodyRecord(
        external_id="5123456789",
        measured_at=datetime.fromtimestamp(1_751_408_400, tz=timezone.utc),
        weight_kg=72.45,
        fat_pct=16.5,
        muscle_mass_kg=58.1,
        device="abc123",
    )


def test_withings_group_without_weight_is_skipped():
    group = {**WITHINGS_GROUP, "measures": [{"value": 120, "type": 10, "unit": 0}]}
    assert map_measure_group(group) is None


def test_withings_feed_follows_pagination():
    calls: list[int | None] = []
    pages = {
        None: {"measuregrps": [WITHINGS_GROUP], "more": 1, "offset": 1},
        1: {"measuregrps": [{**WITHINGS_GROUP, "grpid": 2}], "more": 0},
    }

    class Client:
        async def get_measures(self, *, startdate, enddate, meastypes, offset=None):
            calls.append(offset)
            assert enddate > startdate
            return pages[offset]

    class Provider:
        client = Client()

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    groups = asyncio.run(
        WithingsBodyFeed(Provider()).list_records(start, start + timedelta(days=1))
    )

    assert calls == [None, 1]
    assert [group["grpid"] for group in groups] == [5_123_456_789, 2]


# ── FDDB ────────────────────────────────────────────────────────────


def test_fddb_day_maps_to_a_nutrition_record():
    record = map_day({"day": date(2026, 3, 1), "kcal": 2100.0, "protein_g": 140})

    assert record == NutritionRecord(day=date(2026, 3, 1), kcal=2100.0, protein_g=140.0)


def test_fddb_day_without_energy_is_skipped():
    assert map_day({"day": date(2026, 3, 1), "kcal": 0}) is None
    assert map_day({"kcal": 2000}) is None


def test_parse_diary_with_data():
    html = (FDDB_FIXTURES / "valid-response.html").read_text(encoding="utf-8")
    data = parse_diary_html(html, date(2024, 8, 27))
    assert data.kcal == 2565
    assert data.fat_g == 110.4
    assert data.carbs_g == 246.2
    assert data.protein_g == 126.4
    assert data.sugar_g == 51
    assert data.fiber_g == 18.3


def test_parse_diary_unauthenticated():
    html = (FDDB_FIXTURES / "unauthenticated.html").read_text(encoding="utf-8")
    with pytest.raises(FddbAuthenticationError):
        parse_diary_html(html, date(2024, 8, 28))


def test_parse_diary_no_data():
    html = (FDDB_FIXTURES / "no-data-available.html").read_text(encoding="utf-8")
    with pytest.raises(FddbNoDataError):
        parse_diary_html(html, date(2024, 8, 28))


def test_parse_diary_redesign_layout():
    """Oct 2026 redesign: combined "kJ (kcal)" column, meal subtotals as rows."""
    html = (FDDB_FIXTURES / "valid-response-redesign.html").read_text(encoding="utf-8")
    data = parse_diary_html(html, date(2026, 10, 22))
    assert data.kcal == 1391
    assert data.fat_g == 97.5
    assert data.carbs_g == 185.25
    assert data.protein_g == 282.75
    assert data.sugar_g == 12.5
    assert data.fiber_g == 23.75
