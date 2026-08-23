"""Order Management System - the only sanctioned path from a trade proposal
to a broker call. Application code must call submit_trade(), never a
BrokerAdapter directly; submit_trade() is what makes the risk gate
structurally unavoidable rather than a convention someone can forget.
"""

import enum
import uuid
from datetime import datetime

from pydantic import BaseModel

from apps.api.app.execution.broker import BrokerAdapter, Fill, OrderRequest
from apps.api.app.risk.engine import evaluate_trade
from apps.api.app.risk.models import (
    AccountState,
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
) -> OMSResult:
    decision = evaluate_trade(
        proposal, account, limits, emergency_stop_active=emergency_stop_active, now=now
    )
    if not decision.approved:
        return OMSResult(status=OMSStatus.REJECTED, risk_decision=decision)

    order = OrderRequest(symbol=proposal.symbol, side=proposal.side, quantity=proposal.quantity)
    fill = broker.submit_order(order, market_price=proposal.estimated_price)
    return OMSResult(status=OMSStatus.FILLED, risk_decision=decision, fill=fill)
