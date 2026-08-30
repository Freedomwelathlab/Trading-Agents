"""compute_portfolio_snapshot() - deterministic, no LLM, no I/O beyond the
DB read and the caller-supplied marks (spec/docs/TOKEN_POLICY.md's
mandatory-deterministic list explicitly names P&L, exposure, and position
sizing; this closes the "P&L" and "exposure" items for read-only
reporting the same way the risk engine already closes them for trade
validation).

Realized/unrealized P&L method is selectable per call via
CostBasisMethod (D041). AVERAGE remains the default and is unchanged from
D022; FIFO and LIFO were added in Phase 34 as alternatives.

AVERAGE (default, D022) - average-cost basis, the same method
BrokerPosition/BrokerAccount already implicitly use (a single running
quantity and a single running cash balance per symbol, no per-lot
tracking) - see docs/DECISIONS.md D014/D006. Fills are replayed in
filled_at order per symbol:
  - BUY fill: avg_cost = (avg_cost * held_qty + fill_price * fill_qty)
    / (held_qty + fill_qty); held_qty += fill_qty.
  - SELL fill: realized_pnl += (fill_price - avg_cost) * fill_qty;
    held_qty -= fill_qty (avg_cost unchanged by a sell - average-cost
    basis, not FIFO/LIFO).

FIFO / LIFO (D041) - lot tracking. D022 declined these on the grounds
that the schema stores no per-lot rows, which is true of *stored state*
but not of the fill history: `orders`/`fills` already record every
individual buy with its own quantity, price and `filled_at`, and that IS
the lot ledger. FIFO/LIFO are therefore derived, not invented - the
replay builds the open-lot list from those fills and consumes it in the
requested order, deterministically, with no assumption the data doesn't
already establish. Per symbol, in filled_at order:
  - A fill that extends the current direction appends a new open lot at
    its own price.
  - A fill that opposes it consumes open lots - oldest-first under FIFO,
    newest-first under LIFO - realizing (fill_price - lot_price) * qty
    per long lot closed, or (lot_price - fill_price) * qty per short lot
    closed. Any leftover quantity beyond the open lots opens new lots in
    the fill's own direction (a flip through zero), so an over-sell is
    accounted as a short position rather than realized against a
    fabricated zero cost.
  - The returned cost basis is the weighted-average price of the lots
    still open at the end, i.e. the true basis of exactly the quantity
    still held; zero when flat, since no held quantity means no basis.

The three methods produce three different realized-P&L figures for the
same history. That is expected and correct - it is why the choice is
offered - and each is independently hand-verified in
tests/portfolio/test_snapshot.py.

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
from apps.api.app.portfolio.models import (
    CostBasisMethod,
    PortfolioPosition,
    PortfolioSnapshot,
)
from apps.api.app.risk.models import Side


def replay_symbol_fills_average(
    fills: list[tuple[Side, Decimal, Decimal]],
) -> tuple[Decimal, Decimal]:
    """Pure function: given one symbol's fills as (side, quantity,
    fill_price) tuples in execution order, returns (avg_cost,
    realized_pnl) under average-cost basis. See this module's docstring.
    No DB, no I/O - directly unit-testable.

    This is verbatim the D022 implementation and must stay that way: it is
    the default method and its numbers are the backward-compatibility
    contract for every existing caller of GET .../portfolio, the persisted
    snapshots (D027) and the snapshot scheduler (D030)."""
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


def replay_symbol_fills_lots(
    fills: list[tuple[Side, Decimal, Decimal]],
    *,
    newest_first: bool,
) -> tuple[Decimal, Decimal]:
    """Pure function: the FIFO/LIFO lot-tracking replay (D041). Same
    (side, quantity, fill_price)-in-execution-order input and same
    (cost_basis, realized_pnl) output as replay_symbol_fills_average(),
    so the two are interchangeable behind CostBasisMethod.

    `newest_first=False` consumes the oldest open lot first (FIFO);
    `newest_first=True` consumes the newest (LIFO). That single flag is
    the *only* difference between the two methods - keeping them one
    function rather than two near-identical copies means FIFO and LIFO
    cannot drift apart in a later edit.

    Open lots are held as (signed_quantity, price) with the sign carrying
    direction: positive for a long lot, negative for a short one. A fill
    in the same direction as the book appends a lot; an opposing fill
    consumes lots from the chosen end, realizing P&L per lot closed, and
    any quantity left after the book is empty opens lots in the fill's own
    direction (a flip through zero). Nothing is ever realized against a
    basis that was not actually recorded by an earlier fill.

    No DB, no I/O - directly unit-testable."""
    lots: list[tuple[Decimal, Decimal]] = []
    realized_pnl = Decimal(0)

    for side, quantity, fill_price in fills:
        # Signed direction of this fill: +1 for a buy, -1 for a sell. Every
        # branch below is written in terms of the sign so long and short
        # books are handled by one symmetric code path.
        direction = Decimal(1) if side is Side.BUY else Decimal(-1)
        remaining = quantity

        while remaining > 0 and lots:
            lot_qty, lot_price = lots[-1] if newest_first else lots[0]
            if (lot_qty > 0) == (direction > 0):
                # Same direction as the existing book - this fill extends
                # the position rather than closing anything.
                break

            closed = min(remaining, abs(lot_qty))
            # Closing a long lot (lot_qty > 0) realizes sell-minus-cost;
            # closing a short lot realizes cost-minus-buy. One expression:
            lot_sign = Decimal(1) if lot_qty > 0 else Decimal(-1)
            realized_pnl += (fill_price - lot_price) * closed * lot_sign
            remaining -= closed

            leftover_lot = abs(lot_qty) - closed
            if leftover_lot == 0:
                if newest_first:
                    lots.pop()
                else:
                    lots.pop(0)
            else:
                partial = (leftover_lot if lot_qty > 0 else -leftover_lot, lot_price)
                if newest_first:
                    lots[-1] = partial
                else:
                    lots[0] = partial

        if remaining > 0:
            lots.append((direction * remaining, fill_price))

    open_qty = sum((qty for qty, _price in lots), start=Decimal(0))
    if open_qty == 0:
        # Flat: no held quantity, so there is no cost basis to report. The
        # AVERAGE method instead carries its last running average forward
        # here; the two genuinely differ, and reporting a basis for a
        # position that no longer exists would be the fabrication.
        return Decimal(0), realized_pnl

    cost_basis = sum((qty * price for qty, price in lots), start=Decimal(0)) / open_qty
    return cost_basis, realized_pnl


def replay_symbol_fills(
    fills: list[tuple[Side, Decimal, Decimal]],
    *,
    method: CostBasisMethod = CostBasisMethod.AVERAGE,
) -> tuple[Decimal, Decimal]:
    """Dispatches one symbol's fill replay to the requested cost-basis
    method, returning (cost_basis, realized_pnl). Defaults to AVERAGE, so
    every pre-D041 call site keeps its exact D022 behaviour untouched.
    Pure - no DB, no I/O."""
    if method is CostBasisMethod.AVERAGE:
        return replay_symbol_fills_average(fills)
    return replay_symbol_fills_lots(fills, newest_first=method is CostBasisMethod.LIFO)


async def _replay_fills(
    session: AsyncSession,
    broker_id: uuid.UUID,
    *,
    method: CostBasisMethod = CostBasisMethod.AVERAGE,
) -> dict[str, tuple[Decimal, Decimal]]:
    """Returns {symbol: (cost_basis, realized_pnl)} for every symbol that
    has ever had a fill on this broker, from replaying that symbol's fills
    (in execution order) through replay_symbol_fills() under `method`."""
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
        symbol: replay_symbol_fills(fills, method=method)
        for symbol, fills in fills_by_symbol.items()
    }


async def compute_portfolio_snapshot(
    broker_id: uuid.UUID,
    session: AsyncSession,
    *,
    marks: dict[str, Decimal],
    default_starting_cash: Decimal | None = None,
    cost_basis_method: CostBasisMethod = CostBasisMethod.AVERAGE,
) -> PortfolioSnapshot:
    """`cost_basis_method` selects how `avg_cost`, `unrealized_pnl` and
    `realized_pnl` are derived from the fill history (D041). It defaults to
    AVERAGE, which is exactly the D022 behaviour, so every existing caller -
    including the persisted-snapshot POST (D027) and the snapshot scheduler
    (D030), neither of which passes it - is unaffected. Cash, current
    quantities and `current_value` are identical under all three methods;
    only the basis-derived figures differ.

    `marks` must carry a current price for every symbol this broker
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

    fills_by_symbol = await _replay_fills(session, broker_id, method=cost_basis_method)

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
