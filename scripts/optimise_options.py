"""Walk-forward "improvement loop" over the MODELLED options backtest
(Phase 100, D119).

Research only: reads stored bars and any stored chain snapshots, writes a
JSON report, trades nothing.

**Every option price behind these numbers is MODELLED** (Black-Scholes on
real TQQQ daily closes, volatility = realised vol x an IV/RV multiplier);
see `apps/api/app/options/backtest.py`. The loop can only tell you which
configuration did best INSIDE THAT MODEL - and, because it is
walk-forward, whether picking the model's best on one year would have
kept working the next quarter. It cannot tell you what real fills would
have been.

The question is the same as `walk_forward_5m.py`'s: **if you had picked
the best configuration on the last `--train` sessions, would it have been
any good on the next `--test`?** Rules that keep it honest:

* train and test windows never overlap, the test always follows its train
  window, and a position open at a window's last bar is closed there - so
  no train trade can peek into its test window;
* selection uses the TRAIN window only (highest mean return-on-risk among
  configurations with at least `--min-trades` trades); the test window is
  only ever scored;
* every configuration is ALSO scored on each test window, so the report
  shows where the chosen one ranked among all of them out of sample - a
  selection procedure with skill ranks near the top; one without it ranks
  anywhere;
* the pooled out-of-sample result (every chosen configuration's test
  trades, in order) is the headline. The in-sample "best in hindsight"
  table is printed for context and labelled as what it is.

    python scripts/optimise_options.py --symbol TQQQ.US --train 252 --test 63
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from apps.api.app.options.backtest import (  # noqa: E402
    MODELLED_NOTE,
    ObservedChain,
    OptionsBacktestConfig,
    OptionsTrade,
    OptionStrategy,
    PreparedSeries,
    derive_iv_rv_multiplier,
    load_backtest_inputs,
    prepare_series,
    run_options_backtest,
    trade_stats,
)

STRATEGIES = [s.value for s in OptionStrategy]
DELTAS = [0.20, 0.30, 0.50]
DTES = [7, 14, 30, 45]
PROFIT_TARGETS: list[float | None] = [0.5, 1.0, None]
STOPS: list[float | None] = [0.5, 1.0, None]


def build_grid(
    strategies: list[str], deltas: list[float], dtes: list[int]
) -> list[OptionsBacktestConfig]:
    return [
        OptionsBacktestConfig(
            strategy=OptionStrategy(s),
            target_delta=d,
            dte_days=n,
            profit_target_pct=pt,
            stop_loss_pct=sl,
        )
        for s, d, n, pt, sl in itertools.product(strategies, deltas, dtes, PROFIT_TARGETS, STOPS)
    ]


# Worker state for the process pool (set once per process, not pickled per task).
_SERIES: PreparedSeries | None = None
_OBSERVED: dict[date, ObservedChain] = {}
_MULT = 1.2


def _init_worker(series: PreparedSeries, observed: dict[date, ObservedChain], mult: float) -> None:
    global _SERIES, _OBSERVED, _MULT
    _SERIES, _OBSERVED, _MULT = series, observed, mult


def _score(task: tuple[int, OptionsBacktestConfig, date, date]) -> tuple[int, dict[str, Any]]:
    idx, config, start, end = task
    assert _SERIES is not None
    result = run_options_backtest(
        _SERIES, config, iv_rv_multiplier=_MULT, start=start, end=end, observed=_OBSERVED
    )
    s = result.stats
    return idx, {
        "n": s.trades,
        "exp_r": s.expectancy_r,
        "t": s.t_stat_r,
        "pnl": float(s.total_pnl),
        "trades": result.trades,
    }


def _make_pool(
    series: PreparedSeries, observed: dict[date, ObservedChain], mult: float, workers: int
) -> ProcessPoolExecutor | None:
    """A process pool whose workers hold the series once, or None to run
    in-process (workers <= 1)."""
    if workers <= 1:
        _init_worker(series, observed, mult)
        return None
    return ProcessPoolExecutor(
        max_workers=workers, initializer=_init_worker, initargs=(series, observed, mult)
    )


def _evaluate(
    pool: ProcessPoolExecutor | None,
    grid: list[OptionsBacktestConfig],
    start: date,
    end: date,
    keep_trades: bool = False,
) -> list[dict[str, Any]]:
    tasks = [(i, c, start, end) for i, c in enumerate(grid)]
    if pool is None:
        results = [_score(t) for t in tasks]
    else:
        results = list(pool.map(_score, tasks, chunksize=16))
    out: list[dict[str, Any]] = [{}] * len(grid)
    for i, r in results:
        if not keep_trades:
            r = {k: v for k, v in r.items() if k != "trades"}
        out[i] = r
    return out


def _fmt(x: float | None, spec: str = "+.3f") -> str:
    return "  n/a" if x is None else format(x, spec)


def _buy_hold(series: PreparedSeries, start: date, end: date) -> float | None:
    i0 = series.index_on_or_after(start)
    i1 = series.index_on_or_before(end)
    if i0 >= len(series.days) or i1 <= i0:
        return None
    # From the close before the window's first entry to its last close.
    base = series.closes[max(i0 - 1, 0)]
    return series.closes[i1] / base - 1


def run_walk_forward(
    series: PreparedSeries,
    observed: dict[date, ObservedChain],
    mult: float,
    grid: list[OptionsBacktestConfig],
    *,
    train: int,
    test: int,
    step: int,
    min_trades: int,
    workers: int,
    select_by: str = "t_stat",
    verbose: bool = True,
) -> dict[str, Any]:
    """`select_by`: "t_stat" ranks train results by mean R / its standard
    error, "expectancy_r" by mean R alone. t_stat is the default because
    the grid mixes 7-DTE configurations (~30 trades a year) with 45-DTE
    ones (~8): ranked by raw mean, an 8-trade fluke beats a 30-trade edge
    of the same size almost every time, which is selecting on luck."""
    key = "t" if select_by == "t_stat" else "exp_r"
    first = series.rv_window  # the first close with a volatility estimate
    n = len(series.days)
    folds: list[dict[str, Any]] = []
    pooled: list[OptionsTrade] = []
    pool = _make_pool(series, observed, mult, workers)
    try:
        k = 0
        while True:
            tr0 = first + k * step
            tr1 = tr0 + train - 1
            te0 = tr1 + 1
            te1 = min(te0 + test - 1, n - 1)
            if te0 >= n or te1 - te0 < max(5, test // 3):
                break
            d_tr0, d_tr1, d_te0, d_te1 = (series.days[i] for i in (tr0, tr1, te0, te1))
            train_scores = _evaluate(pool, grid, d_tr0, d_tr1)
            eligible = [
                i for i, s in enumerate(train_scores)
                if s["n"] >= min_trades and s["exp_r"] is not None and s[key] is not None
            ]
            test_scores = _evaluate(pool, grid, d_te0, d_te1, keep_trades=True)
            fold: dict[str, Any] = {
                "fold": k + 1,
                "train": [d_tr0.isoformat(), d_tr1.isoformat()],
                "test": [d_te0.isoformat(), d_te1.isoformat()],
                "buy_hold_test_return": _buy_hold(series, d_te0, d_te1),
            }
            if not eligible:
                fold["chosen"] = None
                fold["why"] = f"no configuration had >= {min_trades} train trades"
                folds.append(fold)
                k += 1
                continue
            best = max(eligible, key=lambda i: (train_scores[i][key], train_scores[i]["pnl"]))
            chosen_test = test_scores[best]
            test_trades: list[OptionsTrade] = list(chosen_test["trades"])
            pooled.extend(test_trades)
            ranked = sorted(
                (s["exp_r"] for s in test_scores if s["exp_r"] is not None), reverse=True
            )
            rank_pct = None
            if chosen_test["exp_r"] is not None and ranked:
                better = sum(1 for r in ranked if r > chosen_test["exp_r"])
                rank_pct = better / len(ranked)
            stats = trade_stats(test_trades)
            fold.update(
                {
                    "chosen": grid[best].to_dict() | {"label": grid[best].label},
                    "train_trades": train_scores[best]["n"],
                    "train_expectancy_r": train_scores[best]["exp_r"],
                    "train_t_stat_r": train_scores[best]["t"],
                    "train_pnl": train_scores[best]["pnl"],
                    "eligible_configs": len(eligible),
                    "test_stats": stats.to_dict(),
                    "test_rank_fraction_better": rank_pct,
                    "test_configs_scored": len(ranked),
                    "test_median_expectancy_r_all_configs": (
                        ranked[len(ranked) // 2] if ranked else None
                    ),
                }
            )
            folds.append(fold)
            if verbose:
                print(
                    f"fold {k + 1:>2} train {d_tr0}..{d_tr1} test {d_te0}..{d_te1} | "
                    f"{grid[best].label:<42} | train n={train_scores[best]['n']:>3} "
                    f"R={_fmt(train_scores[best]['exp_r'])} | test n={stats.trades:>3} "
                    f"win={_fmt(stats.win_rate, '.0%')} R={_fmt(stats.expectancy_r)} "
                    f"pnl={float(stats.total_pnl):>9.2f} | "
                    f"rank={_fmt(rank_pct, '.0%')} of {len(ranked)} | "
                    f"B&H={_fmt(fold['buy_hold_test_return'], '+.1%')}",
                    flush=True,
                )
            k += 1
    finally:
        if pool is not None:
            pool.shutdown()
    pooled_stats = trade_stats(pooled)
    bh = [f["buy_hold_test_return"] for f in folds if f["buy_hold_test_return"] is not None]
    compounded = 1.0
    for r in bh:
        compounded *= 1 + r
    ranks = [f["test_rank_fraction_better"] for f in folds if f.get("test_rank_fraction_better")
             is not None]
    return {
        "folds": folds,
        "pooled_oos": pooled_stats.to_dict(),
        "pooled_oos_trades": [t.to_dict() for t in pooled],
        "folds_with_positive_oos_expectancy": sum(
            1
            for f in folds
            if f.get("test_stats") and (f["test_stats"]["expectancy_r"] or 0) > 0
        ),
        "folds_scored": sum(1 for f in folds if f.get("test_stats")),
        "mean_test_rank_fraction_better": (sum(ranks) / len(ranks)) if ranks else None,
        "buy_hold_over_test_windows": compounded - 1 if bh else None,
    }


def _summary(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "pooled_oos": run["pooled_oos"],
        "folds_with_positive_oos_expectancy": run["folds_with_positive_oos_expectancy"],
        "folds_scored": run["folds_scored"],
        "mean_test_rank_fraction_better": run["mean_test_rank_fraction_better"],
        "chosen_by_fold": [
            f["chosen"]["label"] if f.get("chosen") else None for f in run["folds"]
        ],
    }


async def _load(symbol: str) -> tuple[list[Any], dict[date, ObservedChain]]:
    url = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://trading_os:trading_os@localhost:5432/trading_os"
    )
    if "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        closes, observed = await load_backtest_inputs(
            session, symbol, start=date(2000, 1, 1), end=datetime.now(UTC).date()
        )
    await engine.dispose()
    return closes, observed


def _default_json(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(type(obj).__name__)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--symbol", default="TQQQ.US")
    ap.add_argument("--train", type=int, default=252, help="daily bars per train window")
    ap.add_argument("--test", type=int, default=63, help="daily bars per test window")
    ap.add_argument("--step", type=int, default=0, help="defaults to --test")
    ap.add_argument("--min-trades", type=int, default=8)
    ap.add_argument("--rv-window", type=int, default=20)
    ap.add_argument("--multiplier", type=float, default=None,
                    help="override the IV/RV multiplier "
                         "(default: measured from snapshots, else 1.2)")
    ap.add_argument("--sensitivity", nargs="*", type=float, default=[1.0, 1.5],
                    help="also re-run the whole walk-forward at these multipliers")
    ap.add_argument("--select-by", choices=["t_stat", "expectancy_r"], default="t_stat")
    ap.add_argument("--strategies", nargs="+", default=STRATEGIES)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    step = args.step or args.test

    closes, observed = asyncio.run(_load(args.symbol))
    if len(closes) < args.rv_window + args.train + 10:
        print(f"NOT ENOUGH DATA for {args.symbol}: {len(closes)} daily closes")
        return 1
    series = prepare_series(closes, args.rv_window)
    measured = derive_iv_rv_multiplier(series, observed)
    if args.multiplier is not None:
        mult_value, mult_source = args.multiplier, f"override --multiplier {args.multiplier}"
    else:
        mult_value, mult_source = measured.value, measured.source

    grid = build_grid(args.strategies, DELTAS, DTES)
    print("ALL OPTION PRICES BELOW ARE MODELLED (Black-Scholes), NOT HISTORICAL QUOTES.")
    print(
        f"{args.symbol}: {len(closes)} daily closes {series.days[0]}..{series.days[-1]}; "
        f"stored snapshot days: {len(observed)}"
    )
    print(f"IV/RV multiplier = {mult_value:.3f} ({mult_source})")
    print(f"grid: {len(grid)} configurations; train={args.train} test={args.test} step={step}")
    t0 = time.perf_counter()
    print(f"selection on train: {args.select_by}")
    main_run = run_walk_forward(
        series, observed, mult_value, grid,
        train=args.train, test=args.test, step=step,
        min_trades=args.min_trades, workers=args.workers, select_by=args.select_by,
    )
    p = main_run["pooled_oos"]
    print(
        f"\nPOOLED OUT-OF-SAMPLE (MODELLED): trades={p['trades']} win={p['win_rate']} "
        f"expectancy=${p['expectancy']} /trade  R={p['expectancy_r']}  t={p['t_stat_r']}  "
        f"total=${p['total_pnl']}  maxDD=${p['max_drawdown']}"
    )
    print(
        f"folds with positive OOS expectancy: {main_run['folds_with_positive_oos_expectancy']}"
        f"/{main_run['folds_scored']}; mean fraction of configs that beat the chosen one OOS: "
        f"{main_run['mean_test_rank_fraction_better']}; buy&hold over the same test windows: "
        f"{main_run['buy_hold_over_test_windows']}"
    )
    elapsed = time.perf_counter() - t0

    # In-sample context: the best configurations over the whole history.
    first_day = series.days[args.rv_window]
    pool = _make_pool(series, observed, mult_value, args.workers)
    try:
        whole = _evaluate(pool, grid, first_day, series.days[-1])
    finally:
        if pool is not None:
            pool.shutdown()
    order = sorted(
        (i for i, s in enumerate(whole) if s["n"] >= args.min_trades and s["exp_r"] is not None),
        key=lambda i: whole[i]["exp_r"],
        reverse=True,
    )
    in_sample_top = [
        {"label": grid[i].label, "trades": whole[i]["n"], "expectancy_r": whole[i]["exp_r"],
         "total_pnl": whole[i]["pnl"]}
        for i in order[:10]
    ]
    per_strategy_in_sample: dict[str, Any] = {}
    for s in args.strategies:
        rows = [whole[i] for i, c in enumerate(grid) if c.strategy.value == s and whole[i]["n"]]
        if rows:
            per_strategy_in_sample[s] = {
                "configs": len(rows),
                "share_with_positive_expectancy": sum(
                    1 for r in rows if (r["exp_r"] or 0) > 0
                ) / len(rows),
                "median_expectancy_r": sorted(r["exp_r"] for r in rows)[len(rows) // 2],
            }
    print("\nIN-SAMPLE best in hindsight (NOT evidence; selected on the data it is scored on):")
    for row in in_sample_top[:5]:
        print(f"  {row['label']:<42} n={row['trades']:>3} R={row['expectancy_r']:+.3f}")

    sensitivity: dict[str, Any] = {}
    for m in args.sensitivity:
        if abs(m - mult_value) < 1e-9:
            continue
        print(f"\nsensitivity: multiplier {m}")
        run = run_walk_forward(
            series, observed, m, grid,
            train=args.train, test=args.test, step=step,
            min_trades=args.min_trades, workers=args.workers, select_by=args.select_by,
            verbose=False,
        )
        sensitivity[str(m)] = _summary(run)
        q = run["pooled_oos"]
        print(f"  pooled OOS: trades={q['trades']} R={q['expectancy_r']} total=${q['total_pnl']}")

    other = "expectancy_r" if args.select_by == "t_stat" else "t_stat"
    print(f"\nselection sensitivity: select by {other}")
    alt = run_walk_forward(
        series, observed, mult_value, grid,
        train=args.train, test=args.test, step=step,
        min_trades=args.min_trades, workers=args.workers, select_by=other, verbose=False,
    )
    q = alt["pooled_oos"]
    print(f"  pooled OOS: trades={q['trades']} R={q['expectancy_r']} total=${q['total_pnl']}")

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "pricing_basis": "MODELLED",
        "note": MODELLED_NOTE,
        "symbol": args.symbol,
        "data": {
            "daily_closes": len(closes),
            "first": series.days[0].isoformat(),
            "last": series.days[-1].isoformat(),
            "snapshot_days": sorted(d.isoformat() for d in observed),
        },
        "iv_rv_multiplier": {"value": mult_value, "source": mult_source,
                             "measured": measured.to_dict()},
        "params": {
            "train": args.train, "test": args.test, "step": step,
            "min_trades": args.min_trades, "rv_window": args.rv_window,
            "selection": f"max {args.select_by} of return-on-risk on the train window among "
                         "configs with >= min_trades train trades",
            "grid": {"strategies": args.strategies, "target_deltas": DELTAS, "dte_days": DTES,
                     "profit_target_pct": PROFIT_TARGETS, "stop_loss_pct": STOPS},
            "costs": {"half_spread": "max($0.02, 3% of mid) per leg",
                      "commission_per_contract": "0.65 per leg, entry and close"},
            "risk_free_rate": 0.04,
        },
        "walk_forward": main_run,
        "in_sample_top10_NOT_EVIDENCE": in_sample_top,
        "in_sample_by_strategy_NOT_EVIDENCE": per_strategy_in_sample,
        "multiplier_sensitivity": sensitivity,
        "selection_sensitivity": {other: _summary(alt)},
        "runtime_seconds_main_walk_forward": round(elapsed, 1),
    }
    out = args.out or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "docs", "research", f"options_walk_forward_{datetime.now(UTC).date().isoformat()}.json",
    )
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, default=_default_json)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
