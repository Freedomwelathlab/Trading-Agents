"""Persists every OMS decision as an append-only Order (+ Fill, if any).

This wraps submit_trade() rather than modifying it - the pure, DB-free
version stays available for fast unit tests and keeps the risk-gate
property (works even with everything downstream unreachable) honest by
construction. This module is the only place that writes an Order row, and
it always does so through submit_trade(), so persistence can never become a
second, divergent path to the broker.
"""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.db.models import Fill as FillRow
from apps.api.app.db.models import Order as OrderRow
from apps.api.app.db.models import OrderStatus
from apps.api.app.execution.broker import BrokerAdapter
from apps.api.app.oms.service import OMSResult, OMSStatus, submit_trade
from apps.api.app.risk.models import AccountState, RiskLimits, TradeProposal


async def submit_trade_and_record(
    session: AsyncSession,
    broker_id: uuid.UUID,
    proposal: TradeProposal,
    account: AccountState,
    limits: RiskLimits,
    broker: BrokerAdapter,
    *,
    emergency_stop_active: bool = False,
    now: datetime | None = None,
) -> OMSResult:
    result = submit_trade(
        proposal,
        account,
        limits,
        broker,
        emergency_stop_active=emergency_stop_active,
        now=now,
    )

    status = OrderStatus.FILLED if result.status is OMSStatus.FILLED else OrderStatus.REJECTED
    block_reason = result.risk_decision.reason.value if result.risk_decision.reason else None
    order_row = OrderRow(
        broker_id=broker_id,
        symbol=proposal.symbol,
        side=proposal.side,
        quantity=proposal.quantity,
        estimated_price=proposal.estimated_price,
        stop_price=proposal.stop_price,
        status=status,
        risk_block_reason=block_reason,
        risk_detail=result.risk_decision.detail,
    )
    session.add(order_row)
    await session.flush()  # assigns order_row.id without ending the transaction

    if result.fill is not None:
        session.add(
            FillRow(
                order_id=order_row.id,
                quantity=result.fill.quantity,
                fill_price=result.fill.fill_price,
                filled_at=result.fill.filled_at,
            )
        )

    await session.commit()
    result.order_id = order_row.id
    return result
