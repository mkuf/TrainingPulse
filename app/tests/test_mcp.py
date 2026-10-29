"""MCP tool registry and the pure helpers behind the tools.

The modules load ``config`` at import time, so a minimal config with a
non-UTC time zone is written before they are imported.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import textwrap
from datetime import date, datetime, timezone

_CONFIG = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
_CONFIG.write(
    textwrap.dedent(
        """
        athlete:
          max_hr: 190
          rest_hr: 60
          ftp: 250
          timezone: Europe/Berlin
        """
    )
)
_CONFIG.close()
# Collection imports every test module before any test runs, so the variable
# is restored right away to keep it from leaking into the config-loader tests.
_previous = os.environ.get("TP_CONFIG_FILE")
os.environ["TP_CONFIG_FILE"] = _CONFIG.name
try:
    from mcp_server import common, server, wellness  # noqa: E402
finally:
    if _previous is None:
        os.environ.pop("TP_CONFIG_FILE", None)
    else:
        os.environ["TP_CONFIG_FILE"] = _previous
    os.unlink(_CONFIG.name)

from metrics import get_power_zone_boundaries  # noqa: E402

EXPECTED_TOOLS = {
    "list_activities",
    "get_activity",
    "get_activity_streams",
    "get_training_summary",
    "compare_training_periods",
    "get_gear_usage",
    "get_training_load",
    "get_athlete_profile",
    "get_data_coverage",
    "get_body_summary",
    "list_body_measurements",
    "get_latest_body_measurement",
    "compare_body_periods",
    "get_nutrition_summary",
    "list_daily_nutrition",
    "compare_nutrition_periods",
    "get_sleep_summary",
    "list_sleep",
    "get_night",
    "compare_sleep_periods",
    "get_wellness_summary",
    "list_daily_wellness",
    "compare_wellness_periods",
    "get_intraday_samples",
}


def test_registered_tools_match_the_documented_set():
    tools = asyncio.run(server.mcp.list_tools())
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    assert all(tool.description for tool in tools)


def test_local_day_bounds_use_athlete_timezone():
    lower, upper = common.local_day_bounds(date(2026, 7, 1), date(2026, 7, 1))
    # Berlin is UTC+2 in summer: the local day starts at 22:00 UTC the day before.
    assert lower == datetime(2026, 6, 30, 22, 0, tzinfo=timezone.utc)
    assert upper == datetime(2026, 7, 1, 22, 0, tzinfo=timezone.utc)


def test_late_evening_utc_belongs_to_next_local_day():
    instant = datetime(2026, 1, 10, 23, 30, tzinfo=timezone.utc)
    assert common.local_day(instant) == date(2026, 1, 11)
    assert common.local_time(instant) == "2026-01-11T00:30:00+01:00"


def test_open_ended_bounds():
    assert common.local_day_bounds(None, None) == (None, None)


def test_inverted_range_is_rejected():
    try:
        common.check_range(date(2026, 2, 1), date(2026, 1, 1))
    except ValueError as exc:
        assert "start_date" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_period_deltas_are_b_minus_a_and_skip_missing():
    a = {"trimp": 100.0, "avg_power": None, "count": 3}
    b = {"trimp": 130.5, "avg_power": 200.0, "count": 1}
    assert common.period_deltas(a, b, ["trimp", "avg_power", "count"]) == {
        "trimp": 30.5,
        "avg_power": None,
        "count": -2.0,
    }


def test_limit_is_clamped():
    assert common.clamp_limit(None) == common.DEFAULT_LIMIT
    assert common.clamp_limit(0) == 1
    assert common.clamp_limit(10_000) == common.MAX_LIMIT


def test_zones_use_zone_second_keys_and_open_top():
    zones = server._zones(get_power_zone_boundaries(200), "watts")
    assert [z["zone"] for z in zones] == [f"zone_{i}" for i in range(1, 8)]
    assert zones[0] == {"zone": "zone_1", "min_watts": 0, "max_watts": 110}
    assert zones[-1]["max_watts"] is None


def test_downsample_keeps_ends_and_size():
    data = list(range(1000))
    sampled = server._downsample(data, 50)
    assert len(sampled) == 50
    assert sampled[0] == 0 and sampled[-1] == 999
    assert server._downsample(data[:10], 50) == data[:10]


def test_intraday_metric_names_are_checked():
    import pytest

    assert wellness._check_metric("hrv") == "hrv"
    with pytest.raises(ValueError, match="heart_rate"):
        wellness._check_metric("steps")


def test_max_points_is_clamped():
    assert wellness._max_points(None) == wellness.DEFAULT_MAX_POINTS
    assert wellness._max_points(1) == 2
    assert wellness._max_points(10**6) == wellness.MAX_POINTS
