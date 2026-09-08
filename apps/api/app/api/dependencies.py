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

from apps.api.app.agents.fundamental_analyst import FundamentalAnalyst
from apps.api.app.agents.news_analyst import NewsAnalyst
from apps.api.app.agents.technical_analyst import TechnicalAnalyst
from apps.api.app.agents.trader import TraderAgent
from apps.api.app.auth.dependencies import require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.db.base import get_session
from apps.api.app.db.models import Broker, BrokerGrant, User
from apps.api.app.execution.live_broker import LiveBrokerAdapter
from apps.api.app.marketdata.fundamentals_provider import FundamentalsProvider
from apps.api.app.marketdata.history_provider import HistoryProvider
from apps.api.app.marketdata.ingestion.backfill import BarBackfillProvider
from apps.api.app.marketdata.news_provider import NewsProvider
from apps.api.app.marketdata.router import MarketDataRouter


def get_market_data_router(request: Request) -> MarketDataRouter | None:
    """None means no vendor is configured (D008/D015) - callers must
    render that as NOT_CONFIGURED, never silently skip the check."""
    return request.app.state.market_data_router


def get_history_provider(request: Request) -> HistoryProvider | None:
    """None means no historical-price vendor is configured (D021) - like
    get_technical_analyst, this is optional context: its absence means a
    real indicator can't be computed, never a reason to block a trade or
    fabricate a value."""
    return request.app.state.history_provider


def get_trader_agent(request: Request) -> TraderAgent | None:
    """None means no LLM provider is configured (D018) - callers must
    render that as NOT_CONFIGURED, never silently skip the check."""
    return request.app.state.trader_agent


def get_technical_analyst(request: Request) -> TechnicalAnalyst | None:
    """None means no LLM provider is configured (D019) - callers must
    treat this exactly like get_trader_agent's NOT_CONFIGURED convention,
    except this analyst is optional context, not a hard requirement: its
    absence means agent-trades proceeds without technical commentary, it
    never blocks the trader agent's own proposal."""
    return request.app.state.technical_analyst


def get_live_broker_adapter(request: Request) -> LiveBrokerAdapter | None:
    """Phase 43 (D058). None means NO live execution path is available -
    because live trading is disabled (the default), or the mode isn't
    live, or the live credential trio is incomplete. Callers must render
    that as NOT_CONFIGURED and refuse the trade; there is deliberately no
    fallback that would quietly route a live-broker request into the
    paper simulator.

    Built once at startup in apps/api/app/main.py, exactly like the market
    data router, so a request never constructs a broker connection itself.
    """
    return request.app.state.live_broker_adapter


def get_market_data_bar_backfill_provider(request: Request) -> BarBackfillProvider | None:
    """Phase 53 (docs/DECISIONS.md D070). None means no historical-bar
    backfill vendor is configured - the same all-or-nothing Longbridge
    credential gate every other market-data provider in this app uses.
    Optional context, exactly like get_history_provider: its absence means
    POST /admin/market-data/backfill answers NOT_CONFIGURED, never a
    fabricated or partially-faked ingestion."""
    return request.app.state.market_data_bar_backfill_provider


def get_fundamentals_provider(request: Request) -> FundamentalsProvider | None:
    """None means no fundamentals vendor is configured (D059) - like
    get_history_provider, this is optional context: its absence means no
    real fundamentals can be read, never a reason to block a trade or
    fabricate a figure."""
    return request.app.state.fundamentals_provider


def get_news_provider(request: Request) -> NewsProvider | None:
    """None means no news vendor is configured (D059) - same optional,
    never-blocking posture as get_fundamentals_provider."""
    return request.app.state.news_provider


def get_fundamental_analyst(request: Request) -> FundamentalAnalyst | None:
    """None means no LLM provider is configured (D059) - optional
    context, exactly like get_technical_analyst."""
    return request.app.state.fundamental_analyst


def get_news_analyst(request: Request) -> NewsAnalyst | None:
    """None means no LLM provider is configured (D059) - optional
    context, exactly like get_technical_analyst."""
    return request.app.state.news_analyst


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
