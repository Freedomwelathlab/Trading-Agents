"""Resource-scoped composition on top of the generic auth dependencies.
Lives here (not in apps/api/app/auth/) because it needs the Broker/
BrokerGrant domain models - the auth module itself stays generic.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.agents.trader import TraderAgent
from apps.api.app.auth.dependencies import require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.db.base import get_session
from apps.api.app.db.models import Broker, BrokerGrant, User
from apps.api.app.marketdata.router import MarketDataRouter


def get_market_data_router(request: Request) -> MarketDataRouter | None:
    """None means no vendor is configured (D008/D015) - callers must
    render that as NOT_CONFIGURED, never silently skip the check."""
    return request.app.state.market_data_router


def get_trader_agent(request: Request) -> TraderAgent | None:
    """None means no LLM provider is configured (D018) - callers must
    render that as NOT_CONFIGURED, never silently skip the check."""
    return request.app.state.trader_agent


@dataclass
class AuthorizedBroker:
    user: User
    broker: Broker


def require_broker_access(
    permission: Permission,
) -> Callable[..., Awaitable[AuthorizedBroker]]:
    """A user must hold `permission` (checked first, via require_permission)
    AND an explicit BrokerGrant row for this specific broker_id. Broker
    existence is checked here too (404) so a route using this dependency
    doesn't need its own separate lookup - and so a nonexistent broker_id
    reliably 404s instead of 403ing just because no grant can exist for a
    row that isn't there.
    """

    async def checker(
        broker_id: uuid.UUID,
        current_user: User = Depends(require_permission(permission)),
        session: AsyncSession = Depends(get_session),
    ) -> AuthorizedBroker:
        broker = (
            await session.execute(select(Broker).where(Broker.id == broker_id))
        ).scalar_one_or_none()
        if broker is None:
            raise HTTPException(status_code=404, detail=f"No broker with id {broker_id}.")

        grant = (
            await session.execute(
                select(BrokerGrant).where(
                    BrokerGrant.user_id == current_user.id,
                    BrokerGrant.broker_id == broker_id,
                )
            )
        ).scalar_one_or_none()
        if grant is None:
            raise HTTPException(
                status_code=403, detail=f"No access grant for broker {broker_id}."
            )

        return AuthorizedBroker(user=current_user, broker=broker)

    return checker
