"""Universe scanning of one validated `StrategyVersion` (Phase 60): the SAME
fixed definition run across MANY symbols over the SAME window, so the
symbols can be ranked against each other.

Where Phase 57's walk-forward asks "does this strategy hold up across
PERIODS", this asks "which MARKETS does it actually suit" - one definition,
one window, n symbols, ranked by what each produced. The two are the same
shape (create a parent row, loop, one child row per unit of work, aggregate)
because they are the same kind of job: an orchestration OF backtests, not a
second way to run one.

**`run_strategy_backtest` is CALLED, never re-implemented.** Every symbol is
an ordinary, fully persisted `BacktestRun` produced by
apps/api/app/backtesting/engine_v2.py exactly as `POST .../backtests`
produces one - same risk engine, same portfolio manager, same broker fill
math, same bar store - and `universe_scan_results.backtest_run_id` points at
that real row. This is the right structure here for the reason it was right
for walk-forward and NOT right for Phase 58's robustness: a scanned symbol
replays the version's OWN definition, unaltered, so a `backtest_runs` row
filed under that version accurately records what ran. Only a PERTURBED
definition - rules no version contains - has to stay out of that table.

**Every symbol starts fresh at the same `starting_cash`.** Symbols do not
share a balance and do not compound through one. The question is "how did
this strategy do on each of these markets, comparably", and a shared
portfolio would make every symbol's figure depend on which other symbols
were in the list and in what order - turning a comparison into a
path-dependent simulation. Running the strategy as one portfolio across a
universe is a genuinely different feature, and it is not this one.

**A symbol whose own backtest FAILED is a normal, recorded outcome**, not an
error that aborts the scan. Scanning a list someone typed will routinely hit
a symbol with no ingested bars over the window; that symbol gets its own
result row with its real FAILED `backtest_run_id` and its real
`error_detail`, its metrics stay NULL (never 0), it counts toward
`num_symbols` but not toward `num_succeeded`, and the scan continues.

**Returns a terminal row; never raises.** Same posture as
`run_strategy_backtest` (D072) and `run_walk_forward`, for the same reason:
the `universe_scans` row is created and flushed before any work happens, so
a failure is something to RECORD - what was asked for, when, by whom, and
exactly why it could not be answered - rather than something that vanishes
into a 500 leaving a RUNNING row stranded.

**Ranking is NOT computed here and is NOT stored.** `order_and_rank_results`
below derives it on read, exactly as D076's leaderboard does. A `rank`
column would be a second copy of an ordering the data already fully
determines, and any later change to the ranking rule would silently disagree
with every row written under the old one.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.backtesting.costs import CostModel
from apps.api.app.backtesting.engine_v2 import (
    QUANTITY_PRECISION_FRACTIONAL,
    QUANTITY_PRECISION_WHOLE_UNITS,
    run_strategy_backtest,
)
from apps.api.app.core.logging import get_logger
from apps.api.app.db.models import (
    BacktestRunStatus,
    MarketDataBar,
    StrategyVersion,
    UniverseScan,
    UniverseScanResult,
    UniverseScanStatus,
)
from apps.api.app.marketdata.bar_provider import HistoricalBarProvider
from apps.api.app.marketdata.bar_router import BarBackfillRouter
from apps.api.app.portfolio_manager.models import PortfolioLimits
from apps.api.app.risk.models import RiskLimits

logger = get_logger(__name__)

_ERROR_DETAIL_MAX_LENGTH = 500
"""`universe_scans.error_detail` is String(500), exactly as
`backtest_runs.error_detail` and `walk_forward_runs.error_detail` are. A
message is truncated to fit rather than being allowed to fail the very write
that records the failure."""

MAX_SCAN_SYMBOLS = 50
"""A universe scan runs synchronously in-request, one backtest per symbol.
50 modest-window backtests is a few seconds of pure in-memory simulation -
fine inline, matching every other analysis job in this codebase (no queue).
A larger universe is a real need, but it needs a job runner this codebase
does not have yet; until then the cap is honest about what a synchronous
request can do."""

SCAN_MODE_EXPLICIT_LIST = "explicit_list"
"""The caller named the symbols. `requested_symbols` holds exactly that
list, normalized."""

SCAN_MODE_ALL_INGESTED = "all_ingested"
"""The caller named none, so the universe is every distinct symbol the bar
store holds at this interval. `requested_symbols` is NULL rather than the
resolved list: the resolved list is what the result rows enumerate, and
copying it into the "what was requested" column would blur the one
distinction that column exists to keep."""

RESULT_STATUS_SUCCEEDED = "succeeded"
RESULT_STATUS_FAILED = "failed"
"""`universe_scan_results.status` mirrors the linked run's own status as a
plain string - display-only, never branched on outside its row (the
`BacktestTrade.side` precedent). Only these two values are ever written: a
`run_strategy_backtest` call always returns a TERMINAL run."""


def normalize_symbols(symbols: Sequence[str]) -> list[str]:
    """Trim, upper-case, de-duplicate, ORDER PRESERVED.

    The trim-and-upper-case half is `schemas_watchlists.normalize_symbol`'s
    rule applied to a list, and for the same reason it gives: the symbol
    persisted must be the same string handed to the bar store, so that
    "aapl" and "AAPL " cannot become two different universes of one market.

    De-duplication matters more here than it does on a watchlist, where a
    unique constraint catches a repeat: scanning the same symbol twice would
    run the backtest twice, write two identical result rows (which
    `uq_universe_scan_result_scan_symbol` refuses anyway), and count that
    symbol twice in every aggregate - inflating `num_qualified` for a
    universe the caller only listed once.

    Order is preserved rather than sorted, so `requested_symbols` reads back
    as what the caller actually typed. It has no effect on the ranking, which
    is derived from returns.
    """
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in symbols:
        symbol = raw.strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            normalized.append(symbol)
    return normalized


async def resolve_scan_symbols(
    session: AsyncSession, symbols: Sequence[str] | None, bar_interval: str
) -> list[str]:
    """The universe this scan will actually cover.

    `symbols is None` means "all ingested": every distinct symbol
    `market_data_bars` holds at this interval, in symbol order. That is a
    real, current answer rather than a configured list - the universe is
    whatever has actually been backfilled, which is the only universe a
    backtest could run over anyway.

    A non-None list is normalized and otherwise taken as given. No vendor
    validation and no "is this symbol known" check, matching
    `POST /watchlists/{id}/items`: a symbol with no bars produces a real
    FAILED result row saying exactly that, which is more useful than a 4xx
    that loses the other forty-nine.

    Called TWICE per request by design - once by the route, to answer 422 for
    an empty or over-cap universe before any row exists, and once by
    `run_universe_scan` for the run it records. One definition called twice
    rather than two copies that could disagree about what "the universe"
    means; the only cost is one extra indexed DISTINCT in "all ingested"
    mode.
    """
    if symbols is not None:
        return normalize_symbols(symbols)
    rows = (
        await session.execute(
            select(distinct(MarketDataBar.symbol))
            .where(MarketDataBar.bar_interval == bar_interval)
            .order_by(MarketDataBar.symbol.asc())
        )
    ).scalars()
    return list(rows)


def order_and_rank_results(
    results: Sequence[UniverseScanResult],
) -> list[tuple[UniverseScanResult, int | None]]:
    """The read-time ordering and 1-indexed ranking of one scan's results:
    SUCCEEDED rows first, best `total_return_pct` first, then every failed
    row after them.

    A FAILED symbol gets `rank=None`, never a last place. It was not measured
    and did not come last - ranking it at all would assert an ordering
    between a real result and a missing one. Same posture as D076's
    leaderboard, which excludes an unmeasurable strategy outright rather than
    ranking it with a zero.

    `total_return_pct` is NOT NULL in practice on a succeeded run, but the
    column is nullable, so a succeeded row that somehow has none is sorted
    with the failed group rather than being compared against a substituted
    number.

    Ties break on `symbol` so paging and repeat requests are deterministic;
    tied symbols genuinely share a position, and this does not pretend
    otherwise beyond giving them stable adjacent ranks.
    """
    measured: list[tuple[Decimal, str, UniverseScanResult]] = []
    unmeasured: list[UniverseScanResult] = []
    for row in results:
        if row.status == RESULT_STATUS_SUCCEEDED and row.total_return_pct is not None:
            measured.append((row.total_return_pct, row.symbol, row))
        else:
            unmeasured.append(row)

    measured.sort(key=lambda entry: (-entry[0], entry[1]))
    unmeasured.sort(key=lambda row: row.symbol)

    ranked: list[tuple[UniverseScanResult, int | None]] = [
        (entry[2], index) for index, entry in enumerate(measured, start=1)
    ]
    ranked.extend((row, None) for row in unmeasured)
    return ranked


async def _finish_failed(
    session: AsyncSession, scan: UniverseScan, detail: str
) -> UniverseScan:
    """Resolve the already-created row to FAILED and hand it back.

    The count columns are left as whatever the caller had already
    established - those are observations - and any that were never counted
    stay NULL, never 0: a scan that counted nothing has no count, and `0`
    would read as "scanned some, none qualified", which is a different and
    fabricated statement (docs/TRADING_SAFETY.md).
    """
    scan.status = UniverseScanStatus.FAILED
    scan.error_detail = detail[:_ERROR_DETAIL_MAX_LENGTH]
    scan.completed_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(scan)
    logger.info(
        "universe_scan_failed",
        universe_scan_id=str(scan.id),
        strategy_version_id=str(scan.strategy_version_id),
        scan_mode=scan.scan_mode,
    )
    return scan


async def run_universe_scan(
    *,
    session: AsyncSession,
    strategy_version: StrategyVersion,
    symbols: list[str] | None,
    bar_interval: str,
    start_date: date,
    end_date: date,
    starting_cash: Decimal,
    bar_provider: HistoricalBarProvider,
    risk_limits: RiskLimits,
    cost_model: CostModel,
    portfolio_limits: PortfolioLimits | None,
    requested_by_user_id: uuid.UUID | None,
) -> UniverseScan:
    """Runs `strategy_version` once per symbol over `[start_date, end_date]`
    and returns the committed `UniverseScan` row, always in a terminal
    status.

    `strategy_version` MUST already be `VALIDATED` - the caller's
    responsibility, identical to `run_strategy_backtest`'s and
    `run_walk_forward`'s contract, and apps/api/app/api/routes/universe_scan.py
    answers 409 before ever calling this. Not re-checked here for the same
    reason it is not re-checked there: one enforcement point, not two that
    can disagree.

    `symbols=None` scans every symbol ingested at `bar_interval`; a list
    scans exactly those, normalized. The route has already resolved the same
    set through `resolve_scan_symbols` and answered 422 for an empty or
    over-cap universe, so this function may assume
    `1 <= len(resolved) <= MAX_SCAN_SYMBOLS` - the cap is a request-shaping
    rule, and enforcing it a second time here would only be able to produce a
    persisted FAILED scan for a request that should never have created a row.

    SUCCEEDS even when `num_succeeded == 0`. A scan in which every symbol
    lacked usable history is a real, informative result - "none of these
    symbols have enough data for this window" - and it comes back with every
    symbol's own FAILED backtest linked so the reader can see exactly which
    and why. Same posture as walk-forward's per-window failures and D076's
    empty leaderboard: an honest empty answer is not an error.
    """
    resolved = await resolve_scan_symbols(session, symbols, bar_interval)

    scan = UniverseScan(
        id=uuid.uuid4(),
        strategy_version_id=strategy_version.id,
        requested_by_user_id=requested_by_user_id,
        bar_interval=bar_interval,
        start_date=start_date,
        end_date=end_date,
        starting_cash=starting_cash,
        scan_mode=SCAN_MODE_ALL_INGESTED if symbols is None else SCAN_MODE_EXPLICIT_LIST,
        requested_symbols=None if symbols is None else resolved,
        status=UniverseScanStatus.RUNNING,
    )
    session.add(scan)
    # Flushed before any work, exactly as `run_strategy_backtest` and
    # `run_walk_forward` flush their own rows: the id has to exist for the
    # whole duration of the scan, which is what makes every failure below
    # recordable rather than lost.
    await session.flush()

    try:
        results: list[UniverseScanResult] = []
        for symbol in resolved:
            # The real engine, unchanged - this returns an already-committed
            # BacktestRun in a terminal status and never raises (D072), so
            # one bad symbol cannot abort the universe.
            backtest_run = await run_strategy_backtest(
                session=session,
                strategy_version=strategy_version,
                symbol=symbol,
                bar_interval=bar_interval,
                start_date=start_date,
                end_date=end_date,
                starting_cash=starting_cash,
                bar_provider=bar_provider,
                risk_limits=risk_limits,
                portfolio_limits=portfolio_limits,
                cost_model=cost_model,
                # PER SYMBOL, not once for the scan: a universe may mix
                # equities (whole shares) with crypto (fractional), and one
                # precision for the whole run would either floor every
                # crypto entry to zero or propose fractional share counts
                # no equity venue accepts. The crypto test is the router's,
                # so there is one definition of "crypto symbol" in the
                # codebase rather than a second copy here.
                quantity_precision=(
                    QUANTITY_PRECISION_FRACTIONAL
                    if BarBackfillRouter.is_crypto_symbol(symbol)
                    else QUANTITY_PRECISION_WHOLE_UNITS
                ),
                requested_by_user_id=requested_by_user_id,
            )
            succeeded = backtest_run.status is BacktestRunStatus.SUCCEEDED
            results.append(
                UniverseScanResult(
                    id=uuid.uuid4(),
                    universe_scan_id=scan.id,
                    symbol=symbol,
                    backtest_run_id=backtest_run.id,
                    status=RESULT_STATUS_SUCCEEDED if succeeded else RESULT_STATUS_FAILED,
                    # Copied from the linked run rather than re-derived: these
                    # are that run's own numbers, and a terminal BacktestRun
                    # is never edited again, so the copy cannot go stale. A
                    # FAILED run computed none of them and they stay NULL.
                    total_return_pct=backtest_run.total_return_pct,
                    max_drawdown_pct=backtest_run.max_drawdown_pct,
                    win_rate_pct=backtest_run.win_rate_pct,
                    num_trades=backtest_run.num_trades,
                    error_detail=backtest_run.error_detail,
                )
            )
    except Exception as exc:
        # Broad on purpose, matching `run_strategy_backtest`'s and
        # `run_walk_forward`'s posture. The engine already absorbs every
        # per-symbol failure into a FAILED BacktestRun, so reaching here
        # means something outside a symbol's replay went wrong - and the scan
        # row already exists to record it, which beats a 500 that strands it
        # in RUNNING forever.
        return await _finish_failed(session, scan, str(exc))

    session.add_all(results)

    # Counts are real observations even when nothing succeeded: "12 scanned,
    # 0 succeeded" is a measurement, not a missing statistic.
    scan.num_symbols = len(results)
    scan.num_succeeded = sum(1 for row in results if row.status == RESULT_STATUS_SUCCEEDED)
    # "Qualified" is deliberately just "profitable over this window" - a
    # stated, simple first-pass filter (see `UniverseScan`'s docstring). A
    # succeeded row whose return is somehow NULL is not counted: it was not
    # measured, and `> 0` cannot be asserted of a number that is not there.
    scan.num_qualified = sum(
        1
        for row in results
        if row.status == RESULT_STATUS_SUCCEEDED
        and row.total_return_pct is not None
        and row.total_return_pct > 0
    )
    scan.status = UniverseScanStatus.SUCCEEDED
    scan.completed_at = datetime.now(UTC)
    await session.commit()
    # Re-read so the returned row carries what the database actually stores -
    # the same reason `run_walk_forward` refreshes: NUMERIC columns round on
    # write, and the caller must see the stored value, not the pre-write one.
    await session.refresh(scan)

    logger.info(
        "universe_scan_succeeded",
        universe_scan_id=str(scan.id),
        strategy_version_id=str(strategy_version.id),
        bar_interval=bar_interval,
        scan_mode=scan.scan_mode,
        num_symbols=scan.num_symbols,
        num_succeeded=scan.num_succeeded,
        num_qualified=scan.num_qualified,
    )
    return scan
