"""Run every Strategy Library strategy on every universe it suits, on real
adjusted daily history, and print what was measured (Phase 108, D135).

    python -m scripts.library_report            # table to stdout
    python -m scripts.library_report --json out.json

Nothing is tuned: each strategy runs with its published defaults. The
"after publication" columns are the out-of-sample ones.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from apps.api.app.library.service import LibraryError, run_backtest
from apps.api.app.library.strategies import PRESETS, get_strategy
from apps.api.app.marketdata.providers.yahoo import YahooDailyProvider


def _f(value: float | None, pct: bool = True) -> str:
    if value is None:
        return "   n/a"
    return f"{value * 100:+6.1f}" if pct else f"{value:6.2f}"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--preset", action="append", default=None)
    args = parser.parse_args()

    provider = YahooDailyProvider()
    rows: list[dict] = []
    header = (
        f"{'universe':<16}{'strategy':<20}{'period':<26}{'CAGR%':>7}{'Sharpe':>7}{'MaxDD%':>8}"
        f"{'t':>6} | {'B&H CAGR%':>10}{'Sharpe':>7}{'MaxDD%':>8}  verdict"
    )
    print(header)
    print("-" * len(header))
    for preset in PRESETS:
        if args.preset and preset.id not in args.preset:
            continue
        for strategy_id in preset.strategies:
            strategy = get_strategy(strategy_id)
            params = {}
            if preset.market == "FX" and strategy_id == "tsmom_12m":
                params = {"allow_short": 1}
            if strategy_id == "momentum_rotation" and len(preset.symbols) <= 5:
                params = {"top_n": 2}
            try:
                report = await run_backtest(
                    provider,
                    strategy_id=strategy_id,
                    symbols=list(preset.symbols),
                    benchmark=list(preset.benchmark),
                    params=params,
                    preset=preset,
                )
            except LibraryError as exc:
                print(f"{preset.id:<16}{strategy_id:<20}{exc}")
                rows.append({"preset": preset.id, "strategy": strategy_id, "error": str(exc)})
                continue
            for seg in report.segments:
                s, b = seg.strategy, seg.benchmark
                if s is None:
                    continue
                print(
                    f"{preset.id:<16}{strategy_id:<20}{seg.label[:25]:<26}"
                    f"{_f(s.cagr):>7}{_f(s.sharpe, False):>7}{_f(s.max_drawdown):>8}"
                    f"{_f(s.t_stat, False):>6} | "
                    f"{_f(b.cagr if b else None):>10}{_f(b.sharpe if b else None, False):>7}"
                    f"{_f(b.max_drawdown if b else None):>8}"
                    + (f"  {report.verdict.code}" if seg is report.segments[-1] else "")
                )
            rows.append(
                {
                    "preset": preset.id,
                    "market": preset.market,
                    "strategy": strategy_id,
                    "strategy_name": strategy.name,
                    "params": report.params,
                    "verdict": report.verdict.code,
                    "headline": report.verdict.headline,
                    "trades": report.trades,
                    "avg_exposure": report.avg_exposure,
                    "survivorship": preset.survivorship,
                    "as_of": report.as_of.isoformat(),
                    "segments": [
                        {
                            "label": seg.label,
                            "strategy": seg.strategy.as_dict() if seg.strategy else None,
                            "benchmark": seg.benchmark.as_dict() if seg.benchmark else None,
                        }
                        for seg in report.segments
                    ],
                }
            )
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
