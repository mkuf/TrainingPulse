"""Integration tests: the app against a real PostgreSQL database.

These run the code paths the unit tests fake out — SQLAlchemy and asyncpg
against Postgres, the FastAPI app with its lifespan and scheduler, and the MCP
tools over an in-process client — so a dependency upgrade that breaks any of
them fails here instead of in production.

They run as their own pytest invocation (``python -m pytest integration_tests``)
because ``config`` is loaded once per process: the config written below has to
be in place before any app module is imported, and the unit tests load theirs.
``docker compose run --rm test`` starts the database and runs both suites.
"""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
if not _DATABASE_URL:
    raise pytest.UsageError(
        "TEST_DATABASE_URL is not set. Run the integration tests with "
        "`docker compose run --rm test`, or point it at a disposable "
        "postgresql+asyncpg:// database: every schema in it is dropped."
    )

_CONFIG_DIR = Path(tempfile.mkdtemp(prefix="tp-integration-"))
_CONFIG_FILE = _CONFIG_DIR / "config.yaml"
_CONFIG_FILE.write_text(
    textwrap.dedent(
        """
        athlete:
          max_hr: 190
          rest_hr: 60
          ftp: 250
          timezone: Europe/Berlin
        providers:
          icu:
            enabled: true
            api_key: test-key
          fddb:
            enabled: true
            user: athlete
            password: secret
            cookie: session
            request_delay_ms: 0
        data:
          activities:
            history_days: 30
          nutrition:
            history_days: 3
        """
    ),
    encoding="utf-8",
)
os.environ["TP_CONFIG_FILE"] = str(_CONFIG_FILE)
os.environ["TP_DATABASE__URL"] = _DATABASE_URL

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
# The recorded payloads are shared with the unit tests.
sys.path.insert(0, str(FIXTURES.parent))


async def reset_database() -> None:
    """Drop everything, then create the schema the way the app's startup does."""
    from sqlalchemy import text

    from config import config
    from database import engine
    from schema_migrate import ensure_schema

    async with engine.begin() as conn:
        for schema in ("public", "withings", "fddb"):
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await ensure_schema(conn, timezone=config.athlete.timezone)


@pytest_asyncio.fixture(loop_scope="session")
async def empty_database():
    await reset_database()


# ── Upstream APIs ───────────────────────────────────────────────────

ICU_ACTIVITY_ID = "i87654321"


def icu_activity(start: date) -> dict:
    from fixtures.payloads import ICU_LIST_ACTIVITY

    return {**ICU_LIST_ACTIVITY, "start_date": f"{start.isoformat()}T07:05:00Z"}


def icu_streams(seconds: int = 3600) -> dict:
    """An hour of one-second samples, so the 20-minute power and NP paths run."""
    return {
        "time": list(range(seconds)),
        "heart_rate": [130 + (i // 60) % 30 for i in range(seconds)],
        "watts": [180 + (i // 30) % 60 for i in range(seconds)],
        "cadence": [88] * seconds,
        "velocity_smooth": [9.0] * seconds,
        "distance": [9.0 * i for i in range(seconds)],
        "altitude": [100.0 + (i % 600) / 10 for i in range(seconds)],
        "latlng": [[47.0 + i / 1e5, 11.0 + i / 1e5] for i in range(seconds)],
    }


class FakeUpstream:
    """Answers intervals.icu and FDDB requests from recorded payloads."""

    def __init__(self, activity_start: date):
        self.activity = icu_activity(activity_start)
        self.fddb_html = (FIXTURES / "fddb" / "valid-response.html").read_text(
            encoding="utf-8"
        )
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "intervals.icu":
            if path == "/api/v1/athlete/0":
                return httpx.Response(200, json={"id": "i424242"})
            if path == "/api/v1/athlete/0/activities":
                return httpx.Response(200, json=[self.activity])
            listed = f"/api/v1/activity/{self.activity['id']}"
            if path == listed:
                return httpx.Response(200, json=self.activity)
            if path == f"{listed}/streams.json":
                return httpx.Response(200, json=icu_streams())
        if host == "fddb.info" and path == "/db/i18n/myday20/":
            return httpx.Response(200, text=self.fddb_html)
        return httpx.Response(404, json={"error": f"unexpected {request.url}"})


@pytest.fixture
def upstream(monkeypatch) -> FakeUpstream:
    """Route every ``httpx.AsyncClient`` the providers open to ``FakeUpstream``."""
    fake = FakeUpstream(activity_start=date.today() - timedelta(days=2))
    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return fake
