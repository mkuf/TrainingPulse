"""SQLAlchemy models for Withings weight sync."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class WithingsToken(Base):
    __tablename__ = "withings_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    userid: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WeightMeasurement(Base):
    __tablename__ = "weight_measurements"
    __table_args__ = (UniqueConstraint("grpid", name="uq_weight_grpid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    grpid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    weight_kg: Mapped[float] = mapped_column(Float, nullable=False)
    fat_mass_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    deviceid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
