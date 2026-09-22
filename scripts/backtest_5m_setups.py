"""Backtest the intraday setups on this platform's own stored 5-minute
bars and print the §21 metric set per setup (Phase 85).

Research only: reads bars, writes nothing, trades nothing.

    python scripts/backtest_5m_setups.py --symbol TQQQ.US \
        --setups quiet_pullback volume_climax_reversal gap_fade
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
from apps.api.app.backtesting.intraday_metrics import compute_metrics  # noqa: E402
from apps.api.app.backtesting.setups import SETUPS  # noqa: E402
from apps.api.app.marketdata.store import MarketDataStore  # noqa: E402


def _t_stat(rs: list[float]) -> float:
    """One-sample t on per-trade R. The question is whether the mean is
    distinguishable from zero, not whether it looks positive."""
    if len(rs) < 2:
        return float("nan")
    sd = statistics.stdev(rs)
    if sd == 0:
        return float("nan")
    return statistics.fmean(rs) / (sd / math.sqrt(len(rs)))


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="TQQQ.US")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--setups", nargs="+", default=sorted(SETUPS))
    ap.add_argument("--min-score", type=int, default=0)
    ap.add_argument("--confirm-symbol", default="QQQ.US")
    args = ap.parse_args()

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
        htf = await store.get_bars(
            args.symbol, bar_interval="15m", start_date=start, end_date=end
        )
        confirm = await store.get_bars(
            args.confirm_symbol, bar_interval=args.interval, start_date=start, end_date=end
        )
    await engine.dispose()

    if not bars:
        print(f"NO DATA for {args.symbol} {args.interval}")
        return 1

    # Real retail costs, not frictionless: a result that only survives at
    # zero cost is not a result.
    costs = CostModel(fee_bps=Decimal("1"), slippage_bps=Decimal("2"))

    print(f"{args.symbol} {args.interval}  {len(bars)} bars  min_score={args.min_score}  "
          f"costs: 1bp fee + 2bps slippage")
    header = (
        f"{'setup':<24}{'trades':>7}{'win%':>7}{'expR':>8}{'t':>7}{'PF':>7}"
        f"{'totR':>9}{'maxDD':>9}{'sessions':>10}"
    )
    print(header)
    print("-" * len(header))

    for name in args.setups:
        if name not in SETUPS:
            print(f"{name:<24}  unknown setup")
            continue
        result = run_intraday_backtest(
            bars,
            IntradayRunConfig(
                symbol=args.symbol,
                setups=(name,),
                costs=costs,
                min_score=args.min_score,
            ),
            higher_timeframe_bars=htf or None,
            confirm_bars=confirm or None,
        )
        m = compute_metrics(result.trades)
        rs = [float(t.r_multiple) for t in result.trades]
        t = _t_stat(rs)
        print(
            f"{name:<24}{m.total_trades:>7}"
            f"{(float(m.win_rate) * 100 if m.win_rate is not None else float('nan')):>7.1f}"
            f"{(float(m.expectancy_r) if m.expectancy_r is not None else float('nan')):>8.3f}"
            f"{t:>7.2f}"
            f"{(float(m.profit_factor) if m.profit_factor is not None else float('nan')):>7.2f}"
            f"{float(m.total_r):>9.2f}"
            f"{float(m.max_drawdown_r):>9.2f}"
            f"{result.sessions_traded:>4}/{result.sessions_available:<5}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
