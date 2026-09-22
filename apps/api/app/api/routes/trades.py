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

Phase 52 returns those three reads to the caller instead of discarding
them, as `technical_analyst`/`fundamental_analyst`/`news_analyst` on
AgentTradeResponse. Strictly additive and strictly a read: the analysts
run exactly when and how they ran before, the prompt text they produce is
byte-identical, and the trade decision above them is untouched. Each field
is nullable ON ITS OWN because each analyst is optional and
failure-isolated on its own - a null is the real absence of that one
read, never a manufactured neutral one (spec Sec57).

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

D058 (Phase 43) adds the LIVE branch to the human-submitted route only.
A request against a broker row whose kind is LIVE must clear, in this
order and BEFORE any of the machinery a paper trade uses:

  1. `confirm: true` in the request body (docs/TRADING_SAFETY.md's
     "requires explicit user confirmation", made enforceable server-side),
  2. an actually-configured live execution path - TRADING_MODE=live,
     LIVE_TRADING_ENABLED=true, and the LONGPORT_LIVE_* credential trio,
     which the committed defaults do NOT satisfy,
  3. the `trade:submit:live` permission, in addition to the
     `trade:submit:paper` this route's dependency already required.

After that it runs the SAME `_execute_trade()` a paper trade runs, so the
emergency stop (D039), the duplicate-order check (D024), the deterministic
Risk Engine, and the Portfolio Manager (D029) gate a live order exactly as
they gate a paper one - they are not re-implemented for live and cannot be
skipped by it. The only two differences below that point are which broker
adapter is used and which risk limits are read (the tighter `live_risk_*`
set - see apps/api/app/risk/limits.py).

The AGENT route is deliberately untouched: it remains paper-only.
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
    get_live_broker_adapter,
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
    AnalystReadOut,
    FundamentalAnalystReadOut,
    NewsAnalystReadOut,
    TechnicalAnalystReadOut,
    TradeSubmissionRequest,
    TradeSubmissionResponse,
)
from apps.api.app.auth.permissions import Permission
from apps.api.app.core.config import Settings, get_settings
from apps.api.app.core.logging import get_logger
from apps.api.app.db.base import get_session
from apps.api.app.db.models import BrokerKind, OrderStatus
from apps.api.app.execution.broker import BrokerAdapter, OrderWouldRestError
from apps.api.app.execution.live_broker import (
    LiveBrokerAdapter,
    LiveBrokerError,
    LiveOrderNotFilledError,
)
from apps.api.app.execution.paper_broker import PaperBrokerAdapter
from apps.api.app.execution.persistence import load_paper_broker, save_paper_broker
from apps.api.app.marketdata.fundamentals_provider import FundamentalsProvider
from apps.api.app.marketdata.history_provider import HistoryProvider
from apps.api.app.marketdata.indicators import InsufficientDataError, rsi, sma
from apps.api.app.marketdata.news_provider import NewsProvider
from apps.api.app.marketdata.portfolio_risk import load_market_risk_inputs
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.router import MarketDataRouter, NoDataAvailableError
from apps.api.app.marketdata.store import MarketDataStore
from apps.api.app.oms.persistence import get_recent_filled_orders, submit_trade_and_record
from apps.api.app.portfolio_manager.manager import portfolio_state_from_positions
from apps.api.app.portfolio_manager.models import PortfolioLimits
from apps.api.app.risk.limits import build_risk_limits
from apps.api.app.risk.models import TradeProposal
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


def _prompt_context(read: AnalystReadOut | None) -> str | None:
    """The one rendering of an analyst read into trader-agent prompt text,
    shared by all three analysts (D019's original format, byte for byte)
    so no analyst's context can drift into a different shape.

    Phase 52 changed only what this is called with: the three helpers now
    return the structured read itself rather than pre-rendered text, so the
    same read can be BOTH appended to the prompt and returned to the
    caller. The string produced here is unchanged, which is why widening
    the response cannot have changed a single trader-agent prompt."""
    if read is None:
        return None
    return f"stance={read.stance.value}, confidence={read.confidence}: {read.summary}"


async def _technical_read(
    *,
    symbol: str,
    price: Decimal,
    as_of: datetime,
    technical_analyst: TechnicalAnalyst | None,
    history_provider: HistoryProvider | None,
) -> TechnicalAnalystReadOut | None:
    """D019/D021, unchanged in behavior by D059 or Phase 52 - the latter
    only changed the return type from pre-rendered prompt text to the
    structured read that text is rendered from. Optional additional
    context only: a failed or unavailable read must never block the trade,
    and is never replaced with a fabricated one.

    Every `return None` below is a real absence the caller surfaces as a
    null `technical_analyst` field, never as an invented neutral read."""
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
    return TechnicalAnalystReadOut(
        stance=read.stance,
        summary=read.summary,
        confidence=read.confidence,
        # The real, deterministically-computed values the analyst was
        # handed (or None - it was handed none, and narrated the quote
        # alone). Reported verbatim rather than re-derived here, so what
        # the caller reads is exactly what the analyst read.
        indicator_context=indicator_context,
    )


async def _fundamental_read(
    *,
    symbol: str,
    analyst: FundamentalAnalyst | None,
    provider: FundamentalsProvider | None,
) -> FundamentalAnalystReadOut | None:
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
    return FundamentalAnalystReadOut(
        stance=read.stance,
        summary=read.summary,
        confidence=read.confidence,
        # Provenance of the figures this read narrates, straight off the
        # vendor's own record - not restated, defaulted, or freshened.
        data_source=fundamentals.source,
        fundamentals_as_of=fundamentals.as_of,
    )


async def _news_read(
    *,
    symbol: str,
    analyst: NewsAnalyst | None,
    provider: NewsProvider | None,
) -> NewsAnalystReadOut | None:
    """D059: real recent headlines -> a narrated read, or nothing. Same
    both-required, never-blocking, never-fabricated posture as
    _fundamental_read above."""
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
    return NewsAnalystReadOut(
        stance=read.stance,
        summary=read.summary,
        confidence=read.confidence,
        # The exact count the analyst was shown and told to cite, counted
        # here in code from the same list - never the model's own number.
        headline_count=len(headlines),
    )


def _require_paper_broker(authorized: AuthorizedBroker) -> None:
    """Used by the AGENT trade route only, as of Phase 43. An
    LLM-originated live order is exactly what docs/TRADING_SAFETY.md and
    docs/AGENT_POLICY.md rule out: the human's `confirm: true` on the
    human-submitted route is a confirmation of a specific side, quantity
    and stop that the human chose, and no equivalent exists when an agent
    invents those. So `/agent-trades` stays paper-only and D058 changed
    nothing about it."""
    if authorized.broker.kind is not BrokerKind.PAPER:
        raise HTTPException(
            status_code=400,
            detail=(
                "NOT_CONFIGURED: this endpoint only supports paper brokers. "
                "Agent-originated live trades are not permitted (spec §3/§46, "
                "docs/AGENT_POLICY.md)."
            ),
        )


def _authorize_live_trade(
    authorized: AuthorizedBroker,
    *,
    confirmed: bool,
    live_broker: LiveBrokerAdapter | None,
) -> None:
    """Phase 43 (D058). Every gate a LIVE trade must clear BEFORE any of
    the machinery a paper trade also clears (risk engine, emergency stop,
    Portfolio Manager) is consulted.

    Order matters and is deliberate. The confirmation check is FIRST, so
    that an unconfirmed live request is refused for the reason that is
    actually true of it - "you did not confirm" - regardless of how the
    server happens to be configured, and so this branch is testable
    without ever enabling live trading anywhere. The configuration gate
    is second: on a default deployment (`LIVE_TRADING_ENABLED=false`)
    `live_broker` is None and the trade stops here, having touched no
    broker. The permission check is last of the three because it is the
    only one that needs the resolved role.

    `trade:submit:live` is required IN ADDITION to the
    `trade:submit:paper` the route's own dependency already enforced -
    strictly more demanding than either alone, which is the correct
    direction for a control that spends real money.
    """
    if not confirmed:
        raise HTTPException(
            status_code=400,
            detail=(
                "LIVE_CONFIRMATION_REQUIRED: this broker is a LIVE broker and this "
                "request would place a real order with real money. Resubmit with "
                '"confirm": true to state that intent explicitly '
                "(docs/TRADING_SAFETY.md, docs/DECISIONS.md D058)."
            ),
        )

    if live_broker is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "NOT_CONFIGURED: no live execution path is available. Live trading "
                "requires TRADING_MODE=live, LIVE_TRADING_ENABLED=true, and all three "
                "LONGPORT_LIVE_* credentials set together (docs/DECISIONS.md D058). "
                "No live-broker request is ever routed to the paper simulator."
            ),
        )

    role = authorized.user.role
    if role is None or Permission.SUBMIT_LIVE_TRADE.value not in role.permissions:
        raise HTTPException(
            status_code=403,
            detail=f"Missing required permission: {Permission.SUBMIT_LIVE_TRADE.value}",
        )


async def _execute_trade(
    *,
    session: AsyncSession,
    settings: Settings,
    broker_id: uuid.UUID,
    proposal: TradeProposal,
    marks: dict[str, Decimal],
    submitted_by_user_id: uuid.UUID,
    live_broker: LiveBrokerAdapter | None = None,
    market_price: Decimal | None = None,
) -> TradeSubmissionResponse:
    """`live_broker` is None for every paper trade, which is the only shape
    this function had before Phase 43; passing one switches the broker and
    the risk limits and nothing else. Everything between those two points -
    the emergency stop read, the duplicate-order read, the Risk Engine, the
    Portfolio Manager, the re-evaluation of a shrunk proposal, and the
    order/fill persistence - is the SAME code for both, by construction
    rather than by convention (D058)."""
    live = live_broker is not None
    paper_adapter: PaperBrokerAdapter | None = None

    if live_broker is not None:
        broker_adapter: BrokerAdapter = live_broker
    else:
        # Locks the broker's account row for the rest of this transaction -
        # see apps/api/app/execution/persistence.py's module docstring for
        # why. A live broker has no such row: the venue, not this database,
        # holds the authoritative cash and positions (spec §62).
        paper_adapter = await load_paper_broker(
            session, broker_id, default_starting_cash=settings.paper_broker_starting_cash
        )
        broker_adapter = paper_adapter
    try:
        account = broker_adapter.get_account_state(marks=marks)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"DATA_UNAVAILABLE: {exc}") from None
    except LiveBrokerError as exc:
        # D058: the live account state could not be read. Fail closed -
        # never proceed to a risk evaluation against an unknown book.
        raise HTTPException(status_code=502, detail=f"DATA_UNAVAILABLE: {exc}") from None

    # D058: the live branch reads ONLY live_risk_* and the paper branch
    # reads ONLY risk_*; see apps/api/app/risk/limits.py.
    limits = build_risk_limits(settings, live=live)

    portfolio_limits = PortfolioLimits(
        max_symbol_pct_of_equity=settings.portfolio_max_symbol_pct_of_equity,
        min_cash_reserve_pct_of_equity=settings.portfolio_min_cash_reserve_pct_of_equity,
        max_open_positions=settings.portfolio_max_open_positions,
        # Phase 62 (D079): the two market-risk ceilings. The Portfolio
        # Manager still only runs the matching check when a MarketRiskInputs
        # is also supplied (below), and skips it - audited, non-binding -
        # for any symbol with too little ingested bar history.
        max_portfolio_volatility_pct=settings.portfolio_max_portfolio_volatility_pct,
        max_position_correlation=settings.portfolio_max_position_correlation,
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
    except LiveBrokerError as exc:
        raise HTTPException(status_code=502, detail=f"DATA_UNAVAILABLE: {exc}") from None

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

    # D079 (D029 rejected alternative (f)): the Portfolio Manager stays
    # zero-I/O, so the real `market_data_bars` read for the two Phase-62
    # market-risk checks happens HERE - one layer above it, exactly like
    # get_recent_filled_orders and is_emergency_stop_active - and the
    # finished MarketRiskInputs is handed down as plain data. The window
    # ends on the proposal's own market-data timestamp. On a checkout with
    # no ingested bars this returns an empty-but-valid MarketRiskInputs and
    # decide() simply skips both new checks - that empty-inputs path IS the
    # off switch, so there is no config flag to disable it.
    market_risk = await load_market_risk_inputs(
        MarketDataStore(session),
        [proposal.symbol, *portfolio.open_symbols],
        as_of=proposal.market_data_as_of.date(),
        lookback_days=settings.portfolio_market_risk_lookback_days,
        min_observations=settings.portfolio_market_risk_min_observations,
    )

    try:
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
            market_risk=market_risk,
            market_price=market_price,
        )
    except OrderWouldRestError as exc:
        # Phase 84 (D101): a non-marketable limit on the paper broker. Not
        # an error in the request; a real answer about the price.
        raise HTTPException(status_code=409, detail=f"ORDER_WOULD_REST: {exc}") from None
    except LiveOrderNotFilledError as exc:
        # D058: the live broker ACCEPTED a real order but has not executed
        # it. The one thing that must not happen here is inventing a fill,
        # so the real order id is surfaced instead (spec §57). Note this is
        # reachable only after every gate above already approved the trade.
        logger.error(
            "live_order_submitted_but_not_filled",
            broker_id=str(broker_id),
            symbol=proposal.symbol,
            broker_order_id=exc.order_id,
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f"LIVE_ORDER_UNCONFIRMED: broker order {exc.order_id} was submitted but "
                "no execution has been reported. This order may still fill - reconcile "
                "it with the broker directly. No fill was recorded and none was assumed."
            ),
        ) from None
    except LiveBrokerError as exc:
        raise HTTPException(status_code=502, detail=f"LIVE_BROKER_ERROR: {exc}") from None

    if paper_adapter is not None:
        # A live broker's cash/positions live at the venue, not in
        # broker_accounts/broker_positions - writing them here would create
        # a second, immediately-divergent book (spec §62). The order/fill
        # rows written by submit_trade_and_record above are still recorded
        # for both kinds; those are an audit record of what THIS system
        # did, which is a different claim from "this is the account state".
        await save_paper_broker(session, broker_id, paper_adapter)
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
    live_broker: LiveBrokerAdapter | None = Depends(get_live_broker_adapter),
) -> TradeSubmissionResponse:
    # D058: exactly one of these two branches runs, decided by the BROKER
    # ROW's kind, never by anything in the request body. A caller cannot
    # opt a paper broker into the live path or vice versa.
    if authorized.broker.kind is BrokerKind.LIVE:
        _authorize_live_trade(authorized, confirmed=request.confirm, live_broker=live_broker)
    else:
        _require_paper_broker(authorized)
        live_broker = None

    if request.order_type == "limit" and request.limit_price is None:
        raise HTTPException(
            status_code=400, detail="LIMIT_PRICE_REQUIRED: a limit order needs limit_price."
        )
    if request.order_type == "market" and request.limit_price is not None:
        raise HTTPException(
            status_code=400, detail="limit_price is only meaningful on a limit order."
        )

    if request.estimated_price is not None:
        estimated_price = request.estimated_price
        market_data_as_of = request.market_data_as_of or datetime.now(UTC)
    elif request.order_type == "limit" and request.limit_price is not None:
        # Phase 84 (D101): the limit price bounds what can be paid, so it
        # is the honest number to size risk on; the vendor is still asked
        # for the current quote so the paper broker can decide if the
        # limit is marketable (a stale or absent quote is a real 400).
        market_price, market_data_as_of = await _resolve_live_quote(
            request.symbol,
            market_data_router,
            not_configured_hint=(
                "NOT_CONFIGURED: a limit order needs a current quote to test against "
                "and no market data vendor is wired (docs/DECISIONS.md D008/D017)."
            ),
        )
        estimated_price = request.limit_price
        request = request.model_copy(
            update={"marks": {**request.marks, "__market__": market_price}}
        )
    else:
        estimated_price, market_data_as_of = await _resolve_live_quote(
            request.symbol,
            market_data_router,
            not_configured_hint=(
                "NOT_CONFIGURED: estimated_price was omitted and no market "
                "data vendor is wired (see docs/DECISIONS.md D008/D017)."
            ),
        )

    market_for_symbol = request.marks.pop("__market__", None) or estimated_price
    proposal = TradeProposal(
        symbol=request.symbol,
        side=request.side,
        quantity=request.quantity,
        estimated_price=estimated_price,
        stop_price=request.stop_price,
        market_data_as_of=market_data_as_of,
        order_type=request.order_type,
        limit_price=request.limit_price,
    )

    return await _execute_trade(
        session=session,
        settings=settings,
        broker_id=broker_id,
        proposal=proposal,
        marks={**request.marks, request.symbol: market_for_symbol},
        submitted_by_user_id=authorized.user.id,
        live_broker=live_broker,
        market_price=market_for_symbol,
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
    #
    # Phase 52: each returns its structured read instead of pre-rendered
    # prompt text. Same three values, same failure isolation, same
    # never-blocking posture - the reads are now used TWICE (rendered into
    # the prompt below, and returned to the caller at the end) rather than
    # rendered once and discarded. Nothing about whether an analyst runs,
    # or what the trade does, changed.
    technical_read, fundamental_read, news_read = await asyncio.gather(
        _technical_read(
            symbol=request.symbol,
            price=estimated_price,
            as_of=market_data_as_of,
            technical_analyst=technical_analyst,
            history_provider=history_provider,
        ),
        _fundamental_read(
            symbol=request.symbol,
            analyst=fundamental_analyst,
            provider=fundamentals_provider,
        ),
        _news_read(
            symbol=request.symbol,
            analyst=news_analyst,
            provider=news_provider,
        ),
    )

    try:
        idea = await trader_agent.propose(
            symbol=request.symbol,
            directive=request.directive,
            technical_context=_prompt_context(technical_read),
            fundamental_context=_prompt_context(fundamental_read),
            news_context=_prompt_context(news_read),
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

    # Phase 52: the trade decision above is returned exactly as it was
    # before this phase - `**result.model_dump()` is the same
    # TradeSubmissionResponse _execute_trade() has always produced, and
    # side/quantity/rationale are the same three agent fields. The three
    # analyst fields are purely additive, and each carries the real read
    # that analyst produced or an explicit null. None of them is ever
    # synthesised from another, and a null is never upgraded into a
    # neutral read (spec Sec57).
    return AgentTradeResponse(
        **result.model_dump(),
        side=idea.side,
        quantity=idea.quantity,
        rationale=idea.rationale,
        technical_analyst=technical_read,
        fundamental_analyst=fundamental_read,
        news_analyst=news_read,
    )
