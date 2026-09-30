"""Walk-forward parameter optimisation for every intraday setup (Phase 99, D118).

Research only: reads bars, writes a JSON report, trades nothing.

For each setup: optimise the exit plan and entry filter on a train window,
score the chosen parameters on the NEXT, unseen window, roll forward, and
pool the out-of-sample trades. The same folds are also run with the default
parameters, so the report says whether optimising actually helped.

It then fits each setup once more on the most RECENT train window and saves
those parameters as the setup's current best - flagged `recommended` only
when the pooled out-of-sample expectancy is positive AND beats the default.
A setup that loses out of sample is reported as such and not recommended,
however good its in-sample fit looks.

This is the "continuous improvement" loop: re-run it as new bars arrive
(e.g. weekly) and the saved parameters and verdicts move with the data.

    python scripts/optimise_setups.py --symbol TQQQ.US --train 40 --test 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from apps.api.app.backtesting.costs import CostModel  # noqa: E402
from apps.api.app.backtesting.intraday_engine import (  # noqa: E402
    IntradayRunConfig,
    run_intraday_backtest,
)
from apps.api.app.backtesting.optimiser import (  # noqa: E402
    DIRECTION_CHOICES,
    Candidate,
    Score,
    coordinate_descent,
    walk_forward,
)
from apps.api.app.backtesting.setups import SETUPS  # noqa: E402
from apps.api.app.marketdata.sessions import session_date  # noqa: E402
from apps.api.app.marketdata.store import MarketDataStore  # noqa: E402

COSTS = CostModel(fee_bps=Decimal("1"), slippage_bps=Decimal("2"))


def _fmt(x: float | None, spec: str = "+.3f") -> str:
    return "  n/a " if x is None else format(x, spec)


def _score_dict(s: Score) -> dict[str, object]:
    return {
        "trades": s.trades,
        "expectancy_r": s.expectancy,
        "t_stat": s.t_stat,
        "win_rate": s.win_rate,
        "total_r": s.total_r,
    }


async def _load(symbol: str, interval: str, days: int, confirm_symbol: str):
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
        confirm = await store.get_bars(
            confirm_symbol, bar_interval=interval, start_date=start, end_date=end
        )
    await engine.dispose()
    return bars, htf, confirm


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="TQQQ.US")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--train", type=int, default=40)
    ap.add_argument("--test", type=int, default=20)
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--min-trades", type=int, default=10)
    ap.add_argument("--setups", nargs="+", default=sorted(SETUPS))
    ap.add_argument("--confirm-symbol", default="QQQ.US")
    ap.add_argument("--out", default="docs/research")
    ap.add_argument(
        "--publish",
        action="store_true",
        help="also write the bot profile (apps/api/app/autotrade/profiles/) - D126",
    )
    args = ap.parse_args()

    bars, htf, confirm = asyncio.run(
        _load(args.symbol, args.interval, args.days, args.confirm_symbol)
    )
    if not bars:
        print(f"NO DATA for {args.symbol} {args.interval}")
        return 1
    days = sorted({session_date(b.ts) for b in bars})
    if len(days) < args.train + args.test:
        print(f"INSUFFICIENT DATA: {len(days)} sessions, need {args.train + args.test}.")
        return 1

    by_day: dict = {}
    for b in bars:
        by_day.setdefault(session_date(b.ts), []).append(b)

    def window(lo: int, hi: int) -> list:
        out: list = []
        for d in days[lo:hi]:
            out.extend(by_day[d])
        return out

    folds = []
    lo = 0
    while lo + args.train + args.test <= len(days):
        tr, te = (lo, lo + args.train), (lo + args.train, lo + args.train + args.test)
        folds.append((
            f"{days[tr[0]]}..{days[tr[1] - 1]}", window(*tr),
            f"{days[te[0]]}..{days[te[1] - 1]}", window(*te),
        ))
        lo += args.test

    print(
        f"{args.symbol} {args.interval}: {len(days)} sessions ({days[0]}..{days[-1]}), "
        f"{len(folds)} fold(s) of train {args.train} / test {args.test}; costs 1bp + 2bps"
    )

    report: dict[str, object] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "symbol": args.symbol,
        "interval": args.interval,
        "sessions": [str(days[0]), str(days[-1]), len(days)],
        "train": args.train,
        "test": args.test,
        "costs": "1bp fee + 2bps slippage per side",
        "setups": {},
    }

    # Built once for the whole run: the regime and confirm timelines depend
    # only on the (fixed) htf/confirm series, never on the setup or plan.
    bias_cache: dict = {}

    for setup in args.setups:
        if setup not in SETUPS:
            continue

        caches: dict[int, dict] = {}

        def run(c: Candidate, w: list, _setup: str = setup, _caches: dict = caches) -> list[float]:
            if not w:
                return []
            # One detection cache per (setup, window): every candidate on the
            # same window reuses the signals and only replays the exits.
            cache = _caches.setdefault(id(w), {})
            res = run_intraday_backtest(
                w,
                IntradayRunConfig(
                    symbol=args.symbol,
                    setups=(_setup,),
                    plan=c.plan(),
                    costs=COSTS,
                    min_score=c.min_score,
                    allow_directions=DIRECTION_CHOICES[c.directions],
                ),
                higher_timeframe_bars=htf or None,
                confirm_bars=confirm or None,
                signal_cache=cache,
                bias_cache=bias_cache,
            )
            return [float(t.r_multiple) for t in res.trades]

        t0 = time.time()
        wf = walk_forward(setup, run, folds, passes=args.passes, min_trades=args.min_trades)
        oos, base = wf.oos, wf.baseline
        latest_window = window(len(days) - args.train, len(days))
        latest, latest_score, _ = coordinate_descent(
            run, latest_window,
            passes=args.passes, min_trades=args.min_trades,
        )
        # Two bars, deliberately different. PROMISING: positive out of sample
        # and better than the defaults - worth watching. RECOMMENDED: also
        # statistically distinguishable from zero (t >= 2 on >= 30 unseen
        # trades). The first run (2026-09-28) produced four promising setups
        # and no recommended one; calling a t of 0.78 "recommended" would be
        # the exact overstatement this loop exists to prevent.
        promising = (
            oos.expectancy is not None
            and oos.expectancy > 0
            and (base.expectancy is None or oos.expectancy > base.expectancy)
        )
        recommended = (
            promising and oos.trades >= 30 and oos.t_stat is not None and oos.t_stat >= 2
        )
        print(
            f"\n{setup:<24} OOS {oos.trades:>4} trades  expR {_fmt(oos.expectancy)}  "
            f"t {_fmt(oos.t_stat, '+.2f')}  win {_fmt(oos.win_rate, '.0%')}   |  "
            f"defaults: {base.trades:>4} trades expR {_fmt(base.expectancy)}   "
            f"{'RECOMMENDED' if recommended else 'promising' if promising else 'not recommended'}  "
            f"({time.time() - t0:.0f}s)"
        )
        for f in wf.folds:
            picked = "no set met min trades" if f.chosen is None else (
                f"stop {f.chosen.atr_stop_buffer} tp {f.chosen.tp1_r_multiple}/"
                f"{f.chosen.tp2_r_multiple} trail {f.chosen.trail_atr_multiple} "
                f"be {'y' if f.chosen.breakeven_after_tp1 else 'n'} "
                f"score>={f.chosen.min_score} {f.chosen.directions}"
            )
            print(
                f"   test {f.test_label}: train expR {_fmt(f.train.expectancy)} -> "
                f"test expR {_fmt(f.test.expectancy)} ({f.test.trades}) | default "
                f"{_fmt(f.baseline_test.expectancy)} ({f.baseline_test.trades}) | {picked}"
            )
        report["setups"][setup] = {  # type: ignore[index]
            "out_of_sample": _score_dict(oos),
            "default_out_of_sample": _score_dict(base),
            "promising": promising,
            "recommended": recommended,
            "current_params": latest.as_dict(),
            "current_params_in_sample": _score_dict(latest_score),
            "folds": [
                {
                    "train": f.train_label,
                    "test": f.test_label,
                    "chosen": None if f.chosen is None else f.chosen.as_dict(),
                    "train_score": _score_dict(f.train),
                    "test_score": _score_dict(f.test),
                    "default_test_score": _score_dict(f.baseline_test),
                    "backtests": f.backtests,
                }
                for f in wf.folds
            ],
        }

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    path = out_dir / f"setup_optimisation_{args.symbol.replace('.', '_')}_{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nreport: {path}")

    if args.publish:
        # D126: the file the Autotrade bot's `optimised` mode reads - only the
        # fields the bot applies, plus the out-of-sample evidence that decides
        # whether a setup is deployable at all.
        from apps.api.app.autotrade.profiles import profile_path

        setups_report: dict = report["setups"]  # type: ignore[assignment]
        profile = {
            "symbol": args.symbol,
            "bar_interval": args.interval,
            "generated_at": report["generated_at"],
            "source_report": path.as_posix(),
            "setups": {
                name: {
                    "params": v["current_params"],
                    "out_of_sample": v["out_of_sample"],
                    "promising": v["promising"],
                    "recommended": v["recommended"],
                }
                for name, v in setups_report.items()
            },
        }
        target = profile_path(args.symbol, args.interval)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(profile, indent=2, default=str), encoding="utf-8")
        print(f"published bot profile: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
