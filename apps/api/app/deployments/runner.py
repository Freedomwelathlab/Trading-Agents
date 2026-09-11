"""The strategy-deployment runner (Phase 63) - an in-process periodic task,
owned by the FastAPI lifespan, that re-evaluates every ACTIVE
`StrategyDeployment` and submits paper trades when the strategy's own rules
say to.

WHAT THIS IS, EXACTLY
---------------------
A live, headless version of the backtest replay loop. Per cycle, per ACTIVE
deployment, per symbol:

  1. read the latest bars from `market_data_bars` (the persisted store,
     Phase 53 - never a live vendor call, exactly like the Phase 61 signal
     route);
  2. `evaluate_current_signal(bars, definition)` - the SAME evaluator the
     signal route uses, so a deployment's action can never disagree with
     what `POST .../signals` would have said for that bar;
  3. if the signal is actionable given the current position (BUY while flat,
     SELL while long - the strategy is long/flat, like the backtest
     executor), build a `TradeProposal` at the latest bar's close and submit
     it through `oms.persistence.submit_trade_and_record` - the ONE
     sanctioned RISK -> PORTFOLIO -> BROKER path, unchanged;
  4. persist a real Phase-61 `SignalEvaluation` row for the symbol, linked
     to this run.

Every cycle for every deployment writes one append-only
`strategy_deployment_runs` row saying what it did or why it did nothing.

WHAT MAKES IT SAFE
------------------
- **Only ACTIVE deployments.** A `PENDING_APPROVAL` deployment is invisible
  to this runner; reaching ACTIVE requires the explicit human approval
  action (`deployments/service.py`).
- **The global emergency stop is checked every cycle**, before any order,
  and a stopped cycle writes `SKIPPED_EMERGENCY_STOP` and places nothing.
  `submit_trade_and_record` also re-checks it inside the Risk Engine, so
  this is belt and braces.
- **`mode='paper'` only.** The runner asserts it and would refuse any other
  value; live deployment is Phase 64.
- **`require_stop_price=False`**, the same `RiskLimits` a backtest of this
  definition uses (D035): a strategy's exit rule IS its stop, re-evaluated
  every cycle, and demanding a stop price here would force fabricating one.
  Everything else in the limits is the production trade-path value.
- **Close-of-bar execution.** The proposal's `estimated_price` and the risk
  engine's `now` are both the latest bar's timestamp, so the market-data
  staleness check measures the trade against the bar it was actually
  decided on - the same framing the backtest uses - rather than rejecting
  every daily-bar trade as stale.
- **One transaction per deployment**, and the run row is always resolved to
  a terminal status even if the work between raises (the D072 orchestrator
  posture): create the run row RUNNING-equivalent and flush, do the work,
  resolve to SUCCEEDED / FAILED, broad outer except -> FAILED, never a
  stranded row.
- **Cross-worker exclusion** via the same Postgres advisory lock the
  snapshot scheduler and reconciler use, on this job's own objid.

DEFAULT: DISABLED
-----------------
`STRATEGY_RUNNER_ENABLED` defaults to false - the same fail-closed posture
as every other background loop here (the snapshot scheduler, the
reconciler, live trading, the emergency stop). This one is if anything more
consequential: it places real (paper) orders on a timer with no per-order
human. Opt-in is deliberate.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Cross-module reuse of the backtest sizing/warmup helpers and the shared
# limit builders - the same D072 precedent walk_forward.py / universe_scan.py
# already set. A deployment sizes a position exactly as a backtest of the
# same definition would, and runs under the same limits (D035).
from apps.api.app.api.routes.strategy_backtests import _portfolio_limits, _risk_limits
from apps.api.app.backtesting.engine_v2 import _desired_quantity, _warmup_bar_count
from apps.api.app.core.config import Settings
from apps.api.app.core.logging import get_logger
from apps.api.app.db.models import (
    Order,
    SignalDirection,
    SignalEvaluation,
    StrategyDeployment,
    StrategyDeploymentRun,
    StrategyDeploymentRunStatus,
    StrategyDeploymentStatus,
    StrategyVersion,
)
from apps.api.app.execution.persistence import load_paper_broker, save_paper_broker
from apps.api.app.marketdata.portfolio_risk import load_market_risk_inputs
from apps.api.app.marketdata.store import MarketDataStore
from apps.api.app.oms.persistence import get_recent_filled_orders, submit_trade_and_record
from apps.api.app.oms.service import OMSStatus
from apps.api.app.portfolio.cycle_lock import (
    DEPLOYMENT_RUNNER_LOCK_OBJID,
    SnapshotCycleLock,
    SnapshotCycleLockDecision,
)
from apps.api.app.portfolio.market_hours import MarketHoursGate
from apps.api.app.portfolio_manager.manager import portfolio_state_from_positions
from apps.api.app.risk.models import BlockReason, Side, TradeProposal
from apps.api.app.safety.emergency_stop import is_emergency_stop_active
from apps.api.app.signals.engine import evaluate_current_signal
from apps.api.app.strategies.expressions import compute_indicator_series

logger = get_logger(__name__)

EVALUATION_BAR_BUFFER = 5
"""Bars fetched beyond `_warmup_bar_count(definition)` - identical to the
Phase 61 signal route's buffer and for the same reason (a crossing operator
on the latest bar needs the bar before it fully evaluated)."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def build_deployment_runner_cycle_lock(*, enabled: bool = True) -> SnapshotCycleLock:
    """This job's cross-worker lock - D047's mechanism and class, on the
    deployment runner's own objid and log prefix."""
    return SnapshotCycleLock(
        enabled=enabled,
        objid=DEPLOYMENT_RUNNER_LOCK_OBJID,
        job_name="strategy_deployment_runner",
    )


class DeploymentCycleStatus(str, Enum):  # noqa: UP042 (str mixin for log/JSON interop)
    RAN = "ran"
    SKIPPED_MARKET_CLOSED = "skipped_market_closed"
    SKIPPED_LOCK_HELD = "skipped_lock_held"


@dataclass(frozen=True)
class DeploymentRunOutcome:
    """The typed result of one deployment's cycle - returned, never raised,
    so one deployment's failure can never abort the rest."""

    deployment_id: uuid.UUID
    run_id: uuid.UUID | None
    status: StrategyDeploymentRunStatus
    symbols_evaluated: int = 0
    signals_actionable: int = 0
    orders_submitted: int = 0
    orders_filled: int = 0
    detail: str | None = None


@dataclass(frozen=True)
class DeploymentCycleResult:
    outcomes: tuple[DeploymentRunOutcome, ...] = field(default=())
    status: DeploymentCycleStatus = DeploymentCycleStatus.RAN
    lock: SnapshotCycleLockDecision = SnapshotCycleLockDecision.LOCK_DISABLED
    detail: str | None = None

    @property
    def ran(self) -> bool:
        return self.status is DeploymentCycleStatus.RAN

    @property
    def orders_filled(self) -> int:
        return sum(o.orders_filled for o in self.outcomes)


def _latest_close_and_ts(bars: list) -> tuple[Decimal, datetime] | None:
    if not bars:
        return None
    last = bars[-1]
    if last.close is None or last.close <= 0:
        return None
    return last.close, last.ts


async def _persist_signal_evaluation(
    session: AsyncSession,
    *,
    deployment: StrategyDeployment,
    run_id: uuid.UUID,
    symbol: str,
    bars: list,
    definition: dict,
) -> tuple[SignalDirection, bool, Decimal | None]:
    """Evaluate and persist a Phase-61 `SignalEvaluation` for one symbol,
    linked to this run. Returns `(direction, insufficient_data,
    latest_close)` for the runner's own decision."""
    current = evaluate_current_signal(bars, definition)
    indicators = definition.get("indicators") or []
    indicator_series = {
        ind["id"]: compute_indicator_series(bars, ind) for ind in indicators
    }
    values = {
        name: (str(series[-1]) if series and series[-1] is not None else None)
        for name, series in indicator_series.items()
    }
    row = SignalEvaluation(
        id=uuid.uuid4(),
        strategy_version_id=deployment.strategy_version_id,
        requested_by_user_id=deployment.requested_by_user_id,
        deployment_run_id=run_id,
        symbol=symbol,
        bar_interval=deployment.bar_interval,
        as_of_bar_date=current.as_of.date() if current.as_of is not None else None,
        latest_close=current.latest_close,
        signal=SignalDirection(current.signal.value),
        entry_rule_held=current.entry_rule_held,
        exit_rule_held=current.exit_rule_held,
        insufficient_data=current.insufficient_data,
        indicator_values=values,
        explanation=current.explanation,
    )
    session.add(row)
    return (
        SignalDirection(current.signal.value),
        current.insufficient_data,
        current.latest_close,
    )


async def _run_one_deployment(
    session: AsyncSession,
    deployment: StrategyDeployment,
    run: StrategyDeploymentRun,
    *,
    settings: Settings,
    clock: Callable[[], datetime],
) -> DeploymentRunOutcome:
    """The work for one ACTIVE deployment, inside a transaction the caller
    owns. `run` already exists and is flushed; this resolves its counters
    and status."""
    assert deployment.mode == "paper"  # nosec - enforced at creation, re-asserted here

    version = (
        await session.execute(
            select(StrategyVersion).where(StrategyVersion.id == deployment.strategy_version_id)
        )
    ).scalar_one()
    definition = version.definition

    store = MarketDataStore(session)
    count = _warmup_bar_count(definition) + EVALUATION_BAR_BUFFER

    broker = await load_paper_broker(
        session,
        deployment.broker_id,
        default_starting_cash=settings.paper_broker_starting_cash,
    )

    # Marks for every held symbol, from the latest ingested bar. A held
    # symbol with no bar cannot be valued, and this system does not
    # fabricate a price (docs/TRADING_SAFETY.md) - the cycle for this
    # deployment fails, visibly, so an operator notices a position that
    # cannot be priced.
    marks: dict[str, Decimal] = {}
    for held_symbol in broker.positions:
        held_bars = await store.get_latest_bars(
            held_symbol, bar_interval=deployment.bar_interval, count=1
        )
        priced = _latest_close_and_ts(held_bars)
        if priced is None:
            run.status = StrategyDeploymentRunStatus.FAILED
            run.error_detail = (
                f"held position {held_symbol} has no ingested {deployment.bar_interval} "
                f"bar to mark against; the account cannot be valued and no price was "
                f"fabricated."
            )
            run.completed_at = clock()
            return DeploymentRunOutcome(
                deployment_id=deployment.id,
                run_id=run.id,
                status=run.status,
                detail=run.error_detail,
            )
        marks[held_symbol] = priced[0]

    risk_limits = _risk_limits(settings)
    portfolio_limits = _portfolio_limits(settings)

    evaluated = 0
    actionable = 0
    submitted = 0
    filled = 0

    for symbol in deployment.symbols:
        bars = await store.get_latest_bars(
            symbol, bar_interval=deployment.bar_interval, count=count
        )
        direction, insufficient, latest_close = await _persist_signal_evaluation(
            session,
            deployment=deployment,
            run_id=run.id,
            symbol=symbol,
            bars=bars,
            definition=definition,
        )
        evaluated += 1

        if insufficient or latest_close is None:
            continue

        held = broker.positions.get(symbol, Decimal(0))
        want_buy = direction is SignalDirection.BUY and held == 0
        want_sell = direction is SignalDirection.SELL and held > 0
        if not (want_buy or want_sell):
            continue
        actionable += 1

        priced = _latest_close_and_ts(bars)
        assert priced is not None  # latest_close is not None implies a real last bar
        price, bar_ts = priced
        marks[symbol] = price

        equity = broker.get_account_state(marks=marks).equity
        if want_buy:
            quantity = _desired_quantity(
                sizing=definition["position_sizing"],
                cash=broker.cash,
                equity=equity,
                price=price,
            )
            side = Side.BUY
        else:
            quantity = held
            side = Side.SELL
        if quantity <= 0:
            continue

        proposal = TradeProposal(
            symbol=symbol,
            side=side,
            quantity=quantity,
            estimated_price=price,
            stop_price=None,
            market_data_as_of=bar_ts,
        )
        account = broker.get_account_state(marks=marks)
        portfolio = portfolio_state_from_positions(broker.positions, marks, broker.cash)
        recent_orders = await get_recent_filled_orders(
            session,
            deployment.broker_id,
            symbol,
            window_seconds=settings.risk_duplicate_order_window_seconds,
        )
        market_risk = await load_market_risk_inputs(
            store,
            [symbol, *portfolio.open_symbols],
            as_of=bar_ts.date(),
            lookback_days=settings.portfolio_market_risk_lookback_days,
            min_observations=settings.portfolio_market_risk_min_observations,
        )
        emergency = await is_emergency_stop_active(
            session, settings_default=settings.emergency_stop_active
        )

        submit_kwargs = dict(
            emergency_stop_active=emergency,
            now=bar_ts,
            submitted_by_user_id=None,
            recent_orders=recent_orders,
            portfolio=portfolio,
            portfolio_limits=portfolio_limits,
            market_risk=market_risk,
        )
        result = await submit_trade_and_record(
            session, deployment.broker_id, proposal, account, risk_limits, broker,
            **submit_kwargs,  # type: ignore[arg-type]
        )
        submitted += 1
        if result.order_id is not None:
            order = await session.get(Order, result.order_id)
            if order is not None:
                order.deployment_run_id = run.id

        # Single risk-engine resize retry, the exact deterministic policy
        # `backtesting/engine.py::_attempt_trade` applies (the engine itself
        # never resizes): if the ONLY problem was position size and the
        # engine reported a max, try once more at that quantity so an
        # `all_in` deployment sizes into the 10%-of-equity per-trade cap
        # rather than never trading. A portfolio REJECT is NOT retried, for
        # the same reason `_attempt_trade` does not retry one.
        if (
            result.status is OMSStatus.REJECTED
            and result.risk_decision.reason is BlockReason.EXCEEDS_MAX_POSITION_SIZE
            and result.risk_decision.max_quantity_allowed
            and result.portfolio_decision is None
        ):
            retry_quantity = result.risk_decision.max_quantity_allowed
            if 0 < retry_quantity < quantity:
                retry_proposal = proposal.model_copy(update={"quantity": retry_quantity})
                result = await submit_trade_and_record(
                    session, deployment.broker_id, retry_proposal, account,
                    risk_limits, broker, **submit_kwargs,  # type: ignore[arg-type]
                )
                submitted += 1
                if result.order_id is not None:
                    order = await session.get(Order, result.order_id)
                    if order is not None:
                        order.deployment_run_id = run.id

        if result.status is OMSStatus.FILLED:
            filled += 1

    await save_paper_broker(session, deployment.broker_id, broker)

    run.status = StrategyDeploymentRunStatus.SUCCEEDED
    run.symbols_evaluated = evaluated
    run.signals_actionable = actionable
    run.orders_submitted = submitted
    run.orders_filled = filled
    run.completed_at = clock()
    deployment.last_evaluated_at = run.completed_at
    return DeploymentRunOutcome(
        deployment_id=deployment.id,
        run_id=run.id,
        status=run.status,
        symbols_evaluated=evaluated,
        signals_actionable=actionable,
        orders_submitted=submitted,
        orders_filled=filled,
    )


async def run_deployment_cycle(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    cycle_lock: SnapshotCycleLock | None = None,
    market_hours_gate: MarketHoursGate | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> DeploymentCycleResult:
    """One full pass over every ACTIVE deployment. Each deployment gets its
    own transaction; one deployment's failure is recorded and the rest still
    run."""
    lock = cycle_lock if cycle_lock is not None else SnapshotCycleLock(enabled=False)
    gate = market_hours_gate if market_hours_gate is not None else MarketHoursGate(enabled=False)

    if not gate.evaluate(clock()).should_run:
        return DeploymentCycleResult(
            status=DeploymentCycleStatus.SKIPPED_MARKET_CLOSED,
            detail="UTC weekend - the runner no-ops, costing zero queries.",
        )

    async with session_factory() as lock_session, lock.hold(lock_session) as lock_decision:
        if lock_decision is SnapshotCycleLockDecision.SKIPPED_LOCK_HELD:
            return DeploymentCycleResult(
                status=DeploymentCycleStatus.SKIPPED_LOCK_HELD, lock=lock_decision
            )

        async with session_factory() as session:
            deployment_ids = list(
                (
                    await session.execute(
                        select(StrategyDeployment.id).where(
                            StrategyDeployment.status == StrategyDeploymentStatus.ACTIVE,
                            StrategyDeployment.mode == "paper",
                        )
                    )
                )
                .scalars()
                .all()
            )

        outcomes: list[DeploymentRunOutcome] = []
        for deployment_id in deployment_ids:
            outcomes.append(
                await _run_deployment_isolated(
                    session_factory, deployment_id, settings=settings, clock=clock
                )
            )
        return DeploymentCycleResult(
            outcomes=tuple(outcomes),
            status=DeploymentCycleStatus.RAN,
            lock=lock_decision,
        )


async def _run_deployment_isolated(
    session_factory: async_sessionmaker[AsyncSession],
    deployment_id: uuid.UUID,
    *,
    settings: Settings,
    clock: Callable[[], datetime],
) -> DeploymentRunOutcome:
    """One deployment, one transaction, ALWAYS a terminal run row - the D072
    posture. The run row is written and committed first so a crash mid-work
    still leaves the evidence that a cycle was attempted."""
    started = clock()
    async with session_factory() as session:
        deployment = await session.get(StrategyDeployment, deployment_id)
        if deployment is None:
            return DeploymentRunOutcome(
                deployment_id=deployment_id,
                run_id=None,
                status=StrategyDeploymentRunStatus.SKIPPED_NOT_ACTIVE,
                detail="deployment vanished between enumeration and run",
            )
        # Re-read the status inside this transaction: it may have been paused
        # or stopped since enumeration.
        if deployment.status is not StrategyDeploymentStatus.ACTIVE:
            run = StrategyDeploymentRun(
                id=uuid.uuid4(),
                deployment_id=deployment_id,
                status=StrategyDeploymentRunStatus.SKIPPED_NOT_ACTIVE,
                started_at=started,
                completed_at=clock(),
            )
            session.add(run)
            await session.commit()
            return DeploymentRunOutcome(
                deployment_id=deployment_id,
                run_id=run.id,
                status=run.status,
                detail=f"deployment is {deployment.status.value}",
            )

        emergency = await is_emergency_stop_active(
            session, settings_default=settings.emergency_stop_active
        )
        run = StrategyDeploymentRun(
            id=uuid.uuid4(),
            deployment_id=deployment_id,
            status=StrategyDeploymentRunStatus.FAILED,  # provisional; resolved below
            started_at=started,
        )
        session.add(run)
        await session.flush()

        if emergency:
            run.status = StrategyDeploymentRunStatus.SKIPPED_EMERGENCY_STOP
            run.completed_at = clock()
            await session.commit()
            return DeploymentRunOutcome(
                deployment_id=deployment_id,
                run_id=run.id,
                status=run.status,
                detail="global emergency stop is active",
            )

        try:
            outcome = await _run_one_deployment(
                session, deployment, run, settings=settings, clock=clock
            )
            await session.commit()
            return outcome
        except Exception as exc:  # noqa: BLE001 - the run row must resolve, never strand
            await session.rollback()
            async with session_factory() as fail_session:
                fail_run = await fail_session.get(StrategyDeploymentRun, run.id)
                if fail_run is not None:
                    fail_run.status = StrategyDeploymentRunStatus.FAILED
                    fail_run.error_detail = f"{type(exc).__name__}: {exc}"
                    fail_run.completed_at = clock()
                    await fail_session.commit()
            logger.error(
                "strategy_deployment_run_failed",
                deployment_id=str(deployment_id),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return DeploymentRunOutcome(
                deployment_id=deployment_id,
                run_id=run.id,
                status=StrategyDeploymentRunStatus.FAILED,
                detail=f"{type(exc).__name__}: {exc}",
            )


class StrategyDeploymentRunner:
    """In-process periodic runner, owned by the FastAPI lifespan - a sibling
    of `PortfolioSnapshotScheduler` and `LiveOrderReconciler`, on its own
    interval and its own advisory-lock objid.

    Runs one cycle immediately on `start()` (a misconfiguration shows up in
    the startup logs, not an interval later), then every `interval_seconds`.
    The loop never dies on a transient error - it logs and retries next
    interval, because a runner that stopped permanently would silently leave
    approved strategies un-evaluated.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings,
        interval_seconds: int,
        cycle_lock: SnapshotCycleLock | None = None,
        market_hours_gate: MarketHoursGate | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError(
                "interval_seconds must be positive; a zero or negative interval would "
                "busy-loop the deployment runner against the database and the paper broker."
            )
        self._session_factory = session_factory
        self._settings = settings
        self._interval_seconds = interval_seconds
        self._cycle_lock = (
            cycle_lock if cycle_lock is not None else SnapshotCycleLock(enabled=False)
        )
        self._market_hours_gate = (
            market_hours_gate if market_hours_gate is not None else MarketHoursGate(enabled=False)
        )
        self._clock = clock
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("StrategyDeploymentRunner.start() called twice")
        self._task = asyncio.create_task(self._run(), name="strategy-deployment-runner")
        logger.info(
            "strategy_deployment_runner_started",
            interval_seconds=self._interval_seconds,
            cycle_lock=(
                f"pg_advisory:{self._cycle_lock.classid}/{self._cycle_lock.objid}"
                if self._cycle_lock.enabled
                else "DISABLED"
            ),
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
        logger.info("strategy_deployment_runner_stopped")

    async def run_once(self) -> DeploymentCycleResult:
        return await run_deployment_cycle(
            self._session_factory,
            settings=self._settings,
            cycle_lock=self._cycle_lock,
            market_hours_gate=self._market_hours_gate,
            clock=self._clock,
        )

    async def _run(self) -> None:
        while True:
            try:
                result = await self.run_once()
                logger.info(
                    "strategy_deployment_cycle",
                    status=result.status.value,
                    deployments=len(result.outcomes),
                    orders_filled=result.orders_filled,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - see class docstring
                logger.error(
                    "strategy_deployment_cycle_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            await asyncio.sleep(self._interval_seconds)
