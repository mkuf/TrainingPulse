"""Withings provider: body measurements from Withings scales, readings from
Withings blood pressure monitors, plus sleep, daily wellness, and intraday
samples from Withings watches and sleep trackers (see
:mod:`providers.withings_wellness`).

Withings also exposes workouts; they have no feed yet.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import async_sessionmaker

from catalog import AuthMethod, DataKind
from providers.base import OAuthStateGuard, RateLimitSnapshot
from providers.feeds import BloodPressureRecord, BodyRecord, Feed
from providers.withings_client import (
    KIND_SCOPES,
    SCOPE_METRICS,
    WithingsClient,
    scopes_for,
)
from providers.withings_wellness import (
    WithingsIntradayFeed,
    WithingsSleepFeed,
    WithingsWellnessFeed,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "withings"

CONNECT_PATH = "/auth/withings"
DISCONNECT_PATH = "/auth/withings/disconnect"
#: Earlier releases registered these with Withings' Partner Hub, so they stay.
#: Withings probes the callback with HEAD when it is saved there.
CALLBACK_PATH = "/plugins/withings/get_token"
LEGACY_CALLBACK_PATH = "/plugins/withings/auth/callback"

#: ``getmeas`` measure types.
MEASURE_WEIGHT = 1
MEASURE_FAT_RATIO = 6
MEASURE_DIASTOLIC = 9
MEASURE_SYSTOLIC = 10
MEASURE_PULSE = 11
MEASURE_MUSCLE_MASS = 76
#: Hydration, in kilograms.
MEASURE_HYDRATION = 77
MEASURE_BONE_MASS = 88
#: Visceral fat, a rating without a unit.
MEASURE_VISCERAL_FAT = 170
_BODY_MEASTYPES = ",".join(
    str(t)
    for t in (
        MEASURE_WEIGHT,
        MEASURE_FAT_RATIO,
        MEASURE_MUSCLE_MASS,
        MEASURE_HYDRATION,
        MEASURE_BONE_MASS,
        MEASURE_VISCERAL_FAT,
    )
)
_BLOOD_PRESSURE_MEASTYPES = f"{MEASURE_DIASTOLIC},{MEASURE_SYSTOLIC},{MEASURE_PULSE}"


def _measure_value(measure: dict) -> float:
    """Withings encodes values as ``value * 10**unit``."""
    return float(measure["value"]) * (10.0 ** int(measure["unit"]))


def _group_values(group: dict) -> dict[int, float]:
    values: dict[int, float] = {}
    for measure in group.get("measures") or []:
        try:
            values[int(measure["type"])] = _measure_value(measure)
        except (KeyError, TypeError, ValueError):
            continue
    return values


def _device(group: dict) -> str | None:
    return str(group["deviceid"])[:128] if group.get("deviceid") else None


def map_measure_group(group: dict) -> BodyRecord | None:
    """Map one ``getmeas`` measure group onto a body record.

    Pure. Groups without a weight (e.g. a blood-pressure reading) are skipped.
    Withings reports hydration in kilograms; it is stored as a share of the
    same weigh-in's weight, like Garmin reports it.
    """
    values = _group_values(group)
    weight = values.get(MEASURE_WEIGHT)
    if not weight or group.get("grpid") is None or group.get("date") is None:
        return None
    hydration = values.get(MEASURE_HYDRATION)
    return BodyRecord(
        external_id=str(group["grpid"]),
        measured_at=datetime.fromtimestamp(int(group["date"]), tz=timezone.utc),
        weight_kg=round(weight, 3),
        fat_pct=values.get(MEASURE_FAT_RATIO),
        muscle_mass_kg=values.get(MEASURE_MUSCLE_MASS),
        bone_mass_kg=values.get(MEASURE_BONE_MASS),
        body_water_pct=(
            round(hydration / weight * 100, 2) if hydration is not None else None
        ),
        visceral_fat=values.get(MEASURE_VISCERAL_FAT),
        device=_device(group),
    )


def map_blood_pressure_group(group: dict) -> BloodPressureRecord | None:
    """Map one ``getmeas`` measure group onto a blood pressure reading.

    Pure. Groups without both systolic and diastolic pressure (a weigh-in,
    which may carry a pulse of its own) are skipped.
    """
    values = _group_values(group)
    systolic = values.get(MEASURE_SYSTOLIC)
    diastolic = values.get(MEASURE_DIASTOLIC)
    if (
        not systolic
        or not diastolic
        or group.get("grpid") is None
        or group.get("date") is None
    ):
        return None
    return BloodPressureRecord(
        external_id=str(group["grpid"]),
        measured_at=datetime.fromtimestamp(int(group["date"]), tz=timezone.utc),
        systolic_mmhg=systolic,
        diastolic_mmhg=diastolic,
        pulse_bpm=values.get(MEASURE_PULSE) or None,
        device=_device(group),
    )


async def _list_measure_groups(
    provider: "WithingsSource", start: datetime, end: datetime, meastypes: str
) -> list[dict]:
    """Every measure group in ``[start, end)``, following ``more``/``offset``."""
    client = provider.client
    startdate = int(start.timestamp())
    # getmeas' enddate is inclusive.
    enddate = int(end.timestamp()) - 1
    groups: list[dict] = []
    offset: int | None = None
    while True:
        body = await client.get_measures(
            startdate=startdate, enddate=enddate, meastypes=meastypes, offset=offset
        )
        groups.extend(body.get("measuregrps") or [])
        if not body.get("more") or body.get("offset") in (None, offset):
            break
        offset = int(body["offset"])
    return groups


class WithingsBodyFeed:
    """Withings weigh-ins as a ``BodyFeed``."""

    kind = DataKind.BODY

    def __init__(self, provider: "WithingsSource"):
        self._provider = provider

    async def list_records(self, start: datetime, end: datetime) -> list[dict]:
        return await _list_measure_groups(self._provider, start, end, _BODY_MEASTYPES)

    def map_record(self, payload: dict) -> BodyRecord | None:
        return map_measure_group(payload)


class WithingsBloodPressureFeed:
    """Withings blood pressure monitor readings as a ``BloodPressureFeed``."""

    kind = DataKind.BLOOD_PRESSURE

    def __init__(self, provider: "WithingsSource"):
        self._provider = provider

    async def list_records(self, start: datetime, end: datetime) -> list[dict]:
        return await _list_measure_groups(
            self._provider, start, end, _BLOOD_PRESSURE_MEASTYPES
        )

    def map_record(self, payload: dict) -> BloodPressureRecord | None:
        return map_blood_pressure_group(payload)


class WithingsSource:
    """Withings account, authenticated with OAuth."""

    name = PROVIDER_NAME
    auth = AuthMethod.OAUTH
    connect_path = CONNECT_PATH
    disconnect_path = DISCONNECT_PATH

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker,
        client_id: str,
        client_secret: str,
        app_base_url: str = "",
        redirect_uri: str = "",
        imports: Iterable[DataKind] = (DataKind.BODY,),
        timezone: str = "UTC",
    ):
        self._session_factory = session_factory
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri.strip() or (
            f"{app_base_url.rstrip('/')}{CALLBACK_PATH}"
        )
        self._session = None
        self._client: WithingsClient | None = None
        self._oauth_state = OAuthStateGuard()
        #: Kinds the configuration imports; the connect flow asks only for
        #: their scopes.
        self._imports = tuple(imports)
        #: Used to turn the engine's UTC windows back into local days.
        self.timezone = ZoneInfo(timezone)
        self._feeds: dict[DataKind, Feed] = {
            DataKind.BODY: WithingsBodyFeed(self),
            DataKind.SLEEP: WithingsSleepFeed(self),
            DataKind.WELLNESS: WithingsWellnessFeed(self),
            DataKind.INTRADAY: WithingsIntradayFeed(self),
            DataKind.BLOOD_PRESSURE: WithingsBloodPressureFeed(self),
        }

    @property
    def redirect_uri(self) -> str:
        return self._redirect_uri

    def _build_client(self, session) -> WithingsClient:
        return WithingsClient(
            session,
            client_id=self._client_id,
            client_secret=self._client_secret,
            redirect_uri=self._redirect_uri,
        )

    @property
    def client(self) -> WithingsClient:
        """Long-lived client for sync work, on its own session.

        A token refresh commits, and must not commit unrelated writes with it.
        """
        if self._client is None:
            self._session = self._session_factory()
            self._client = self._build_client(self._session)
        return self._client

    # ── Identity / auth ─────────────────────────────────────────────

    def is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    async def is_connected(self) -> bool:
        if not self.is_configured():
            return False
        async with self._session_factory() as session:
            client = self._build_client(session)
            try:
                return await client.is_connected()
            finally:
                await client.close()

    async def granted_kinds(self) -> frozenset[DataKind]:
        """Kinds whose scopes the stored token holds.

        A token granted before a kind was imported (or before Withings had
        more than body data) lacks its scope until the user reconnects.
        """
        if not self.is_configured():
            return frozenset()
        async with self._session_factory() as session:
            client = self._build_client(session)
            try:
                granted = await client.granted_scopes()
            finally:
                await client.close()
        return frozenset(
            kind for kind in self._feeds if KIND_SCOPES[kind.value] <= granted
        )

    async def verify_credentials(self) -> int | None:
        """Make one cheap authenticated call; return the Withings user id.

        The call needs a scope the token holds: a token for sleep only has no
        ``user.metrics``.
        """
        now = datetime.now(timezone.utc)
        if SCOPE_METRICS in await self.client.granted_scopes():
            await self.client.get_measures(
                startdate=int(now.timestamp()) - 86400,
                enddate=int(now.timestamp()),
                meastypes=str(MEASURE_WEIGHT),
            )
        else:
            today = now.date().isoformat()
            await self.client.get_activity(startdateymd=today, enddateymd=today)
        user_id = await self.client.get_external_user_id()
        try:
            return int(user_id) if user_id is not None else None
        except ValueError:
            return None

    def register_auth_routes(self, app: FastAPI) -> None:
        @app.get(CONNECT_PATH)
        async def withings_connect():
            if not self.is_configured():
                return RedirectResponse(
                    url="/?error=withings_not_configured", status_code=303
                )
            state = self._oauth_state.issue()
            async with self._session_factory() as session:
                client = self._build_client(session)
                url = client.authorization_url(
                    scopes=scopes_for(self._imports), state=state
                )
                await client.close()
            return RedirectResponse(url=url, status_code=307)

        @app.api_route(CALLBACK_PATH, methods=["GET", "HEAD"])
        @app.api_route(LEGACY_CALLBACK_PATH, methods=["GET", "HEAD"])
        async def withings_callback(
            request: Request,
            code: str | None = None,
            state: str | None = None,
            error: str | None = None,
        ):
            if request.method == "HEAD":
                return Response(status_code=200)
            if error or not code:
                return RedirectResponse(
                    url=f"/?error={error or 'missing_code'}", status_code=303
                )
            if not self._oauth_state.verify(state):
                logger.warning("Withings OAuth callback rejected: bad or missing state")
                return RedirectResponse(
                    url="/?error=withings_invalid_state", status_code=303
                )
            async with self._session_factory() as session:
                client = self._build_client(session)
                try:
                    body = await client.exchange_code(code)
                    await client.store_tokens(body)
                except Exception as exc:  # noqa: BLE001 - surfaced to the user
                    logger.error("Withings OAuth exchange failed: %s", exc)
                    return RedirectResponse(
                        url="/?error=withings_oauth_failed", status_code=303
                    )
                finally:
                    await client.close()
            return RedirectResponse(url="/?connected=withings", status_code=303)

        @app.post(DISCONNECT_PATH)
        async def withings_disconnect():
            async with self._session_factory() as session:
                client = self._build_client(session)
                try:
                    await client.disconnect()
                finally:
                    await client.close()
            return RedirectResponse(url="/", status_code=303)

    # ── Feeds and bookkeeping ───────────────────────────────────────

    def feeds(self) -> dict[DataKind, Feed]:
        return dict(self._feeds)

    def rate_limits(self) -> RateLimitSnapshot:
        # 120 requests/minute, but no quota headers to report usage from.
        return RateLimitSnapshot(known=False)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._session is not None:
            await self._session.close()
            self._session = None
