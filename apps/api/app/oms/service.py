"""Order Management System - the only sanctioned path from a trade proposal
to a broker call. Application code must call submit_trade(), never a
BrokerAdapter directly; submit_trade() is what makes the risk gate
structurally unavoidable rather than a convention someone can forget.

D029 inserts the trade-path Portfolio Manager
(apps/api/app/portfolio_manager/) between the risk gate and the broker
call, exactly where docs/ARCHITECTURE.md's Target diagram and
docs/TRADING_SAFETY.md's pipeline put it. Two properties are load-bearing:

  1. The Portfolio Manager only ever sees a proposal the Risk Engine has
     already approved. It is a second, narrower gate, never a way around
     the first one.
  2. If it MODIFIES the quantity, the modified proposal goes back through
     evaluate_trade() before any broker call. No quantity this system
     produces - from an LLM, from a human, or from the Portfolio Manager
     itself - reaches a broker without the deterministic Risk Engine
     having approved that exact quantity.

`portfolio`/`portfolio_limits` are optional: omitting them skips the
Portfolio Manager entirely and restores pre-D029 behaviour. That is
deliberately not a fail-closed decision, because skipping this component
cannot make a trade less safe - the Risk Engine still gated it, and this
component can only ever shrink or stop a trade. The HTTP trade path
(apps/api/app/api/routes/trades.py) always supplies both.

`market_risk` (Phase 62, D079) is optional on the same footing: every
backtest caller omits it and the Portfolio Manager's two market-risk
checks then never run, so no existing backtest result moves; the trade
path builds a `MarketRiskInputs` from `market_data_bars` and supplies it.
"""

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from apps.api.app.execution.broker import (
    BrokerAdapter,
    Fill,
    OrderNotConfirmedError,
    OrderRequest,
    UnconfirmedSubmissionContext,
)
from apps.api.app.portfolio_manager.manager import decide as portfolio_decide
from apps.api.app.portfolio_manager.models import (
    MarketRiskInputs,
    PortfolioAction,
    PortfolioDecision,
    PortfolioLimits,
    PortfolioState,
)
from apps.api.app.risk.engine import evaluate_trade
from apps.api.app.risk.models import (
    AccountState,
    RecentOrder,
    RiskDecision,
    RiskLimits,
    TradeProposal,
)


class OMSStatus(str, enum.Enum):  # noqa: UP042 (str mixin kept for interop)
    FILLED = "filled"
    REJECTED = "rejected"


class OMSResult(BaseModel):
    status: OMSStatus
    risk_decision: RiskDecision
    portfolio_decision: PortfolioDecision | None = None
    """None when no Portfolio Manager ran - either because the caller
    supplied no portfolio state, or because the Risk Engine rejected the
    proposal before the Portfolio Manager was ever consulted (D029). A
    caller must not read `None` as 'the portfolio approved it'."""
    effective_quantity: Decimal | None = None
    """The quantity actually submitted to the broker, which differs from
    proposal.quantity when the Portfolio Manager returned MODIFY. None when
    nothing reached the broker."""
    fill: Fill | None = None
    order_id: uuid.UUID | None = None
    """Set only by submit_trade_and_record() (apps/api/app/oms/persistence.py)
    - the pure submit_trade() has no database, so this stays None there."""


def submit_trade(
    proposal: TradeProposal,
    account: AccountState,
    limits: RiskLimits,
    broker: BrokerAdapter,
    *,
    emergency_stop_active: bool = False,
    now: datetime | None = None,
    recent_orders: list[RecentOrder] | None = None,
    portfolio: PortfolioState | None = None,
    portfolio_limits: PortfolioLimits | None = None,
    market_risk: MarketRiskInputs | None = None,
    market_price: Decimal | None = None,
) -> OMSResult:
    """`market_price` (Phase 84, D101) is the CURRENT quote handed to the
    adapter, which for a limit order differs from `proposal.estimated_price`
    (the limit). Defaults to the estimated price, which is what every
    market-order caller passed before."""
    decision = evaluate_trade(
        proposal,
        account,
        limits,
        emergency_stop_active=emergency_stop_active,
        now=now,
        recent_orders=recent_orders,
    )
    if not decision.approved:
        return OMSResult(status=OMSStatus.REJECTED, risk_decision=decision)

    effective = proposal
    portfolio_decision: PortfolioDecision | None = None

    if portfolio is not None and portfolio_limits is not None:
        portfolio_decision = portfolio_decide(
            proposal, portfolio, portfolio_limits, market_risk
        )

        if portfolio_decision.action is PortfolioAction.REJECT:
            return OMSResult(
                status=OMSStatus.REJECTED,
                risk_decision=decision,
                portfolio_decision=portfolio_decision,
            )

        if portfolio_decision.action is PortfolioAction.MODIFY:
            effective = proposal.model_copy(
                update={"quantity": portfolio_decision.approved_quantity}
            )
            # The resized proposal is a new proposal, and every proposal
            # goes through the Risk Engine - including one this system
            # produced itself. See this module's docstring.
            decision = evaluate_trade(
                effective,
                account,
                limits,
                emergency_stop_active=emergency_stop_active,
                now=now,
                recent_orders=recent_orders,
            )
            if not decision.approved:
                return OMSResult(
                    status=OMSStatus.REJECTED,
                    risk_decision=decision,
                    portfolio_decision=portfolio_decision,
                )

    order = OrderRequest(
        symbol=effective.symbol,
        side=effective.side,
        quantity=effective.quantity,
        order_type=effective.order_type,
        limit_price=effective.limit_price,
    )
    try:
        fill = broker.submit_order(
            order,
            market_price=market_price if market_price is not None else effective.estimated_price,
        )
    except OrderNotConfirmedError as exc:
        # Phase 49 (D066). A REAL order now exists at a REAL broker and this
        # function is about to unwind past every local variable describing
        # it. Attaching that description to the exception is what lets
        # `submit_trade_and_record()` persist a truthful `orders` row -
        # specifically `effective.quantity`, which is what was actually sent
        # and differs from `proposal.quantity` whenever the Portfolio
        # Manager shrank the order (D029).
        #
        # This is the ONE thing this pure function does about a live-broker
        # condition, and it deliberately neither swallows the exception nor
        # changes its type: every existing caller still sees exactly the
        # exception it saw before this phase. Caught at the port
        # (`OrderNotConfirmedError`, apps/api/app/execution/broker.py) rather
        # than as the concrete `LiveOrderNotFilledError`, so this module
        # stays free of any dependency on a specific broker.
        exc.submission_context = UnconfirmedSubmissionContext(
            effective_quantity=effective.quantity,
            risk_detail=decision.detail,
            portfolio_action=(
                portfolio_decision.action.value if portfolio_decision else None
            ),
            portfolio_binding_constraint=(
                portfolio_decision.binding_constraint.value
                if portfolio_decision and portfolio_decision.binding_constraint
                else None
            ),
            portfolio_detail=portfolio_decision.detail if portfolio_decision else None,
            portfolio_requested_quantity=(
                portfolio_decision.requested_quantity if portfolio_decision else None
            ),
        )
        raise
    return OMSResult(
        status=OMSStatus.FILLED,
        risk_decision=decision,
        portfolio_decision=portfolio_decision,
        effective_quantity=effective.quantity,
        fill=fill,
    )
