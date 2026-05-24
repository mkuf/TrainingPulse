"""SQLAlchemy models for FDDB daily nutrition."""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Integer, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DailyNutrition(Base):
    __tablename__ = "daily_nutrition"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, unique=True, nullable=False, index=True)
    kcal: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    protein_g: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    carbs_g: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sugar_g: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fat_g: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fiber_g: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
