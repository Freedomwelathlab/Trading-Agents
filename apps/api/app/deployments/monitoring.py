"""Strategy-deployment monitoring (Phase 65, D083) - read-only reporting of
one `StrategyDeployment`'s REAL performance next to the strategy version's
own REAL backtest, never blended and never fabricated when data is missing.

This module places no orders and writes nothing to the database. It answers
one question an operator of a live (paper, today) deployment actually has:
"is this thing doing what the backtest said it would?" - by building two
numbers from two entirely separate, honest sources and handing them back
side by side.

WHY "ACTUAL" IS BUILT ONLY FROM THIS DEPLOYMENT'S OWN ORDERS
-------------------------------------------------------------
`build_deployment_monitoring` joins `orders` to `strategy_deployment_runs`
and filters to `deployment_id == deployment.id` - never "every order this
broker account has ever seen." A broker account can hold positions and
orders from other sources (a human trading the same paper account through
`POST /brokers/{id}/trades`, a different deployment sharing the account, a
future path this codebase doesn't have yet). Reporting "actual performance"
off the whole account would silently attribute someone else's trade to this
strategy's track record - exactly the kind of fabrication
docs/TRADING_SAFETY.md forbids, just one level removed from inventing a
number outright. Only `FILLED` orders whose `deployment_run_id` traces back
to a `strategy_deployment_runs` row for THIS deployment ever enter the
calculation.

WHY ROUND TRIPS USE THE FILL'S PRICE/TIME, NOT THE PROPOSAL'S ESTIMATE
------------------------------------------------------------------------
`Order.estimated_price` is what the proposal was priced at when the Risk
Engine evaluated it - the latest bar's close, per `deployments/runner.py`.
It is not what the paper broker actually executed the order at. `Fill` is
the broker's own answer (`fill_price`, `filled_at`), the same distinction
`execution/reconciliation.py` draws for the live path ("a fill is written
only from the broker's own answer"). "Actual performance" that quietly used
the estimate instead of the fill would not be actual performance at all -
it would be a second copy of the proposal, dressed up as a result.

WHY THE REFERENCE BACKTEST IS `_latest_succeeded_backtest`, REUSED VERBATIM
------------------------------------------------------------------------------
`apps.api.app.strategies.scoring._latest_succeeded_backtest` is the exact
function the Phase 59 leaderboard uses to answer "what is this version's
headline backtest result" - the newest SUCCEEDED, non-walk-forward-window
`BacktestRun` for the version. Reusing it here (the D072 cross-module
private-helper-reuse precedent `deployments/runner.py` and
`strategies/walk_forward.py` already set) means this module can never
disagree with the leaderboard, the strategy detail page, or any other
surface about which backtest is "the" backtest for a version. Writing a
second, monitoring-specific query that picked a different run - even one
that filtered by symbol - would create two competing definitions of
"this strategy's backtest" in the same codebase, and a reader comparing two
screens would have no way to know they were looking at different things.

**A known, deliberate scope limit, not glossed over**: a deployment can
trade several symbols (`StrategyDeployment.symbols`), but
`_latest_succeeded_backtest` returns ONE run, over ONE symbol
(`BacktestRun.symbol`). `expected.symbol` names exactly which symbol the
comparison numbers describe; the caller must not read "expected" as
"expected for every symbol this deployment trades," only "expected for the
symbol the version was last backtested on." A per-symbol reference backtest
lookup is future work, not something this phase silently pretends already
exists by omitting the field.

WHY A RATE IS `None`, NEVER A FABRICATED NUMBER, WHEN THERE ARE ZERO ROUND
TRIPS YET
------------------------------------------------------------------------------
A brand-new deployment - or one that has only ever bought and never sold -
has zero completed round trips. `num_winning / num_round_trips` is undefined
at that point, not zero: reporting `0%` would read as "every trade so far
has lost," and reporting `100%` would read the opposite way, and both would
be inventing a verdict this system has no evidence for. `win_rate_pct` and
`avg_return_pct` are `None` in exactly that case - the same "sentinel, not a
guess" posture docs/TRADING_SAFETY.md requires for missing market data,
applied to a missing statistic instead. `total_realized_pnl` stays `0` with
zero round trips because it is a true sum over an empty set, not a rate -
"the realized P&L of no trades is zero" is an honest statement, unlike
"the win rate of no trades is 0%."
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.db.models import (
    Fill,
    Order,
    OrderStatus,
    StrategyDeployment,
    StrategyDeploymentRun,
)
from apps.api.app.deployments.runner import utc_now
from apps.api.app.risk.models import Side
from apps.api.app.strategies.scoring import _latest_succeeded_backtest

_QUANTUM = Decimal("0.0001")
"""Four decimal places, the same precision `strategies/scoring.py` quantizes
its own percentages to - so a percentage this module reports and one the
scoring module reports round the same way."""


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class RoundTrip:
    """One closed position: a BUY fill followed by a SELL fill in the same
    symbol, for THIS deployment's own orders. `quantity`, `entry_price` and
    `entered_at` describe the opening (BUY) fill; `exit_price`/`exited_at`
    the closing (SELL) fill - both real fills, never the proposal's
    estimated price."""

    symbol: str
    quantity: Decimal
    entry_price: Decimal
    entered_at: datetime
    exit_price: Decimal
    exited_at: datetime
    realized_pnl: Decimal
    return_pct: Decimal


@dataclass(frozen=True)
class OpenLot:
    """A position this deployment opened and has not yet closed, carrying
    the REAL entry fill's price and time - the same provenance discipline
    `RoundTrip` applies to closed positions.

    Phase 69 (D087) needs the entry price, not just the quantity: an
    unattended live runner's capital ceiling is denominated in committed
    cost basis, and its unrealized-P&L circuit breaker marks each open lot
    against the latest bar. Both are unanswerable from quantity alone.
    `build_deployment_monitoring` continues to expose only the quantities
    (`DeploymentActualPerformance.open_positions`), so Phase 65's public
    monitoring shape is unchanged by this."""

    quantity: Decimal
    entry_price: Decimal
    entered_at: datetime

    @property
    def cost_basis(self) -> Decimal:
        return self.entry_price * self.quantity


def _build_round_trips(
    order_fills: Sequence[tuple[Order, Fill]],
) -> tuple[list[RoundTrip], dict[str, OpenLot]]:
    """Walk `(Order, Fill)` pairs - already filtered to `status == FILLED`
    and ordered by `Order.submitted_at` ascending by the caller - maintaining
    one open lot per symbol, and return the closed round trips plus the
    symbols still holding an open lot at the end (for `open_positions`).

    Query shape: the caller joins `orders` to `fills` on `order_id` (a 1:1
    relationship today, per `Fill`'s docstring) so this function never needs
    a session of its own - it is pure, in-memory walking logic, easy to unit
    test with hand-built rows and no database.

    This runner's own long-or-flat discipline (`want_buy`/`want_sell` in
    `deployments/runner.py`) means a BUY only ever fires while flat and a
    SELL only ever fires while holding a lot, so a BUY-while-open or a
    SELL-while-flat should never appear in this deployment's own order
    history. This function does not assume that discipline held, though: if
    either happens anyway, the offending order is SKIPPED rather than
    raising, and it is never merged into an existing open lot by averaging
    or otherwise combining quantities - this system does not fabricate a
    cost basis it did not explicitly choose to compute (D083).
    """
    open_lots: dict[str, tuple[Decimal, Decimal, datetime]] = {}
    round_trips: list[RoundTrip] = []

    for order, fill in order_fills:
        symbol = order.symbol
        if order.side is Side.BUY:
            if symbol in open_lots:
                # A BUY while a lot is already open for this symbol cannot
                # happen from this runner's own discipline. Skip it rather
                # than average it into the existing lot.
                continue
            open_lots[symbol] = (fill.quantity, fill.fill_price, fill.filled_at)
        elif order.side is Side.SELL:
            lot = open_lots.pop(symbol, None)
            if lot is None:
                # A SELL with no open lot cannot happen from this runner's
                # own discipline either. Skip it rather than inventing an
                # entry price for a position that, by this order history,
                # was never opened.
                continue
            quantity, entry_price, entered_at = lot
            exit_price = fill.fill_price
            exited_at = fill.filled_at
            realized_pnl = (exit_price - entry_price) * quantity
            return_pct = (exit_price - entry_price) / entry_price * Decimal(100)
            round_trips.append(
                RoundTrip(
                    symbol=symbol,
                    quantity=quantity,
                    entry_price=entry_price,
                    entered_at=entered_at,
                    exit_price=exit_price,
                    exited_at=exited_at,
                    realized_pnl=realized_pnl,
                    return_pct=return_pct,
                )
            )

    remaining = {
        symbol: OpenLot(quantity=quantity, entry_price=entry_price, entered_at=entered_at)
        for symbol, (quantity, entry_price, entered_at) in open_lots.items()
    }
    return round_trips, remaining


@dataclass(frozen=True)
class DeploymentActualPerformance:
    """What THIS deployment's own orders actually did - built strictly from
    real fills, never blended with the reference backtest."""

    round_trips: tuple[RoundTrip, ...]
    open_positions: dict[str, Decimal]
    num_round_trips: int
    num_winning: int
    win_rate_pct: Decimal | None
    """`None` when `num_round_trips == 0` - never a fabricated 0% or 100%
    (see module docstring)."""
    total_realized_pnl: Decimal
    """A true sum of zero items is `0` when there are no round trips - this
    is a sum, not a rate, so `0` is the honest answer rather than a
    sentinel."""
    avg_return_pct: Decimal | None
    """`None` when `num_round_trips == 0`, for the same reason as
    `win_rate_pct`."""


@dataclass(frozen=True)
class DeploymentExpectedPerformance:
    """What the strategy version's own latest real backtest reported - a
    read of `BacktestRun`, never a re-derived or re-estimated number.

    `status == "no_reference_backtest"` means every other field is `None`;
    there is no partial or estimated fallback."""

    status: Literal["available", "no_reference_backtest"]
    reference_backtest_run_id: uuid.UUID | None
    symbol: str | None
    total_return_pct: Decimal | None
    max_drawdown_pct: Decimal | None
    win_rate_pct: Decimal | None
    num_trades: int | None


@dataclass(frozen=True)
class DeploymentMonitoringResult:
    deployment_id: uuid.UUID
    as_of: datetime
    actual: DeploymentActualPerformance
    expected: DeploymentExpectedPerformance


async def build_deployment_monitoring(
    session: AsyncSession,
    deployment: StrategyDeployment,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> DeploymentMonitoringResult:
    """Build the read-only monitoring report for one deployment. Places no
    orders, writes nothing - a pure read composed from two real sources:
    this deployment's own filled orders (`actual`) and its strategy
    version's own latest succeeded backtest (`expected`)."""
    rows = (
        await session.execute(
            select(Order, Fill)
            .join(Fill, Fill.order_id == Order.id)
            .join(
                StrategyDeploymentRun,
                StrategyDeploymentRun.id == Order.deployment_run_id,
            )
            .where(
                StrategyDeploymentRun.deployment_id == deployment.id,
                Order.status == OrderStatus.FILLED,
            )
            .order_by(Order.submitted_at.asc(), Order.id.asc())
        )
    ).all()
    order_fills: list[tuple[Order, Fill]] = [(row[0], row[1]) for row in rows]

    round_trips, open_lots = _build_round_trips(order_fills)
    # Phase 65's reported shape is quantities only; the lots' entry prices
    # exist for Phase 69's capital guard and are deliberately not widened
    # into this report's API.
    open_positions = {symbol: lot.quantity for symbol, lot in open_lots.items()}

    num_round_trips = len(round_trips)
    num_winning = sum(1 for rt in round_trips if rt.realized_pnl > 0)
    total_realized_pnl = sum((rt.realized_pnl for rt in round_trips), start=Decimal(0))

    if num_round_trips > 0:
        win_rate_pct: Decimal | None = _quantize(
            Decimal(num_winning) / Decimal(num_round_trips) * Decimal(100)
        )
        avg_return_pct: Decimal | None = sum(
            (rt.return_pct for rt in round_trips), start=Decimal(0)
        ) / Decimal(num_round_trips)
    else:
        win_rate_pct = None
        avg_return_pct = None

    actual = DeploymentActualPerformance(
        round_trips=tuple(round_trips),
        open_positions=open_positions,
        num_round_trips=num_round_trips,
        num_winning=num_winning,
        win_rate_pct=win_rate_pct,
        total_realized_pnl=total_realized_pnl,
        avg_return_pct=avg_return_pct,
    )

    backtest_run = await _latest_succeeded_backtest(session, deployment.strategy_version_id)
    if backtest_run is None:
        expected = DeploymentExpectedPerformance(
            status="no_reference_backtest",
            reference_backtest_run_id=None,
            symbol=None,
            total_return_pct=None,
            max_drawdown_pct=None,
            win_rate_pct=None,
            num_trades=None,
        )
    else:
        expected = DeploymentExpectedPerformance(
            status="available",
            reference_backtest_run_id=backtest_run.id,
            symbol=backtest_run.symbol,
            total_return_pct=backtest_run.total_return_pct,
            max_drawdown_pct=backtest_run.max_drawdown_pct,
            win_rate_pct=backtest_run.win_rate_pct,
            num_trades=backtest_run.num_trades,
        )

    return DeploymentMonitoringResult(
        deployment_id=deployment.id,
        as_of=clock(),
        actual=actual,
        expected=expected,
    )
