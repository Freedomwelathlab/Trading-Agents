"""Runs a library backtest end to end and says, in fixed rules, what it found.

Server cost was a design input (Phase 108). Twenty years of daily closes
for a few dozen symbols is a few megabytes; storing it in Postgres would
grow the one service that already dominates the bill. So history lives in
a small bounded in-process cache and is re-fetched after a few hours, and
finished results are cached the same way. Nothing here writes a row.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol

from apps.api.app.library.engine import PriceTable, RunResult, run_strategy
from apps.api.app.library.metrics import Metrics, compute_metrics, segment, yearly_returns
from apps.api.app.library.quality import DataQualityError, screen_history
from apps.api.app.library.strategies import (
    BASELINE,
    LibraryStrategy,
    Preset,
    default_cost_bps,
    get_strategy,
    market_of,
)
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.providers.yahoo import DailyHistory

EARLIEST = date(1990, 1, 1)
HISTORY_TTL_SECONDS = 6 * 3600
HISTORY_CACHE_SIZE = 80
RESULT_TTL_SECONDS = 6 * 3600
RESULT_CACHE_SIZE = 48
MAX_SYMBOLS = 25
FETCH_CONCURRENCY = 4
CURVE_POINTS = 260
MIN_EVAL_DAYS = 250
"""Under about a year of evaluation the run is refused: annualised figures
from less are not worth showing."""


class LibraryError(Exception):
    """A request the library cannot honour, with a code the route maps."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class DailyHistoryProvider(Protocol):
    name: str

    async def get_daily_history(self, symbol: str, *, start: date, end: date) -> DailyHistory: ...


class HistoryCache:
    """A bounded, time-limited cache of whole-symbol histories."""

    def __init__(
        self, *, ttl_seconds: float = HISTORY_TTL_SECONDS, max_size: int = HISTORY_CACHE_SIZE
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_size
        self._items: OrderedDict[str, tuple[float, DailyHistory]] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    def clear(self) -> None:
        self._items.clear()

    async def get(self, provider: DailyHistoryProvider, symbol: str) -> DailyHistory:
        key = symbol.upper()
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._items.get(key)
            if hit is not None and time.monotonic() - hit[0] < self._ttl:
                self._items.move_to_end(key)
                return hit[1]
            history = screen_history(
                await provider.get_daily_history(key, start=EARLIEST, end=datetime.now(UTC).date())
            )
            self._items[key] = (time.monotonic(), history)
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)
            return history


_HISTORY = HistoryCache()
_RESULTS: OrderedDict[tuple[Any, ...], tuple[float, BacktestReport]] = OrderedDict()


def reset_caches() -> None:
    _HISTORY.clear()
    _RESULTS.clear()


async def load_histories(
    provider: DailyHistoryProvider, symbols: list[str], *, cache: HistoryCache | None = None
) -> list[DailyHistory]:
    cache = cache or _HISTORY
    gate = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def one(symbol: str) -> DailyHistory:
        async with gate:
            try:
                return await cache.get(provider, symbol)
            except DataQualityError as exc:
                raise LibraryError("DATA_QUALITY", str(exc)) from None
            except DataUnavailableError as exc:
                raise LibraryError("DATA_UNAVAILABLE", f"{symbol}: {exc}") from None
            except VendorError as exc:
                raise LibraryError("DATA_UNAVAILABLE", f"{symbol}: {exc}") from None
            except ValueError as exc:
                raise LibraryError("INVALID_SYMBOL", str(exc)) from None

    return list(await asyncio.gather(*(one(s) for s in symbols)))


@dataclass(frozen=True)
class SegmentReport:
    label: str
    strategy: Metrics | None
    benchmark: Metrics | None


@dataclass(frozen=True)
class SymbolData:
    symbol: str
    vendor_symbol: str
    currency: str | None
    first: date
    last: date
    bars: int
    last_close: float
    cost_bps: float
    removed: tuple[date, ...] = ()
    truncated_before: date | None = None


@dataclass(frozen=True)
class Verdict:
    code: str
    """beats_benchmark | survivorship_biased | lower_risk | no_edge |
    insufficient_history"""
    headline: str
    detail: str


@dataclass(frozen=True)
class BacktestReport:
    strategy_id: str
    symbols: tuple[str, ...]
    benchmark_symbols: tuple[str, ...]
    params: dict[str, Any]
    lag: int
    published: date
    segments: tuple[SegmentReport, ...]
    verdict: Verdict
    curve: tuple[tuple[date, float, float | None], ...]
    years: tuple[tuple[int, float, float | None], ...]
    trades: int
    turnover_per_year: float | None
    cost_drag_per_year: float | None
    avg_exposure: float
    target: dict[str, float]
    decided_on: date | None
    explain: dict[str, str]
    data: tuple[SymbolData, ...]
    warnings: tuple[str, ...]
    source: str
    as_of: date
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _eval_index(table: PriceTable, strategy: LibraryStrategy, params: dict[str, Any]) -> int:
    warmup = strategy.warmup(params)
    needed = strategy.min_ready(params, len(table.symbols))
    for i in range(len(table)):
        ready = sum(1 for s in table.symbols if table.bars(s, i) > warmup)
        if ready >= needed:
            return i
    raise LibraryError(
        "INSUFFICIENT_DATA",
        f"{strategy.name} needs {warmup + 1} sessions of history on at least {needed} "
        "symbol(s); the data never reaches that.",
    )


def _downsample(n: int, points: int) -> list[int]:
    if n <= points:
        return list(range(n))
    step = (n - 1) / (points - 1)
    picked = sorted({round(k * step) for k in range(points)})
    if picked[-1] != n - 1:
        picked.append(n - 1)
    return picked


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.1f}%"


def judge(strategy: Metrics | None, benchmark: Metrics | None, *, label: str) -> Verdict:
    """A fixed rule, so the same numbers always read the same way.

    Judged on the period the caller passes - the out-of-sample one when
    there is enough of it. A strategy 'beats' its benchmark only when it
    is ahead on BOTH growth and risk-adjusted return.
    """
    if strategy is None or strategy.cagr is None or strategy.sharpe is None:
        return Verdict(
            "insufficient_history",
            "Not enough history to judge",
            f"The {label} period is too short for annualised statistics.",
        )
    line = (
        f"{label}: {_pct(strategy.cagr)} a year, Sharpe {strategy.sharpe:.2f}, "
        f"worst drawdown {_pct(strategy.max_drawdown)}"
    )
    if benchmark is None or benchmark.cagr is None or benchmark.sharpe is None:
        if strategy.sharpe >= 0.5 and (strategy.t_stat or 0) >= 2:
            return Verdict(
                "beats_benchmark", "Positive on its own; no benchmark to compare", line + "."
            )
        return Verdict(
            "no_edge",
            "No reliable edge measured",
            line + ". With no benchmark, the test is whether the return is distinguishable "
            f"from zero: t-statistic {strategy.t_stat or 0:.2f} (2 or more would be).",
        )
    versus = (
        f" Buy-and-hold: {_pct(benchmark.cagr)} a year, Sharpe {benchmark.sharpe:.2f}, "
        f"worst drawdown {_pct(benchmark.max_drawdown)}."
    )
    if strategy.cagr > benchmark.cagr and strategy.sharpe > benchmark.sharpe:
        return Verdict(
            "beats_benchmark", "Ahead of buy-and-hold on return and risk", line + "." + versus
        )
    shallower = abs(strategy.max_drawdown) <= abs(benchmark.max_drawdown) * 0.75
    if strategy.cagr > 0 and (strategy.sharpe >= benchmark.sharpe or shallower):
        return Verdict(
            "lower_risk",
            "Lower return than buy-and-hold, with less risk",
            line + "." + versus,
        )
    return Verdict("no_edge", "Did not beat buy-and-hold", line + "." + versus)


def build_report(
    strategy: LibraryStrategy,
    histories: list[DailyHistory],
    benchmark_histories: list[DailyHistory],
    params: dict[str, Any],
    *,
    lag: int = 1,
    start: date | None = None,
    cost_bps_override: float | None = None,
    preset: Preset | None = None,
) -> BacktestReport:
    """Pure: the same histories and parameters always give the same report."""
    table = PriceTable(histories)

    def cost(symbol: str) -> float:
        return cost_bps_override if cost_bps_override is not None else default_cost_bps(symbol)

    eval_index = _eval_index(table, strategy, params)
    if start is not None:
        later = next((i for i, d in enumerate(table.dates) if d >= start), None)
        if later is None:
            raise LibraryError("INSUFFICIENT_DATA", f"No data on or after {start.isoformat()}.")
        eval_index = max(eval_index, later)
    if len(table) - eval_index < MIN_EVAL_DAYS:
        raise LibraryError(
            "INSUFFICIENT_DATA",
            f"Only {len(table) - eval_index} sessions remain after the warm-up; at least "
            f"{MIN_EVAL_DAYS} are needed.",
        )

    run = run_strategy(table, strategy.fn, params, eval_index=eval_index, lag=lag, cost_bps=cost)
    eval_start = run.dates[0]

    bench: RunResult | None = None
    if benchmark_histories:
        bench_table = PriceTable(benchmark_histories)
        bench_index = next((i for i, d in enumerate(bench_table.dates) if d >= eval_start), None)
        if bench_index is not None and len(bench_table) - bench_index >= 2:
            bench = run_strategy(
                bench_table, BASELINE.fn, {}, eval_index=bench_index, lag=0, cost_bps=cost
            )
    bench_by_date = dict(zip(bench.dates, bench.curve, strict=True)) if bench else {}

    def bench_segment(lo: date | None, hi: date | None) -> Metrics | None:
        return segment(bench.dates, bench.curve, start=lo, end=hi) if bench else None

    published = strategy.published
    segments = [
        SegmentReport(
            "Whole period",
            compute_metrics(run.dates, run.curve),
            bench_segment(None, None),
        )
    ]
    has_before = eval_start < published
    has_after = run.dates[-1] > published
    if has_before and has_after:
        segments.append(
            SegmentReport(
                f"Before publication (to {published.year})",
                segment(run.dates, run.curve, start=None, end=published),
                bench_segment(None, published),
            )
        )
    if has_after:
        after_start = max(published, eval_start)
        segments.append(
            SegmentReport(
                f"After publication (from {after_start.year})",
                segment(run.dates, run.curve, start=after_start, end=None),
                bench_segment(after_start, None),
            )
        )
    judged = segments[-1]
    verdict = judge(judged.strategy, judged.benchmark, label=judged.label)
    if verdict.code == "beats_benchmark" and preset is not None and preset.survivorship:
        verdict = Verdict(
            "survivorship_biased",
            "Ahead of buy-and-hold - on a list of today's survivors",
            verdict.detail + " The list was chosen with hindsight, so this is an upper "
            "bound, not a result anyone could have earned.",
        )

    idx = _downsample(len(run.dates), CURVE_POINTS)
    last_bench: float | None = None
    curve: list[tuple[date, float, float | None]] = []
    cursor = 0
    for k in idx:
        while cursor <= k:
            value = bench_by_date.get(run.dates[cursor])
            if value is not None:
                last_bench = value
            cursor += 1
        curve.append((run.dates[k], run.curve[k], last_bench))

    bench_years = dict(yearly_returns(bench.dates, bench.curve)) if bench else {}
    years = tuple(
        (year, value, bench_years.get(year)) for year, value in yearly_returns(run.dates, run.curve)
    )

    span_years = (run.dates[-1] - run.dates[0]).days / 365.25
    warnings: list[str] = []
    if preset is not None and preset.survivorship:
        warnings.append(
            "Survivorship bias: this list is today's survivors. Names that were dropped or "
            "delisted are missing, so the result is better than anyone could have achieved."
        )
    if preset is not None and preset.note:
        warnings.append(preset.note)
    markets = {market_of(s) for s in table.symbols}
    if "FX" in markets:
        warnings.append(
            "FX: spot rates only. Overnight financing is not charged or credited, and a "
            "leveraged CFD account pays it every night."
        )
    if any(w < 0 for w in run.target.values()) or params.get("allow_short"):
        warnings.append("Shorting is modelled with no borrow or financing cost.")
    if len(markets) > 1:
        warnings.append(
            "Mixed markets share one calendar; lookbacks count calendar sessions of the "
            "combined list, so they are shorter for the market that trades fewer days."
        )
    if span_years < 10:
        warnings.append(
            f"Only {span_years:.1f} years of evaluation. One market cycle is not a track record."
        )
    dropped = sum(len(h.removed) for h in (*histories, *benchmark_histories))
    if dropped:
        warnings.append(
            f"{dropped} session(s) were dropped as vendor price errors (a spike that reversed "
            "within days). They are listed per symbol under Data."
        )
    cut = [h.symbol for h in (*histories, *benchmark_histories) if h.truncated_before]
    if cut:
        warnings.append(
            "Earlier history was discarded for " + ", ".join(sorted(set(cut))) + ": it "
            "contained a one-session jump the screen could not explain (often an unadjusted "
            "split or pre-listing data). Each starts on the date shown under Data."
        )
    if not has_after:
        warnings.append(
            "No data after the strategy's publication date: nothing here is out-of-sample."
        )
    warnings.append("Cash earns nothing in this backtest. Prices are dividend- and split-adjusted.")

    data = tuple(
        SymbolData(
            symbol=h.symbol,
            vendor_symbol=h.vendor_symbol,
            currency=h.currency,
            first=h.dates[0],
            last=h.dates[-1],
            bars=len(h),
            last_close=h.closes[-1],
            cost_bps=cost(h.symbol),
            removed=h.removed,
            truncated_before=h.truncated_before,
        )
        for h in histories
    )
    exposure = run.exposure[1:] or [0.0]
    return BacktestReport(
        strategy_id=strategy.id,
        symbols=table.symbols,
        benchmark_symbols=tuple(h.symbol for h in benchmark_histories),
        params=params,
        lag=lag,
        published=published,
        segments=tuple(segments),
        verdict=verdict,
        curve=tuple(curve),
        years=years,
        trades=run.trades,
        turnover_per_year=run.turnover / span_years if span_years > 0 else None,
        cost_drag_per_year=run.cost_paid / span_years if span_years > 0 else None,
        avg_exposure=sum(exposure) / len(exposure),
        target=run.target,
        decided_on=run.decided_on,
        explain=run.explain,
        data=data,
        warnings=tuple(warnings),
        source=histories[0].source,
        as_of=table.dates[-1],
    )


def normalise_symbols(symbols: list[str]) -> list[str]:
    out: list[str] = []
    for raw in symbols:
        symbol = raw.strip().upper()
        if symbol and symbol not in out:
            out.append(symbol)
    if not out:
        raise LibraryError("INVALID_REQUEST", "Give at least one symbol.")
    if len(out) > MAX_SYMBOLS:
        raise LibraryError("INVALID_REQUEST", f"At most {MAX_SYMBOLS} symbols per backtest.")
    return out


async def run_backtest(
    provider: DailyHistoryProvider,
    *,
    strategy_id: str,
    symbols: list[str],
    benchmark: list[str] | None = None,
    params: dict[str, Any] | None = None,
    lag: int = 1,
    start: date | None = None,
    cost_bps: float | None = None,
    preset: Preset | None = None,
    use_cache: bool = True,
) -> BacktestReport:
    try:
        strategy = get_strategy(strategy_id)
    except KeyError as exc:
        raise LibraryError("UNKNOWN_STRATEGY", str(exc.args[0])) from None
    symbols = normalise_symbols(symbols)
    if len(symbols) < strategy.min_symbols:
        raise LibraryError(
            "INVALID_REQUEST", f"{strategy.name} needs at least {strategy.min_symbols} symbols."
        )
    try:
        resolved = strategy.resolve_params(params)
    except ValueError as exc:
        raise LibraryError("INVALID_REQUEST", str(exc)) from None
    if not 0 <= lag <= 5:
        raise LibraryError("INVALID_REQUEST", "Execution lag must be 0 to 5 sessions.")
    bench_symbols = normalise_symbols(benchmark) if benchmark else []

    key = (
        strategy.id,
        tuple(symbols),
        tuple(bench_symbols),
        tuple(sorted(resolved.items())),
        lag,
        start,
        cost_bps,
        datetime.now(UTC).date(),
    )
    if use_cache:
        hit = _RESULTS.get(key)
        if hit is not None and time.monotonic() - hit[0] < RESULT_TTL_SECONDS:
            _RESULTS.move_to_end(key)
            return hit[1]

    histories = await load_histories(provider, symbols)
    bench_histories = await load_histories(provider, bench_symbols) if bench_symbols else []
    report = await asyncio.to_thread(
        build_report,
        strategy,
        histories,
        bench_histories,
        resolved,
        lag=lag,
        start=start,
        cost_bps_override=cost_bps,
        preset=preset,
    )
    if use_cache:
        _RESULTS[key] = (time.monotonic(), report)
        while len(_RESULTS) > RESULT_CACHE_SIZE:
            _RESULTS.popitem(last=False)
    return report
