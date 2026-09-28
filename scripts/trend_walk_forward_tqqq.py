"""Optimisation loop for the trend ride: `trend_pullback` entries with the
structure-trail exit, selected by rolling walk-forward (Phase 101, D120).

Research only: reads bars, writes a JSON report, trades nothing.

The procedure, fixed before the first run:

1. Every parameter combination in GRID is backtested ONCE over every
   stored session through the house intraday engine
   (`run_intraday_backtest`) with real costs (1bp fee + 2bps slippage),
   the engine's own risk controls, one position at a time, and
   `BracketPlan(structure_trail=True)`.
2. Sessions are split into rolling folds - TRAIN sessions, then the
   strictly later TEST sessions, stepping by the test length. A trade
   belongs to the window its ENTRY session is in. In the overnight mode a
   train-window trade whose exit falls after the train window is dropped
   from the train score, so selection never sees a price from the test
   window.
3. In each fold the combination with the best TRAIN expectancy (mean R
   per trade, at least `--min-trades` trades) is selected. Only that
   combination is scored on the TEST window. The test window is never
   consulted to choose.
4. The pooled out-of-sample trades are the result. Everything else -
   the in-sample best, the per-fold train figures - is context.

It runs twice, as two separately declared procedures: flat by the bell
(the house convention) and held overnight until the structure breaks
(what the operator's brief literally asks for). Neither is picked after
the fact; both are reported.

    python scripts/trend_walk_forward_tqqq.py --symbol TQQQ.US --train 40 --test 20
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import math
import os
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.api.app.backtesting.brackets import BracketPlan  # noqa: E402
from apps.api.app.backtesting.costs import CostModel  # noqa: E402
from apps.api.app.backtesting.intraday_engine import (  # noqa: E402
    IntradayRunConfig,
    run_intraday_backtest,
)
from apps.api.app.backtesting.setups import SETUPS, trend_pullback_setup  # noqa: E402
from apps.api.app.marketdata.sessions import is_regular_hours, session_date  # noqa: E402
from apps.api.app.marketdata.structure import Direction  # noqa: E402

DIRECTIONS = {
    "long": (Direction.LONG,),
    "short": (Direction.SHORT,),
    "both": (Direction.LONG, Direction.SHORT),
}

GRID = {
    "swing_strength": [2, 3],
    "min_trend_legs": [1, 2],
    "depth_band": [(0.0, 1.0), (0.236, 0.618), (0.5, 0.786)],
    "support": ["any", "ema9", "ema21", "fib"],
    "stop_buffer_atr": [0.1, 0.3],
    "direction": ["long", "short", "both"],
    "atr_trail": [None, 2.0],
}
"""Seven parameters, 576 combinations, chosen from the trend-anatomy study
(docs/RESEARCH_TREND_TQQQ.md) BEFORE any walk-forward was run: the depth
bands bracket the measured median retracement (~0.6 of the impulse), the
supports are the levels pullbacks most often touched, and the ATR trail is
the operator's "trailing take profit if the trend keeps continuing"."""

BASELINE = {
    "swing_strength": 2,
    "min_trend_legs": 1,
    "depth_band": (0.236, 0.786),
    "support": "any",
    "stop_buffer_atr": 0.3,
    "direction": "both",
    "atr_trail": None,
}
"""The setup's own defaults, scored with no selection at all."""


@dataclass(frozen=True)
class T:
    """One trade, reduced to what the fold arithmetic needs."""

    entry_day: str
    exit_ts: str
    r: float
    pnl: float
    direction: str
    reason: str


def combo_key(c: dict) -> str:
    lo, hi = c["depth_band"]
    trail = "none" if c["atr_trail"] is None else f"{c['atr_trail']:g}"
    return (
        f"s{c['swing_strength']}|legs{c['min_trend_legs']}|depth{lo:g}-{hi:g}|{c['support']}"
        f"|buf{c['stop_buffer_atr']:g}|{c['direction']}|trail{trail}"
    )


def all_combos() -> list[dict]:
    keys = list(GRID)
    return [
        dict(zip(keys, values, strict=True))
        for values in itertools.product(*(GRID[k] for k in keys))
    ]


# --- worker ------------------------------------------------------------------

_BARS: list = []


def _init(bars: list) -> None:
    """Hold the bars for this worker and memoise the engine's indicator
    calls across combinations.

    The memo is exact, not approximate: every window the engine hands an
    indicator is a fresh list of references to the SAME bar objects (or
    their `close` Decimals), so the identities of its elements plus the
    arguments identify the window completely for as long as `_BARS` is
    alive. Results are identical to an unmemoised run; only the
    grid gets faster, because indicator values do not depend on the setup
    parameters being searched. The key is the identity of EVERY element,
    so two windows that merely share endpoints can never collide.
    """
    global _BARS
    _BARS = bars
    from apps.api.app.backtesting import intraday_engine

    original = intraday_engine._indicator
    memo: dict = {}

    def cached(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        window = args[0] if args else None
        if not window or kwargs:
            return original(fn, *args, **kwargs)
        key = (fn.__name__, tuple(map(id, window)), args[1:])
        if key not in memo:
            memo[key] = original(fn, *args, **kwargs)
        return memo[key]

    intraday_engine._indicator = cached  # type: ignore[assignment]


def run_combo(args: tuple[dict, bool, str]) -> tuple[str, list[T]]:
    combo, overnight, symbol = args
    name = "trend_pullback__wf"
    lo, hi = combo["depth_band"]
    directions = DIRECTIONS[combo["direction"]]
    SETUPS[name] = partial(
        trend_pullback_setup,
        min_trend_legs=combo["min_trend_legs"],
        min_depth=Decimal(str(lo)),
        max_depth=Decimal(str(hi)),
        support=combo["support"],
        atr_stop_buffer=Decimal(str(combo["stop_buffer_atr"])),
        directions=directions,
    )
    plan = BracketPlan(
        structure_trail=True,
        structure_swing_strength=combo["swing_strength"],
        structure_trail_buffer_atr=Decimal(str(combo["stop_buffer_atr"])),
        structure_atr_trail_multiple=(
            None if combo["atr_trail"] is None else Decimal(str(combo["atr_trail"]))
        ),
        hold_overnight=overnight,
    )
    config = IntradayRunConfig(
        symbol=symbol,
        setups=(name,),
        plan=plan,
        costs=CostModel(fee_bps=Decimal("1"), slippage_bps=Decimal("2")),
        swing_strength=combo["swing_strength"],
        allow_directions=directions,
    )
    result = run_intraday_backtest(_BARS, config)
    trades = [
        T(
            entry_day=session_date(t.entry_ts).isoformat(),
            exit_ts=(t.exit_ts or t.entry_ts).isoformat(),
            r=float(t.r_multiple),
            pnl=float(t.gross_pnl),
            direction=t.direction.value,
            reason=t.fills[-1].reason.value if t.fills else "",
        )
        for t in result.trades
    ]
    return combo_key(combo), trades


# --- statistics ----------------------------------------------------------------


def t_stat(rs: list[float]) -> float | None:
    if len(rs) < 2:
        return None
    sd = statistics.stdev(rs)
    return statistics.fmean(rs) / (sd / math.sqrt(len(rs))) if sd > 0 else None


def max_drawdown(path: list[float]) -> float:
    peak = dd = 0.0
    level = 0.0
    for step in path:
        level += step
        peak = max(peak, level)
        dd = min(dd, level - peak)
    return dd


def metrics(trades: list[T], equity: float = 100_000.0) -> dict:
    rs = [t.r for t in trades]
    if not rs:
        return {"trades": 0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    eq_path = [t.pnl for t in trades]
    level, peak, dd_pct = equity, equity, 0.0
    for p in eq_path:
        level += p
        peak = max(peak, level)
        dd_pct = min(dd_pct, (level - peak) / peak * 100)
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    return {
        "trades": len(rs),
        "win_rate_pct": 100 * len(wins) / len(rs),
        "expectancy_r": statistics.fmean(rs),
        "t_stat": t_stat(rs),
        "total_r": sum(rs),
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        "max_drawdown_r": max_drawdown(rs),
        "return_pct": sum(eq_path) / equity * 100,
        "max_drawdown_pct": dd_pct,
        "avg_win_r": statistics.fmean(wins) if wins else None,
        "avg_loss_r": statistics.fmean(losses) if losses else None,
        "longs": sum(t.direction == "long" for t in trades),
        "shorts": sum(t.direction == "short" for t in trades),
        "exit_reasons": reasons,
    }


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None

    def ranks(v: list[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        out = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                out[order[k]] = (i + j) / 2
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den > 0 else None


# --- the procedure --------------------------------------------------------------


def buy_and_hold(regular: list, days: list[str]) -> float | None:
    wanted = set(days)
    window = [b for b in regular if session_date(b.ts).isoformat() in wanted]
    if not window:
        return None
    first = float(window[0].open if window[0].open is not None else window[0].close)
    return (float(window[-1].close) / first - 1) * 100


def walk_forward(
    results: dict[str, list[T]],
    days: list[str],
    regular: list,
    *,
    train: int,
    test: int,
    step: int,
    min_trades: int,
    overnight: bool,
) -> dict:
    last_bar_of_day: dict[str, str] = {}
    for b in regular:
        last_bar_of_day[session_date(b.ts).isoformat()] = b.ts.isoformat()

    folds = []
    pooled: list[T] = []
    bh_chain = 1.0
    lo = 0
    while lo + train + test <= len(days):
        tr_days = days[lo : lo + train]
        te_days = days[lo + train : lo + train + test]
        tr_set, te_set = set(tr_days), set(te_days)
        tr_end = last_bar_of_day[tr_days[-1]]
        scored = []
        for key, trades in results.items():
            tr = [
                t
                for t in trades
                if t.entry_day in tr_set and (not overnight or t.exit_ts <= tr_end)
            ]
            if len(tr) < min_trades:
                continue
            scored.append((statistics.fmean(t.r for t in tr), len(tr), key))
        test_by_key = {
            key: [t for t in trades if t.entry_day in te_set] for key, trades in results.items()
        }
        fold: dict = {
            "train": [tr_days[0], tr_days[-1]],
            "test": [te_days[0], te_days[-1]],
            "eligible_combos": len(scored),
            "buy_and_hold_pct": buy_and_hold(regular, te_days),
        }
        if scored:
            # Diagnostic only: does a good train score predict a good test
            # score ACROSS the grid? Never used to choose.
            pairs = [
                (exp, statistics.fmean(t.r for t in test_by_key[key]))
                for exp, _, key in scored
                if test_by_key[key]
            ]
            fold["train_test_rank_corr"] = spearman([p[0] for p in pairs], [p[1] for p in pairs])
            fold["eligible_median_test_expectancy_r"] = (
                statistics.median(p[1] for p in pairs) if pairs else None
            )
            scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
            best_exp, best_n, best_key = scored[0]
            tr_trades = [
                t
                for t in results[best_key]
                if t.entry_day in tr_set and (not overnight or t.exit_ts <= tr_end)
            ]
            te_trades = test_by_key[best_key]
            pooled.extend(te_trades)
            fold.update(
                {
                    "picked": best_key,
                    "train_metrics": metrics(tr_trades),
                    "test_metrics": metrics(te_trades),
                    "runner_up": [k for _, _, k in scored[1:4]],
                }
            )
        else:
            fold["picked"] = None
        if fold["buy_and_hold_pct"] is not None:
            bh_chain *= 1 + fold["buy_and_hold_pct"] / 100
        folds.append(fold)
        lo += step

    oos_days = [d for f in folds for d in days if f["test"][0] <= d <= f["test"][1]]
    return {
        "folds": folds,
        "pooled_oos": metrics(pooled),
        "distinct_picks": sorted({f["picked"] for f in folds if f["picked"]}),
        "buy_and_hold_oos_pct": (bh_chain - 1) * 100,
        "oos_sessions": len(oos_days),
    }


async def load(symbol: str, days: int) -> list:
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
    async with factory() as session:
        bars = await MarketDataStore(session).get_bars(
            symbol, bar_interval="5m", start_date=end - timedelta(days=days), end_date=end
        )
    await engine.dispose()
    return bars


def fmt(m: dict) -> str:
    if not m.get("trades"):
        return "0 trades"
    t = m["t_stat"]
    return (
        f"{m['trades']} trades, win {m['win_rate_pct']:.0f}%, expR {m['expectancy_r']:+.3f}, "
        f"t {t:+.2f}, totR {m['total_r']:+.1f}, maxDD {m['max_drawdown_r']:.1f}R, "
        f"ret {m['return_pct']:+.2f}%"
        if t is not None
        else f"{m['trades']} trades, expR {m['expectancy_r']:+.3f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="TQQQ.US")
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--train", type=int, default=40)
    ap.add_argument("--test", type=int, default=20)
    ap.add_argument("--step", type=int, default=0)
    ap.add_argument("--min-trades", type=int, default=15)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--modes", nargs="+", default=["intraday", "overnight"])
    ap.add_argument("--out", default="")
    ap.add_argument("--smoke", action="store_true", help="run the baseline combination only")
    args = ap.parse_args()
    step = args.step or args.test

    bars = asyncio.run(load(args.symbol, args.days))
    if not bars:
        print(f"NO DATA for {args.symbol} 5m")
        return 1
    regular = [b for b in bars if is_regular_hours(b.ts)]
    days = sorted({session_date(b.ts).isoformat() for b in regular})
    if len(days) < args.train + args.test:
        print(f"INSUFFICIENT DATA: {len(days)} sessions, need {args.train + args.test}.")
        return 1

    combos = all_combos()
    if args.smoke:
        # A wiring check, not a result: the baseline alone, printed and
        # never written as a report.
        for overnight in (False, True):
            _init(bars)
            key, trades = run_combo((BASELINE, overnight, args.symbol))
            print(f"smoke overnight={overnight} {key}: {fmt(metrics(trades))}")
            print(f"   exits: {metrics(trades).get('exit_reasons')}")
        return 0
    report: dict = {
        "phase": "101",
        "decision": "D120",
        "symbol": args.symbol,
        "generated": datetime.now(UTC).isoformat(),
        "sessions": len(days),
        "first_session": days[0],
        "last_session": days[-1],
        "costs": "1bp fee + 2bps slippage per side",
        "train_sessions": args.train,
        "test_sessions": args.test,
        "step_sessions": step,
        "min_train_trades": args.min_trades,
        "grid": {k: [list(v) if isinstance(v, tuple) else v for v in vs] for k, vs in GRID.items()},
        "combos": len(combos),
        "buy_and_hold_full_pct": buy_and_hold(regular, days),
        "modes": {},
    }
    print(
        f"{args.symbol}: {len(days)} sessions {days[0]}..{days[-1]}, {len(combos)} combos, "
        f"train {args.train} / test {args.test} / step {step}, min {args.min_trades} train trades"
    )

    for mode in args.modes:
        overnight = mode == "overnight"
        started = time.time()
        with ProcessPoolExecutor(
            max_workers=args.workers, initializer=_init, initargs=(bars,)
        ) as pool:
            results = dict(
                pool.map(run_combo, [(c, overnight, args.symbol) for c in combos], chunksize=4)
            )
        print(f"\n== mode: {mode}  ({time.time() - started:.0f}s for the grid)")

        wf = walk_forward(
            results,
            days,
            regular,
            train=args.train,
            test=args.test,
            step=step,
            min_trades=args.min_trades,
            overnight=overnight,
        )
        for i, f in enumerate(wf["folds"], 1):
            train_span = f"{f['train'][0]}..{f['train'][1]}"
            if not f["picked"]:
                print(f"fold {i} {train_span} -> no combination met the trade minimum")
                continue
            print(f"fold {i} train {train_span}  test {f['test'][0]}..{f['test'][1]}")
            print(f"   picked {f['picked']}")
            print(f"   train: {fmt(f['train_metrics'])}")
            print(f"   test:  {fmt(f['test_metrics'])}   buy&hold {f['buy_and_hold_pct']:+.1f}%")
            print(f"   rank corr(train expR, test expR) across grid: {f['train_test_rank_corr']}")
        bh = wf["buy_and_hold_oos_pct"]
        print(f"POOLED OOS: {fmt(wf['pooled_oos'])}   buy&hold over the same sessions {bh:+.1f}%")

        full = {key: metrics(trades) for key, trades in results.items()}
        ranked = sorted(
            (k for k in full if full[k].get("trades", 0) >= 2 * args.min_trades),
            key=lambda k: full[k]["expectancy_r"],
            reverse=True,
        )
        in_sample_best = ranked[0] if ranked else None
        baseline_key = combo_key(BASELINE)
        if baseline_key in results:
            baseline_trades = results[baseline_key]
        else:
            # The setup's defaults are not a grid point; score them on
            # their own. They take no part in selection.
            _init(bars)
            baseline_trades = run_combo((BASELINE, overnight, args.symbol))[1]
        baseline_metrics = metrics(baseline_trades)
        oos_days = {d for f in wf["folds"] for d in days if f["test"][0] <= d <= f["test"][1]}
        positive = sum(1 for k in full if full[k].get("trades") and full[k]["expectancy_r"] > 0)
        print(
            f"IN-SAMPLE best over all {len(days)} sessions (selected with hindsight): "
            f"{in_sample_best} -> {fmt(full[in_sample_best]) if in_sample_best else 'n/a'}"
        )
        baseline_full = fmt(baseline_metrics)
        print(f"baseline (setup defaults, no selection), all sessions: {baseline_full}")
        print(
            "baseline on the same OOS sessions: "
            f"{fmt(metrics([t for t in baseline_trades if t.entry_day in oos_days]))}"
        )
        print(f"combos with positive full-period expectancy: {positive} of {len(full)}")

        report["modes"][mode] = {
            "hold_overnight": overnight,
            "walk_forward": wf,
            "in_sample_best": {"combo": in_sample_best, "metrics": full.get(in_sample_best)}
            if in_sample_best
            else None,
            "baseline": {
                "combo": baseline_key,
                "all_sessions": baseline_metrics,
                "oos_sessions": metrics([t for t in baseline_trades if t.entry_day in oos_days]),
            },
            "combos_positive_full_period": positive,
            "top10_full_period_in_sample": [{"combo": k, "metrics": full[k]} for k in ranked[:10]],
            "full_period_distribution": {
                "median_expectancy_r": statistics.median(
                    [m["expectancy_r"] for m in full.values() if m.get("trades")]
                ),
                "median_trades": statistics.median([m.get("trades", 0) for m in full.values()]),
            },
        }

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(
                report,
                fh,
                indent=2,
                default=lambda o: asdict(o) if hasattr(o, "__dataclass_fields__") else str(o),
            )
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
