"""Provider contract: one connectable account.

A provider authenticates once, owns one HTTP client and one rate budget, and
exposes a feed (see :mod:`providers.feeds`) for each data kind the user chose
to import from it. It knows nothing about scheduling, backfill, precedence, or
storage, which the core does identically for every provider.

The activity sources in :mod:`sources` are providers too: each implements this
contract and is its own ``ActivityFeed``. A provider that gains a second kind
returns a separate feed object for it from :meth:`Provider.feeds`.
"""

from __future__ import annotations

from typing import Mapping, Protocol, runtime_checkable

from fastapi import FastAPI
from trainingpulse_common.catalog import AuthMethod, DataKind

from providers.feeds import Feed
from sources.base import RateLimitSnapshot


@runtime_checkable
class Provider(Protocol):
    """One account the user connects."""

    #: Key in ``providers:`` and ``catalog.PROVIDERS``; stored as the
    #: ``provider`` / ``source`` column on every row this provider writes.
    name: str
    auth: AuthMethod
    #: Route that starts authorization, or "" when ``auth`` is not OAUTH.
    connect_path: str
    #: Route that removes stored credentials, or "".
    disconnect_path: str

    def is_configured(self) -> bool:
        """True when the configuration has the credentials it needs."""

    async def is_connected(self) -> bool:
        """True when the provider can make authenticated calls right now."""

    async def granted_kinds(self) -> frozenset[DataKind]:
        """Imported kinds the current credentials can actually read.

        Every provider offers a single kind today, so this is "all of them
        when connected". Once a provider offers several, OAuth providers should
        request scopes for the configured ``imports`` only and compare the
        stored token's granted scopes here, so the status page can ask for a
        reconnect instead of the new feed failing on every sync.
        """

    async def verify_credentials(self) -> int | None:
        """Check credentials; return the provider's numeric user id, if any.

        Raise on an authentication failure. Never used for zones or FTP.
        """

    def register_auth_routes(self, app: FastAPI) -> None:
        """Mount ``connect_path``, its callback, and ``disconnect_path``.

        Callback paths are fixed per provider and stay stable, because users
        have registered them with each provider's developer console.
        """

    def feeds(self) -> Mapping[DataKind, Feed]:
        """One feed per imported kind, each satisfying
        ``feeds.FEED_CONTRACTS[kind]``.
        """

    def rate_limits(self) -> RateLimitSnapshot:
        """Last-seen quota, shared by every feed of this provider."""

    async def close(self) -> None:
        """Release HTTP resources for the provider and all its feeds."""


async def granted_if_connected(provider: Provider) -> frozenset[DataKind]:
    """``granted_kinds`` for a provider whose credentials cover every feed."""
    if not await provider.is_connected():
        return frozenset()
    return frozenset(provider.feeds())
