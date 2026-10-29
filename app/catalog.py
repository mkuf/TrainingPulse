"""What TrainingPulse can import, and from where.

Two independent axes:

- A **data kind** is something TrainingPulse stores and shows: activities,
  body measurements, nutrition, sleep. The core owns each kind's canonical
  table, its duplicate rule, its dashboards, and its MCP tools, so they work
  the same no matter which provider filled them.
- A **provider** is an account the user connects. It authenticates once, owns
  one rate budget, and offers a feed for each kind it can supply.

Which provider supplies which kind is a configuration choice, constrained only
by :data:`PROVIDERS`. Everything here is pure data so both the config loader
and the app can share it without importing FastAPI or SQLAlchemy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DataKind(StrEnum):
    ACTIVITIES = "activities"
    BODY = "body"
    NUTRITION = "nutrition"
    SLEEP = "sleep"
    #: Daily recovery values: resting heart rate, HRV, stress, Body Battery.
    WELLNESS = "wellness"
    #: Timestamped samples through the day and night (heart rate, HRV, ...).
    INTRADAY = "intraday"


class Dedup(StrEnum):
    """How rows of one kind from different providers are reconciled.

    Every provider's rows are stored; the rule only decides which are visible.
    """

    #: Sessions with a start and an end. Two rows compete when their starts are
    #: close *and* their time ranges overlap; the higher-precedence one wins.
    #: This is today's ``engine.choose_winners``.
    OVERLAP = "overlap"
    #: Values that belong to a calendar day in ``athlete.timezone``. For each
    #: day, only the highest-precedence provider with any row that day is
    #: visible. Summing across providers would double-count, so nothing merges.
    PER_DAY = "per_day"


class AuthMethod(StrEnum):
    API_KEY = "api_key"
    OAUTH = "oauth"
    #: Username, password, and session cookie (FDDB scraping).
    SESSION = "session"


@dataclass(frozen=True)
class KindSpec:
    kind: DataKind
    label: str
    dedup: Dedup
    #: How far back the first sync lists.
    default_history_days: int
    #: How far back every later sync re-lists to pick up upstream edits.
    #: 0 disables re-listing.
    default_refresh_days: int


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    label: str
    auth: AuthMethod
    #: Kinds this provider has a working feed for. Config validation rejects
    #: importing anything else, so add a kind here only with its feed.
    kinds: tuple[DataKind, ...]


KINDS: dict[DataKind, KindSpec] = {
    spec.kind: spec
    for spec in (
        KindSpec(DataKind.ACTIVITIES, "Activities", Dedup.OVERLAP, 3650, 90),
        KindSpec(DataKind.BODY, "Body measurements", Dedup.PER_DAY, 3650, 7),
        KindSpec(DataKind.NUTRITION, "Nutrition", Dedup.PER_DAY, 365, 7),
        # One main sleep per night, keyed by the day it ends. Every provider
        # reports sleep that way, so the per-day rule fits and no overlap
        # engine is needed.
        KindSpec(DataKind.SLEEP, "Sleep", Dedup.PER_DAY, 3650, 7),
        KindSpec(DataKind.WELLNESS, "Daily wellness", Dedup.PER_DAY, 3650, 7),
        # Samples cost a request per day and metric, so the default history is
        # short. Today's samples keep growing, hence the one-day refresh.
        KindSpec(DataKind.INTRADAY, "Intraday samples", Dedup.PER_DAY, 30, 1),
    )
}

#: Known providers, in declaration order. This order is also the tie-break
#: for providers that a kind's default precedence below does not mention.
PROVIDERS: dict[str, ProviderSpec] = {
    spec.name: spec
    for spec in (
        ProviderSpec("icu", "intervals.icu", AuthMethod.API_KEY, (DataKind.ACTIVITIES,)),
        ProviderSpec("strava", "Strava", AuthMethod.OAUTH, (DataKind.ACTIVITIES,)),
        ProviderSpec(
            "google_health", "Google Health", AuthMethod.OAUTH, (DataKind.ACTIVITIES,)
        ),
        ProviderSpec("hammerhead", "Hammerhead", AuthMethod.OAUTH, (DataKind.ACTIVITIES,)),
        ProviderSpec(
            "garmin",
            "Garmin Connect",
            AuthMethod.API_KEY,
            (
                DataKind.ACTIVITIES,
                DataKind.BODY,
                DataKind.SLEEP,
                DataKind.WELLNESS,
                DataKind.INTRADAY,
            ),
        ),
        ProviderSpec("withings", "Withings", AuthMethod.OAUTH, (DataKind.BODY,)),
        ProviderSpec("fddb", "FDDB", AuthMethod.SESSION, (DataKind.NUTRITION,)),
    )
}

PROVIDER_NAMES: tuple[str, ...] = tuple(PROVIDERS)

#: Default precedence per kind, highest first. Only lists providers that
#: support the kind; supporting providers left out rank after these.
DEFAULT_PRECEDENCE: dict[DataKind, tuple[str, ...]] = {
    DataKind.ACTIVITIES: ("icu", "strava", "google_health", "hammerhead", "garmin"),
    # A dedicated scale beats a phone app that often just mirrors the scale.
    DataKind.BODY: ("withings", "garmin", "google_health"),
    DataKind.NUTRITION: ("fddb", "google_health"),
    DataKind.SLEEP: ("garmin", "withings", "google_health", "icu"),
    DataKind.WELLNESS: ("garmin", "withings", "google_health", "icu"),
    DataKind.INTRADAY: ("garmin", "withings", "google_health"),
}


def providers_supporting(kind: DataKind) -> list[str]:
    """Provider names that can supply ``kind``, in declaration order."""
    return [name for name, spec in PROVIDERS.items() if kind in spec.kinds]


def default_precedence(kind: DataKind) -> list[str]:
    """Full default precedence for ``kind``: every supporting provider, ranked."""
    supporting = providers_supporting(kind)
    ranked = [name for name in DEFAULT_PRECEDENCE.get(kind, ()) if name in supporting]
    return ranked + [name for name in supporting if name not in ranked]
