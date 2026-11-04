"""The status page and every MCP tool, against the demo data set."""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from conftest import reset_database
from mcp import Client

from mcp_server import server
from sync import sync_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

TODAY = date.today()
WEEK_AGO = (TODAY - timedelta(days=7)).isoformat()
MONTH_AGO = (TODAY - timedelta(days=30)).isoformat()
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def seeded():
    import seed_demo_data

    await reset_database()
    await seed_demo_data._seed(argparse.Namespace(days=60, seed=7, force=False))


async def _call(client: Client, tool: str, **arguments) -> dict:
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, f"{tool}: {result.content}"
    assert result.structured_content is not None, tool
    return result.structured_content


async def test_every_mcp_tool_answers(seeded):
    period = {"start_date": MONTH_AGO, "end_date": YESTERDAY}
    periods = {
        "period_a_start": MONTH_AGO,
        "period_a_end": WEEK_AGO,
        "period_b_start": WEEK_AGO,
        "period_b_end": YESTERDAY,
    }

    async with Client(server.mcp) as client:
        listed = await _call(client, "list_activities", **period, limit=5)
        assert listed["activities"], listed
        activity_id = listed["activities"][0]["id"]

        sleep = await _call(client, "list_sleep", **period, limit=1)
        night = sleep["nights"][0]["day"]

        calls = {
            "list_activities": {**period, "limit": 5},
            "get_activity": {"activity_id": activity_id},
            "get_activity_streams": {"activity_id": activity_id, "max_points": 50},
            "get_training_summary": period,
            "compare_training_periods": periods,
            "get_gear_usage": period,
            "get_training_load": period,
            "get_athlete_profile": {},
            "get_data_coverage": {},
            "get_body_summary": period,
            "list_body_measurements": {**period, "limit": 5},
            "get_latest_body_measurement": {},
            "compare_body_periods": periods,
            "get_blood_pressure_summary": period,
            "list_blood_pressure_readings": {**period, "limit": 5},
            "get_nutrition_summary": period,
            "list_daily_nutrition": {**period, "limit": 5},
            "compare_nutrition_periods": periods,
            "get_sleep_summary": period,
            "list_sleep": {**period, "limit": 5},
            "get_night": {"day": night, "max_points": 50},
            "compare_sleep_periods": periods,
            "get_wellness_summary": period,
            "list_daily_wellness": {**period, "limit": 5},
            "compare_wellness_periods": periods,
            "get_intraday_samples": {
                "metric": "heart_rate",
                "start_date": WEEK_AGO,
                "end_date": YESTERDAY,
                "max_points": 50,
            },
        }
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == set(calls)
        for tool, arguments in calls.items():
            await _call(client, tool, **arguments)

        pressure = await _call(client, "list_blood_pressure_readings", **period, limit=5)
        assert pressure["readings"], pressure
        body = await _call(client, "get_body_summary", **period)
        assert body["avg_body_water_pct"] is not None, body


async def test_mcp_tool_errors_reach_the_client(seeded):
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "get_training_summary", {"start_date": YESTERDAY, "end_date": MONTH_AGO}
        )
    assert result.is_error


@pytest.fixture
def no_scheduled_sync(monkeypatch):
    """Keep the lifespan's first sync from reaching the providers' real APIs."""
    import main

    ran = asyncio.Event()

    async def trigger_sync_task(force_resync: bool = False):
        ran.set()

    monkeypatch.setattr(main, "trigger_sync_task", trigger_sync_task)
    return ran


async def test_status_page_and_api(seeded, no_scheduled_sync):
    import main

    async with main.app.router.lifespan_context(main.app):
        await asyncio.wait_for(no_scheduled_sync.wait(), timeout=10)
        assert main.scheduler.get_job(main.SYNC_JOB_ID) is not None

        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            home = await client.get("/")
            assert home.status_code == 200, home.text
            assert "intervals.icu" in home.text
            assert f"/dashboards?tag={main.GRAFANA_TAG}" in home.text
            for uid, _title in main.GRAFANA_DASHBOARDS:
                assert f"/d/{uid}" in home.text
            assert 'id="today-title"' in home.text
            assert "kcal / day" in home.text

            live = await client.get("/partials/live")
            assert live.status_code == 200, live.text

            status = await client.get("/sync/status")
            assert status.status_code == 200
            assert status.json()["activities_by_source"]

            css = await client.get("/static/css/app.css")
            assert css.status_code == 200
            assert css.headers["content-type"].startswith("text/css")

            trigger = await client.post("/sync/trigger")
            assert trigger.status_code == 303
            assert not sync_state.is_running


async def test_status_page_links_match_the_bundled_dashboards():
    import json

    import main

    dashboards_dir = Path(__file__).resolve().parents[2] / "grafana" / "dashboards"
    bundled = {}
    for path in dashboards_dir.glob("*.json"):
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        assert main.GRAFANA_TAG in dashboard["tags"], path.name
        bundled[dashboard["uid"]] = dashboard["title"]
    assert bundled
    assert {uid for uid, _ in main.GRAFANA_DASHBOARDS} == set(bundled)
    for uid, title in main.GRAFANA_DASHBOARDS:
        assert bundled[uid].endswith(title), (uid, bundled[uid])


async def test_today_view_reads_the_seeded_days(seeded):
    from zoneinfo import ZoneInfo

    from config import config
    from database import async_session
    from today import today_view

    async with async_session() as session:
        view = await today_view(session, ZoneInfo(config.athlete.timezone))

    assert view.sleep is not None and view.sleep.score is not None
    assert view.recovery is not None
    assert view.form is not None and view.form.day == view.today
    assert view.nutrition is not None
    assert view.nutrition.through < view.today
    assert view.nutrition.kcal and view.nutrition.days_logged >= 1


async def test_today_view_shows_what_is_logged_today(seeded):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from config import config
    from database import async_session
    from models import DailyNutrition
    from sqlalchemy import delete
    from today import today_view

    tz = ZoneInfo(config.athlete.timezone)
    day = datetime.now(tz).date()
    async with async_session() as session:
        await session.execute(
            delete(DailyNutrition).where(DailyNutrition.day == day)
        )
        session.add(
            DailyNutrition(
                provider="fddb", day=day, kcal=1234.4, protein_g=88.6, fat_g=40.0
            )
        )
        await session.commit()

        view = await today_view(session, tz)

        await session.execute(
            delete(DailyNutrition).where(DailyNutrition.day == day)
        )
        await session.commit()

    assert view.nutrition_today is not None
    assert view.nutrition_today.kcal == 1234
    assert view.nutrition_today.protein_g == 89
    assert view.nutrition_today.carbs_g is None
    # Today is not part of the weekly average.
    assert view.nutrition is None or view.nutrition.through < day
