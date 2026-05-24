"""Withings API client: OAuth token exchange and measure sync."""

import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from withings_plugin.config import settings
from withings_plugin.models import WithingsToken

logger = logging.getLogger(__name__)

MEASURE_TYPE_WEIGHT = 1
MEASURE_TYPE_FAT_RATIO = 6


def measure_value_to_float(value: int, unit: int) -> float:
    return float(value) * (10.0**unit)


class WithingsClient:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _get_token_row(self) -> WithingsToken | None:
        result = await self.session.execute(select(WithingsToken).limit(1))
        return result.scalar_one_or_none()

    async def exchange_code(self, code: str) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                settings.WITHINGS_TOKEN_URL,
                data={
                    "action": "requesttoken",
                    "grant_type": "authorization_code",
                    "client_id": settings.WITHINGS_CLIENT_ID,
                    "client_secret": settings.WITHINGS_CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": settings.oauth_callback_url,
                },
            )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != 0:
            raise RuntimeError(payload.get("error", "Token exchange failed"))
        return payload["body"]

    async def _refresh_access_token(self, token: WithingsToken) -> WithingsToken:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                settings.WITHINGS_TOKEN_URL,
                data={
                    "action": "requesttoken",
                    "grant_type": "refresh_token",
                    "client_id": settings.WITHINGS_CLIENT_ID,
                    "client_secret": settings.WITHINGS_CLIENT_SECRET,
                    "refresh_token": token.refresh_token,
                },
            )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != 0:
            raise RuntimeError(payload.get("error", "Token refresh failed"))
        body = payload["body"]
        token.access_token = body["access_token"]
        token.refresh_token = body.get("refresh_token", token.refresh_token)
        token.expires_at = int(body["expires_in"]) + int(time.time())
        await self.session.commit()
        return token

    async def get_valid_access_token(self) -> str:
        token = await self._get_token_row()
        if token is None:
            raise RuntimeError("Not connected to Withings")
        now = int(time.time())
        if token.expires_at <= now + 60:
            token = await self._refresh_access_token(token)
        return token.access_token

    async def store_tokens(self, body: dict[str, Any]) -> WithingsToken:
        userid = int(body["userid"])
        expires_at = int(body["expires_in"]) + int(time.time())
        values = {
            "userid": userid,
            "access_token": body["access_token"],
            "refresh_token": body["refresh_token"],
            "expires_at": expires_at,
        }
        stmt = pg_insert(WithingsToken).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["userid"],
            set_={
                "access_token": stmt.excluded.access_token,
                "refresh_token": stmt.excluded.refresh_token,
                "expires_at": stmt.excluded.expires_at,
            },
        )
        await self.session.execute(stmt)
        await self.session.commit()
        result = await self.session.execute(
            select(WithingsToken).where(WithingsToken.userid == userid)
        )
        return result.scalar_one()

    async def get_measurements(
        self,
        *,
        startdate: int,
        enddate: int,
        meastype: int = MEASURE_TYPE_WEIGHT,
    ) -> dict[str, Any]:
        access_token = await self.get_valid_access_token()
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{settings.WITHINGS_API_BASE}/measure",
                data={
                    "action": "getmeas",
                    "meastype": meastype,
                    "startdate": startdate,
                    "enddate": enddate,
                },
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if response.status_code == 401:
            token = await self._get_token_row()
            if token is None:
                response.raise_for_status()
            await self._refresh_access_token(token)
            return await self.get_measurements(
                startdate=startdate, enddate=enddate, meastype=meastype
            )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != 0:
            raise RuntimeError(payload.get("error", "getmeas failed"))
        return payload["body"]


def parse_measure_groups(body: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group in body.get("measuregrps", []):
        weight_kg: float | None = None
        fat_mass_pct: float | None = None
        for measure in group.get("measures", []):
            mtype = measure.get("type")
            value = measure_value_to_float(measure["value"], measure["unit"])
            if mtype == MEASURE_TYPE_WEIGHT:
                weight_kg = value
            elif mtype == MEASURE_TYPE_FAT_RATIO:
                fat_mass_pct = value
        if weight_kg is None:
            continue
        measured_at = datetime.fromtimestamp(group["date"], tz=timezone.utc)
        rows.append(
            {
                "grpid": int(group["grpid"]),
                "measured_at": measured_at,
                "weight_kg": weight_kg,
                "fat_mass_pct": fat_mass_pct,
                "deviceid": group.get("deviceid"),
            }
        )
    return rows
