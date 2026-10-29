"""SQLAlchemy models for TrainingPulse."""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    and_,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class OAuthToken(Base):
    """OAuth tokens, one row per provider that needs them."""

    __tablename__ = "oauth_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Optional opaque subject / athlete id from the provider
    external_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: Space-separated scopes the user granted, or NULL for tokens stored
    #: before scopes were recorded.
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AthleteSettings(Base):
    """Stores athlete HR zones and settings."""

    __tablename__ = "athlete_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    athlete_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    max_hr: Mapped[int] = mapped_column(Integer, nullable=False, default=190)
    rest_hr: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    hr_zones: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ftp: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    estimated_ftp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Activity(Base):
    """A single activity from intervals.icu, Google Health, or another provider."""

    __tablename__ = "activities"

    # Prefixed provider ids, e.g. "icu_i55610271" or "ghealth_<datapointId>"
    id: Mapped[str] = mapped_column(String(128), primary_key=True, autoincrement=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    athlete_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    private_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    sport_type: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    start_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    elapsed_time: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    moving_time: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    distance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    average_heartrate: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_heartrate: Mapped[float | None] = mapped_column(Float, nullable=True)
    has_heartrate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    average_watts: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_watts: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_speed: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_elevation_gain: Mapped[float | None] = mapped_column(Float, nullable=True)
    kilojoules: Mapped[float | None] = mapped_column(Float, nullable=True)
    calories: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Computed by TrainingPulse when the streams are processed (see
    #: ``calories.py``); ``calories``/``kilojoules`` keep the imported values.
    #: Read ``COALESCE(computed_calories, calories)``.
    computed_calories: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Mechanical work, only for measured power.
    computed_kilojoules: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Rule behind ``computed_calories``: power, acsm, hr_power or met.
    computed_calories_method: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    device_watts: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    device_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gear_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gear_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    weighted_average_watts: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Strava's "relative effort". Only Strava reports it; NULL elsewhere.
    suffer_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    training_load: Mapped[float | None] = mapped_column(Float, nullable=True)
    detail_synced: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    trimp: Mapped[float | None] = mapped_column(Float, nullable=True)
    hr_zone_seconds: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    power_zone_seconds: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    best_20min_power: Mapped[float | None] = mapped_column(Float, nullable=True)
    power_curve: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    synced_streams: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    provider_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    superseded_by: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    #: Set when the provider answered "gone" (404/410) for this activity. The
    #: row, its streams and its FIT file are kept: a provider may also stop
    #: serving an activity because of a retention limit or a lost scope, not
    #: only because the user deleted it. Cleared if the activity is listed again.
    deleted_upstream_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


#: SQL condition for activities that are shown anywhere: neither a duplicate
#: hidden behind another source's copy nor gone upstream.
ACTIVITY_VISIBLE = and_(
    Activity.superseded_by.is_(None), Activity.deleted_upstream_at.is_(None)
)


class ActivityStream(Base):
    """Raw telemetry streams for an activity (GPS, HR, power, etc.).

    The 'data' column stores a JSONB object keyed by stream type, e.g.:
    {
        "time": [0, 1, 2, ...],
        "latlng": [[lat, lng], ...],
        "heartrate": [120, 121, ...],
        "altitude": [100.0, 100.5, ...],
        "velocity_smooth": [3.2, 3.3, ...],
        "watts": [200, 210, ...],
        "cadence": [80, 82, ...],
        "distance": [0, 3.5, 7.1, ...],
        "grade_smooth": [0.0, 1.2, ...]
    }
    """

    __tablename__ = "activity_streams"

    activity_id: Mapped[str] = mapped_column(
        String(128), primary_key=True, autoincrement=False
    )
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    stream_types: Mapped[str] = mapped_column(
        String(500), nullable=False, default=""
    )
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ActivityFitFile(Base):
    """Raw .fit file bytes, permanently stored once per activity.

    Generic across any FIT-capable source: fetch_detail and fetch_streams both
    read/write this table (via fitfiles.store) so a provider's FIT file is
    only ever downloaded once, and can be re-analyzed later without another
    round-trip to the provider.
    """

    __tablename__ = "activity_fit_files"

    activity_id: Mapped[str] = mapped_column(
        String(128), primary_key=True, autoincrement=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    raw_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DailyMetrics(Base):
    """Daily aggregated training metrics (CTL, ATL, TSB)."""

    __tablename__ = "daily_metrics"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    athlete_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True
    )
    daily_trimp: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ctl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    atl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    tsb: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    activity_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BodyMeasurement(Base):
    """One weigh-in from any provider that supplies body data.

    Every provider's rows are kept. ``superseded_by`` names the provider that
    wins this row's ``day``, or is NULL when the row is visible; the
    ``active_body_measurements`` view holds the visible rows.
    """

    __tablename__ = "body_measurements"
    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_body_provider_external"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    #: Calendar day of ``measured_at`` in ``athlete.timezone``; the unit of
    #: deduplication.
    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    weight_kg: Mapped[float] = mapped_column(Float, nullable=False)
    fat_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    muscle_mass_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    device: Mapped[str | None] = mapped_column(String(128), nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DailyNutrition(Base):
    """One day's intake totals from any provider that supplies nutrition.

    One row per provider and day. ``superseded_by`` works as for
    :class:`BodyMeasurement`; ``active_daily_nutrition`` holds visible rows.
    """

    __tablename__ = "daily_nutrition"
    __table_args__ = (
        UniqueConstraint("provider", "day", name="uq_nutrition_provider_day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    kcal: Mapped[float] = mapped_column(Float, nullable=False)
    protein_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    carbs_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    sugar_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    fat_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    fiber_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SleepSession(Base):
    """One night's main sleep from any provider that supplies sleep.

    One row per provider and wake ``day``. ``superseded_by`` works as for
    :class:`BodyMeasurement`; ``active_sleep_sessions`` holds visible rows.
    Scores and sleeping heart rate are in :class:`DailyScores`.
    """

    __tablename__ = "sleep_sessions"
    __table_args__ = (UniqueConstraint("provider", "day", name="uq_sleep_provider_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: The day the sleep ends, as the provider assigns it.
    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    asleep_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deep_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    light_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rem_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    awake_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    nap_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: ``[{"start": iso, "end": iso, "stage": "deep"}, ...]`` or NULL when the
    #: provider has no stage timeline.
    stages: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DailyWellness(Base):
    """One day's recovery values from any provider that supplies them.

    One row per provider and day. ``superseded_by`` works as for
    :class:`BodyMeasurement`; ``active_daily_wellness`` holds visible rows.
    """

    __tablename__ = "daily_wellness"
    __table_args__ = (
        UniqueConstraint("provider", "day", name="uq_wellness_provider_day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    resting_hr: Mapped[float | None] = mapped_column(Float, nullable=True)
    hrv_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    respiration: Mapped[float | None] = mapped_column(Float, nullable=True)
    spo2_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    spo2_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    skin_temp_deviation_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    steps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active_kcal: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_kcal: Mapped[float | None] = mapped_column(Float, nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DailyScores(Base):
    """Values TrainingPulse derives per day from the visible sleep, wellness,
    and sample rows, identically for every provider.

    Recomputed after every sync (see :mod:`wellness_scores`). Each score
    keeps its parts so it can be explained.
    """

    __tablename__ = "daily_scores"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    #: Mean and lowest of the heart-rate samples while asleep.
    sleep_avg_hr: Mapped[float | None] = mapped_column(Float, nullable=True)
    sleep_min_hr: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Mean heart rate of the day's samples outside the night's sleep.
    awake_avg_hr: Mapped[float | None] = mapped_column(Float, nullable=True)
    sleep_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    sleep_score_parts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    recovery_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    recovery_score_parts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WellnessSample(Base):
    """One timestamped value of one metric (heart rate, HRV, Body Battery, ...).

    ``day`` is the local day of ``measured_at`` and the unit of
    deduplication: a provider's samples for a day are visible together or not
    at all. ``active_wellness_samples`` holds the visible rows.
    """

    __tablename__ = "wellness_samples"
    __table_args__ = (
        UniqueConstraint(
            "provider", "metric", "measured_at", name="uq_sample_provider_metric_time"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    superseded_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RecordSyncCursor(Base):
    """The last local day a provider's feed for one kind was fully listed.

    Lets the next sync start from there even when the provider had nothing to
    store for a while (days without diary entries or weigh-ins), instead of
    re-listing the whole gap since its newest stored record every run.
    """

    __tablename__ = "record_sync_cursors"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    synced_through: Mapped[date] = mapped_column(Date, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
