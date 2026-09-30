"""FX intraday research: the existing setups, on real EUR/USD bars, under
the spread cost model and the 24h FX calendar (Phase 103, D123).

Research only: reads local files or the bar store, writes nothing to the
database, trades nothing. Results are written up in docs/RESEARCH_FX.md.

Two data sources, and the output names which one was used:

* `--histdata FILE [FILE ...]` - HistData.com free 1-minute BID files,
  read from local disk (see `marketdata/providers/histdata.py` for the
  terms and the bid-vs-mid caveat). Resampled here to 5m and 15m.
* otherwise the bar store (`market_data_bars`), e.g. IG mid bars once the
  IG key works and `FX_BAR_PROVIDER=ig` has backfilled them.

What it runs:

1. Every setup over the whole sample, at the pair's default half-spread,
   at zero cost (to show how much the spread takes) and at a stress
   multiple of the default.
2. A rolling walk-forward identical in rule to `walk_forward_5m.py`: pick
   the best setup by expectancy on `--train` sessions (min `--min-trades`
   trades), score it on the next `--test` sessions, roll by `--test`.

**One full-series replay per setup, then bucket trades by session.** The
engine is strictly causal and every one of its per-session controls
(daily loss limit, consecutive-loss pause, one position at a time) resets
each session, so a trade's existence and R never depend on a later
session. Replaying once and assigning trades to folds by session date is
therefore the same experiment as replaying each fold separately - except
that a fold's first session keeps its real previous-day levels instead of
losing them to the slice boundary. It is also ~20x faster.

    python scripts/research_fx.py --histdata DAT_ASCII_EURUSD_M1_2024.csv \\
        DAT_ASCII_EURUSD_M1_2025.csv --symbol EURUSD.FX
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.api.app.backtesting.costs import FxSpreadCostModel  # noqa: E402
from apps.api.app.backtesting.intraday_engine import (  # noqa: E402
    IntradayRunConfig,
    run_intraday_backtest,
)
from apps.api.app.backtesting.setups import SETUPS  # noqa: E402
from apps.api.app.marketdata.fx import calendar_for_symbol, pair_spec, resample_bars  # noqa: E402
from apps.api.app.marketdata.providers.histdata import load_m1_file  # noqa: E402

VOLUME_GATED = ("quiet_pullback", "volume_climax_reversal", "vwap_reversion")
"""Setups that need volume (directly, or through session VWAP). On FX they
must produce zero signals - asserted in the output, not assumed."""


def _t(rs: list[float]) -> float:
    if len(rs) < 2:
        return float("nan")
    sd = statistics.stdev(rs)
    return float("nan") if sd == 0 else statistics.fmean(rs) / (sd / math.sqrt(len(rs)))


def _row(name: str, rs: list[float]) -> str:
    if not rs:
        return f"{name:<24}{0:>6}{'':>9}{'':>8}{'':>8}{'':>9}"
    wins = sum(1 for r in rs if r > 0) / len(rs) * 100
    return (
        f"{name:<24}{len(rs):>6}{statistics.fmean(rs):>+9.3f}{_t(rs):>+8.2f}"
        f"{wins:>7.1f}%{sum(rs):>+9.1f}"
    )


async def _load_store(symbol: str, interval: str, days: int):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from apps.api.app.marketdata.store import MarketDataStore

    url = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://trading_os:trading_os@localhost:5432/trading_os"
    )
    if "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    end = datetime.now(UTC).date()
    start = end - timedelta(days=days)
    async with factory() as session:
        store = MarketDataStore(session)
        bars = await store.get_bars(symbol, bar_interval=interval, start_date=start, end_date=end)
        htf = await store.get_bars(symbol, bar_interval="15m", start_date=start, end_date=end)
    await engine.dispose()
    return bars, htf


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="EURUSD.FX")
    ap.add_argument("--histdata", nargs="*", default=None)
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--days", type=int, default=730, help="bar-store window (store mode)")
    ap.add_argument("--half-spread-pips", type=Decimal, default=None)
    ap.add_argument("--stress-multiple", type=Decimal, default=Decimal("2"))
    ap.add_argument("--setups", nargs="+", default=sorted(SETUPS))
    ap.add_argument("--train", type=int, default=60)
    ap.add_argument("--test", type=int, default=20)
    ap.add_argument("--min-trades", type=int, default=10)
    ap.add_argument("--trade-asian-session", action="store_true")
    args = ap.parse_args()

    from apps.api.app.marketdata.sessions import FX_CALENDAR, FxCalendar

    calendar = FxCalendar(trade_asian_session=True) if args.trade_asian_session else FX_CALENDAR
    assert calendar_for_symbol(args.symbol) is FX_CALENDAR, "not an FX symbol"
    spec = pair_spec(args.symbol)
    half = spec.default_half_spread_pips if args.half_spread_pips is None else args.half_spread_pips

    t0 = time.time()
    if args.histdata:
        m1 = []
        for path in args.histdata:
            m1.extend(load_m1_file(path, symbol=args.symbol))
        m1.sort(key=lambda b: b.ts)
        bars = resample_bars(m1, args.interval, symbol=args.symbol)
        htf = resample_bars(m1, "15m", symbol=args.symbol)
        source = f"HistData.com free M1 BID files ({len(args.histdata)}), resampled"
        del m1
    else:
        bars, htf = asyncio.run(_load_store(args.symbol, args.interval, args.days))
        source = f"bar store ({bars[0].source if bars else 'empty'})"
    if not bars:
        print(f"NO DATA for {args.symbol} {args.interval}: nothing to research.")
        return 1

    sessions = sorted(
        {calendar.session_date(b.ts) for b in bars if calendar.is_regular_hours(b.ts)}
    )
    print(
        f"{args.symbol} {args.interval} from {source}; {len(bars)} bars, {len(sessions)} "
        f"sessions {sessions[0]}..{sessions[-1]}; calendar={calendar.name}"
        f"{' (Asia traded)' if args.trade_asian_session else ' (London+NY traded)'}; "
        f"loaded in {time.time() - t0:.0f}s"
    )
    print(
        f"cost model: half-spread {half} pips/side (round trip {half * 2} pips), "
        f"no financing (flat by the 17:00 NY roll)"
    )

    variants = {
        "zero": FxSpreadCostModel(
            pair=spec.pair, pip_size=spec.pip_size, half_spread_pips=Decimal(0)
        ),
        "base": FxSpreadCostModel(pair=spec.pair, pip_size=spec.pip_size, half_spread_pips=half),
        "stress": FxSpreadCostModel(
            pair=spec.pair, pip_size=spec.pip_size, half_spread_pips=half * args.stress_multiple
        ),
    }
    bias_cache: dict = {}
    trades_by: dict[str, dict[str, list[tuple[date, float]]]] = defaultdict(dict)
    signals_seen: dict[str, int] = {}
    stop_bp: dict[str, list[float]] = defaultdict(list)
    for name in args.setups:
        if name not in SETUPS:
            continue
        cache: dict = {}
        for label, costs in variants.items():
            t1 = time.time()
            result = run_intraday_backtest(
                bars,
                IntradayRunConfig(
                    symbol=args.symbol, setups=(name,), costs=costs, calendar=calendar
                ),
                higher_timeframe_bars=htf,
                signal_cache=cache,
                bias_cache=bias_cache,
            )
            trades_by[name][label] = [
                (calendar.session_date(t.entry_ts), float(t.r_multiple)) for t in result.trades
            ]
            if label == "base":
                signals_seen[name] = result.signals_seen
                stop_bp[name] = [float(t.risk_per_share / spec.pip_size) for t in result.trades]
            took = time.time() - t1
            print(
                f"  ran {name:<24}{label:<7}{len(result.trades):>5} trades {took:>6.0f}s",
                flush=True,
            )

    print()
    print("== volume-gated setups on a series with no volume ==")
    for name in VOLUME_GATED:
        if name in signals_seen:
            print(f"  {name:<24} signals_seen={signals_seen[name]}")

    for label in ("zero", "base", "stress"):
        print()
        print(f"== full sample, costs={label} ==")
        print(f"{'setup':<24}{'n':>6}{'expR':>9}{'t':>8}{'win':>8}{'totR':>9}")
        for name in sorted(trades_by):
            print(_row(name, [r for _, r in trades_by[name][label]]))

    print()
    print("== median initial stop distance (pips), base costs ==")
    for name in sorted(stop_bp):
        if stop_bp[name]:
            median = statistics.median(stop_bp[name])
            print(f"  {name:<24} {median:6.1f} pips over {len(stop_bp[name])}")

    print()
    print(
        f"== walk-forward (base costs): train={args.train} test={args.test} sessions, "
        f"min-trades={args.min_trades} =="
    )
    pooled: list[float] = []
    picks: list[str] = []
    fold, lo = 0, 0
    while lo + args.train + args.test <= len(sessions):
        train = set(sessions[lo : lo + args.train])
        test = set(sessions[lo + args.train : lo + args.train + args.test])
        best, best_exp = None, None
        for name, variants_trades in trades_by.items():
            rs = [r for d, r in variants_trades["base"] if d in train]
            if len(rs) < args.min_trades:
                continue
            exp = statistics.fmean(rs)
            if best_exp is None or exp > best_exp:
                best, best_exp = name, exp
        fold += 1
        window = f"{sessions[lo]}..{sessions[lo + args.train - 1]}"
        if best is None:
            print(f"  {fold:<4}{window}  (no setup met --min-trades)")
        else:
            test_rs = [r for d, r in trades_by[best]["base"] if d in test]
            pooled.extend(test_rs)
            picks.append(best)
            test_exp = statistics.fmean(test_rs) if test_rs else float("nan")
            print(
                f"  {fold:<4}{window}  {best:<24} train {best_exp:+.3f}  "
                f"test {test_exp:+.3f} (n={len(test_rs)})"
            )
        lo += args.test
    if pooled:
        print(
            f"  pooled out-of-sample: {len(pooled)} trades  expR {statistics.fmean(pooled):+.3f}  "
            f"t {_t(pooled):+.2f}  totR {sum(pooled):+.1f}"
        )
        distinct = sorted(set(picks))
        print(
            f"  selection stability: {len(distinct)} distinct pick(s) over {len(picks)} folds: "
            f"{', '.join(distinct)}"
        )
    else:
        print("  no out-of-sample trades at all: nothing to conclude.")
    print(f"total runtime {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
