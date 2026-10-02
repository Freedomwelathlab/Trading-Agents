"""The scan board behind every bot dashboard (Phase 107, D133).

For one symbol at its latest stored bar this evaluates EVERY registered
setup - not only the best one, which is all the bot's own scanner keeps -
and says, per setup: whether it fired, its direction and score, the entry,
stop and 1R target it proposes, its MEASURED confidence (the win rate of
its setup/direction/score bucket over recent sessions: +1R before the stop,
same rule as the chart signals, D117), and whether this bot would act on
it. The best qualifying signal becomes the recommendation (BUY / SELL); with
none, the recommendation is WAIT and the headline says why.

The context is built by `build_latest_context`, the bot's own function, so
a dashboard can never show a score the bot would not compute. Nothing here
places an order or stores anything.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

from apps.api.app.autotrade.scanner import LatestContext, build_latest_context
from apps.api.app.backtesting.brackets import BracketPlan, stop_quality_ok
from apps.api.app.backtesting.setups import SETUPS, run_detector
from apps.api.app.backtesting.signal_calibration import (
    Bucket,
    BucketKey,
    calibrate,
    scan_signals,
)
from apps.api.app.marketdata.bar_router import BarBackfillRouter
from apps.api.app.marketdata.fx import is_fx_symbol
from apps.api.app.marketdata.sessions import (
    CRYPTO_24H_CALENDAR,
    FX_24H_CALENDAR,
    US_EQUITY_CALENDAR,
    SessionCalendar,
    build_session_levels,
    group_by_session,
    session_vwap,
)
from apps.api.app.marketdata.structure import Direction

AssetClass = Literal["crypto", "forex", "equity"]
Recommendation = Literal["BUY", "SELL", "WAIT"]
CALIBRATION_SESSIONS = 60


def asset_class_of(symbol: str) -> AssetClass:
    if BarBackfillRouter.is_crypto_symbol(symbol):
        return "crypto"
    if is_fx_symbol(symbol):
        return "forex"
    return "equity"


def calendar_for(symbol: str, market_type: str) -> SessionCalendar:
    """The clock a symbol is scanned on for a bot of this market type."""
    if market_type == "24h":
        cls = asset_class_of(symbol)
        if cls == "crypto":
            return CRYPTO_24H_CALENDAR
        if cls == "forex":
            return FX_24H_CALENDAR
    return US_EQUITY_CALENDAR


@dataclass(frozen=True)
class SetupScan:
    setup: str
    enabled: bool
    """In the bot's active setup list (after demotion)."""
    fired: bool
    direction: str | None = None
    score: int | None = None
    entry: Decimal | None = None
    stop: Decimal | None = None
    target_1r: Decimal | None = None
    risk_pct: Decimal | None = None
    confidence_pct: Decimal | None = None
    """Measured win rate of this setup/direction/score bucket; None below
    the minimum sample - never estimated."""
    sample: int | None = None
    verdict: str = "no_signal"
    """qualifies | below_min_score | direction_not_allowed | stop_quality |
    not_enabled | no_signal"""


@dataclass(frozen=True)
class SymbolBoard:
    symbol: str
    asset_class: AssetClass
    last_close: Decimal | None
    last_ts: datetime | None
    phase: str | None
    recommendation: Recommendation
    headline: str
    best: SetupScan | None
    setups: tuple[SetupScan, ...]
    bars_used: int


def _q(x: Decimal | None, places: str = "0.000001") -> Decimal | None:
    return None if x is None else x.quantize(Decimal(places))


def calibrate_symbol(
    bars: Sequence[Any], *, market_type: str, calendar: SessionCalendar
) -> dict[BucketKey, Bucket]:
    """Measured outcome buckets over the last 60 sessions of `bars`, on the
    symbol's own clock. Empty when there is too little history."""
    if not bars:
        return {}
    by_day = group_by_session(bars, calendar=calendar)
    levels_by_day = build_session_levels(bars, calendar=calendar)

    def vwap_for_day(day_bars: list[Any]) -> dict[Any, Any]:
        return {p.ts: p for p in session_vwap(day_bars, calendar=calendar)}

    signals, _ = scan_signals(
        bars,
        names=sorted(SETUPS),
        sessions=CALIBRATION_SESSIONS,
        market_type="regular" if market_type == "regular" else "auto",
        levels_by_day=levels_by_day,
        by_day=by_day,
        vwap_for_day=vwap_for_day,
        calendar=calendar,
    )
    return dict(calibrate(signals))


def build_board(
    symbol: str,
    bars: Sequence[Any],
    *,
    enabled_setups: Sequence[str],
    market_type: str,
    plan: BracketPlan,
    min_score: int,
    allow_directions: Sequence[Direction],
    calibration: dict[BucketKey, Bucket] | None = None,
) -> SymbolBoard:
    cls = asset_class_of(symbol)
    calendar = calendar_for(symbol, market_type)
    ordered = sorted(bars, key=lambda b: b.ts)
    last = ordered[-1] if ordered else None
    built = build_latest_context(ordered, market_type=market_type, calendar=calendar)
    if isinstance(built, str):
        return SymbolBoard(
            symbol=symbol, asset_class=cls,
            last_close=last.close if last else None, last_ts=last.ts if last else None,
            phase=None, recommendation="WAIT", headline=_why_no_context(built),
            best=None, setups=(), bars_used=len(ordered),
        )
    return _evaluate(
        symbol, cls, built, enabled_setups=enabled_setups, plan=plan, min_score=min_score,
        allow_directions=allow_directions, calibration=calibration or {},
        bars_used=len(ordered),
    )


def _why_no_context(reason: str) -> str:
    if reason == "no_bars":
        return "No bars stored yet for this symbol - the first bot cycle or chart view loads them."
    if reason.startswith("phase_"):
        phase = reason.removeprefix("phase_").removesuffix("_not_allowed")
        return f"Outside this bot's trading hours (market phase: {phase})."
    if reason.startswith("only_"):
        n = reason.split("_")[1]
        return f"Only {n} bars in the current session so far; setups need at least 6."
    return reason.replace("_", " ")


def _evaluate(
    symbol: str,
    cls: AssetClass,
    built: LatestContext,
    *,
    enabled_setups: Sequence[str],
    plan: BracketPlan,
    min_score: int,
    allow_directions: Sequence[Direction],
    calibration: dict[BucketKey, Bucket],
    bars_used: int,
) -> SymbolBoard:
    ctx = built.ctx
    enabled = set(enabled_setups)
    rows: list[SetupScan] = []
    for name in sorted(SETUPS):
        signal = run_detector(SETUPS[name], ctx, atr_stop_buffer=plan.atr_stop_buffer)
        if signal is None:
            rows.append(SetupScan(setup=name, enabled=name in enabled, fired=False))
            continue
        risk = abs(signal.entry_price - signal.stop_price)
        long = signal.direction is Direction.LONG
        target = signal.entry_price + risk if long else signal.entry_price - risk
        bucket = calibration.get((name, signal.direction.value, signal.score))
        if name not in enabled:
            verdict = "not_enabled"
        elif signal.direction not in allow_directions:
            verdict = "direction_not_allowed"
        elif signal.score < min_score:
            verdict = "below_min_score"
        elif not stop_quality_ok(
            entry_price=signal.entry_price, stop_price=signal.stop_price, atr=ctx.atr, plan=plan
        ):
            verdict = "stop_quality"
        else:
            verdict = "qualifies"
        rows.append(
            SetupScan(
                setup=name, enabled=name in enabled, fired=True,
                direction=signal.direction.value, score=signal.score,
                entry=_q(signal.entry_price), stop=_q(signal.stop_price), target_1r=_q(target),
                risk_pct=(risk * 100 / signal.entry_price).quantize(Decimal("0.01"))
                if signal.entry_price else None,
                confidence_pct=bucket.win_rate if bucket else None,
                sample=bucket.resolved if bucket else None,
                verdict=verdict,
            )
        )

    def rank(r: SetupScan) -> tuple[int, Decimal]:
        return (r.score or 0, r.confidence_pct or Decimal(-1))

    qualifying = sorted((r for r in rows if r.verdict == "qualifies"), key=rank, reverse=True)
    fired = sorted((r for r in rows if r.fired), key=rank, reverse=True)
    rows.sort(key=lambda r: (not r.fired, -(r.score or 0), r.setup))
    phase = built.phase.value
    if qualifying:
        best = qualifying[0]
        rec: Recommendation = "BUY" if best.direction == "long" else "SELL"
        conf = f", measured {best.confidence_pct}% on {best.sample}" if best.confidence_pct else ""
        headline = (
            f"{rec} {symbol}: {best.setup} scored {best.score}/10{conf}. Entry {best.entry}, "
            f"stop {best.stop}, 1R target {best.target_1r}."
        )
    elif fired:
        best = fired[0]
        rec = "WAIT"
        why = {
            "below_min_score": f"scored {best.score}, below this bot's minimum {min_score}",
            "direction_not_allowed": f"is {best.direction}, which this bot does not trade",
            "stop_quality": "has a stop too tight or too wide for the plan",
            "not_enabled": "is not one of this bot's active setups",
        }.get(best.verdict, best.verdict)
        headline = f"WAIT: the strongest signal, {best.setup} {best.direction}, {why}."
    else:
        best = None
        rec = "WAIT"
        headline = "WAIT: no setup fires on the latest bar."
    last_ts = built.last_ts if isinstance(built.last_ts, datetime) else None
    return SymbolBoard(
        symbol=symbol, asset_class=cls, last_close=built.last_close, last_ts=last_ts,
        phase=phase, recommendation=rec, headline=headline, best=best,
        setups=tuple(rows), bars_used=bars_used,
    )


# --- calibration cache ------------------------------------------------------
#
# A 60-session replay of every setup takes seconds for an equity and over a
# minute for a 24-hour market (17,000+ five-minute bars). A dashboard must
# not wait for that, so the replay runs in the background once per symbol,
# interval and clock per UTC day; until it lands the board says
# "calculating" instead of showing a number.

CalibrationKey = tuple[str, str, str, str, date]
_CALIBRATION: dict[CalibrationKey, dict[BucketKey, Bucket]] = {}
_JOBS: dict[CalibrationKey, asyncio.Task[None]] = {}


def calibration_key(symbol: str, bar_interval: str, market_type: str) -> CalibrationKey:
    calendar = calendar_for(symbol, market_type)
    return (symbol, bar_interval, market_type, calendar.name, datetime.now(UTC).date())


def calibration_or_start(
    symbol: str,
    bar_interval: str,
    market_type: str,
    loader: Callable[[], Awaitable[Sequence[Any]]],
) -> tuple[dict[BucketKey, Bucket] | None, str]:
    """(buckets, "ready") when today's replay exists; otherwise (None,
    "calculating") after making sure one is running, or (None,
    "unavailable: ...") when the last attempt failed today."""
    key = calibration_key(symbol, bar_interval, market_type)
    hit = _CALIBRATION.get(key)
    if hit is not None:
        return hit, "ready"
    job = _JOBS.get(key)
    if job is not None and job.done():
        exc = job.exception()
        if exc is not None:
            return None, f"unavailable: {type(exc).__name__}: {str(exc)[:160]}"
    if job is None:
        if len(_CALIBRATION) > 256:
            _CALIBRATION.clear()
        calendar = calendar_for(symbol, market_type)

        async def run() -> None:
            bars = await loader()
            _CALIBRATION[key] = await asyncio.to_thread(
                calibrate_symbol, bars, market_type=market_type, calendar=calendar
            )

        _JOBS[key] = asyncio.get_running_loop().create_task(run())
    return None, "calculating"
