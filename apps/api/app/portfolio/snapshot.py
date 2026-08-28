"""compute_portfolio_snapshot() - deterministic, no LLM, no I/O beyond the
DB read and the caller-supplied marks (spec/docs/TOKEN_POLICY.md's
mandatory-deterministic list explicitly names P&L, exposure, and position
sizing; this closes the "P&L" and "exposure" items for read-only
reporting the same way the risk engine already closes them for trade
validation).

Realized/unrealized P&L method: average-cost basis, chosen because it's
the same method BrokerPosition/BrokerAccount already implicitly use (a
single running quantity and a single running cash balance per symbol, no
per-lot tracking) - see docs/DECISIONS.md D014/D006. FIFO or LIFO lot
tracking would require storing which specific buy lot a sell closes,
which no table here does; average-cost is the only method this data
model can compute without inventing lot assignments that were never
recorded. Fills are replayed in filled_at order per symbol:
  - BUY fill: avg_cost = (avg_cost * held_qty + fill_price * fill_qty)
    / (held_qty + fill_qty); held_qty += fill_qty.
  - SELL fill: realized_pnl += (fill_price - avg_cost) * fill_qty;
    held_qty -= fill_qty (avg_cost unchanged by a sell - average-cost
    basis, not FIFO/LIFO).
Current quantity is taken from BrokerPosition (the authoritative current
state broker_accounts/broker_positions already establish - see D014's
docstrings), not from the fills replay total, so a snapshot always
matches what the execution layer itself believes it holds even if fills
history and current state ever disagree for a reason outside this
module's control (e.g. a manually corrected position row). avg_cost and
realized_pnl still come from the fills replay, since those two figures
have no other source of truth in this schema.
"""

import uuid
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.db.models import BrokerAccount, BrokerPosition
from apps.api.app.db.models import Fill as FillRow
from apps.api.app.db.models import Order as OrderRow
from apps.api.app.portfolio.errors import BrokerAccountNotFoundError, MissingMarkError
from apps.api.app.portfolio.models import PortfolioPosition, PortfolioSnapshot
from apps.api.app.risk.models import Side


def replay_symbol_fills(
    fills: list[tuple[Side, Decimal, Decimal]],
) -> tuple[Decimal, Decimal]:
    """Pure function: given one symbol's fills as (side, quantity,
    fill_price) tuples in execution order, returns (avg_cost,
    realized_pnl) after replaying all of them. See this module's
    docstring for the average-cost-basis method and why it was chosen.
    No DB, no I/O - directly unit-testable."""
    held_qty = Decimal(0)
    avg_cost = Decimal(0)
    realized_pnl = Decimal(0)

    for side, quantity, fill_price in fills:
        if side is Side.BUY:
            new_qty = held_qty + quantity
            avg_cost = (
                (avg_cost * held_qty + fill_price * quantity) / new_qty
                if new_qty != 0
                else Decimal(0)
            )
            held_qty = new_qty
        else:
            realized_pnl += (fill_price - avg_cost) * quantity
            held_qty -= quantity

    return avg_cost, realized_pnl


async def _replay_fills(
    session: AsyncSession, broker_id: uuid.UUID
) -> dict[str, tuple[Decimal, Decimal]]:
    """Returns {symbol: (avg_cost, realized_pnl)} for every symbol that has
    ever had a fill on this broker, from replaying that symbol's fills (in
    execution order) through replay_symbol_fills()."""
    rows = (
        await session.execute(
            select(OrderRow.symbol, OrderRow.side, FillRow.quantity, FillRow.fill_price)
            .join(FillRow, FillRow.order_id == OrderRow.id)
            .where(OrderRow.broker_id == broker_id)
            .order_by(FillRow.filled_at.asc())
        )
    ).all()

    fills_by_symbol: dict[str, list[tuple[Side, Decimal, Decimal]]] = defaultdict(list)
    for symbol, side, quantity, fill_price in rows:
        fills_by_symbol[symbol].append((side, quantity, fill_price))

    return {
        symbol: replay_symbol_fills(fills) for symbol, fills in fills_by_symbol.items()
    }


async def compute_portfolio_snapshot(
    broker_id: uuid.UUID,
    session: AsyncSession,
    *,
    marks: dict[str, Decimal],
    default_starting_cash: Decimal | None = None,
) -> PortfolioSnapshot:
    """`marks` must carry a current price for every symbol this broker
    currently holds a nonzero position in - raises MissingMarkError
    otherwise, never a guessed/stale price (spec Sec57, same discipline as
    PaperBrokerAdapter.get_account_state).

    `default_starting_cash`, if given, is used only when the broker has no
    broker_accounts row yet (a broker that has never had a trade loaded
    against it - see apps/api/app/execution/persistence.py's lazy seeding).
    It is never persisted here; this module only reports, never writes.
    Without it, a missing broker_accounts row raises
    BrokerAccountNotFoundError rather than silently reporting zero cash.
    """
    account = (
        await session.execute(select(BrokerAccount).where(BrokerAccount.broker_id == broker_id))
    ).scalar_one_or_none()

    if account is None:
        if default_starting_cash is None:
            raise BrokerAccountNotFoundError(
                f"No broker_accounts row for broker {broker_id} and no "
                "default_starting_cash was supplied."
            )
        cash = default_starting_cash
    else:
        cash = account.cash

    position_rows = (
        await session.execute(select(BrokerPosition).where(BrokerPosition.broker_id == broker_id))
    ).scalars().all()

    fills_by_symbol = await _replay_fills(session, broker_id)

    positions: list[PortfolioPosition] = []
    total_unrealized_pnl = Decimal(0)
    total_current_value = Decimal(0)

    for row in position_rows:
        if row.quantity == 0:
            continue
        if row.symbol not in marks:
            raise MissingMarkError(
                f"No mark supplied for open position {row.symbol!r}; cannot value account."
            )
        avg_cost, realized_pnl = fills_by_symbol.get(row.symbol, (Decimal(0), Decimal(0)))
        mark = marks[row.symbol]
        current_value = row.quantity * mark
        unrealized_pnl = (mark - avg_cost) * row.quantity

        positions.append(
            PortfolioPosition(
                symbol=row.symbol,
                quantity=row.quantity,
                avg_cost=avg_cost,
                current_value=current_value,
                unrealized_pnl=unrealized_pnl,
                realized_pnl=realized_pnl,
            )
        )
        total_unrealized_pnl += unrealized_pnl
        total_current_value += current_value

    # Realized P&L is reported across every symbol ever traded, including
    # ones with no open position left - a closed-out symbol's history
    # doesn't disappear just because PortfolioPosition only lists open
    # ones above.
    total_realized_pnl = sum((pnl for _avg, pnl in fills_by_symbol.values()), start=Decimal(0))

    return PortfolioSnapshot(
        broker_id=broker_id,
        cash=cash,
        positions=positions,
        total_equity=cash + total_current_value,
        total_unrealized_pnl=total_unrealized_pnl,
        total_realized_pnl=total_realized_pnl,
    )
