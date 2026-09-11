"""Phase 69 (docs/DECISIONS.md D087). The gate every unattended live
trading cycle must clear before any real order is placed.

Two independent questions, deliberately answered by two separate functions
in a fixed order, because they fail for opposite reasons and an operator
needs to be able to tell them apart:

  1. `evaluate_live_arming` - is unattended live execution ARMED at all?
     Purely a configuration question. Reads settings, touches no database,
     calls no broker. Answers "no" for every default checkout.

  2. `evaluate_live_capital` - given that it is armed, do this deployment's
     real, persisted positions and P&L still fit inside the operator's
     capital bounds? Purely a data question, computed from real fills every
     cycle.

Arming is checked FIRST and cheaply, so a disabled robot never reads a
single row, and the refusal it produces is the one that is actually true of
it ("you did not arm this") regardless of what the book happens to look
like. This mirrors the ordering `api/routes/trades.py::_authorize_live_trade`
chose for the interactive live path in Phase 43 (D058), and for the same
reason.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
It never places, cancels, or resizes an order, and it never closes a
position. A breached circuit breaker PAUSES the deployment and stops new
entries; it does not liquidate. Auto-liquidating on the day a loss limit
fires is how a bad hour is converted into a realized loss at whatever price
the market happens to be offering during the event that tripped the
breaker. The positions stay, the robot stops, a human decides. Re-arming is
a human action (resume the deployment) - which is the entire point of a
breaker, as opposed to a filter.

It also never fabricates a figure. Every number below is derived from real
`Fill` rows and real ingested bars. When a held symbol cannot be marked
because no bar exists for it, this module reports that it cannot evaluate
the breakers rather than marking the position at cost, at zero, or at the
last price it happens to remember - consistent with docs/TRADING_SAFETY.md
and with how `runner.py`'s paper path already handles an unpriceable
position.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.core.config import Settings, TradingMode
from apps.api.app.db.models import (
    Fill,
    Order,
    OrderStatus,
    StrategyDeployment,
    StrategyDeploymentRun,
)

if TYPE_CHECKING:
    # `deployments/monitoring.py` imports `utc_now` from `deployments/
    # runner.py`, and `runner.py` imports THIS module - so a module-level
    # import of monitoring here closes the cycle
    # runner -> live_guard -> monitoring -> runner.
    #
    # Resolved the way `runner.py::_check_and_record_drift` already
    # resolves the identical shape for `deployments/drift.py` (D084): the
    # type-only name comes in under TYPE_CHECKING (free at runtime, and
    # `from __future__ import annotations` above makes the annotations
    # strings), and the one runtime dependency is imported inside the
    # function that needs it.
    from apps.api.app.deployments.monitoring import OpenLot


class LiveArmingStatus(str, enum.Enum):  # noqa: UP042 (str mixin for interop)
    """Whether unattended live execution is armed, and if not, which gate
    said no. Every value except `ARMED` resolves the cycle to
    `SKIPPED_LIVE_TRADING_DISABLED`."""

    ARMED = "armed"
    NOT_ARMED = "not_armed"
    """`STRATEGY_LIVE_AUTO_EXECUTION_ENABLED` is false - the default, and
    the answer for every checkout nobody has deliberately armed."""
    LIVE_TRADING_DISABLED = "live_trading_disabled"
    """The robot switch is on but `TRADING_MODE`/`LIVE_TRADING_ENABLED` are
    not. Startup validation (`Settings._enforce_live_auto_execution_is_
    fully_configured`) normally makes this unreachable; it is re-checked
    here anyway rather than assumed, because this is the last gate before
    real money and a settings object can be constructed directly in a test
    or a script without passing through app startup."""
    NOT_CONFIGURED = "not_configured"
    """Armed, but a capital bound is missing. Same belt-and-braces
    reasoning as above."""
    NO_LIVE_BROKER = "no_live_broker"
    """Armed and configured, but `build_live_broker_adapter` returned None -
    the live credential trio is absent or incomplete. Fails closed: an
    absent broker is never substituted with a paper one, which would place
    simulated orders while the audit trail said `live`."""


@dataclass(frozen=True)
class LiveArmingDecision:
    status: LiveArmingStatus
    detail: str

    @property
    def armed(self) -> bool:
        return self.status is LiveArmingStatus.ARMED


def evaluate_live_arming(
    settings: Settings, *, live_broker_available: bool
) -> LiveArmingDecision:
    """Configuration-only gate. No I/O.

    `live_broker_available` is passed in rather than built here so this
    function stays pure and trivially testable, and so the caller controls
    when the (credential-reading, network-constructing) adapter is built.
    """
    if not settings.strategy_live_auto_execution_enabled:
        return LiveArmingDecision(
            status=LiveArmingStatus.NOT_ARMED,
            detail=(
                "unattended live execution is not armed: "
                "STRATEGY_LIVE_AUTO_EXECUTION_ENABLED is false. No live order was "
                "placed and no broker was contacted."
            ),
        )

    if settings.trading_mode is not TradingMode.LIVE or not settings.live_trading_enabled:
        return LiveArmingDecision(
            status=LiveArmingStatus.LIVE_TRADING_DISABLED,
            detail=(
                "unattended live execution is armed but TRADING_MODE=live and "
                "LIVE_TRADING_ENABLED=true are not both set. Refusing to place a live "
                "order through a live path that is itself disabled."
            ),
        )

    if settings.strategy_live_total_capital is None or (
        settings.strategy_live_capital_per_trade is None
    ):
        return LiveArmingDecision(
            status=LiveArmingStatus.NOT_CONFIGURED,
            detail=(
                "unattended live execution is armed but STRATEGY_LIVE_TOTAL_CAPITAL "
                "and/or STRATEGY_LIVE_CAPITAL_PER_TRADE is unset. Refusing to trade "
                "real money with no capital bound."
            ),
        )

    if not live_broker_available:
        return LiveArmingDecision(
            status=LiveArmingStatus.NO_LIVE_BROKER,
            detail=(
                "unattended live execution is armed but no live broker could be "
                "constructed: the LONGPORT_LIVE_* credential trio is absent or "
                "incomplete. No paper broker was substituted."
            ),
        )

    return LiveArmingDecision(
        status=LiveArmingStatus.ARMED,
        detail="unattended live execution is armed and fully configured",
    )


class LiveCapitalStatus(str, enum.Enum):  # noqa: UP042 (str mixin for interop)
    """The outcome of the capital circuit breakers for one live cycle."""

    OK = "ok"
    """Within every bound. New entries are allowed this cycle."""
    DAILY_LOSS_BREACHED = "daily_loss_breached"
    TOTAL_LOSS_BREACHED = "total_loss_breached"
    UNPRICEABLE_POSITION = "unpriceable_position"
    """A position this deployment believes it holds cannot be marked, so the
    breakers cannot be evaluated honestly. Treated as a HALT, not as a pass:
    a breaker that cannot see the book must not wave the robot through. It
    does NOT pause the deployment, though - an un-ingested bar is an
    operational gap, not a loss event, and pausing on it would turn a stale
    data feed into a silent trading stop that looks like a risk decision.

    This fires in two situations, and usefully so in both. The obvious one
    is a missing bar. The second is a DIVERGENCE between the two books: the
    runner's `marks` are built from the symbols the BROKER reports holding,
    while `open_lots` comes from the orders THIS DEPLOYMENT placed. If a
    human closed one of the robot's positions by hand at the broker, the
    deployment's own history still shows it open, the symbol has no mark,
    and the robot stops instead of continuing to reason about a position it
    no longer owns. Halting when the two books disagree is the correct
    answer to that, and it is the same instinct
    `execution/reconciliation.py` applies to the interactive live path."""


@dataclass(frozen=True)
class LiveCapitalDecision:
    """The full computed picture, not just a verdict - every figure the
    decision rests on is returned so it can be logged and audited rather
    than recomputed or guessed at by the caller."""

    status: LiveCapitalStatus
    detail: str
    deployed_cost_basis: Decimal
    """Cost basis of positions this deployment currently holds, from real
    entry fills."""
    realized_pnl_today: Decimal
    unrealized_pnl: Decimal
    realized_pnl_total: Decimal
    open_position_count: int
    remaining_capital: Decimal
    """`strategy_live_total_capital` minus `deployed_cost_basis`, floored at
    zero. The most cost basis a new entry this cycle may commit, before the
    per-trade cap is also applied."""

    @property
    def halted(self) -> bool:
        return self.status is not LiveCapitalStatus.OK

    @property
    def pauses_deployment(self) -> bool:
        """Whether this outcome should pause the deployment, as opposed to
        merely skipping this cycle. Loss breaches pause; an unpriceable
        position does not (see `UNPRICEABLE_POSITION`)."""
        return self.status in (
            LiveCapitalStatus.DAILY_LOSS_BREACHED,
            LiveCapitalStatus.TOTAL_LOSS_BREACHED,
        )


async def _live_order_fills(
    session: AsyncSession, deployment: StrategyDeployment
) -> list[tuple[Order, Fill]]:
    """This deployment's own filled orders, joined to their fills, oldest
    first - the identical query shape `build_deployment_monitoring` uses,
    and for the identical reason: an unattended robot's capital ceiling must
    be computed from the orders IT placed, never from the whole brokerage
    account, which may hold positions a human opened by hand or another
    deployment opened for its own reasons."""
    rows = (
        await session.execute(
            select(Order, Fill)
            .join(Fill, Fill.order_id == Order.id)
            .join(StrategyDeploymentRun, StrategyDeploymentRun.id == Order.deployment_run_id)
            .where(
                StrategyDeploymentRun.deployment_id == deployment.id,
                Order.status == OrderStatus.FILLED,
            )
            .order_by(Order.submitted_at.asc(), Order.id.asc())
        )
    ).all()
    return [(row[0], row[1]) for row in rows]


def _unrealized(
    open_lots: dict[str, OpenLot], marks: dict[str, Decimal]
) -> Decimal | None:
    """Mark-to-market on open lots. Returns None - never a partial sum and
    never a zero - if ANY lot cannot be marked, because a partial total
    would understate the loss the breakers are there to catch, in the
    direction that keeps the robot trading."""
    total = Decimal(0)
    for symbol, lot in open_lots.items():
        mark = marks.get(symbol)
        if mark is None:
            return None
        total += (mark - lot.entry_price) * lot.quantity
    return total


async def evaluate_live_capital(
    session: AsyncSession,
    deployment: StrategyDeployment,
    *,
    marks: dict[str, Decimal],
    settings: Settings,
    clock: Callable[[], datetime],
) -> LiveCapitalDecision:
    """Evaluate every capital circuit breaker for one live deployment.

    Reads only. Writes nothing, places nothing, pauses nothing - the caller
    acts on the returned decision, so this function stays testable without
    a broker and without a transaction to roll back.

    `clock` is required, with no default. The runner always has one (its
    cycle clock, which tests drive), and defaulting it here would mean
    importing `utc_now` from `runner.py` - which imports this module - or
    adding a fourth private copy of it to this codebase. Neither is worth
    saving one keyword argument at the single call site.

    `marks` are the latest ingested closes the runner has already loaded for
    this cycle; they are passed in rather than re-queried so the breakers
    and the trading decision that follows reason about exactly the same
    prices, the same way `LiveBrokerAdapter` caches one account snapshot per
    request so every gate sees one book (D058).

    DAILY LOSS IS MEASURED CONSERVATIVELY, ON PURPOSE. `realized_pnl_today`
    counts round trips CLOSED today; `unrealized_pnl` counts every open lot
    regardless of when it was opened. Their sum therefore charges a
    multi-day open loss against today's breaker on each day it persists,
    rather than only on the day it moved. A strict day-over-day measure
    would need a persisted start-of-day equity snapshot, which does not
    exist for live deployments. Given the choice between a breaker that
    fires somewhat early and one that needs a number this system cannot
    honestly produce, a control whose job is to stop losses should err
    toward firing early, and should say so rather than implying a precision
    it does not have.
    """
    from apps.api.app.deployments.monitoring import _build_round_trips

    total_capital = settings.strategy_live_total_capital
    assert total_capital is not None  # nosec - arming gate checked this first

    order_fills = await _live_order_fills(session, deployment)
    round_trips, open_lots = _build_round_trips(order_fills)

    deployed_cost_basis = sum(
        (lot.cost_basis for lot in open_lots.values()), start=Decimal(0)
    )
    realized_pnl_total = sum((rt.realized_pnl for rt in round_trips), start=Decimal(0))

    today = clock().astimezone(UTC).date()
    realized_pnl_today = sum(
        (
            rt.realized_pnl
            for rt in round_trips
            if rt.exited_at.astimezone(UTC).date() == today
        ),
        start=Decimal(0),
    )

    unrealized = _unrealized(open_lots, marks)
    remaining_capital = max(Decimal(0), total_capital - deployed_cost_basis)

    if unrealized is None:
        unmarked = sorted(symbol for symbol in open_lots if symbol not in marks)
        return LiveCapitalDecision(
            status=LiveCapitalStatus.UNPRICEABLE_POSITION,
            detail=(
                f"held position(s) {', '.join(unmarked)} have no ingested bar to mark "
                "against, so the live capital circuit breakers cannot be evaluated. "
                "Refusing to trade rather than marking the position at cost or "
                "assuming no loss."
            ),
            deployed_cost_basis=deployed_cost_basis,
            realized_pnl_today=realized_pnl_today,
            unrealized_pnl=Decimal(0),
            realized_pnl_total=realized_pnl_total,
            open_position_count=len(open_lots),
            remaining_capital=remaining_capital,
        )

    def decide(status: LiveCapitalStatus, detail: str) -> LiveCapitalDecision:
        return LiveCapitalDecision(
            status=status,
            detail=detail,
            deployed_cost_basis=deployed_cost_basis,
            realized_pnl_today=realized_pnl_today,
            unrealized_pnl=unrealized,
            realized_pnl_total=realized_pnl_total,
            open_position_count=len(open_lots),
            remaining_capital=remaining_capital,
        )

    daily_pnl = realized_pnl_today + unrealized
    daily_limit = -(total_capital * settings.strategy_live_max_daily_loss_pct / Decimal(100))
    if daily_pnl <= daily_limit:
        return decide(
            LiveCapitalStatus.DAILY_LOSS_BREACHED,
            (
                f"daily loss circuit breaker: net P&L {daily_pnl} (realized today "
                f"{realized_pnl_today} + unrealized {unrealized}) is at or beyond the "
                f"limit {daily_limit} "
                f"({settings.strategy_live_max_daily_loss_pct}% of {total_capital}). "
                "Deployment paused; open positions were NOT liquidated."
            ),
        )

    total_pnl = realized_pnl_total + unrealized
    total_limit = -(total_capital * settings.strategy_live_max_total_loss_pct / Decimal(100))
    if total_pnl <= total_limit:
        return decide(
            LiveCapitalStatus.TOTAL_LOSS_BREACHED,
            (
                f"total loss circuit breaker: cumulative net P&L {total_pnl} (realized "
                f"{realized_pnl_total} + unrealized {unrealized}) is at or beyond the "
                f"limit {total_limit} "
                f"({settings.strategy_live_max_total_loss_pct}% of {total_capital}). "
                "Deployment paused; open positions were NOT liquidated."
            ),
        )

    return decide(LiveCapitalStatus.OK, "within every live capital bound")


def live_entry_budget(
    decision: LiveCapitalDecision, *, settings: Settings
) -> Decimal:
    """The most cost basis ONE new live entry may commit right now: the
    smaller of what is left of the total capital allowance and the
    per-trade cap.

    Returned as cost basis (currency), not a quantity - the caller converts
    using the same price it is about to trade at, so the cap binds against
    the real price rather than an estimate made here.
    """
    per_trade = settings.strategy_live_capital_per_trade
    assert per_trade is not None  # nosec - arming gate checked this first
    return min(decision.remaining_capital, per_trade)
