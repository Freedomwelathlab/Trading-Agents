"""Persists every OMS decision as an append-only Order (+ Fill, if any).

This wraps submit_trade() rather than modifying it - the pure, DB-free
version stays available for fast unit tests and keeps the risk-gate
property (works even with everything downstream unreachable) honest by
construction. This module is the only place that writes an Order row, and
it always does so through submit_trade(), so persistence can never become a
second, divergent path to the broker.

Deliberately does NOT commit. The caller (apps/api/app/api/routes/trades.py)
also saves the broker's post-trade cash/position state
(apps/api/app/execution/persistence.py) in the same transaction and commits
once at the end - see docs/DECISIONS.md D014. Committing here would release
load_paper_broker()'s row lock before that save happens, reopening the
double-spend race the lock exists to close. Direct-test callers
(tests/db/test_order_persistence.py) don't need an explicit commit either:
they query within the same still-open session/transaction, which sees its
own flushed-but-uncommitted writes.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.db.models import Fill as FillRow
from apps.api.app.db.models import Order as OrderRow
from apps.api.app.db.models import OrderStatus
from apps.api.app.execution.broker import BrokerAdapter
from apps.api.app.oms.service import OMSResult, OMSStatus, submit_trade
from apps.api.app.risk.models import AccountState, RecentOrder, RiskLimits, TradeProposal


async def get_recent_filled_orders(
    session: AsyncSession,
    broker_id: uuid.UUID,
    symbol: str,
    *,
    window_seconds: int,
    now: datetime | None = None,
) -> list[RecentOrder]:
    """Queries this broker's FILLED orders for `symbol` submitted within the
    last `window_seconds` - the only DB read the duplicate-order check
    needs (docs/DECISIONS.md D024). Deliberately excludes REJECTED orders:
    D024 explains why a rejected order's identical retry should not itself
    be treated as a duplicate. Filtered by symbol (not just broker_id) so
    this stays a targeted, indexed read (ix_orders_broker_symbol_submitted,
    migration 0007) rather than a full per-broker order-history scan.
    """
    now = now or datetime.now(UTC)
    since = now - timedelta(seconds=window_seconds)
    rows = (
        await session.execute(
            select(OrderRow).where(
                OrderRow.broker_id == broker_id,
                OrderRow.symbol == symbol,
                OrderRow.status == OrderStatus.FILLED,
                OrderRow.submitted_at >= since,
            )
        )
    ).scalars()
    return [
        RecentOrder(
            symbol=row.symbol,
            side=row.side,
            quantity=row.quantity,
            estimated_price=row.estimated_price,
            submitted_at=row.submitted_at,
        )
        for row in rows
    ]


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
    submitted_by_user_id: uuid.UUID | None = None,
    recent_orders: list[RecentOrder] | None = None,
) -> OMSResult:
    result = submit_trade(
        proposal,
        account,
        limits,
        broker,
        emergency_stop_active=emergency_stop_active,
        now=now,
        recent_orders=recent_orders,
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
        submitted_by_user_id=submitted_by_user_id,
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

    await session.flush()
    result.order_id = order_row.id
    return result
