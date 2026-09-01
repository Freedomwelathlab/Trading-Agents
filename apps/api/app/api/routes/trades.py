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

D059 adds two more optional analyst reads on the same footing - a
FundamentalAnalyst over real vendor fundamentals and a NewsAnalyst over
real recent headlines, both from the already-credentialed Longbridge
relationship. All three analysts now run concurrently (they have no
cross-dependency, per docs/AGENT_POLICY.md) and each is independently
failure-isolated: an absent analyst, an absent vendor, an unsupported
symbol, or a failed call simply omits that one paragraph of context. None
of them can block a trade, and none of them can supply a price, quantity,
or side. SentimentAnalyst is explicitly out of scope - see D059 for why.

D024 adds deterministic duplicate-order detection: `_execute_trade()`
(shared by both routes below, so a human-submitted and an LLM-originated
trade get identical protection) queries this broker+symbol's recent
FILLED orders and hands them to the risk engine as data - never a check
the engine performs its own I/O for. See docs/DECISIONS.md D024 for the
duplicate definition and window.

D029 adds the trade-path Portfolio Manager to `_execute_trade()` (again
shared by both routes, so a human-submitted and an LLM-originated trade get
identical portfolio-level treatment). It runs inside the OMS, after the
Risk Engine has approved a proposal and before the broker call, and can
APPROVE, shrink (MODIFY), or REJECT. A shrunk proposal is re-evaluated by
the Risk Engine before it reaches the broker - see
apps/api/app/oms/service.py's docstring. Its portfolio view is built from
the same broker positions and the same `marks` dict the risk engine's
AccountState came from, so no second, possibly-divergent valuation exists.

D039 makes the emergency stop a persisted, live-flippable control:
`_execute_trade()` reads its current state from `emergency_stop_events`
(apps/api/app/safety/emergency_stop.py) on every submission and passes the
resulting boolean down. `Settings.emergency_stop_active` remains only as
the bootstrap default used while no flip has ever been recorded. The Risk
Engine's signature and behavior are unchanged - it still receives a plain
boolean and still returns BlockReason.EMERGENCY_STOP_ACTIVE; only the
source of that boolean moved.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.agents.fundamental_analyst import FundamentalAnalyst
from apps.api.app.agents.news_analyst import NewsAnalyst
from apps.api.app.agents.technical_analyst import AnalystOutputError, TechnicalAnalyst
from apps.api.app.agents.trader import AgentOutputError, TraderAgent, stop_price_from_distance
from apps.api.app.api.dependencies import (
    AuthorizedBroker,
    get_fundamental_analyst,
    get_fundamentals_provider,
    get_history_provider,
    get_market_data_router,
    get_news_analyst,
    get_news_provider,
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
from apps.api.app.marketdata.fundamentals_provider import FundamentalsProvider
from apps.api.app.marketdata.history_provider import HistoryProvider
from apps.api.app.marketdata.indicators import InsufficientDataError, rsi, sma
from apps.api.app.marketdata.news_provider import NewsProvider
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.router import MarketDataRouter, NoDataAvailableError
from apps.api.app.oms.persistence import get_recent_filled_orders, submit_trade_and_record
from apps.api.app.portfolio_manager.manager import portfolio_state_from_positions
from apps.api.app.portfolio_manager.models import PortfolioLimits
from apps.api.app.risk.models import RiskLimits, TradeProposal
from apps.api.app.safety.emergency_stop import is_emergency_stop_active

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


NEWS_HEADLINE_LIMIT = 10
"""How many real headlines the NewsAnalyst is shown. A deliberate cap:
the analyst cites a deterministically-counted set, and an unbounded list
would grow the prompt without improving a three-sentence read."""


def _render_read(stance: str, confidence: Decimal, summary: str) -> str:
    """The one rendering of an analyst read into trader-agent prompt text,
    shared by all three analysts (D019's original format, unchanged) so no
    analyst's context can drift into a different shape."""
    return f"stance={stance}, confidence={confidence}: {summary}"


async def _technical_context(
    *,
    symbol: str,
    price: Decimal,
    as_of: datetime,
    technical_analyst: TechnicalAnalyst | None,
    history_provider: HistoryProvider | None,
) -> str | None:
    """D019/D021, unchanged in behavior by D059 - only extracted into a
    helper so the three analysts can run concurrently. Optional additional
    context only: a failed or unavailable read must never block the trade,
    and is never replaced with a fabricated one."""
    if technical_analyst is None:
        return None

    indicator_context: str | None = None
    if history_provider is not None:
        # D021: real, deterministically-computed indicators (never
        # asked of an LLM) - optional, like everything else here. A
        # short history, a vendor failure, or no configured provider all
        # just mean no indicator context this call, never a blocked trade
        # or a guessed value.
        try:
            closes = await history_provider.get_daily_closes(symbol, count=30)
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
            logger.warning("history_provider_unavailable", symbol=symbol, error=str(exc))

    try:
        read = await technical_analyst.analyze(
            symbol=symbol,
            price=price,
            as_of=as_of.isoformat(),
            indicator_context=indicator_context,
        )
    except AnalystOutputError as exc:
        logger.warning("technical_analyst_unavailable", symbol=symbol, error=str(exc))
        return None
    return _render_read(read.stance.value, read.confidence, read.summary)


async def _fundamental_context(
    *,
    symbol: str,
    analyst: FundamentalAnalyst | None,
    provider: FundamentalsProvider | None,
) -> str | None:
    """D059: real vendor fundamentals -> a narrated read, or nothing.

    Requires BOTH a configured analyst and a configured provider: unlike
    the technical analyst (which can still comment qualitatively on the
    live quote it is always given), there is no fundamentals equivalent
    of "one price" to fall back on, so with no vendor data there is
    nothing real to narrate - and narrating without data is exactly the
    fabrication spec Sec57 forbids. A symbol the vendor doesn't cover
    (DataUnavailableError) or a vendor failure both simply omit this
    context; neither blocks the trade.
    """
    if analyst is None or provider is None:
        return None

    try:
        fundamentals = await provider.get_fundamentals(symbol)
    except (DataUnavailableError, VendorError) as exc:
        logger.warning("fundamentals_provider_unavailable", symbol=symbol, error=str(exc))
        return None

    try:
        read = await analyst.analyze(fundamentals=fundamentals)
    except AnalystOutputError as exc:
        logger.warning("fundamental_analyst_unavailable", symbol=symbol, error=str(exc))
        return None
    return _render_read(read.stance.value, read.confidence, read.summary)


async def _news_context(
    *,
    symbol: str,
    analyst: NewsAnalyst | None,
    provider: NewsProvider | None,
) -> str | None:
    """D059: real recent headlines -> a narrated read, or nothing. Same
    both-required, never-blocking, never-fabricated posture as
    _fundamental_context above."""
    if analyst is None or provider is None:
        return None

    try:
        headlines = await provider.get_recent_headlines(symbol, limit=NEWS_HEADLINE_LIMIT)
    except (DataUnavailableError, VendorError) as exc:
        logger.warning("news_provider_unavailable", symbol=symbol, error=str(exc))
        return None

    if not headlines:
        # A provider is contractually supposed to raise rather than return
        # an empty list, but an empty list must never reach the analyst -
        # there would be nothing real for it to cite.
        return None

    try:
        read = await analyst.analyze(symbol=symbol, headlines=headlines)
    except AnalystOutputError as exc:
        logger.warning("news_analyst_unavailable", symbol=symbol, error=str(exc))
        return None
    return _render_read(read.stance.value, read.confidence, read.summary)


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
        duplicate_order_window_seconds=settings.risk_duplicate_order_window_seconds,
    )

    portfolio_limits = PortfolioLimits(
        max_symbol_pct_of_equity=settings.portfolio_max_symbol_pct_of_equity,
        min_cash_reserve_pct_of_equity=settings.portfolio_min_cash_reserve_pct_of_equity,
        max_open_positions=settings.portfolio_max_open_positions,
    )

    # D029: the trade-path Portfolio Manager's view of the book, built from
    # the same broker positions and the same marks the risk engine's
    # AccountState came from - so the traded symbol is valued at exactly the
    # price the proposal carries, which is what makes the projected
    # post-trade concentration exact rather than a mixed-mark estimate.
    try:
        portfolio = portfolio_state_from_positions(
            broker_adapter.positions, marks, broker_adapter.cash
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"DATA_UNAVAILABLE: {exc}") from None

    # D039: the emergency stop's authoritative state is a persisted row,
    # read here (one layer above the Risk Engine) and handed down as a
    # plain boolean - the engine stays zero-I/O, exactly as with
    # recent_orders below. Read per submission, never cached, so an
    # admin's flip takes effect on the next trade with no restart.
    # settings.emergency_stop_active is only the bootstrap default used
    # while no flip has ever been persisted.
    emergency_stop_active = await is_emergency_stop_active(
        session, settings_default=settings.emergency_stop_active
    )

    # D024: a targeted, indexed read of this broker+symbol's recent FILLED
    # orders, handed to the (still pure) risk engine as plain data - never
    # a DB access the engine makes itself.
    recent_orders = await get_recent_filled_orders(
        session,
        broker_id,
        proposal.symbol,
        window_seconds=settings.risk_duplicate_order_window_seconds,
    )

    result = await submit_trade_and_record(
        session,
        broker_id,
        proposal,
        account,
        limits,
        broker_adapter,
        emergency_stop_active=emergency_stop_active,
        submitted_by_user_id=submitted_by_user_id,
        recent_orders=recent_orders,
        portfolio=portfolio,
        portfolio_limits=portfolio_limits,
    )

    await save_paper_broker(session, broker_id, broker_adapter)
    await session.commit()

    assert result.order_id is not None  # always set by submit_trade_and_record
    portfolio_decision = result.portfolio_decision
    return TradeSubmissionResponse(
        order_id=result.order_id,
        status=OrderStatus(result.status.value),
        approved=result.risk_decision.approved,
        block_reason=result.risk_decision.reason,
        detail=result.risk_decision.detail,
        portfolio_action=portfolio_decision.action if portfolio_decision else None,
        portfolio_binding_constraint=(
            portfolio_decision.binding_constraint if portfolio_decision else None
        ),
        portfolio_detail=portfolio_decision.detail if portfolio_decision else None,
        portfolio_requested_quantity=(
            portfolio_decision.requested_quantity if portfolio_decision else None
        ),
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
    fundamental_analyst: FundamentalAnalyst | None = Depends(get_fundamental_analyst),
    fundamentals_provider: FundamentalsProvider | None = Depends(get_fundamentals_provider),
    news_analyst: NewsAnalyst | None = Depends(get_news_analyst),
    news_provider: NewsProvider | None = Depends(get_news_provider),
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

    # D059: the three analysts have no cross-dependency, so they run
    # concurrently (docs/AGENT_POLICY.md: "Parallelize agents with no
    # cross-dependency"). Each helper is independently failure-isolated
    # and returns None rather than raising, so one analyst's vendor or
    # LLM failure can never affect the other two - or the trade.
    technical_context, fundamental_context, news_context = await asyncio.gather(
        _technical_context(
            symbol=request.symbol,
            price=estimated_price,
            as_of=market_data_as_of,
            technical_analyst=technical_analyst,
            history_provider=history_provider,
        ),
        _fundamental_context(
            symbol=request.symbol,
            analyst=fundamental_analyst,
            provider=fundamentals_provider,
        ),
        _news_context(
            symbol=request.symbol,
            analyst=news_analyst,
            provider=news_provider,
        ),
    )

    try:
        idea = await trader_agent.propose(
            symbol=request.symbol,
            directive=request.directive,
            technical_context=technical_context,
            fundamental_context=fundamental_context,
            news_context=news_context,
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
