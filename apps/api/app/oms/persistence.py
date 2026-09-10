"""Persists every OMS decision as an append-only Order (+ Fill, if any).

This wraps submit_trade() rather than modifying it - the pure, DB-free
version stays available for fast unit tests and keeps the risk-gate
property (works even with everything downstream unreachable) honest by
construction. This module is the only place that writes an Order row, and
it always does so through submit_trade(), so persistence can never become a
second, divergent path to the broker.

Deliberately does NOT commit - with exactly one documented exception, the
unconfirmed-live-order path added in Phase 49 (see
`submit_trade_and_record` below). The caller
(apps/api/app/api/routes/trades.py) also saves the broker's post-trade
cash/position state (apps/api/app/execution/persistence.py) in the same
transaction and commits once at the end - see docs/DECISIONS.md D014.
Committing here would release load_paper_broker()'s row lock before that
save happens, reopening the double-spend race the lock exists to close.
Direct-test callers (tests/db/test_order_persistence.py) don't need an
explicit commit either: they query within the same still-open
session/transaction, which sees its own flushed-but-uncommitted writes.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.core.logging import get_logger
from apps.api.app.db.models import Fill as FillRow
from apps.api.app.db.models import Order as OrderRow
from apps.api.app.db.models import OrderStatus
from apps.api.app.execution.broker import BrokerAdapter, OrderNotConfirmedError
from apps.api.app.oms.service import OMSResult, OMSStatus, submit_trade
from apps.api.app.portfolio_manager.models import (
    MarketRiskInputs,
    PortfolioLimits,
    PortfolioState,
)
from apps.api.app.risk.models import AccountState, RecentOrder, RiskLimits, TradeProposal

logger = get_logger(__name__)


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


async def _record_unconfirmed_order(
    session: AsyncSession,
    broker_id: uuid.UUID,
    proposal: TradeProposal,
    exc: OrderNotConfirmedError,
    *,
    submitted_by_user_id: uuid.UUID | None,
) -> None:
    """Persist - and COMMIT - the one row that says "a real order exists at
    a real broker and we do not yet know what happened to it" (Phase 49,
    docs/DECISIONS.md D066).

    WHY THIS COMMITS, WHEN NOTHING ELSE IN THIS MODULE DOES
    -------------------------------------------------------
    The caller of a failed live submission is
    apps/api/app/api/routes/trades.py, which turns this exception into
    `502 LIVE_ORDER_UNCONFIRMED` and therefore never reaches its own
    `session.commit()`. Without an explicit commit here the row is rolled
    back when the request's session closes, and the system forgets it placed
    a real order - which is precisely the Phase 43 gap this phase exists to
    close. A live order that moved (or is about to move) real money is not
    something whose record may depend on the HTTP response succeeding.

    That commit is safe here, and only here, for a reason that is structural
    rather than incidental: `OrderNotConfirmedError` can only be raised by a
    broker adapter that talks to a real venue, and the LIVE branch of
    `_execute_trade()` never calls `load_paper_broker()`, so no
    `SELECT ... FOR UPDATE` row lock is outstanding and no paper
    cash/position write is pending. The session holds exactly the row added
    two lines above and nothing else, so committing it cannot release
    D014's lock early or half-write a paper book. On the paper path this
    function is unreachable: `PaperBrokerAdapter` fills synchronously and
    has no unconfirmed state to be in.

    NOTHING IS INVENTED
    -------------------
    `quantity` comes from the OMS's own `submission_context` - the quantity
    that was ACTUALLY handed to the broker, which differs from
    `proposal.quantity` whenever the Portfolio Manager shrank the order
    (D029). If that context is somehow absent, this refuses to write a row
    at all rather than substituting the proposal's quantity: a number in an
    append-only audit trail that was never sent to any venue is worse than
    an honest log line saying the record could not be made.

    No `fills` row is written, and none may be: no execution has been
    reported. That is the entire distinction this status carries.
    """
    context = exc.submission_context
    if context is None:
        # Defensive, and deliberately loud. Reachable only if some future
        # adapter raises OrderNotConfirmedError outside submit_trade().
        logger.error(
            "live_order_unconfirmed_not_recorded",
            broker_id=str(broker_id),
            symbol=proposal.symbol,
            broker_order_id=exc.order_id,
            detail=(
                "The broker accepted a real order but no submission context was "
                "attached, so the quantity actually sent is unknown. No orders row was "
                "written rather than one guessing at it. Reconcile this order id with "
                "the broker by hand (docs/DECISIONS.md D066)."
            ),
        )
        return

    session.add(
        OrderRow(
            broker_id=broker_id,
            symbol=proposal.symbol,
            side=proposal.side,
            quantity=context.effective_quantity,
            estimated_price=proposal.estimated_price,
            stop_price=proposal.stop_price,
            status=OrderStatus.SUBMITTED_UNCONFIRMED,
            # Null: this system did not block anything. The Risk Engine and
            # the Portfolio Manager both APPROVED this order - that is how
            # it reached a broker at all - so a block reason here would be
            # a fabrication. `risk_detail` still carries the approving
            # decision's own wording, exactly as it does on a filled row.
            risk_block_reason=None,
            risk_detail=context.risk_detail,
            portfolio_action=context.portfolio_action,
            portfolio_binding_constraint=context.portfolio_binding_constraint,
            portfolio_detail=context.portfolio_detail,
            portfolio_requested_quantity=context.portfolio_requested_quantity,
            submitted_by_user_id=submitted_by_user_id,
            broker_order_id=exc.order_id,
            # Both null until a reconciliation cycle actually asks the
            # broker. "We have not looked yet" must not read as "we looked
            # and it was still open".
            broker_status=None,
            reconciled_at=None,
        )
    )
    await session.commit()

    logger.warning(
        "live_order_recorded_unconfirmed",
        broker_id=str(broker_id),
        symbol=proposal.symbol,
        broker_order_id=exc.order_id,
        quantity=str(context.effective_quantity),
        detail=(
            "A real live order was accepted by the broker but no execution has been "
            "reported. It is recorded as submitted_unconfirmed and will be resolved by "
            "LiveOrderReconciler if that job is enabled; no fill was recorded and none "
            "was assumed."
        ),
    )


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
    portfolio: PortfolioState | None = None,
    portfolio_limits: PortfolioLimits | None = None,
    market_risk: MarketRiskInputs | None = None,
) -> OMSResult:
    try:
        result = submit_trade(
            proposal,
            account,
            limits,
            broker,
            emergency_stop_active=emergency_stop_active,
            now=now,
            recent_orders=recent_orders,
            portfolio=portfolio,
            portfolio_limits=portfolio_limits,
            market_risk=market_risk,
        )
    except OrderNotConfirmedError as exc:
        await _record_unconfirmed_order(
            session,
            broker_id,
            proposal,
            exc,
            submitted_by_user_id=submitted_by_user_id,
        )
        raise

    status = OrderStatus.FILLED if result.status is OMSStatus.FILLED else OrderStatus.REJECTED
    block_reason = result.risk_decision.reason.value if result.risk_decision.reason else None
    pd = result.portfolio_decision
    # `quantity` records what was actually acted on, so a Portfolio-Manager
    # resize is visible as quantity != portfolio_requested_quantity rather
    # than as a silently rewritten proposal (D029).
    acted_quantity = (
        result.effective_quantity if result.effective_quantity is not None else proposal.quantity
    )
    order_row = OrderRow(
        broker_id=broker_id,
        symbol=proposal.symbol,
        side=proposal.side,
        quantity=acted_quantity,
        estimated_price=proposal.estimated_price,
        stop_price=proposal.stop_price,
        status=status,
        risk_block_reason=block_reason,
        risk_detail=result.risk_decision.detail,
        portfolio_action=pd.action.value if pd else None,
        portfolio_binding_constraint=(
            pd.binding_constraint.value if pd and pd.binding_constraint else None
        ),
        portfolio_detail=pd.detail if pd else None,
        portfolio_requested_quantity=pd.requested_quantity if pd else None,
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
