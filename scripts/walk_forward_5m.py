"""Rolling walk-forward over the intraday setups (Phase 88, §22).

Research only: reads bars, writes nothing, trades nothing.

The question this answers is narrower and harder than "which setup had
the best backtest". It is: **if you had picked the best setup on the last
N sessions, would it have been the best on the next M?** A single
train/test split answers that once, with whatever luck that one boundary
carried. Rolling the boundary forward answers it repeatedly, and the
spread across folds is the part worth reading - a procedure that picks a
different winner every fold has discovered nothing but noise, however
good any individual fold looks.

Two properties keep the result honest:

* **The test window is always AFTER the train window it was selected on,
  and the two never overlap.** Anything else is selection on data the
  choice already saw.
* **Selection is by out-of-sample expectancy on the train window only.**
  The test window is never consulted to choose; it is only ever scored.

Both the per-fold table and the pooled out-of-sample figure are printed.
The pooled figure is the one that means anything: it is the result of
actually following the procedure.

    python scripts/walk_forward_5m.py --symbol TQQQ.US --train 40 --test 20
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import statistics
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from apps.api.app.backtesting.costs import CostModel  # noqa: E402
from apps.api.app.backtesting.intraday_engine import (  # noqa: E402
    IntradayRunConfig,
    run_intraday_backtest,
)
from apps.api.app.backtesting.setups import SETUPS  # noqa: E402
from apps.api.app.marketdata.sessions import session_date  # noqa: E402
from apps.api.app.marketdata.store import MarketDataStore  # noqa: E402


def _t_stat(rs: list[float]) -> float:
    if len(rs) < 2:
        return float("nan")
    sd = statistics.stdev(rs)
    if sd == 0:
        return float("nan")
    return statistics.fmean(rs) / (sd / math.sqrt(len(rs)))


def _slice(bars, days, lo, hi):
    """Bars whose SESSION date falls in `days[lo:hi]`.

    Sliced by session rather than by bar count so a fold boundary never
    lands in the middle of a trading day - half a session is not a sample
    of a session, and a setup that only fires in the first hour would be
    scored on folds that systematically include or exclude it.
    """
    wanted = set(days[lo:hi])
    return [b for b in bars if session_date(b.ts) in wanted]


def _score(bars, htf, confirm, symbol, setup, costs, min_score):
    if not bars:
        return [], 0
    result = run_intraday_backtest(
        bars,
        IntradayRunConfig(symbol=symbol, setups=(setup,), costs=costs, min_score=min_score),
        higher_timeframe_bars=htf or None,
        confirm_bars=confirm or None,
    )
    return [float(t.r_multiple) for t in result.trades], result.sessions_traded


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="TQQQ.US")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--train", type=int, default=40, help="sessions per train window")
    ap.add_argument("--test", type=int, default=20, help="sessions per test window")
    ap.add_argument(
        "--step",
        type=int,
        default=0,
        help="sessions the window advances each fold; defaults to --test (non-overlapping tests)",
    )
    ap.add_argument("--setups", nargs="+", default=sorted(SETUPS))
    ap.add_argument("--min-score", type=int, default=0)
    ap.add_argument("--min-trades", type=int, default=10,
                    help="a setup with fewer trades in a train window is not selectable")
    ap.add_argument("--confirm-symbol", default="QQQ.US")
    args = ap.parse_args()
    step = args.step or args.test

    url = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://trading_os:trading_os@localhost:5432/trading_os"
    )
    if "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    end = datetime.now(UTC).date()
    start = end - timedelta(days=args.days)
    async with factory() as session:
        store = MarketDataStore(session)
        bars = await store.get_bars(
            args.symbol, bar_interval=args.interval, start_date=start, end_date=end
        )
        htf = await store.get_bars(args.symbol, bar_interval="15m", start_date=start, end_date=end)
        confirm = await store.get_bars(
            args.confirm_symbol, bar_interval=args.interval, start_date=start, end_date=end
        )
    await engine.dispose()

    if not bars:
        print(f"NO DATA for {args.symbol} {args.interval}")
        return 1

    days = sorted({session_date(b.ts) for b in bars})
    if len(days) < args.train + args.test:
        print(
            f"INSUFFICIENT DATA: {len(days)} sessions stored, "
            f"{args.train + args.test} needed for one fold. No walk-forward is reported "
            f"rather than one computed from a shortened window."
        )
        return 1

    costs = CostModel(fee_bps=Decimal("1"), slippage_bps=Decimal("2"))
    print(
        f"{args.symbol} {args.interval}  {len(days)} sessions  "
        f"train={args.train} test={args.test} step={step}  min_score={args.min_score}  "
        f"costs: 1bp fee + 2bps slippage"
    )
    header = (
        f"{'fold':<6}{'train window':<26}{'picked':<24}"
        f"{'trainExpR':>10}{'testExpR':>10}{'testN':>7}"
    )
    print(header)
    print("-" * len(header))

    pooled: list[float] = []
    picks: list[str] = []
    fold = 0
    lo = 0
    while lo + args.train + args.test <= len(days):
        tr_lo, tr_hi = lo, lo + args.train
        te_lo, te_hi = tr_hi, tr_hi + args.test
        train_bars = _slice(bars, days, tr_lo, tr_hi)
        test_bars = _slice(bars, days, te_lo, te_hi)

        best_name, best_exp = None, None
        for name in args.setups:
            if name not in SETUPS:
                continue
            rs, _ = _score(
                train_bars, htf, confirm, args.symbol, name, costs, args.min_score
            )
            if len(rs) < args.min_trades:
                continue
            exp = statistics.fmean(rs)
            if best_exp is None or exp > best_exp:
                best_name, best_exp = name, exp

        fold += 1
        if best_name is None:
            # A real, expected outcome: no setup cleared the trade
            # minimum on this window. The fold is reported as a
            # no-selection rather than filled with the least-bad option.
            print(
                f"{fold:<6}{days[tr_lo]!s}..{days[tr_hi - 1]!s}  "
                f"{'(no setup met --min-trades)':<24}{'':>10}{'':>10}{'':>7}"
            )
            lo += step
            continue

        test_rs, _ = _score(
            test_bars, htf, confirm, args.symbol, best_name, costs, args.min_score
        )
        pooled.extend(test_rs)
        picks.append(best_name)
        test_exp = statistics.fmean(test_rs) if test_rs else float("nan")
        print(
            f"{fold:<6}{days[tr_lo]!s}..{days[tr_hi - 1]!s}  {best_name:<24}"
            f"{best_exp:>10.3f}{test_exp:>10.3f}{len(test_rs):>7}"
        )
        lo += step

    print("-" * len(header))
    if not pooled:
        print("No out-of-sample trades at all: nothing to conclude.")
        return 0

    exp = statistics.fmean(pooled)
    t = _t_stat(pooled)
    distinct = sorted(set(picks))
    print(
        f"pooled out-of-sample: {len(pooled)} trades  expR {exp:+.3f}  t {t:+.2f}  "
        f"totR {sum(pooled):+.2f}"
    )
    print(
        f"selection stability: {len(distinct)} distinct setup(s) chosen across "
        f"{len(picks)} fold(s) -> {', '.join(distinct)}"
    )
    if exp <= 0:
        print(
            "READ THIS AS: following this selection procedure would have lost money "
            "out of sample. That is the result, not a reason to retune the windows."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
