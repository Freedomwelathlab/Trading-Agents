"""Automatic/scheduled portfolio snapshotting (Phase 27, docs/DECISIONS.md
D030), built on top of Phase 25/D027's deliberately manual-only capture.

THE CENTRAL PROBLEM AND HOW IT IS RESOLVED
------------------------------------------
`compute_portfolio_snapshot()` cannot value an account without a current
mark for every symbol the broker holds. The manual endpoint solves this by
making the caller supply `marks` in the request body. A scheduler has no
caller and therefore no marks - and this project's hardest rule
(docs/TRADING_SAFETY.md, spec Sec57) forbids inventing one, carrying a
stale one forward, or defaulting to average cost.

So a scheduled snapshot sources every mark from the *existing*
`MarketDataRouter` (apps/api/app/marketdata/router.py) - the same real
Longbridge-backed quote path `GET /market-data/{symbol}/quote` and the
optional-`estimated_price` trade path (D015/D017) already use. Nothing new
fabricates a price, and no new provider was added.

When a real quote cannot be obtained for even one held symbol, this module
does NOT partially value the account and does NOT substitute anything. It
skips that broker for that cycle and returns a typed
`ScheduledSnapshotOutcome` recording exactly which symbols were unpriced
and why, which the scheduler loop logs as a structured event. Skipping is
the correct failure mode here specifically because the table is append-only
history: a snapshot row silently missing one position's value would be
indistinguishable, forever after, from a snapshot where that position was
genuinely closed. A gap in the series is honest; a wrong row is not.

A broker holding *no* open positions needs no marks at all, so its
cash-only snapshot is captured normally even when market data is entirely
NOT_CONFIGURED. That is a real, complete valuation, not a degraded one.

WHY AN in-process asyncio TASK, NOT A SCHEDULER LIBRARY
-------------------------------------------------------
No new dependency was added. The requirement is "run this coroutine every N
seconds while the app is up", which `asyncio.sleep` in a task owned by the
FastAPI lifespan expresses completely. APScheduler/Celery would buy cron
expressions, persistence across restarts, and multi-worker leader election
- none of which this phase needs, all of which would need their own
configuration, failure modes, and safety review before touching real broker
state. See D030 for the full reasoning and for what would justify revisiting
this (notably: running more than one API worker, where each worker would
currently run its own independent loop).

DEFAULT: DISABLED
-----------------
`PORTFOLIO_SNAPSHOT_SCHEDULER_ENABLED` defaults to false. This is new
automated behavior that reads real broker state and writes real rows on a
timer with no human in the loop; opt-in is the fail-closed posture this
codebase applies to every other consequential switch (live trading, the
emergency stop, the market-data and LLM credential trios). See D030.
"""

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.app.core.logging import get_logger
from apps.api.app.db.models import BrokerAccount, BrokerPosition
from apps.api.app.marketdata.router import MarketDataRouter, NoDataAvailableError
from apps.api.app.portfolio.errors import BrokerAccountNotFoundError, MissingMarkError
from apps.api.app.portfolio.persistence import persist_portfolio_snapshot
from apps.api.app.portfolio.snapshot import compute_portfolio_snapshot

logger = get_logger(__name__)


class ScheduledSnapshotStatus(str, Enum):  # noqa: UP042 (str mixin for log/JSON interop)
    """Every way one broker's scheduled capture can end. Exhaustive and
    typed on purpose: a scheduled job that swallowed failures into a bare
    `except` would make "no snapshot exists for 14:00" indistinguishable
    from "the market data vendor was down at 14:00", which is precisely the
    distinction docs/TRADING_SAFETY.md's no-fabrication rule cares about.
    """

    CAPTURED = "captured"
    """A real snapshot row was written from a complete, fully-marked
    valuation."""

    SKIPPED_MARKET_DATA_NOT_CONFIGURED = "skipped_market_data_not_configured"
    """The broker holds open positions but no MarketDataProvider is
    configured at all (the NOT_CONFIGURED sentinel state - see
    apps/api/app/marketdata/providers/longbridge.py and D015). Distinct
    from DATA_UNAVAILABLE below: this is a deployment/config gap, not a
    vendor failure, and it will not fix itself on the next cycle."""

    SKIPPED_MARKET_DATA_UNAVAILABLE = "skipped_market_data_unavailable"
    """A router exists but returned no real quote for at least one held
    symbol. `unpriced_symbols` names exactly which."""

    SKIPPED_NO_BROKER_ACCOUNT = "skipped_no_broker_account"
    """The broker has no `broker_accounts` row, so its cash balance has no
    source of truth. Deliberately NOT defaulted to
    `Settings.paper_broker_starting_cash` the way the read-only HTTP
    endpoint does - see this module's `capture_scheduled_snapshot`
    docstring."""

    SKIPPED_INCOMPLETE_VALUATION = "skipped_incomplete_valuation"
    """`compute_portfolio_snapshot()` still raised `MissingMarkError` after
    marks were fetched - i.e. a position was opened by a concurrent trade
    between reading this broker's symbols and computing the snapshot. Rare,
    benign, and retried on the next cycle; recorded rather than hidden."""


@dataclass(frozen=True)
class ScheduledSnapshotOutcome:
    """The typed result of attempting one broker's scheduled snapshot.
    Returned (never raised) so a single broker's data gap can never abort
    the rest of the cycle."""

    broker_id: uuid.UUID
    status: ScheduledSnapshotStatus
    snapshot_id: uuid.UUID | None = None
    """Set only when status is CAPTURED."""
    unpriced_symbols: tuple[str, ...] = ()
    """Held symbols no real mark could be obtained for. Empty unless the
    status is one of the market-data skips."""
    detail: str | None = None
    """Human-readable reason, carrying the underlying
    NO_DATA_AVAILABLE:/NOT_CONFIGURED: sentinel text verbatim where one
    exists, so a log reader sees the vendor's actual message rather than a
    paraphrase."""

    @property
    def captured(self) -> bool:
        return self.status is ScheduledSnapshotStatus.CAPTURED


@dataclass(frozen=True)
class SnapshotCycleResult:
    """One full pass over every eligible broker."""

    outcomes: tuple[ScheduledSnapshotOutcome, ...] = field(default=())

    @property
    def captured_count(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.captured)

    @property
    def skipped_count(self) -> int:
        return len(self.outcomes) - self.captured_count


async def open_position_symbols(session: AsyncSession, broker_id: uuid.UUID) -> list[str]:
    """Symbols this broker currently holds a nonzero quantity of - exactly
    the set `compute_portfolio_snapshot()` will demand a mark for. Sorted
    so a cycle's quote requests (and its logs) are deterministic."""
    rows = (
        (
            await session.execute(
                select(BrokerPosition.symbol).where(
                    BrokerPosition.broker_id == broker_id,
                    BrokerPosition.quantity != 0,
                )
            )
        )
        .scalars()
        .all()
    )
    return sorted(rows)


async def resolve_marks(
    symbols: Sequence[str],
    market_data_router: MarketDataRouter,
) -> tuple[dict[str, Decimal], dict[str, str]]:
    """Fetches a real quote for each symbol through the existing router.

    Returns `(marks, failures)` where `failures` maps a symbol to the
    router's own NO_DATA_AVAILABLE sentinel message. A symbol never appears
    in both. Nothing is substituted for a failed symbol - the caller's job
    is to refuse to build a snapshot at all, not to fill the hole.

    Quotes are fetched concurrently: a portfolio of N symbols would
    otherwise take N sequential vendor round-trips per cycle, and each
    symbol's result is completely independent of the others'.
    """
    marks: dict[str, Decimal] = {}
    failures: dict[str, str] = {}

    async def _one(symbol: str) -> tuple[str, Decimal | None, str | None]:
        try:
            snapshot = await market_data_router.get_snapshot(symbol)
        except NoDataAvailableError as exc:
            return symbol, None, str(exc)
        return symbol, snapshot.price, None

    for symbol, price, failure in await asyncio.gather(*(_one(s) for s in symbols)):
        if price is None:
            failures[symbol] = failure or "NO_DATA_AVAILABLE: unknown reason"
        else:
            marks[symbol] = price

    return marks, failures


async def capture_scheduled_snapshot(
    broker_id: uuid.UUID,
    session: AsyncSession,
    *,
    market_data_router: MarketDataRouter | None,
) -> ScheduledSnapshotOutcome:
    """Attempts one broker's automatic snapshot. Never raises for a data
    gap; returns a typed outcome instead.

    Unlike the HTTP endpoints, this passes `default_starting_cash=None` to
    `compute_portfolio_snapshot()`. A human calling
    `GET .../portfolio` on a never-traded broker is asking "what would this
    account look like", and answering with the configured starting cash is
    a reasonable, clearly-labelled read. A scheduler writing that same
    number into append-only history would be manufacturing a permanent
    record of a cash balance no `broker_accounts` row ever asserted - so
    here it skips instead (SKIPPED_NO_BROKER_ACCOUNT).
    """
    symbols = await open_position_symbols(session, broker_id)

    marks: dict[str, Decimal] = {}
    if symbols:
        if market_data_router is None:
            return ScheduledSnapshotOutcome(
                broker_id=broker_id,
                status=ScheduledSnapshotStatus.SKIPPED_MARKET_DATA_NOT_CONFIGURED,
                unpriced_symbols=tuple(symbols),
                detail=(
                    "NOT_CONFIGURED: no market data provider is configured, so no real "
                    f"mark can be obtained for held symbols {symbols}. Snapshot skipped "
                    "rather than valued with a fabricated price."
                ),
            )

        marks, failures = await resolve_marks(symbols, market_data_router)
        if failures:
            return ScheduledSnapshotOutcome(
                broker_id=broker_id,
                status=ScheduledSnapshotStatus.SKIPPED_MARKET_DATA_UNAVAILABLE,
                unpriced_symbols=tuple(sorted(failures)),
                detail="; ".join(
                    f"{symbol}: {reason}" for symbol, reason in sorted(failures.items())
                ),
            )

    try:
        computed = await compute_portfolio_snapshot(
            broker_id,
            session,
            marks=marks,
            default_starting_cash=None,
        )
    except BrokerAccountNotFoundError as exc:
        return ScheduledSnapshotOutcome(
            broker_id=broker_id,
            status=ScheduledSnapshotStatus.SKIPPED_NO_BROKER_ACCOUNT,
            detail=str(exc),
        )
    except MissingMarkError as exc:
        return ScheduledSnapshotOutcome(
            broker_id=broker_id,
            status=ScheduledSnapshotStatus.SKIPPED_INCOMPLETE_VALUATION,
            detail=str(exc),
        )

    row = await persist_portfolio_snapshot(session, broker_id, computed)
    return ScheduledSnapshotOutcome(
        broker_id=broker_id,
        status=ScheduledSnapshotStatus.CAPTURED,
        snapshot_id=row.id,
    )


async def eligible_broker_ids(session: AsyncSession) -> list[uuid.UUID]:
    """Brokers a scheduled cycle will attempt: exactly those with a
    `broker_accounts` row.

    That row is created lazily the first time the execution layer loads a
    broker (apps/api/app/execution/persistence.py, D014), so its presence
    means "this broker has real, persisted state worth recording". A
    `brokers` row that has never been traded against has no cash balance to
    snapshot - see `capture_scheduled_snapshot`'s docstring for why the
    scheduler will not invent one.
    """
    rows = (await session.execute(select(BrokerAccount.broker_id))).scalars().all()
    return sorted(rows, key=str)


async def run_snapshot_cycle(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    market_data_router: MarketDataRouter | None,
) -> SnapshotCycleResult:
    """One full pass: snapshot every eligible broker, independently.

    Each broker gets its own session so one broker's failed commit cannot
    poison another's transaction, and so a long cycle does not hold a single
    connection open across every vendor round-trip. Brokers are processed
    sequentially rather than concurrently: the vendor round-trips within one
    broker are already parallel (`resolve_marks`), and fanning out across
    brokers as well would multiply concurrent DB connections and vendor
    rate-limit pressure for no benefit at this scale.
    """
    async with session_factory() as session:
        broker_ids = await eligible_broker_ids(session)

    outcomes: list[ScheduledSnapshotOutcome] = []
    for broker_id in broker_ids:
        async with session_factory() as session:
            outcome = await capture_scheduled_snapshot(
                broker_id, session, market_data_router=market_data_router
            )
        outcomes.append(outcome)

        if outcome.captured:
            logger.info(
                "portfolio_snapshot_captured",
                broker_id=str(outcome.broker_id),
                snapshot_id=str(outcome.snapshot_id),
            )
        else:
            # Deliberately warning, not debug: a skipped cycle leaves a
            # permanent hole in an append-only time series. It must be
            # visible, and it must say which symbols and why.
            logger.warning(
                "portfolio_snapshot_skipped",
                broker_id=str(outcome.broker_id),
                status=outcome.status.value,
                unpriced_symbols=list(outcome.unpriced_symbols),
                detail=outcome.detail,
            )

    return SnapshotCycleResult(outcomes=tuple(outcomes))


class PortfolioSnapshotScheduler:
    """In-process periodic snapshot task, owned by the FastAPI lifespan.

    Runs one cycle immediately on `start()`, then every
    `interval_seconds`. Running immediately is deliberate: it makes a
    misconfiguration (no market data, no eligible brokers) visible in the
    startup logs rather than an hour later, and it means a restart never
    silently loses the cycle it was mid-way through.

    The loop never dies. Any unexpected exception from a cycle is logged
    and the loop sleeps and tries again - a scheduler that stops
    permanently on one transient DB blip would silently stop recording
    history, which is a worse failure than a logged, retried error.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        market_data_router: MarketDataRouter | None,
        interval_seconds: int,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError(
                "interval_seconds must be positive; a zero or negative interval "
                "would busy-loop the snapshot cycle against the DB and the market "
                "data vendor."
            )
        self._session_factory = session_factory
        self._market_data_router = market_data_router
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("PortfolioSnapshotScheduler.start() called twice")
        self._task = asyncio.create_task(self._run(), name="portfolio-snapshot-scheduler")
        logger.info(
            "portfolio_snapshot_scheduler_started",
            interval_seconds=self._interval_seconds,
            market_data="configured" if self._market_data_router else "NOT_CONFIGURED",
        )

    async def stop(self) -> None:
        """Cancels the loop and waits for it to actually finish, so shutdown
        never races a half-committed snapshot cycle."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
        logger.info("portfolio_snapshot_scheduler_stopped")

    async def run_once(self) -> SnapshotCycleResult:
        """One cycle, awaited directly. Used by the loop below and by tests
        that need a deterministic single pass rather than wall-clock
        timing."""
        return await run_snapshot_cycle(
            self._session_factory, market_data_router=self._market_data_router
        )

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - see class docstring
                logger.error(
                    "portfolio_snapshot_cycle_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            await asyncio.sleep(self._interval_seconds)
