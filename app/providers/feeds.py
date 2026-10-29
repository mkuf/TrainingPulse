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


#: Stage names in ``SleepStage.stage`` and ``sleep_sessions.stages``. Devices
#: that only tell asleep from awake use ``asleep``.
SLEEP_STAGES = ("awake", "light", "deep", "rem", "asleep")


@dataclass(frozen=True)
class SleepStage:
    """One stretch of a night spent in one stage."""

    start: datetime
    end: datetime
    stage: str


@dataclass(frozen=True)
class SleepRecord:
    """One night's main sleep. Deduplicated per day.

    The baseline every sleep provider can fill: when, how long, and how the
    time split into stages. ``day`` is the provider's own calendar day for the
    night, which is the day the sleep ends (a 22:45–07:00 night belongs to the
    morning). Scores and sleeping heart rate are not provider fields: the core
    derives them the same way for everyone (see :mod:`wellness_scores`).
    """

    external_id: str
    day: date
    start: datetime
    end: datetime
    asleep_seconds: int | None = None
    deep_seconds: int | None = None
    light_seconds: int | None = None
    rem_seconds: int | None = None
    awake_seconds: int | None = None
    #: Daytime naps on ``day``, in total. Not part of ``asleep_seconds``.
    nap_seconds: int | None = None
    #: The night's stage timeline, when the provider has one.
    stages: tuple[SleepStage, ...] | None = None


@dataclass(frozen=True)
class WellnessRecord:
    """One day's recovery values. Deduplicated per day.

    Only measurements most current wearables report, in the same units.
    Night values (HRV, respiration, SpO2, skin temperature) belong to the day
    the night ends.
    """

    day: date
    resting_hr: float | None = None
    #: The night's average RMSSD, in milliseconds.
    hrv_ms: float | None = None
    #: Breaths per minute while asleep.
    respiration: float | None = None
    spo2_avg: float | None = None
    spo2_min: float | None = None
    #: Nightly skin temperature minus the provider's personal baseline, °C.
    skin_temp_deviation_c: float | None = None
    steps: int | None = None
    active_kcal: float | None = None
    total_kcal: float | None = None

    @property
    def external_id(self) -> str:
        return self.day.isoformat()


#: Metric names in ``wellness_samples.metric``.
METRIC_HEART_RATE = "heart_rate"
METRIC_HRV = "hrv"
METRIC_SPO2 = "spo2"
SAMPLE_METRICS = (METRIC_HEART_RATE, METRIC_HRV, METRIC_SPO2)
#: Unit of each metric, for display.
SAMPLE_UNITS = {METRIC_HEART_RATE: "bpm", METRIC_HRV: "ms", METRIC_SPO2: "%"}
#: Heart rate is stored as averages over windows of this many seconds, the
#: resolution Garmin reports natively; finer streams are averaged down.
HEART_RATE_WINDOW_SECONDS = 120


@dataclass(frozen=True)
class SampleRecord:
    """One timestamped value of one metric. Deduplicated per day.

    A provider's samples for a local day are visible together or not at all,
    so a night's curve never mixes two providers.
    """

    metric: str
    measured_at: datetime
    value: float

    @property
    def external_id(self) -> str:
        return f"{self.metric}@{self.measured_at.isoformat()}"


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


class WellnessFeed(RecordFeed[WellnessRecord], Protocol):
    kind: ClassVar[DataKind] = DataKind.WELLNESS


class IntradayFeed(RecordFeed[SampleRecord], Protocol):
    kind: ClassVar[DataKind] = DataKind.INTRADAY


#: Which contract a provider's feed for each kind must satisfy.
FEED_CONTRACTS: dict[DataKind, type[Feed]] = {
    DataKind.ACTIVITIES: ActivityFeed,
    DataKind.BODY: BodyFeed,
    DataKind.NUTRITION: NutritionFeed,
    DataKind.SLEEP: SleepFeed,
    DataKind.WELLNESS: WellnessFeed,
    DataKind.INTRADAY: IntradayFeed,
}
