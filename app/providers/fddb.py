"""FDDB provider: daily nutrition totals scraped from fddb.info."""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI

from catalog import AuthMethod, DataKind
from fddb_client import FddbClient, FddbNoDataError
from providers.base import RateLimitSnapshot, granted_if_connected
from providers.feeds import Feed, NutritionRecord
from providers.mapping import as_float

logger = logging.getLogger(__name__)

PROVIDER_NAME = "fddb"


def map_day(payload: dict) -> NutritionRecord | None:
    """Map one scraped diary day onto a nutrition record. Pure."""
    raw_day = payload.get("day")
    if isinstance(raw_day, datetime):
        day = raw_day.date()
    elif isinstance(raw_day, date):
        day = raw_day
    elif raw_day:
        day = date.fromisoformat(str(raw_day)[:10])
    else:
        return None
    kcal = as_float(payload.get("kcal"))
    if kcal is None or kcal <= 0:
        return None
    return NutritionRecord(
        day=day,
        kcal=kcal,
        protein_g=as_float(payload.get("protein_g")),
        carbs_g=as_float(payload.get("carbs_g")),
        sugar_g=as_float(payload.get("sugar_g")),
        fat_g=as_float(payload.get("fat_g")),
        fiber_g=as_float(payload.get("fiber_g")),
    )


def local_days(start: datetime, end: datetime, tz: ZoneInfo) -> tuple[date, date]:
    """Inclusive local days covered by the half-open UTC range ``[start, end)``."""
    first = start.astimezone(tz).date()
    last = (end - timedelta(microseconds=1)).astimezone(tz).date()
    return first, last


class FddbNutritionFeed:
    """FDDB diary days as a ``NutritionFeed``."""

    kind = DataKind.NUTRITION

    def __init__(self, provider: "FddbSource"):
        self._provider = provider

    async def list_records(self, start: datetime, end: datetime) -> list[dict]:
        """One payload per diary day with entries. One request per day."""
        first, last = local_days(start, end, self._provider.timezone)
        if last < first:
            return []
        days = await self._provider.client.fetch_range(first, last)
        return [asdict(day) for day in days]

    def map_record(self, payload: dict) -> NutritionRecord | None:
        return map_day(payload)


class FddbSource:
    """FDDB account, authenticated with username, password, and session cookie."""

    name = PROVIDER_NAME
    auth = AuthMethod.SESSION
    connect_path = ""
    disconnect_path = ""

    def __init__(
        self,
        *,
        user: str,
        password: str,
        cookie: str,
        base_url: str = "https://fddb.info",
        lang: str = "de",
        request_delay_ms: int = 300,
        timezone: str = "UTC",
    ):
        self._user = user.strip()
        self._password = password.strip()
        self._cookie = cookie.strip()
        self.timezone = ZoneInfo(timezone)
        self.client = FddbClient(
            user=self._user,
            password=self._password,
            cookie=self._cookie,
            base_url=base_url,
            lang=lang,
            request_delay_ms=request_delay_ms,
        )
        self._feeds: dict[DataKind, Feed] = {
            DataKind.NUTRITION: FddbNutritionFeed(self)
        }

    def is_configured(self) -> bool:
        return bool(self._user and self._password and self._cookie)

    async def is_connected(self) -> bool:
        # Nothing to authorize: a bad cookie surfaces from the first scrape.
        return self.is_configured()

    async def granted_kinds(self) -> frozenset[DataKind]:
        return await granted_if_connected(self)

    async def verify_credentials(self) -> int | None:
        """Scrape today's diary; raises ``FddbAuthenticationError`` if rejected."""
        today = datetime.now(self.timezone).date()
        async with httpx.AsyncClient(timeout=30.0) as http:
            try:
                await self.client.fetch_day(http, today)
            except FddbNoDataError:
                pass
        return None

    def register_auth_routes(self, app: FastAPI) -> None:
        """No routes: credentials come from configuration."""

    def feeds(self) -> dict[DataKind, Feed]:
        return dict(self._feeds)

    def rate_limits(self) -> RateLimitSnapshot:
        # A scraper: the only throttle is our own request_delay_ms.
        return RateLimitSnapshot(known=False)

    async def close(self) -> None:
        """Each scrape opens and closes its own HTTP client."""
