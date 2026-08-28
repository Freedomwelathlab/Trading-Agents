"""The only HTTP entrypoints that can move a trade toward the broker. Both
always go through submit_trade_and_record() (apps/api/app/oms/persistence.py),
which itself always goes through the risk engine first - there is no
shortcut from either route to a broker call.

estimated_price is optional on the human-submitted route (D017): if the
caller omits it, a live quote is fetched from the configured market data
vendor and used for both the price and market_data_as_of - never
fabricated, never fetched-then-ignored. If the caller supplies a price,
it is authoritative and no vendor is consulted, exactly as before D017.

The agent-trades route (D018) never accepts a price at all - side,
quantity, and stop distance come from the TraderAgent, and price always
comes from the same live-quote path, never the agent (docs/AGENT_POLICY.md:
no agent output is authoritative for price).

D019 adds an optional TechnicalAnalyst read of that same live quote as
extra context appended to the TraderAgent's prompt - purely informational,
never a separate trusted channel, never required (its absence, or a
failure, silently omits the context rather than blocking the trade).

D021 adds an optional real indicator computation (SMA/RSI, deterministic
code, never the LLM) from a HistoryProvider's daily closes, narrated
(never calculated) by the TechnicalAnalyst when available - same
optional, never-blocking posture as D019.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.agents.technical_analyst import AnalystOutputError, TechnicalAnalyst
from apps.api.app.agents.trader import AgentOutputError, TraderAgent, stop_price_from_distance
from apps.api.app.api.dependencies import (
    AuthorizedBroker,
    get_history_provider,
    get_market_data_router,
    get_technical_analyst,
    get_trader_agent,
    require_broker_access,
)
from apps.api.app.api.schemas import (
    AgentTradeRequest,
    AgentTradeResponse,
    TradeSubmissionRequest,
    TradeSubmissionResponse,
)
from apps.api.app.auth.permissions import Permission
from apps.api.app.core.config import Settings, get_settings
from apps.api.app.core.logging import get_logger
from apps.api.app.db.base import get_session
from apps.api.app.db.models import BrokerKind, OrderStatus
from apps.api.app.execution.persistence import load_paper_broker, save_paper_broker
from apps.api.app.marketdata.history_provider import HistoryProvider
from apps.api.app.marketdata.indicators import InsufficientDataError, rsi, sma
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.router import MarketDataRouter, NoDataAvailableError
from apps.api.app.oms.persistence import submit_trade_and_record
from apps.api.app.risk.models import RiskLimits, TradeProposal

router = APIRouter(prefix="/brokers/{broker_id}/trades", tags=["trades"])
agent_router = APIRouter(prefix="/brokers/{broker_id}/agent-trades", tags=["trades", "agents"])
logger = get_logger(__name__)


async def _resolve_live_quote(
    symbol: str, market_data_router: MarketDataRouter | None, *, not_configured_hint: str
) -> tuple[Decimal, datetime]:
    if market_data_router is None:
        raise HTTPException(status_code=400, detail=not_configured_hint)
    try:
        snapshot = await market_data_router.get_snapshot(symbol)
    except NoDataAvailableError as exc:
        # Message is already NO_DATA_AVAILABLE:-prefixed by the router.
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return snapshot.price, snapshot.as_of


def _require_paper_broker(authorized: AuthorizedBroker) -> None:
    if authorized.broker.kind is not BrokerKind.PAPER:
        raise HTTPException(
            status_code=400,
            detail=(
                "NOT_CONFIGURED: this endpoint only supports paper brokers. "
                "Live trading has no implemented execution path (spec §3/§46)."
            ),
        )


async def _execute_trade(
    *,
    session: AsyncSession,
    settings: Settings,
    broker_id: uuid.UUID,
    proposal: TradeProposal,
    marks: dict[str, Decimal],
    submitted_by_user_id: uuid.UUID,
) -> TradeSubmissionResponse:
    # Locks the broker's account row for the rest of this transaction -
    # see apps/api/app/execution/persistence.py's module docstring for why.
    broker_adapter = await load_paper_broker(
        session, broker_id, default_starting_cash=settings.paper_broker_starting_cash
    )
    try:
        account = broker_adapter.get_account_state(marks=marks)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"DATA_UNAVAILABLE: {exc}") from None

    limits = RiskLimits(
        max_position_pct_of_equity=settings.risk_max_position_pct_of_equity,
        max_portfolio_exposure_pct_of_equity=settings.risk_max_portfolio_exposure_pct_of_equity,
        max_risk_pct_of_equity_per_trade=settings.risk_max_risk_pct_of_equity_per_trade,
        require_stop_price=settings.risk_require_stop_price,
        max_market_data_age_seconds=settings.risk_max_market_data_age_seconds,
    )

    result = await submit_trade_and_record(
        session,
        broker_id,
        proposal,
        account,
        limits,
        broker_adapter,
        emergency_stop_active=settings.emergency_stop_active,
        submitted_by_user_id=submitted_by_user_id,
    )

    await save_paper_broker(session, broker_id, broker_adapter)
    await session.commit()

    assert result.order_id is not None  # always set by submit_trade_and_record
    return TradeSubmissionResponse(
        order_id=result.order_id,
        status=OrderStatus(result.status.value),
        approved=result.risk_decision.approved,
        block_reason=result.risk_decision.reason,
        detail=result.risk_decision.detail,
        fill_quantity=result.fill.quantity if result.fill else None,
        fill_price=result.fill.fill_price if result.fill else None,
    )


@router.post("", response_model=TradeSubmissionResponse)
async def submit_trade_endpoint(
    broker_id: uuid.UUID,
    request: TradeSubmissionRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    authorized: AuthorizedBroker = Depends(require_broker_access(Permission.SUBMIT_PAPER_TRADE)),
    market_data_router: MarketDataRouter | None = Depends(get_market_data_router),
) -> TradeSubmissionResponse:
    _require_paper_broker(authorized)

    if request.estimated_price is not None:
        estimated_price = request.estimated_price
        market_data_as_of = request.market_data_as_of or datetime.now(UTC)
    else:
        estimated_price, market_data_as_of = await _resolve_live_quote(
            request.symbol,
            market_data_router,
            not_configured_hint=(
                "NOT_CONFIGURED: estimated_price was omitted and no market "
                "data vendor is wired (see docs/DECISIONS.md D008/D017)."
            ),
        )

    proposal = TradeProposal(
        symbol=request.symbol,
        side=request.side,
        quantity=request.quantity,
        estimated_price=estimated_price,
        stop_price=request.stop_price,
        market_data_as_of=market_data_as_of,
    )

    return await _execute_trade(
        session=session,
        settings=settings,
        broker_id=broker_id,
        proposal=proposal,
        marks={**request.marks, request.symbol: estimated_price},
        submitted_by_user_id=authorized.user.id,
    )


@agent_router.post("", response_model=AgentTradeResponse)
async def submit_agent_trade_endpoint(
    broker_id: uuid.UUID,
    request: AgentTradeRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    authorized: AuthorizedBroker = Depends(require_broker_access(Permission.SUBMIT_PAPER_TRADE)),
    market_data_router: MarketDataRouter | None = Depends(get_market_data_router),
    trader_agent: TraderAgent | None = Depends(get_trader_agent),
    technical_analyst: TechnicalAnalyst | None = Depends(get_technical_analyst),
    history_provider: HistoryProvider | None = Depends(get_history_provider),
) -> AgentTradeResponse:
    _require_paper_broker(authorized)

    if trader_agent is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "NOT_CONFIGURED: no LLM provider is wired "
                "(see docs/DECISIONS.md D018)."
            ),
        )

    # Price is always deterministic (D017), never any agent's - fetched
    # up front so both the (optional) TechnicalAnalyst and the
    # TraderAgent's price-free proposal share the exact same quote.
    estimated_price, market_data_as_of = await _resolve_live_quote(
        request.symbol,
        market_data_router,
        not_configured_hint=(
            "NOT_CONFIGURED: no market data vendor is wired "
            "(see docs/DECISIONS.md D008/D017)."
        ),
    )

    indicator_context: str | None = None
    if history_provider is not None:
        # D021: real, deterministically-computed indicators (never
        # asked of an LLM) - optional, like everything else here. A
        # short history, a vendor failure, or no configured provider all
        # just mean no indicator context this call, never a blocked trade
        # or a guessed value.
        try:
            closes = await history_provider.get_daily_closes(request.symbol, count=30)
            indicator_lines = []
            try:
                indicator_lines.append(f"SMA(20)={sma(closes, 20)}")
            except InsufficientDataError:
                pass
            try:
                indicator_lines.append(f"RSI(14)={rsi(closes, 14)}")
            except InsufficientDataError:
                pass
            if indicator_lines:
                indicator_context = ", ".join(indicator_lines)
        except (DataUnavailableError, VendorError) as exc:
            logger.warning("history_provider_unavailable", symbol=request.symbol, error=str(exc))

    technical_context: str | None = None
    if technical_analyst is not None:
        # D019/D021: optional additional context only - a failed or
        # unavailable technical read must never block the trade, since
        # this analyst is informational and TraderAgent already treats
        # a missing directive-adjacent context as normal. Never
        # fabricated: on failure the context is simply omitted, not
        # guessed.
        try:
            read = await technical_analyst.analyze(
                symbol=request.symbol,
                price=estimated_price,
                as_of=market_data_as_of.isoformat(),
                indicator_context=indicator_context,
            )
        except AnalystOutputError as exc:
            logger.warning("technical_analyst_unavailable", symbol=request.symbol, error=str(exc))
        else:
            technical_context = (
                f"stance={read.stance.value}, confidence={read.confidence}: {read.summary}"
            )

    try:
        idea = await trader_agent.propose(
            symbol=request.symbol,
            directive=request.directive,
            technical_context=technical_context,
        )
    except AgentOutputError as exc:
        raise HTTPException(status_code=502, detail=f"AGENT_OUTPUT_INVALID: {exc}") from None

    stop_price = stop_price_from_distance(
        price=estimated_price, side=idea.side, stop_distance_pct=idea.stop_distance_pct
    )

    proposal = TradeProposal(
        symbol=request.symbol,
        side=idea.side,
        quantity=idea.quantity,
        estimated_price=estimated_price,
        stop_price=stop_price,
        market_data_as_of=market_data_as_of,
    )

    result = await _execute_trade(
        session=session,
        settings=settings,
        broker_id=broker_id,
        proposal=proposal,
        marks={**request.marks, request.symbol: estimated_price},
        submitted_by_user_id=authorized.user.id,
    )

    return AgentTradeResponse(
        **result.model_dump(), side=idea.side, quantity=idea.quantity, rationale=idea.rationale
    )
