"""Loads a PaperBrokerAdapter's state from broker_accounts/broker_positions
at the start of a request and saves it back at the end - see
docs/DECISIONS.md D014.

load_paper_broker() locks the broker_accounts row with SELECT ... FOR
UPDATE, which serializes all trades against one broker within Postgres.
Without that lock, two concurrent trades on the same broker could both
read the same starting cash and both succeed when only one should - the
exact double-spend race a real (even paper) trading system must not
allow (spec's fail-closed posture applies here too, not only to the risk
engine). The lock is held for the rest of the request's transaction, so
save_paper_broker() must be called - and the transaction committed - in
the same request that called load_paper_broker(), never deferred.
"""

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.db.models import BrokerAccount, BrokerPosition
from apps.api.app.execution.paper_broker import PaperBrokerAdapter


async def load_paper_broker(
    session: AsyncSession,
    broker_id: uuid.UUID,
    *,
    default_starting_cash: Decimal,
    allow_short: bool = False,
) -> PaperBrokerAdapter:
    """`allow_short` defaults to False so every existing caller keeps the
    long-only adapter it has always had (Phase 87, D106). It is a property
    of the CALLER, not of the stored broker row: the same paper broker can
    back a long-only strategy deployment and a short-enabled bot, and the
    account state itself does not change either way."""
    account = (
        await session.execute(
            select(BrokerAccount).where(BrokerAccount.broker_id == broker_id).with_for_update()
        )
    ).scalar_one_or_none()

    if account is None:
        account = BrokerAccount(broker_id=broker_id, cash=default_starting_cash)
        session.add(account)
        await session.flush()

    position_rows = (
        await session.execute(select(BrokerPosition).where(BrokerPosition.broker_id == broker_id))
    ).scalars().all()
    positions = {row.symbol: row.quantity for row in position_rows}

    return PaperBrokerAdapter(
        starting_cash=account.cash, positions=positions, allow_short=allow_short
    )


async def save_paper_broker(
    session: AsyncSession, broker_id: uuid.UUID, broker: PaperBrokerAdapter
) -> None:
    account = (
        await session.execute(select(BrokerAccount).where(BrokerAccount.broker_id == broker_id))
    ).scalar_one()
    account.cash = broker.cash

    existing_rows = (
        await session.execute(select(BrokerPosition).where(BrokerPosition.broker_id == broker_id))
    ).scalars().all()
    existing_by_symbol = {row.symbol: row for row in existing_rows}

    for symbol, quantity in broker.positions.items():
        if quantity == 0:
            if symbol in existing_by_symbol:
                await session.delete(existing_by_symbol[symbol])
            continue
        if symbol in existing_by_symbol:
            existing_by_symbol[symbol].quantity = quantity
        else:
            session.add(BrokerPosition(broker_id=broker_id, symbol=symbol, quantity=quantity))
