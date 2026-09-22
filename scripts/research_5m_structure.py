"""Measure the 5-minute structure of a symbol before proposing anything
(Phase 85). Research only — writes nothing, trades nothing.

Answers, from this platform's own stored bars, the questions the brief
asks in order:

  1. Trend by the Dow definition (higher highs AND higher lows) per
     session, from CONFIRMED swings only.
  2. What happens AFTER each swing high / swing low — the tops and bottoms
     of the intraday rallies — measured in ATR, so "can a reversal at a
     session extreme be traded" has a number rather than an opinion.
  3. Whether volume confirms: is the move after a high-volume swing
     different from after a low-volume one?
  4. Pullbacks against volume: after an impulse, does a low-volume
     pullback resume more often than a high-volume one?
  5. Location: do the same reversals behave differently at the marked
     session levels (PDH/PDL/premarket/opening range/VWAP bands)?
  6. Gap behaviour — the only visible trace of overnight news and
     corporate actions in bar data. This is NOT a news feed; it is
     labelled as what it is.

Every number printed is computed from real bars. Nothing here decides to
trade; `docs/RESEARCH_5M.md` records what the numbers said.

    python scripts/research_5m_structure.py --symbol TQQQ.US --days 30
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from apps.api.app.marketdata.indicators import (  # noqa: E402
    InsufficientDataError,
    atr,
)
from apps.api.app.marketdata.sessions import (  # noqa: E402
    build_session_levels,
    group_by_session,
    is_regular_hours,
    session_vwap,
)
from apps.api.app.marketdata.store import MarketDataStore  # noqa: E402
from apps.api.app.marketdata.structure import (  # noqa: E402
    SwingKind,
    find_swings,
)

FORWARD_BARS = 12
"""One hour on 5-minute bars — the horizon every "what happened next"
figure below is measured over."""

SWING_STRENGTH = 3
"""Bars either side of a fractal pivot. CRITICAL: a pivot is therefore not
KNOWABLE until `SWING_STRENGTH` bars after it printed, which is exactly
what `SwingPoint.confirmed_ts` records. Every forward measurement below
starts at the CONFIRMATION bar, never at the pivot bar — measuring from
the pivot would score the three bars that defined it as if they were
future, and turn a definition into a fake edge."""


def _f(x: Decimal | float | None) -> float:
    return float(x) if x is not None else float("nan")


@dataclass
class Excursion:
    """What price did in the FORWARD_BARS after an event, in ATR units."""

    up: float
    down: float
    close: float


def _excursion(bars, i: int, unit: float) -> Excursion | None:
    window = bars[i + 1 : i + 1 + FORWARD_BARS]
    if not window or unit <= 0:
        return None
    entry = _f(bars[i].close)
    highs = max(_f(b.high if b.high is not None else b.close) for b in window)
    lows = min(_f(b.low if b.low is not None else b.close) for b in window)
    return Excursion(
        up=(highs - entry) / unit,
        down=(lows - entry) / unit,
        close=(_f(window[-1].close) - entry) / unit,
    )


def _mean(xs: list[float]) -> float:
    return statistics.fmean(xs) if xs else float("nan")


def _pct(n: int, d: int) -> float:
    return 100.0 * n / d if d else float("nan")


def _atr_at(bars, i: int) -> float:
    try:
        return _f(atr(bars[max(0, i - 20) : i + 1], 14))
    except (InsufficientDataError, Exception):  # noqa: BLE001 - research script
        return float("nan")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="TQQQ.US")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--interval", default="5m")
    args = ap.parse_args()

    url = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://trading_os:trading_os@localhost:5432/trading_os"
    )
    if "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    end = datetime.now(UTC).date()
    start = end - timedelta(days=args.days * 2)  # calendar days -> ~sessions
    async with factory() as session:
        store = MarketDataStore(session)
        bars = await store.get_bars(
            args.symbol, bar_interval=args.interval, start_date=start, end_date=end
        )
    await engine.dispose()

    if not bars:
        print(f"NO DATA for {args.symbol} {args.interval} in {start}..{end}")
        return 1

    sessions = group_by_session(bars)
    levels_by_session = build_session_levels(bars)
    days = sorted(sessions)[-args.days :]
    print(f"{args.symbol} {args.interval}: {len(bars)} bars, {len(days)} sessions "
          f"({days[0]} .. {days[-1]})")

    # --- 1. Dow trend per session ------------------------------------------
    trend_counts: dict[str, int] = defaultdict(int)
    # --- 2/3. after a swing high / low, split by volume --------------------
    after: dict[tuple[str, str], list[Excursion]] = defaultdict(list)
    # --- 4. pullback resumption --------------------------------------------
    pullback: dict[str, list[float]] = defaultdict(list)
    # --- 5. at a marked level vs in open space -----------------------------
    at_level: dict[tuple[str, str], list[Excursion]] = defaultdict(list)
    # --- 6. gaps ------------------------------------------------------------
    gaps: list[tuple[float, float]] = []

    prev_close: float | None = None
    for day in days:
        day_bars = sessions[day]
        regular = [b for b in day_bars if is_regular_hours(b.ts)]
        if len(regular) < 30:
            continue
        levels = levels_by_session.get(day)
        vwap = {p.ts: p for p in session_vwap(day_bars)}
        swings = find_swings(regular, strength=SWING_STRENGTH)

        highs = [s for s in swings if s.kind is SwingKind.HIGH]
        lows = [s for s in swings if s.kind is SwingKind.LOW]
        if len(highs) >= 2 and len(lows) >= 2:
            hh = highs[-1].price > highs[-2].price
            hl = lows[-1].price > lows[-2].price
            label = "up" if (hh and hl) else "down" if (not hh and not hl) else "sideways"
            trend_counts[label] += 1
        else:
            trend_counts["undetermined"] += 1

        if prev_close is not None:
            open_px = _f(regular[0].open if regular[0].open is not None else regular[0].close)
            unit = _atr_at(regular, min(14, len(regular) - 1))
            if unit == unit and unit > 0:
                gap = (open_px - prev_close) / unit
                day_ret = (_f(regular[-1].close) - open_px) / unit
                gaps.append((gap, day_ret))
        prev_close = _f(regular[-1].close)

        vols = [b.volume or 0 for b in regular]
        median_vol = statistics.median(vols) if vols else 0

        ts_index = {b.ts: i for i, b in enumerate(regular)}
        for sw in swings:
            pivot = ts_index.get(sw.ts)
            if pivot is None:
                continue
            # Trade from where the pivot became knowable, not from the
            # pivot itself. See SWING_STRENGTH.
            i = pivot + SWING_STRENGTH
            if i + FORWARD_BARS >= len(regular):
                continue
            unit = _atr_at(regular, i)
            if unit != unit or unit <= 0:
                continue
            exc = _excursion(regular, i, unit)
            if exc is None:
                continue
            kind = "high" if sw.kind is SwingKind.HIGH else "low"
            vol_tag = "highvol" if (regular[pivot].volume or 0) > median_vol else "lowvol"
            after[(kind, "all")].append(exc)
            after[(kind, vol_tag)].append(exc)

            # location: within 0.25 ATR of a marked level, or VWAP band
            near = False
            if levels is not None:
                for lv in (
                    levels.previous_high, levels.previous_low, levels.previous_close,
                    levels.premarket_high, levels.premarket_low,
                    levels.opening_range_high, levels.opening_range_low,
                ):
                    if lv is not None and abs(_f(sw.price) - _f(lv)) <= 0.25 * unit:
                        near = True
                        break
            vp = vwap.get(regular[i].ts)
            if not near and vp is not None:
                for band in vp.band(2):
                    if abs(_f(sw.price) - _f(band)) <= 0.25 * unit:
                        near = True
                        break
            at_level[(kind, "at_level" if near else "open_space")].append(exc)

        # pullbacks: an impulse leg of >= 1 ATR, then 2-6 bars against it
        for i in range(20, len(regular) - FORWARD_BARS):
            unit = _atr_at(regular, i)
            if unit != unit or unit <= 0:
                continue
            leg = (_f(regular[i].close) - _f(regular[i - 6].close)) / unit
            if abs(leg) < 1.0:
                continue
            pull_vol = statistics.fmean([regular[j].volume or 0 for j in range(i - 2, i + 1)])
            impulse_vol = statistics.fmean([regular[j].volume or 0 for j in range(i - 6, i - 2)])
            if impulse_vol <= 0:
                continue
            tag = "quiet_pullback" if pull_vol < impulse_vol else "loud_pullback"
            fwd = (_f(regular[min(i + FORWARD_BARS, len(regular) - 1)].close)
                   - _f(regular[i].close)) / unit
            pullback[tag].append(fwd if leg > 0 else -fwd)

    print("\n1) Dow trend by session (confirmed swings only)")
    total = sum(trend_counts.values())
    for k in ("up", "down", "sideways", "undetermined"):
        print(f"   {k:<13} {trend_counts[k]:>3}  ({_pct(trend_counts[k], total):.0f}%)")

    print(f"\n2/3) What happened in the {FORWARD_BARS} bars after a swing (ATR units)")
    print(f"   {'event':<18}{'n':>5}{'mean up':>10}{'mean down':>11}{'mean close':>12}"
          f"{'% closed against':>18}")
    for kind in ("high", "low"):
        for tag in ("all", "highvol", "lowvol"):
            xs = after[(kind, tag)]
            if not xs:
                continue
            against = sum(1 for e in xs if (e.close < 0 if kind == "high" else e.close > 0))
            print(f"   {kind + '/' + tag:<18}{len(xs):>5}{_mean([e.up for e in xs]):>10.2f}"
                  f"{_mean([e.down for e in xs]):>11.2f}{_mean([e.close for e in xs]):>12.2f}"
                  f"{_pct(against, len(xs)):>17.0f}%")

    print("\n4) Pullback against volume (forward move in the impulse direction, ATR)")
    for tag in ("quiet_pullback", "loud_pullback"):
        xs = pullback[tag]
        if xs:
            cont = sum(1 for x in xs if x > 0)
            print(f"   {tag:<18}{len(xs):>5}  mean {_mean(xs):>6.2f}  "
                  f"continued {_pct(cont, len(xs)):.0f}%")

    print("\n5) Same swings, at a marked level vs in open space")
    for kind in ("high", "low"):
        for tag in ("at_level", "open_space"):
            xs = at_level[(kind, tag)]
            if not xs:
                continue
            against = sum(1 for e in xs if (e.close < 0 if kind == "high" else e.close > 0))
            print(f"   {kind + '/' + tag:<20}{len(xs):>5}  "
                  f"mean close {_mean([e.close for e in xs]):>6.2f}  "
                  f"reversed {_pct(against, len(xs)):.0f}%")

    print("\n6) Overnight gap (proxy for news/corporate actions; NOT a news feed)")
    if gaps:
        big = [(g, r) for g, r in gaps if abs(g) >= 1.0]
        small = [(g, r) for g, r in gaps if abs(g) < 1.0]
        for tag, xs in (("gap >= 1 ATR", big), ("gap < 1 ATR", small)):
            if xs:
                fade = sum(1 for g, r in xs if (r < 0) == (g > 0))
                print(f"   {tag:<14}{len(xs):>4}  "
                      f"mean session move {_mean([r for _, r in xs]):>6.2f} ATR  "
                      f"faded {_pct(fade, len(xs)):.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
