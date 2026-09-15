"""FDDB.info client — fetch and parse daily nutrition HTML."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from fddb_plugin.options import FddbOptions

logger = logging.getLogger(__name__)

BERLIN = ZoneInfo("Europe/Berlin")
DAY_OFFSET_HOURS = 2
AUTH_LINK_TEXT = {"Anmelden", "Login"}
SUGAR_LABELS = ("davon zucker", "thereof sugar")
FIBER_LABELS = ("ballaststoffe", "dietary fibre", "dietary fiber")


class FddbAuthenticationError(Exception):
    """FDDB session or credentials are invalid."""


class FddbNoDataError(Exception):
    """No diary entries for the requested day."""


class FddbParseError(Exception):
    """Diary page has an unexpected structure (FDDB layout change?)."""


@dataclass(frozen=True)
class DailyNutritionData:
    day: date
    kcal: float
    protein_g: float
    carbs_g: float
    sugar_g: float
    fat_g: float
    fiber_g: float


def _extract_number(text: str) -> float:
    match = re.search(r"-?\d+(?:[.,]\d+)?", text)
    if not match:
        return 0.0
    return float(match.group(0).replace(",", "."))


KCAL_RE = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*kcal", re.IGNORECASE)
KJ_RE = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*kj", re.IGNORECASE)
KJ_PER_KCAL = 4.184


def _extract_kcal(text: str) -> float | None:
    """kcal from "2565 kcal" or "10741 KJ (2565 kcal)"; kJ-only is converted."""
    match = KCAL_RE.search(text)
    if match:
        return float(match.group(1).replace(",", "."))
    match = KJ_RE.search(text)
    if match:
        return float(match.group(1).replace(",", ".")) / KJ_PER_KCAL
    return None


def day_timeframe(day: date) -> tuple[int, int]:
    start = datetime(day.year, day.month, day.day, tzinfo=BERLIN) + timedelta(
        hours=DAY_OFFSET_HOURS
    )
    end = start + timedelta(hours=24) - timedelta(seconds=1)
    return int(start.timestamp()), int(end.timestamp())


def parse_diary_html(html: str, day: date) -> DailyNutritionData:
    soup = BeautifulSoup(html, "html.parser")

    quicklinks = soup.select("div.quicklinks a.v2hdlnk")
    for link in quicklinks:
        if link.get_text(strip=True) in AUTH_LINK_TEXT:
            raise FddbAuthenticationError(
                "FDDB login failed — check FDDB_USER, FDDB_PW, and FDDB_COOKIE"
            )

    rows = soup.select("table.myday-table-std tr")
    if not rows:
        raise FddbNoDataError(f"No diary data for {day.isoformat()}")

    footer = soup.select_one("table.myday-table-std tr:has(td.myday-table-std-td-footer)")
    footer_cells = (footer or rows[-1]).find_all("td")

    # Layouts: old = [name, kJ, kcal, fat, carbs, protein];
    # redesign (Oct 2026) = [name, "kJ (kcal)", fat, carbs, protein].
    # Locate the kcal cell and read the three macro cells that follow it.
    kcal = None
    kcal_idx = -1
    for idx, cell in enumerate(footer_cells[1:], start=1):
        text = cell.get_text(" ", strip=True)
        if KCAL_RE.search(text):
            kcal, kcal_idx = _extract_kcal(text), idx
            break
    if kcal is None and len(footer_cells) > 1:
        # kJ-only energy cell
        kcal = _extract_kcal(footer_cells[1].get_text(" ", strip=True))
        kcal_idx = 1
    if kcal is None or len(footer_cells) < kcal_idx + 4:
        totals = (footer or rows[-1]).get_text(" | ", strip=True)[:200]
        raise FddbParseError(
            f"Unrecognized diary totals row for {day.isoformat()} "
            f"({len(footer_cells)} cells): {totals!r}"
        )
    if kcal <= 0:
        raise FddbNoDataError(f"Empty diary day {day.isoformat()}")

    fat_cell, carbs_cell, protein_cell = footer_cells[kcal_idx + 1 : kcal_idx + 4]

    sugar_g = _find_summary_value(soup, SUGAR_LABELS)
    fiber_g = _find_summary_value(soup, FIBER_LABELS)

    return DailyNutritionData(
        day=day,
        kcal=kcal,
        protein_g=_extract_number(protein_cell.get_text()),
        carbs_g=_extract_number(carbs_cell.get_text()),
        sugar_g=sugar_g,
        fat_g=_extract_number(fat_cell.get_text()),
        fiber_g=fiber_g,
    )


def _find_summary_value(soup: BeautifulSoup, labels: tuple[str, ...]) -> float:
    for row in soup.select("table tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        label = cells[0].get_text(" ", strip=True).lower()
        if any(part in label for part in labels):
            return _extract_number(cells[1].get_text())
    return 0.0


class FddbClient:
    def __init__(self, options: FddbOptions) -> None:
        self.options = options
        self._cookie = options.cookie.strip()
        self._auth_header = self._basic_auth_header(options.user, options.password)

    @staticmethod
    def _basic_auth_header(user: str, password: str) -> str:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        return f"Basic {token}"

    def _headers(self) -> dict[str, str]:
        return {
            "Cookie": f"fddb={self._cookie}",
            "Authorization": self._auth_header,
            "User-Agent": "trainingpulse-fddb/1.0",
        }

    async def fetch_day(self, client: httpx.AsyncClient, day: date) -> DailyNutritionData:
        start_ts, end_ts = day_timeframe(day)
        url = (
            f"{self.options.base_url}/db/i18n/myday20/"
            f"?lang={self.options.lang}&p={start_ts}&q={end_ts}"
        )
        response = await client.get(url, headers=self._headers(), follow_redirects=True)
        response.raise_for_status()
        return parse_diary_html(response.text, day)

    async def fetch_range(
        self,
        start_day: date,
        end_day: date,
        *,
        delay_ms: int | None = None,
    ) -> list[DailyNutritionData]:
        if end_day < start_day:
            raise ValueError("end_day must be on or after start_day")

        pause = (
            delay_ms if delay_ms is not None else self.options.request_delay_ms
        ) / 1000
        results: list[DailyNutritionData] = []
        parse_errors: list[FddbParseError] = []
        day = start_day

        async with httpx.AsyncClient(timeout=30.0) as client:
            while day <= end_day:
                try:
                    results.append(await self.fetch_day(client, day))
                except FddbNoDataError:
                    logger.debug("Skipping empty day %s", day.isoformat())
                except FddbAuthenticationError:
                    raise
                except FddbParseError as exc:
                    logger.warning("Could not parse FDDB diary for %s: %s", day.isoformat(), exc)
                    parse_errors.append(exc)
                except httpx.HTTPError as exc:
                    logger.warning("HTTP error for %s: %s", day.isoformat(), exc)
                day += timedelta(days=1)
                if day <= end_day and pause > 0:
                    await asyncio.sleep(pause)

        if parse_errors and not results:
            raise parse_errors[0]
        return results
