"""Trend anatomy of TQQQ: rises, pullbacks and the break that ends a trend
(Phase 101, D120). Research only - reads bars, writes nothing to the
database, trades nothing.

The operator's question was how price behaves DURING a trend - how far it
rises, how deep and how long the pullbacks are, where they hold, how
strong the trend is, and what gives way at the reversal - so that entries
and exits can be timed from the market's own structure. This measures
exactly that, from this platform's stored bars:

  * The tape is segmented into trend legs from CONFIRMED swings only. An
    uptrend is established on the bar that completes a higher high AND a
    higher low (the Dow definition `dow_trend` already uses). It ends on
    the first CLOSE below the most recent confirmed higher low - the
    market-structure break. Downtrends mirror.
  * Every higher low confirmed inside an uptrend is a pullback. For each:
    depth (fraction of the prior impulse, and ATR), duration, which level
    it held at, and what followed.
  * Every swing high inside an uptrend is a candidate top. For each: was
    it a failed higher high, was there an RSI divergence, was volume
    heavy - and was the NEXT structural event a continuation (a new higher
    low) or the break? That is the "what precedes a reversal" table, and it
    is measured against the base rate rather than read off the breaks alone.
  * Trend-strength features (ADX, EMA spread, EMA slope, 15m and daily
    bias) at each pullback entry, against what riding it to the break
    earned.

**Causality.** Swings enter the state machine on their `confirmed_ts`
bar, never their pivot bar. Every feature attached to an entry is
computed from bars up to that entry. Outcomes (what happened next) are,
by construction, the future - they are the thing being measured.

The 15m bias is taken from 15m bars that had CLOSED by the 5m bar's
close (a bar stamped 10:00 closes 10:15). Session VWAP is anchored at the
regular open. The 5m tape is the regular session only, joined across days,
so a trend can span an overnight gap; how often it does is reported,
because the intraday engine is flat by the bell.

    python scripts/trend_anatomy_tqqq.py --symbol TQQQ.US --strength 3
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from apps.api.app.marketdata.sessions import is_regular_hours, session_date  # noqa: E402
from apps.api.app.marketdata.store import MarketDataStore  # noqa: E402
from apps.api.app.marketdata.structure import SwingKind, find_swings  # noqa: E402

COST_BPS_PER_SIDE = 3.0
"""1bp fee + 2bps slippage, the house research cost, charged on both legs
when a gross ride R is converted to a net one."""

HOLD_TOLERANCE_ATR = 0.25
"""A pullback 'held at' a level when its extreme came within this many ATR
of it. The same tolerance the setups use for location."""


# ---------------------------------------------------------------------------
# Series and causal indicators (floats: this is measurement, not money)
# ---------------------------------------------------------------------------


@dataclass
class Series:
    name: str
    ts: list[datetime]
    day: list[date]
    o: list[float]
    h: list[float]
    low: list[float]
    c: list[float]
    v: list[float]
    bars: list  # the original bar objects, for find_swings
    atr: list[float | None] = field(default_factory=list)
    ema9: list[float | None] = field(default_factory=list)
    ema21: list[float | None] = field(default_factory=list)
    rsi: list[float | None] = field(default_factory=list)
    adx: list[float | None] = field(default_factory=list)
    vwap: list[float | None] = field(default_factory=list)
    vol_avg: list[float | None] = field(default_factory=list)
    htf_bias: list[int | None] = field(default_factory=list)  # +1 up / -1 down
    daily_bias: list[int | None] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.c)


def _f(x) -> float:
    return float(x) if x is not None else float("nan")


def make_series(name: str, bars: list) -> Series:
    return Series(
        name=name,
        ts=[b.ts for b in bars],
        day=[session_date(b.ts) for b in bars],
        o=[_f(b.open if b.open is not None else b.close) for b in bars],
        h=[_f(b.high if b.high is not None else b.close) for b in bars],
        low=[_f(b.low if b.low is not None else b.close) for b in bars],
        c=[_f(b.close) for b in bars],
        v=[float(b.volume or 0) for b in bars],
        bars=bars,
    )


def ema_series(values: list[float], period: int) -> list[float | None]:
    """SMA-seeded EMA, the same convention as `indicators.ema`, run once
    over the whole series instead of re-seeded per window."""
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    value = sum(values[:period]) / period
    out[period - 1] = value
    for i in range(period, len(values)):
        value = (values[i] - value) * k + value
        out[i] = value
    return out


def add_indicators(s: Series, *, anchor_vwap: bool) -> None:
    n = len(s)
    s.htf_bias = [None] * n
    s.daily_bias = [None] * n
    tr = [s.h[0] - s.low[0]] + [
        max(s.h[i] - s.low[i], abs(s.h[i] - s.c[i - 1]), abs(s.low[i] - s.c[i - 1]))
        for i in range(1, n)
    ]
    # ATR(14): simple mean of the last 14 true ranges (indicators.atr).
    s.atr = [None] * n
    for i in range(14, n):
        s.atr[i] = sum(tr[i - 13 : i + 1]) / 14
    s.ema9 = ema_series(s.c, 9)
    s.ema21 = ema_series(s.c, 21)
    # RSI(14): simple average of the last 14 changes (indicators.rsi).
    s.rsi = [None] * n
    for i in range(14, n):
        gains = losses = 0.0
        for j in range(i - 13, i + 1):
            d = s.c[j] - s.c[j - 1]
            if d > 0:
                gains += d
            else:
                losses -= d
        s.rsi[i] = 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)
    # ADX(14), Wilder smoothing - the conventional trend-strength gauge.
    s.adx = [None] * n
    p = 14
    if n > 2 * p + 1:
        pdm = [0.0] * n
        mdm = [0.0] * n
        for i in range(1, n):
            up = s.h[i] - s.h[i - 1]
            dn = s.low[i - 1] - s.low[i]
            pdm[i] = up if up > dn and up > 0 else 0.0
            mdm[i] = dn if dn > up and dn > 0 else 0.0
        tr_s = sum(tr[1 : p + 1])
        p_s = sum(pdm[1 : p + 1])
        m_s = sum(mdm[1 : p + 1])
        dxs: list[float] = []
        adx_val: float | None = None
        for i in range(p + 1, n):
            tr_s = tr_s - tr_s / p + tr[i]
            p_s = p_s - p_s / p + pdm[i]
            m_s = m_s - m_s / p + mdm[i]
            if tr_s <= 0:
                continue
            pdi = 100 * p_s / tr_s
            mdi = 100 * m_s / tr_s
            dx = 0.0 if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi)
            if adx_val is None:
                dxs.append(dx)
                if len(dxs) == p:
                    adx_val = sum(dxs) / p
                    s.adx[i] = adx_val
            else:
                adx_val = (adx_val * (p - 1) + dx) / p
                s.adx[i] = adx_val
    # Session VWAP anchored at the regular open.
    s.vwap = [None] * n
    if anchor_vwap:
        cum_v = cum_pv = 0.0
        current = None
        for i in range(n):
            if s.day[i] != current:
                current = s.day[i]
                cum_v = cum_pv = 0.0
            if s.v[i] > 0:
                cum_v += s.v[i]
                cum_pv += (s.h[i] + s.low[i] + s.c[i]) / 3 * s.v[i]
            s.vwap[i] = cum_pv / cum_v if cum_v > 0 else None
    # Mean volume of the 20 bars BEFORE this one.
    s.vol_avg = [None] * n
    for i in range(20, n):
        s.vol_avg[i] = sum(s.v[i - 20 : i]) / 20


def add_bias(s: Series, htf_bars: list, daily_bars: list, *, exec_minutes: int) -> None:
    """15m and daily EMA21-vs-EMA50 bias, read only from bars that had
    CLOSED by the close of each execution bar."""
    n = len(s)
    s.htf_bias = [None] * n
    s.daily_bias = [None] * n
    if htf_bars:
        closes = [float(b.close) for b in htf_bars]
        e21, e50 = ema_series(closes, 21), ema_series(closes, 50)
        known_at = [b.ts + timedelta(minutes=15) for b in htf_bars]
        for i in range(n):
            j = bisect.bisect_right(known_at, s.ts[i] + timedelta(minutes=exec_minutes)) - 1
            if j >= 0 and e21[j] is not None and e50[j] is not None:
                s.htf_bias[i] = 1 if e21[j] > e50[j] else -1
    if daily_bars:
        closes = [float(b.close) for b in daily_bars]
        e21, e50 = ema_series(closes, 21), ema_series(closes, 50)
        days = [session_date(b.ts) for b in daily_bars]
        for i in range(n):
            j = bisect.bisect_left(days, s.day[i]) - 1  # strictly earlier sessions
            if j >= 0 and e21[j] is not None and e50[j] is not None:
                s.daily_bias[i] = 1 if e21[j] > e50[j] else -1


# ---------------------------------------------------------------------------
# The causal trend state machine
# ---------------------------------------------------------------------------


@dataclass
class Swing:
    kind: str  # "H" | "L"
    idx: int
    price: float
    confirm: int


@dataclass
class Pullback:
    direction: int  # +1 pullback in an uptrend (a higher low), -1 mirrored
    trend_id: int
    leg_no: int
    pivot_idx: int
    confirm_idx: int
    depth_frac: float
    depth_atr: float
    impulse_atr: float
    impulse_pct: float
    impulse_bars: int
    pullback_bars: int
    held_at: str
    near: dict[str, bool]
    vol_ratio: float | None
    adx: float | None
    ema_spread_atr: float | None
    ema21_slope_atr: float | None
    htf_aligned: bool | None
    daily_aligned: bool | None
    # outcomes, filled when known
    continued: bool | None = None  # a new extreme beyond the impulse before the break
    ride_r_gross: float | None = None
    ride_r_session_gross: float | None = None
    cost_r: float | None = None
    mfe_r: float | None = None
    ride_bars: int | None = None
    spans_session: bool | None = None


@dataclass
class Extreme:
    """A swing in the trend's direction (a high in an uptrend): a
    candidate top, with the features known when it was confirmed."""

    direction: int
    trend_id: int
    idx: int
    confirm_idx: int
    failed: bool  # a lower high in an uptrend
    divergence: bool | None
    vol_ratio: float | None
    adx_falling: bool | None
    outcome: str | None = None  # "continued" | "broke"


@dataclass
class Trend:
    trend_id: int
    direction: int
    origin_idx: int
    origin_price: float
    confirm_idx: int
    confirm_price: float
    adx: float | None
    ema_spread_atr: float | None
    htf_aligned: bool | None
    daily_aligned: bool | None
    end_idx: int | None = None
    end_price: float | None = None
    peak_idx: int | None = None
    peak_price: float | None = None
    n_pullbacks: int = 0
    next_direction: int | None = None  # direction of the next established trend


def _signed_atr(s: Series, i: int) -> float | None:
    a = s.atr[i]
    return a if a and a > 0 else None


def segment(s: Series, strength: int, *, stop_buffer_atr: float = 0.3):
    """Walk the series bar by bar; return trends, pullbacks, extremes."""
    n = len(s)
    raw = find_swings(s.bars, strength=strength)
    index_of = {t: i for i, t in enumerate(s.ts)}
    by_confirm: dict[int, list[Swing]] = defaultdict(list)
    for sw in raw:
        i, k = index_of[sw.ts], index_of[sw.confirmed_ts]
        by_confirm[k].append(
            Swing("H" if sw.kind is SwingKind.HIGH else "L", i, float(sw.price), k)
        )

    highs: list[Swing] = []
    lows: list[Swing] = []
    trends: list[Trend] = []
    pullbacks: list[Pullback] = []
    extremes: list[Extreme] = []
    state = 0
    protected: Swing | None = None
    cur: Trend | None = None
    last_end = -1
    open_pullbacks: list[Pullback] = []
    open_extremes: list[Extreme] = []

    def features(k: int, direction: int) -> dict:
        a = _signed_atr(s, k)
        spread = (
            (s.ema9[k] - s.ema21[k]) / a
            if a and s.ema9[k] is not None and s.ema21[k] is not None
            else None
        )
        slope = None
        if a and k >= 6 and s.ema21[k] is not None and s.ema21[k - 6] is not None:
            slope = (s.ema21[k] - s.ema21[k - 6]) / a
        return {
            "adx": s.adx[k],
            "ema_spread_atr": spread * direction if spread is not None else None,
            "ema21_slope_atr": slope * direction if slope is not None else None,
            "htf_aligned": None if s.htf_bias[k] is None else s.htf_bias[k] == direction,
            "daily_aligned": None if s.daily_bias[k] is None else s.daily_bias[k] == direction,
        }

    def record_pullback(k: int, pivot: Swing, origin: Swing, direction: int) -> None:
        assert cur is not None
        lo, hi = origin.idx, pivot.idx
        if direction == 1:
            ext_i = max(range(lo, hi + 1), key=lambda j: s.h[j])
            ext = s.h[ext_i]
        else:
            ext_i = min(range(lo, hi + 1), key=lambda j: s.low[j])
            ext = s.low[ext_i]
        span = abs(ext - origin.price)
        a = _signed_atr(s, pivot.idx) or _signed_atr(s, k)
        if span <= 0 or a is None:
            return
        depth = abs(ext - pivot.price) / span
        tol = HOLD_TOLERANCE_ATR * a
        levels: dict[str, float | None] = {
            "ema9": s.ema9[pivot.idx],
            "ema21": s.ema21[pivot.idx],
            "vwap": s.vwap[pivot.idx] if s.day[pivot.idx] == s.day[k] else None,
        }
        opp = highs if direction == 1 else lows
        prior = [x for x in opp if x.idx < ext_i and x.confirm <= k]
        levels["prior_swing"] = prior[-1].price if prior else None
        for ratio in (0.382, 0.5, 0.618):
            levels[f"fib{ratio}"] = ext - direction * ratio * span
        near = {
            name: (lv is not None and abs(pivot.price - lv) <= tol) for name, lv in levels.items()
        }
        dists = [(abs(pivot.price - lv), name) for name, lv in levels.items() if lv is not None]
        held = min(dists)[1] if dists and min(dists)[0] <= tol else "open space"
        imp_vol = [s.v[j] for j in range(lo, ext_i + 1)]
        pb_vol = [s.v[j] for j in range(ext_i, hi + 1)]
        vol_ratio = (
            (sum(pb_vol) / len(pb_vol)) / (sum(imp_vol) / len(imp_vol))
            if imp_vol and pb_vol and sum(imp_vol) > 0
            else None
        )
        f = features(k, direction)
        pb = Pullback(
            direction=direction,
            trend_id=cur.trend_id,
            leg_no=cur.n_pullbacks + 1,
            pivot_idx=pivot.idx,
            confirm_idx=k,
            depth_frac=depth,
            depth_atr=abs(ext - pivot.price) / a,
            impulse_atr=span / a,
            impulse_pct=span / origin.price * 100,
            impulse_bars=ext_i - lo,
            pullback_bars=hi - ext_i,
            held_at=held,
            near=near,
            vol_ratio=vol_ratio,
            **f,
        )
        # The trade this pullback offers: enter at the confirmation close,
        # stop beyond the pivot, exit at the structure break.
        entry = s.c[k]
        stop = pivot.price - direction * stop_buffer_atr * a
        risk = abs(entry - stop)
        pb.cost_r = (2 * COST_BPS_PER_SIDE / 10_000 * entry) / risk if risk > 0 else None
        pb.__dict__["_entry"] = entry
        pb.__dict__["_risk"] = risk
        pb.__dict__["_ext"] = ext
        cur.n_pullbacks += 1
        pullbacks.append(pb)
        open_pullbacks.append(pb)

    def close_trend(k: int, price: float) -> None:
        nonlocal state, cur, protected, last_end
        assert cur is not None
        cur.end_idx, cur.end_price = k, price
        rng = range(cur.origin_idx, k + 1)
        if cur.direction == 1:
            cur.peak_idx = max(rng, key=lambda j: s.h[j])
            cur.peak_price = s.h[cur.peak_idx]
        else:
            cur.peak_idx = min(rng, key=lambda j: s.low[j])
            cur.peak_price = s.low[cur.peak_idx]
        for pb in open_pullbacks:
            finish_pullback(pb, k, price)
        open_pullbacks.clear()
        for ex in open_extremes:
            ex.outcome = "broke"
        open_extremes.clear()
        state, protected, last_end = 0, None, k

    def finish_pullback(pb: Pullback, k: int, exit_price: float) -> None:
        entry, risk, ext = pb.__dict__["_entry"], pb.__dict__["_risk"], pb.__dict__["_ext"]
        d = pb.direction
        if risk <= 0:
            return
        pb.ride_r_gross = d * (exit_price - entry) / risk
        pb.ride_bars = k - pb.confirm_idx
        pb.spans_session = s.day[k] != s.day[pb.confirm_idx]
        # Flat by the bell: the same ride cut at the entry session's last bar.
        last_same = k
        for j in range(pb.confirm_idx, k + 1):
            if s.day[j] != s.day[pb.confirm_idx]:
                last_same = j - 1
                break
        pb.ride_r_session_gross = d * (s.c[last_same] - entry) / risk
        seg = range(pb.confirm_idx + 1, k + 1)
        if seg:
            best = max(s.h[j] for j in seg) if d == 1 else min(s.low[j] for j in seg)
            pb.mfe_r = d * (best - entry) / risk
            beyond = max(s.h[j] for j in seg) > ext if d == 1 else min(s.low[j] for j in seg) < ext
            pb.continued = bool(beyond)
        else:
            pb.mfe_r, pb.continued = 0.0, False

    for k in range(n):
        # 1. The break, judged against structure known at the previous close.
        if state != 0 and protected is not None:
            if (state == 1 and s.c[k] < protected.price) or (
                state == -1 and s.c[k] > protected.price
            ):
                close_trend(k, s.c[k])

        # 2. Swings that become known at this bar's close.
        new = by_confirm.get(k, [])
        for sw in sorted(new, key=lambda x: x.idx):
            if sw.kind == "H":
                prev_h = highs[-1] if highs else None
                highs.append(sw)
            else:
                prev_h = None
                lows.append(sw)
            if state == 0 or cur is None:
                continue
            with_trend = (sw.kind == "H") == (state == 1)
            if with_trend:
                # A candidate top (uptrend) / bottom (downtrend).
                prev = prev_h if sw.kind == "H" else (lows[-2] if len(lows) > 1 else None)
                failed = prev is not None and (
                    sw.price < prev.price if state == 1 else sw.price > prev.price
                )
                div = None
                if prev is not None and s.rsi[sw.idx] is not None and s.rsi[prev.idx] is not None:
                    new_ext = sw.price > prev.price if state == 1 else sw.price < prev.price
                    weaker = (
                        s.rsi[sw.idx] < s.rsi[prev.idx]
                        if state == 1
                        else s.rsi[sw.idx] > s.rsi[prev.idx]
                    )
                    div = new_ext and weaker
                va = s.vol_avg[sw.idx]
                adx_fall = None
                if prev is not None and s.adx[sw.idx] is not None and s.adx[prev.idx] is not None:
                    adx_fall = s.adx[sw.idx] < s.adx[prev.idx]
                ex = Extreme(
                    direction=state,
                    trend_id=cur.trend_id,
                    idx=sw.idx,
                    confirm_idx=k,
                    failed=failed,
                    divergence=div,
                    vol_ratio=(s.v[sw.idx] / va) if va else None,
                    adx_falling=adx_fall,
                )
                extremes.append(ex)
                open_extremes.append(ex)
            else:
                # A pullback pivot against the trend.
                assert protected is not None
                holds = sw.price > protected.price if state == 1 else sw.price < protected.price
                if holds:
                    record_pullback(k, sw, protected, state)
                    protected = sw
                    for ex in open_extremes:
                        ex.outcome = "continued"
                    open_extremes.clear()

        # 3. Establish a trend from the Dow shape, using swings after the last break.
        if state == 0 and new and len(highs) >= 2 and len(lows) >= 2:
            h1, h2, l1, l2 = highs[-2], highs[-1], lows[-2], lows[-1]
            for direction in (1, -1):
                pivots = (l1, l2) if direction == 1 else (h1, h2)
                opp = (h1, h2) if direction == 1 else (l1, l2)
                ok = (
                    pivots[1].price > pivots[0].price and opp[1].price > opp[0].price
                    if direction == 1
                    else pivots[1].price < pivots[0].price and opp[1].price < opp[0].price
                )
                # At least one defining swing must be new since the last
                # break, or the break would be undone by the very structure
                # it just broke.
                if not ok or max(pivots[1].confirm, opp[1].confirm) <= last_end:
                    continue
                f = features(k, direction)
                cur = Trend(
                    trend_id=len(trends),
                    direction=direction,
                    origin_idx=pivots[0].idx,
                    origin_price=pivots[0].price,
                    confirm_idx=k,
                    confirm_price=s.c[k],
                    adx=f["adx"],
                    ema_spread_atr=f["ema_spread_atr"],
                    htf_aligned=f["htf_aligned"],
                    daily_aligned=f["daily_aligned"],
                )
                if trends and trends[-1].next_direction is None and trends[-1].end_idx is not None:
                    trends[-1].next_direction = direction
                trends.append(cur)
                state = direction
                # When the swing that completed the shape IS the pullback
                # (a higher low after the higher high), it is also the
                # first entry the trend offers.
                if pivots[1].confirm == k and pivots[0].idx < opp[1].idx < pivots[1].idx:
                    record_pullback(k, pivots[1], pivots[0], direction)
                protected = pivots[1]
                break

    # Anything still open at the end of the data is censored, not a result.
    return trends, pullbacks, extremes


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------


def q(values: list[float], p: float) -> float:
    xs = sorted(values)
    if not xs:
        return float("nan")
    pos = (len(xs) - 1) * p
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def t_stat(xs: list[float]) -> float:
    if len(xs) < 2:
        return float("nan")
    sd = statistics.stdev(xs)
    return statistics.fmean(xs) / (sd / math.sqrt(len(xs))) if sd > 0 else float("nan")


def dist_row(label: str, xs: list[float], fmt: str = "{:.2f}") -> str:
    if not xs:
        return f"| {label} | 0 | | | | | |"
    return (
        f"| {label} | {len(xs)} | "
        + " | ".join(
            fmt.format(v)
            for v in (q(xs, 0.25), q(xs, 0.5), statistics.fmean(xs), q(xs, 0.75), q(xs, 0.9))
        )
        + " |"
    )


DIST_HEAD = "| measure | n | p25 | median | mean | p75 | p90 |\n|---|---:|---:|---:|---:|---:|---:|"


def outcome_row(label: str, pbs: list[Pullback], net: bool = True) -> str:
    rs = [
        p.ride_r_gross - (p.cost_r or 0) if net else p.ride_r_gross
        for p in pbs
        if p.ride_r_gross is not None
    ]
    cont = [p.continued for p in pbs if p.continued is not None]
    if not rs:
        return f"| {label} | 0 | | | | |"
    won = 100 * sum(r > 0 for r in rs) / len(rs)
    return (
        f"| {label} | {len(rs)} | {100 * sum(cont) / len(cont):.0f}% | "
        f"{won:.0f}% | {statistics.fmean(rs):+.3f} | {t_stat(rs):+.2f} |"
    )


OUTCOME_HEAD = (
    "| bucket | n | made a new extreme | ride R > 0 | mean ride R (net) | t |\n"
    "|---|---:|---:|---:|---:|---:|"
)


def terciles(pbs: list[Pullback], attr: str) -> list[tuple[str, list[Pullback]]]:
    vals = sorted(getattr(p, attr) for p in pbs if getattr(p, attr) is not None)
    if len(vals) < 9:
        return []
    a, b = q(vals, 1 / 3), q(vals, 2 / 3)
    lo = [p for p in pbs if getattr(p, attr) is not None and getattr(p, attr) <= a]
    mid = [p for p in pbs if getattr(p, attr) is not None and a < getattr(p, attr) <= b]
    hi = [p for p in pbs if getattr(p, attr) is not None and getattr(p, attr) > b]
    return [
        (f"{attr} <= {a:.2f}", lo),
        (f"{a:.2f} < {attr} <= {b:.2f}", mid),
        (f"{attr} > {b:.2f}", hi),
    ]


def report(s: Series, strength: int, *, intraday: bool) -> tuple[list[str], dict]:
    trends, pbs, exs = segment(s, strength)
    closed = [t for t in trends if t.end_idx is not None]
    out: list[str] = []
    summary: dict = {"series": s.name, "strength": strength, "bars": len(s)}
    unit = "bars"
    out.append(f"\n## {s.name} - swing strength {strength} ({len(s)} bars)\n")

    # --- Trend legs ---
    out.append("### Trend legs (origin -> peak, and what the break left)\n")
    out.append(DIST_HEAD)
    for d, label in ((1, "up"), (-1, "down")):
        ts_ = [t for t in closed if t.direction == d]
        if not ts_:
            continue
        leg_pct = [abs(t.peak_price - t.origin_price) / t.origin_price * 100 for t in ts_]
        leg_atr = [
            abs(t.peak_price - t.origin_price) / (s.atr[t.confirm_idx] or float("nan"))
            for t in ts_
            if s.atr[t.confirm_idx]
        ]
        bars_to_peak = [t.peak_idx - t.origin_idx for t in ts_]
        life = [t.end_idx - t.origin_idx for t in ts_]
        # How much of the leg was still there after the trend was confirmed
        # and after the break: the part a structure rider can capture.
        after_confirm = [
            d * (t.peak_price - t.confirm_price) / abs(t.peak_price - t.origin_price)
            for t in ts_
            if t.peak_price != t.origin_price
        ]
        captured = [
            d * (t.end_price - t.confirm_price) / abs(t.peak_price - t.origin_price)
            for t in ts_
            if t.peak_price != t.origin_price
        ]
        out.append(dist_row(f"{label}: leg size %", leg_pct))
        out.append(dist_row(f"{label}: leg size ATR", leg_atr))
        out.append(dist_row(f"{label}: {unit} origin->peak", bars_to_peak, "{:.0f}"))
        out.append(dist_row(f"{label}: {unit} origin->break", life, "{:.0f}"))
        out.append(
            dist_row(f"{label}: pullbacks per trend", [t.n_pullbacks for t in ts_], "{:.1f}")
        )
        out.append(dist_row(f"{label}: leg left after confirmation (frac)", after_confirm))
        out.append(dist_row(f"{label}: confirm->break captured (frac of leg)", captured))
        summary[f"trends_{label}"] = {
            "n": len(ts_),
            "median_leg_pct": q(leg_pct, 0.5),
            "median_bars_to_break": q(life, 0.5),
            "median_pullbacks": q([t.n_pullbacks for t in ts_], 0.5),
            "mean_captured_frac": statistics.fmean(captured) if captured else None,
        }
    reversals = [t for t in closed if t.next_direction is not None]
    if reversals:
        flips = sum(t.next_direction == -t.direction for t in reversals)
        n_up = sum(t.direction == 1 for t in closed)
        flip_pct = 100 * flips / len(reversals)
        out.append(
            f"\n{len(closed)} completed trends ({n_up} up, {len(closed) - n_up} down); "
            f"{len(trends) - len(closed)} still open at the end of data. After a break, the "
            f"NEXT trend established was the opposite direction **{flip_pct:.0f}%** of the "
            f"time ({flips}/{len(reversals)}) and the same direction {100 - flip_pct:.0f}%."
        )
        summary["break_then_opposite_pct"] = flip_pct
    if intraday:
        spans = [t for t in closed if s.day[t.origin_idx] != s.day[t.end_idx]]
        out.append(
            f"{100 * len(spans) / max(1, len(closed)):.0f}% of completed trends span at least "
            f"one overnight gap (origin and break on different sessions)."
        )
        summary["trends_spanning_sessions_pct"] = 100 * len(spans) / max(1, len(closed))

    # --- Pullbacks ---
    done = [p for p in pbs if p.ride_r_gross is not None]
    out.append("\n### Pullbacks inside a trend (each confirmed higher low / lower high)\n")
    out.append(DIST_HEAD)
    out.append(dist_row("depth, fraction of prior impulse", [p.depth_frac for p in done]))
    out.append(dist_row("depth, ATR", [p.depth_atr for p in done]))
    out.append(dist_row(f"pullback duration, {unit}", [p.pullback_bars for p in done], "{:.0f}"))
    out.append(dist_row(f"impulse duration, {unit}", [p.impulse_bars for p in done], "{:.0f}"))
    out.append(dist_row("impulse size, ATR", [p.impulse_atr for p in done]))
    out.append(
        dist_row(
            "pullback/impulse volume ratio", [p.vol_ratio for p in done if p.vol_ratio is not None]
        )
    )
    summary["pullbacks"] = len(done)
    summary["median_depth_frac"] = q([p.depth_frac for p in done], 0.5)
    summary["median_depth_atr"] = q([p.depth_atr for p in done], 0.5)

    out.append("\n#### Where pullbacks held (nearest level within 0.25 ATR of the pivot)\n")
    out.append(OUTCOME_HEAD)
    groups: dict[str, list[Pullback]] = defaultdict(list)
    for p in done:
        groups[p.held_at].append(p)
    for name in sorted(groups, key=lambda g: -len(groups[g])):
        out.append(outcome_row(f"held at {name}", groups[name]))
    out.append("\nTouch rates (a pullback can touch several levels at once):\n")
    out.append("| level | touched | share of pullbacks |\n|---|---:|---:|")
    touch_counts = defaultdict(int)
    for p in done:
        for k_, v_ in p.near.items():
            touch_counts[k_] += int(v_)
    for k_, v_ in sorted(touch_counts.items(), key=lambda kv: -kv[1]):
        out.append(f"| {k_} | {v_} | {100 * v_ / max(1, len(done)):.0f}% |")
    summary["held_at"] = {g: len(v) for g, v in groups.items()}

    out.append(
        "\n#### Pullback depth vs what followed "
        "(entry at confirmation, exit at the structure break)\n"
    )
    out.append(OUTCOME_HEAD)
    bands = [(0, 0.236), (0.236, 0.382), (0.382, 0.5), (0.5, 0.618), (0.618, 0.786), (0.786, 1.01)]
    depth_rows = {}
    for lo_, hi_ in bands:
        sel = [p for p in done if lo_ <= p.depth_frac < hi_]
        out.append(outcome_row(f"depth {lo_:.3f}-{min(hi_, 1):.3f}", sel))
        depth_rows[f"{lo_}-{hi_}"] = len(sel)
    summary["depth_band_counts"] = depth_rows
    out.append("\n#### Leg number within the trend\n")
    out.append(OUTCOME_HEAD)
    for leg in (1, 2, 3):
        out.append(outcome_row(f"pullback #{leg}", [p for p in done if p.leg_no == leg]))
    out.append(outcome_row("pullback #4+", [p for p in done if p.leg_no >= 4]))

    out.append("\n#### Trend strength at the entry vs the ride\n")
    out.append(OUTCOME_HEAD)
    for attr in ("adx", "ema_spread_atr", "ema21_slope_atr", "impulse_atr", "vol_ratio"):
        for label, sel in terciles(done, attr):
            out.append(outcome_row(label, sel))
    for attr in ("htf_aligned", "daily_aligned"):
        for flag in (True, False):
            sel = [p for p in done if getattr(p, attr) is flag]
            if sel:
                out.append(outcome_row(f"{attr} = {flag}", sel))

    out.append("\n#### Direction, and the cost of being flat by the bell\n")
    out.append(OUTCOME_HEAD)
    for d, label in (
        (1, "long (higher lows in uptrends)"),
        (-1, "short (lower highs in downtrends)"),
    ):
        out.append(outcome_row(label, [p for p in done if p.direction == d]))
    out.append(outcome_row("all, GROSS (before costs)", done, net=False))
    all_net = [p.ride_r_gross - (p.cost_r or 0) for p in done]
    summary["ride_all_net_mean_r"] = statistics.fmean(all_net) if all_net else None
    summary["ride_all_net_t"] = t_stat(all_net) if all_net else None
    summary["ride_all_n"] = len(all_net)
    if intraday:
        sess = [
            p.ride_r_session_gross - (p.cost_r or 0)
            for p in done
            if p.ride_r_session_gross is not None
        ]
        spans = [p for p in done if p.spans_session]
        span_pct = 100 * len(spans) / max(1, len(done))
        out.append(
            f"\nHeld to the break (overnight allowed): mean net ride "
            f"**{statistics.fmean(all_net):+.3f}R** (t {t_stat(all_net):+.2f}, n {len(all_net)}). "
            f"Same entries flattened at the entry session's close: "
            f"**{statistics.fmean(sess):+.3f}R** (t {t_stat(sess):+.2f}). "
            f"{span_pct:.0f}% of rides would have crossed an overnight gap."
        )
        summary["ride_session_net_mean_r"] = statistics.fmean(sess) if sess else None
        summary["ride_session_net_t"] = t_stat(sess) if sess else None
        summary["rides_spanning_sessions_pct"] = 100 * len(spans) / max(1, len(done))
    costs = [p.cost_r for p in done if p.cost_r is not None]
    out.append(
        f"Median round-trip cost of one entry: **{q(costs, 0.5):.3f}R** "
        f"(6bps of price divided by the stop distance)."
    )
    summary["median_cost_r"] = q(costs, 0.5)
    mfe = [p.mfe_r for p in done if p.mfe_r is not None]
    out.append(
        f"Median best excursion before the break (MFE): {q(mfe, 0.5):.2f}R; "
        f"p75 {q(mfe, 0.75):.2f}R."
    )
    summary["median_mfe_r"] = q(mfe, 0.5)

    # --- What precedes the break ---
    resolved = [e for e in exs if e.outcome is not None]
    out.append(
        "\n### What precedes a reversal: each with-trend swing, "
        "and whether the next event was a break\n"
    )
    out.append(
        "| feature at the swing | n | next event = break | n (without) | break (without) |\n"
        "|---|---:|---:|---:|---:|"
    )
    base = sum(e.outcome == "broke" for e in resolved) / max(1, len(resolved))
    for attr, label in (
        ("failed", "failed extreme (lower high in an uptrend / higher low in a downtrend)"),
        ("divergence", "RSI divergence (new extreme, weaker RSI)"),
        ("adx_falling", "ADX lower than at the previous extreme"),
    ):
        yes = [e for e in resolved if getattr(e, attr) is True]
        no = [e for e in resolved if getattr(e, attr) is False]
        if yes and no:
            broke_yes = 100 * sum(e.outcome == "broke" for e in yes) / len(yes)
            broke_no = 100 * sum(e.outcome == "broke" for e in no) / len(no)
            out.append(
                f"| {label} | {len(yes)} | {broke_yes:.0f}% | {len(no)} | {broke_no:.0f}% |"
            )
            summary[f"break_rate_{attr}"] = {"with": broke_yes, "without": broke_no, "n": len(yes)}
    heavy = [e for e in resolved if e.vol_ratio is not None and e.vol_ratio >= 1.5]
    light = [e for e in resolved if e.vol_ratio is not None and e.vol_ratio < 1.5]
    if heavy and light:
        out.append(
            f"| volume at the swing >= 1.5x its 20-bar mean | {len(heavy)} | "
            f"{100 * sum(e.outcome == 'broke' for e in heavy) / len(heavy):.0f}% | {len(light)} | "
            f"{100 * sum(e.outcome == 'broke' for e in light) / len(light):.0f}% |"
        )
    out.append(
        f"\nBase rate: {100 * base:.0f}% of {len(resolved)} with-trend swings "
        f"were followed by the break."
    )
    summary["extremes_resolved"] = len(resolved)
    summary["extreme_break_base_rate_pct"] = 100 * base
    return out, summary


# ---------------------------------------------------------------------------


async def load(symbol: str, days: int):
    url = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://trading_os:trading_os@localhost:5432/trading_os"
    )
    if "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    end = datetime.now(UTC).date()
    async with factory() as session:
        store = MarketDataStore(session)
        five = await store.get_bars(
            symbol, bar_interval="5m", start_date=end - timedelta(days=days), end_date=end
        )
        fifteen = await store.get_bars(
            symbol, bar_interval="15m", start_date=end - timedelta(days=days), end_date=end
        )
        daily = await store.get_bars(
            symbol, bar_interval="1d", start_date=end - timedelta(days=4000), end_date=end
        )
    await engine.dispose()
    return five, fifteen, daily


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="TQQQ.US")
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--strength", type=int, nargs="+", default=[3, 2])
    ap.add_argument("--daily-strength", type=int, default=2)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    five, fifteen, daily = await load(args.symbol, args.days)
    if not five:
        print(f"NO DATA for {args.symbol} 5m")
        return 1
    regular = [b for b in five if is_regular_hours(b.ts)]
    sessions = sorted({session_date(b.ts) for b in regular})
    print(f"# Trend anatomy - {args.symbol}\n")
    print(
        f"5m regular-hours bars: {len(regular)} over {len(sessions)} sessions "
        f"({sessions[0]} to {sessions[-1]}); 15m bars {len(fifteen)}; daily bars {len(daily)}"
        + (f" ({session_date(daily[0].ts)} to {session_date(daily[-1].ts)})" if daily else "")
        + "."
    )
    first_open, last_close = float(regular[0].open or regular[0].close), float(regular[-1].close)
    print(f"Buy and hold over the 5m window: {100 * (last_close / first_open - 1):+.1f}%.")

    summaries = []
    s5 = make_series(f"{args.symbol} 5m (regular hours, joined across sessions)", regular)
    add_indicators(s5, anchor_vwap=True)
    add_bias(s5, fifteen, daily, exec_minutes=5)
    for strength in args.strength:
        lines, summary = report(s5, strength, intraday=True)
        print("\n".join(lines))
        summaries.append(summary)

    if daily:
        sd = make_series(f"{args.symbol} 1d", daily)
        add_indicators(sd, anchor_vwap=False)
        lines, summary = report(sd, args.daily_strength, intraday=False)
        print("\n".join(lines))
        summaries.append(summary)
        print(
            f"\nBuy and hold over the daily window: "
            f"{100 * (float(daily[-1].close) / float(daily[0].close) - 1):+.1f}%."
        )

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "symbol": args.symbol,
                    "generated": datetime.now(UTC).isoformat(),
                    "series": summaries,
                },
                fh,
                indent=2,
                default=str,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
