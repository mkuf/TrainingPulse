"""Feed contracts: one per data kind a provider can supply.

A feed knows how to list one kind of data for a date window and map a provider
payload onto that kind's canonical record. Authentication, rate limits, and HTTP
clients belong to the provider that owns the feed, so every feed of one
provider shares a single budget and a single token.

There are two shapes:

- :class:`ActivityFeed` is today's ``ActivitySource`` minus the provider-level
  members. Activities are the only kind with a detail call and time-series
  streams, so they keep their own richer contract and the existing engine.
- :class:`RecordFeed` covers every other kind. A record is complete in the
  list payload; the engine lists, maps, upserts, and deduplicates, and there
  is no enrichment pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import ClassVar, Protocol, TypeVar, runtime_checkable

from catalog import DataKind
from models import Activity

# ── Canonical records ───────────────────────────────────────────────
#
# What a RecordFeed's mapper returns. Each becomes a row in that kind's core
# table (``models.BodyMeasurement``, ``models.DailyNutrition``), keyed by
# provider plus external id or day; the engine adds ``provider``, so mappers
# never need to know their own name.


@dataclass(frozen=True)
class BodyRecord:
    """One weigh-in. Deduplicated per day in ``athlete.timezone``."""

    external_id: str
    measured_at: datetime
    weight_kg: float
    fat_pct: float | None = None
    muscle_mass_kg: float | None = None
    device: str | None = None


@dataclass(frozen=True)
class NutritionRecord:
    """One day's intake totals. Deduplicated per day.

    ``day`` is the provider's own calendar day, not derived from a timestamp:
    a food diary already records which day an entry belongs to.
    """

    day: date
    kcal: float
    protein_g: float | None = None
    carbs_g: float | None = None
    sugar_g: float | None = None
    fat_g: float | None = None
    fiber_g: float | None = None

    @property
    def external_id(self) -> str:
        return self.day.isoformat()


@dataclass(frozen=True)
class SleepRecord:
    """One sleep period. Deduplicated by overlap, like activities.

    No provider has a sleep feed and there is no sleep table yet; the record
    is defined so the first sleep feed has a contract to implement.
    """

    external_id: str
    start: datetime
    end: datetime
    asleep_seconds: int | None = None
    deep_seconds: int | None = None
    light_seconds: int | None = None
    rem_seconds: int | None = None
    awake_seconds: int | None = None
    #: Provider's own 0–100 score. Not comparable across providers.
    score: float | None = None


R_co = TypeVar("R_co", covariant=True)


# ── Contracts ───────────────────────────────────────────────────────


@runtime_checkable
class Feed(Protocol):
    """Anything a provider exposes for one data kind."""

    kind: ClassVar[DataKind]


@runtime_checkable
class ActivityFeed(Feed, Protocol):
    """Activities: list, then per-activity detail and streams.

    The members match today's ``providers.base.ActivitySource`` one to one, so
    the existing engine, mappers, and tests carry over unchanged. Only
    ``name``, auth, ``rate_limits``, and ``close`` move up to the provider.
    """

    id_prefix: str
    supports_detail: bool
    supports_streams: bool
    supports_reconcile: bool

    async def list_activities(self, start: datetime, end: datetime) -> list[dict]:
        """Provider payloads for activities in ``[start, end)``."""

    async def fetch_detail(self, external_id: str) -> dict | None:
        """Detail payload; ``None`` means gone upstream. Raise to retry."""

    async def fetch_streams(self, activity: Activity) -> dict[str, list]:
        """Canonical streams for one stored activity."""

    def map_row(self, payload: dict, athlete_id: int) -> dict | None:
        """Pure: provider payload to ``activities`` column values."""


@runtime_checkable
class RecordFeed(Feed, Protocol[R_co]):
    """Every non-activity kind: list a window, map each payload, done."""

    async def list_records(self, start: datetime, end: datetime) -> list[dict]:
        """Provider payloads for ``[start, end)``.

        For per-day kinds the window is in UTC but covers whole days in
        ``athlete.timezone``, so a feed whose API takes local dates can
        convert without losing an edge day.
        """

    def map_record(self, payload: dict) -> R_co | None:
        """Pure: provider payload to a canonical record, or None to skip."""


class BodyFeed(RecordFeed[BodyRecord], Protocol):
    kind: ClassVar[DataKind] = DataKind.BODY


class NutritionFeed(RecordFeed[NutritionRecord], Protocol):
    kind: ClassVar[DataKind] = DataKind.NUTRITION


class SleepFeed(RecordFeed[SleepRecord], Protocol):
    kind: ClassVar[DataKind] = DataKind.SLEEP


#: Which contract a provider's feed for each kind must satisfy.
FEED_CONTRACTS: dict[DataKind, type[Feed]] = {
    DataKind.ACTIVITIES: ActivityFeed,
    DataKind.BODY: BodyFeed,
    DataKind.NUTRITION: NutritionFeed,
    DataKind.SLEEP: SleepFeed,
}
