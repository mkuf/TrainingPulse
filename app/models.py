"""SQLAlchemy models for TrainingPulse."""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class StravaToken(Base):
    """Stores Strava OAuth tokens. Only one row per athlete."""

    __tablename__ = "strava_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    athlete_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
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
    """A single Strava activity."""

    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    athlete_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    device_watts: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    device_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gear_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gear_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    weighted_average_watts: Mapped[float | None] = mapped_column(Float, nullable=True)
    suffer_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    strava_detail_synced: Mapped[bool] = mapped_column(
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
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

    activity_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False
    )
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    stream_types: Mapped[str] = mapped_column(
        String(500), nullable=False, default=""
    )
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
