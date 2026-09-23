"""The playbook's reversal setups, as parameterised detectors (Phase 73,
D091).

Each setup answers one question at one bar: *given everything knowable at
this close, is there a risk-defined entry here?* It returns an entry
price, a structural stop and the evidence behind it - never a position
size and never a target, because those belong to the bracket plan and are
derived from the stop.

**Nothing here is TQQQ-specific.** Every level is derived from the
instrument's own session data and every distance is in ATR units, so the
same setup applies unchanged to any symbol with intraday bars. TQQQ is
the instrument these were written FOR - a 3x ETF whose bars are wide
enough that the playbook's stop framework matters - not a constant baked
into them.

**The scoring model is the playbook's, and its thresholds are explicitly
untested.** Section 14 assigns points to each piece of evidence and gates
trading at 8/10. Its own text calls those "starting test values, not
empirically proven cutoffs". They are carried through here with that
status intact: `score` is reported on every signal so the threshold can
be varied and measured, rather than being hard-coded into who trades.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from apps.api.app.marketdata.candles import (
    PatternDirection,
    Trend,
    detect_at,
)
from apps.api.app.marketdata.indicators import InsufficientDataError
from apps.api.app.marketdata.indicators import rsi as _indicator_rsi
from apps.api.app.marketdata.sessions import SessionLevels, VwapPoint
from apps.api.app.marketdata.structure import (
    Direction,
    SwingKind,
    SwingPoint,
    detect_structure_shift,
    detect_sweep,
    last_confirmed_swing,
)


@dataclass(frozen=True)
class SetupSignal:
    """A risk-defined entry proposal at one bar."""

    setup_name: str
    direction: Direction
    entry_index: int
    entry_price: Decimal
    stop_price: Decimal
    score: int
    evidence: dict[str, str] = field(default_factory=dict)


@dataclass
class BarContext:
    """Everything knowable at one bar, assembled by the engine.

    Passed as a single object rather than a long argument list because
    every setup needs a different subset, and because it keeps the "what
    was knowable when" boundary in ONE place: if a field is here, the
    engine has already established it was available at this bar.
    """

    bars: Sequence  # session bars up to and including index
    index: int
    levels: SessionLevels
    swings: Sequence[SwingPoint]
    vwap: VwapPoint | None
    atr: Decimal | None
    rsi: Decimal | None
    ema_fast: Decimal | None
    ema_slow: Decimal | None
    higher_tf_bias: Direction | None = None
    confirm_bias: Direction | None = None

    @property
    def bar(self):
        return self.bars[self.index]


def _high(bar) -> Decimal:
    return bar.high if bar.high is not None else bar.close


def _low(bar) -> Decimal:
    return bar.low if bar.low is not None else bar.close


def _stop_from_extreme(
    extreme: Decimal, direction: Direction, atr: Decimal | None, buffer: Decimal
) -> Decimal:
    """Structural level first, volatility buffer second - the playbook's
    exact ordering. The stop sits beyond the swept extreme, not beyond the
    level that was swept, because price has already proven it can reach
    the extreme."""
    pad = (atr or Decimal(0)) * buffer
    return extreme - pad if direction is Direction.LONG else extreme + pad


# ---------------------------------------------------------------------------
# Strategy 1 / Section 25 - Liquidity sweep + 5M market structure shift
# ---------------------------------------------------------------------------


def sweep_mss_setup(
    ctx: BarContext,
    *,
    atr_stop_buffer: Decimal = Decimal("0.30"),
    max_bars_to_reclaim: int = 6,
    lookback: int = 24,
    require_structure_shift: bool = True,
    require_retest: bool = True,
    retest_tolerance_atr: Decimal = Decimal("0.25"),
) -> SetupSignal | None:
    """The playbook's priority-10 setup, and its "best single setup".

    *Sweep a session level -> reclaim -> break the opposing structure ->
    RETEST -> enter.* The playbook is emphatic that a sweep without a
    structure shift is a stand-aside, which `require_structure_shift`
    honours by default; it is a parameter only so the contribution of
    that rule can be measured rather than assumed.

    **The retest is not decoration - it is what makes the risk work.**
    Entering on the structure break itself puts the entry at the top of
    the move while the stop stays down at the swept extreme, so the whole
    advance becomes the risk distance and a 1R target then demands that
    same distance again. Measured on real TQQQ data, that version reached
    its first target on 15% of trades while being stopped on 76%, and the
    asymmetry was an artefact of the entry location rather than a fact
    about the market. Waiting for price to come back to the broken level
    puts the entry near the invalidation point, which is the entire
    purpose of the sequence.

    Both directions, from the same four levels mirrored: previous-day and
    premarket extremes.
    """
    if ctx.index < 4:
        return None

    window_start = max(0, ctx.index - lookback)
    window = ctx.bars[window_start : ctx.index + 1]

    candidates: list[tuple[str, Decimal, Direction]] = []
    if ctx.levels.previous_low is not None:
        candidates.append(("PDL", ctx.levels.previous_low, Direction.LONG))
    if ctx.levels.premarket_low is not None:
        candidates.append(("premarket_low", ctx.levels.premarket_low, Direction.LONG))
    if ctx.levels.previous_high is not None:
        candidates.append(("PDH", ctx.levels.previous_high, Direction.SHORT))
    if ctx.levels.premarket_high is not None:
        candidates.append(("premarket_high", ctx.levels.premarket_high, Direction.SHORT))

    for name, price, direction in candidates:
        sweep = detect_sweep(
            window,
            level_price=price,
            level_name=name,
            direction=direction,
            max_bars_to_reclaim=max_bars_to_reclaim,
        )
        if sweep is None or sweep.reclaimed_ts > ctx.bar.ts:
            continue

        score = 2  # liquidity sweep
        evidence = {"sweep": f"{name} @ {price}", "extreme": str(sweep.extreme_price)}

        shift = detect_structure_shift(
            window, ctx.swings, direction=direction, after_ts=sweep.reclaimed_ts
        )
        if shift is not None and shift.break_ts <= ctx.bar.ts:
            score += 2
            evidence["mss"] = f"broke {shift.broken_swing.price} at {shift.break_ts:%H:%M}"
        elif require_structure_shift:
            continue

        # --- Retest: price returns to the broken level, then resumes. ---
        if require_retest:
            if shift is None:
                continue
            if ctx.bar.ts <= shift.break_ts:
                continue
            tolerance = (ctx.atr or Decimal(0)) * retest_tolerance_atr
            level = shift.broken_swing.price
            after_break = [b for b in window if shift.break_ts < b.ts <= ctx.bar.ts]
            if not after_break:
                continue
            touched = any(
                _low(b) <= level + tolerance
                if direction is Direction.LONG
                else _high(b) >= level - tolerance
                for b in after_break
            )
            # The confirmation close the playbook asks for: price must
            # hold the level again, not merely reach it. A bar that
            # touches and keeps going is a failed retest, and entering on
            # it is entering into the continuation of the move against us.
            resumed = (
                ctx.bar.close > level
                if direction is Direction.LONG
                else ctx.bar.close < level
            )
            if not (touched and resumed):
                continue
            score += 1
            evidence["retest"] = f"held {level}"

        # Confirmation layers, each worth a point in section 14's model.
        if ctx.confirm_bias is direction:
            score += 1
            evidence["cross_market"] = "confirms"
        if ctx.vwap is not None:
            lower, upper = ctx.vwap.band(2)
            stretched = (
                ctx.bar.close < lower
                if direction is Direction.LONG
                else ctx.bar.close > upper
            )
            if stretched:
                score += 1
                evidence["vwap_extension"] = "beyond 2 sigma"
        if ctx.rsi is not None:
            exhausted = (
                ctx.rsi <= Decimal(35)
                if direction is Direction.LONG
                else ctx.rsi >= Decimal(65)
            )
            if exhausted:
                score += 1
                evidence["rsi"] = str(ctx.rsi.quantize(Decimal("0.1")))
        if name in ("PDL", "PDH"):
            score += 1
            evidence["major_level"] = name

        return SetupSignal(
            setup_name="sweep_mss",
            direction=direction,
            entry_index=ctx.index,
            entry_price=ctx.bar.close,
            stop_price=_stop_from_extreme(
                sweep.extreme_price, direction, ctx.atr, atr_stop_buffer
            ),
            score=score,
            evidence=evidence,
        )
    return None


# ---------------------------------------------------------------------------
# Strategy 2 - VWAP deviation reversal
# ---------------------------------------------------------------------------


def vwap_reversion_setup(
    ctx: BarContext,
    *,
    sigma_multiple: Decimal = Decimal("2"),
    atr_stop_buffer: Decimal = Decimal("0.30"),
    require_reentry: bool = True,
) -> SetupSignal | None:
    """Price stretched beyond ±2σ of session VWAP, then closing back inside.

    The playbook's critical filter is quoted almost verbatim in the
    `require_reentry` default: *"a strong trend can stay outside the band,
    therefore extreme alone = no trade"*. Entering on the touch rather
    than the re-entry is what turns this from mean reversion into
    catching a falling knife, and on a 3x ETF in a trend it can stay
    outside the band for hours.
    """
    if ctx.vwap is None or ctx.index < 2:
        return None

    lower, upper = ctx.vwap.band(sigma_multiple)
    if ctx.vwap.sigma <= 0:
        return None

    previous = ctx.bars[ctx.index - 1]
    bar = ctx.bar

    # Long: the prior bar closed below the lower band, this one closed back inside.
    if previous.close < lower <= bar.close:
        direction = Direction.LONG
        extreme = min(_low(previous), _low(bar))
    elif previous.close > upper >= bar.close:
        direction = Direction.SHORT
        extreme = max(_high(previous), _high(bar))
    elif require_reentry:
        return None
    else:
        return None

    score = 1  # vwap extension
    evidence = {
        "vwap": str(ctx.vwap.vwap.quantize(Decimal("0.01"))),
        "band": f"+/-{sigma_multiple} sigma",
    }
    if ctx.rsi is not None:
        exhausted = (
            ctx.rsi <= Decimal(30)
            if direction is Direction.LONG
            else ctx.rsi >= Decimal(70)
        )
        if exhausted:
            score += 1
            evidence["rsi"] = str(ctx.rsi.quantize(Decimal("0.1")))
    if ctx.confirm_bias is direction:
        score += 1
        evidence["cross_market"] = "confirms"

    return SetupSignal(
        setup_name="vwap_reversion",
        direction=direction,
        entry_index=ctx.index,
        entry_price=bar.close,
        stop_price=_stop_from_extreme(extreme, direction, ctx.atr, atr_stop_buffer),
        score=score,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Strategy 4 - Opening-range failure (ORB trap)
# ---------------------------------------------------------------------------


def orb_failure_setup(
    ctx: BarContext,
    *,
    atr_stop_buffer: Decimal = Decimal("0.30"),
    max_bars_to_reclaim: int = 6,
    lookback: int = 18,
) -> SetupSignal | None:
    """A break of the opening range that fails back inside it.

    Distinct from `sweep_mss` despite the similar shape: the level is the
    session's OWN first fifteen minutes rather than a previous day's
    extreme, so it exists on every session including gap days where the
    previous day's levels are far away and irrelevant.
    """
    if ctx.levels.opening_range_high is None or ctx.levels.opening_range_low is None:
        return None
    if ctx.index < 4:
        return None

    window = ctx.bars[max(0, ctx.index - lookback) : ctx.index + 1]

    for name, price, direction in (
        ("opening_range_high", ctx.levels.opening_range_high, Direction.SHORT),
        ("opening_range_low", ctx.levels.opening_range_low, Direction.LONG),
    ):
        sweep = detect_sweep(
            window,
            level_price=price,
            level_name=name,
            direction=direction,
            max_bars_to_reclaim=max_bars_to_reclaim,
        )
        if sweep is None or sweep.reclaimed_ts != ctx.bar.ts:
            # Only fire on the bar that completes the failure, so the
            # setup is not re-detected on every later bar in the window.
            continue

        score = 2 + 1  # sweep + opening-range confluence
        evidence = {"failed_break": f"{name} @ {price}"}
        if ctx.rsi is not None:
            evidence["rsi"] = str(ctx.rsi.quantize(Decimal("0.1")))
        if ctx.confirm_bias is direction:
            score += 1
            evidence["cross_market"] = "confirms"

        return SetupSignal(
            setup_name="orb_failure",
            direction=direction,
            entry_index=ctx.index,
            entry_price=ctx.bar.close,
            stop_price=_stop_from_extreme(
                sweep.extreme_price, direction, ctx.atr, atr_stop_buffer
            ),
            score=score,
            evidence=evidence,
        )
    return None


# ---------------------------------------------------------------------------
# Strategy 7 - EMA exhaustion + reversal pullback
# ---------------------------------------------------------------------------


def ema_reversal_setup(
    ctx: BarContext, *, atr_stop_buffer: Decimal = Decimal("0.30")
) -> SetupSignal | None:
    """A fast/slow EMA cross taken only as CONFIRMATION of a prior swing.

    The playbook states plainly that the crossover is confirmation and not
    a reason to enter by itself, so the setup additionally requires a
    confirmed swing to anchor the stop. Without that anchor there is no
    structural invalidation level and the stop would be an arbitrary
    distance - which the stop-quality section explicitly rejects.
    """
    if ctx.ema_fast is None or ctx.ema_slow is None or ctx.index < 2:
        return None

    direction = Direction.LONG if ctx.ema_fast > ctx.ema_slow else Direction.SHORT
    kind = SwingKind.LOW if direction is Direction.LONG else SwingKind.HIGH
    swing = last_confirmed_swing(ctx.swings, kind, as_of=ctx.bar.ts)
    if swing is None:
        return None

    # Require the cross to be fresh: the previous bar must not already
    # have been on this side, or every bar of a trend fires a signal.
    previous_close = ctx.bars[ctx.index - 1].close
    crossed_now = (
        previous_close <= ctx.ema_slow < ctx.bar.close
        if direction is Direction.LONG
        else previous_close >= ctx.ema_slow > ctx.bar.close
    )
    if not crossed_now:
        return None

    score = 1
    evidence = {"ema_cross": f"{ctx.ema_fast:.2f}/{ctx.ema_slow:.2f}"}
    if ctx.higher_tf_bias is direction:
        score += 1
        evidence["higher_tf"] = "aligned"

    return SetupSignal(
        setup_name="ema_reversal",
        direction=direction,
        entry_index=ctx.index,
        entry_price=ctx.bar.close,
        stop_price=_stop_from_extreme(
            swing.price, direction, ctx.atr, atr_stop_buffer
        ),
        score=score,
        evidence=evidence,
    )



# ---------------------------------------------------------------------------
# ASTA SMM/PAPA - candlestick reversal at a session level
# ---------------------------------------------------------------------------


def dow_trend(ctx: BarContext) -> Trend:
    """Trend by the Dow definition the SMM module teaches: an uptrend is
    higher highs AND higher lows.

    Derived from CONFIRMED swings only, so the trend a setup sees was
    knowable at this bar - the same discipline `structure.py` enforces for
    a market-structure shift. With fewer than two confirmed swings of each
    kind there is no Dow trend to state, and the honest answer is
    SIDEWAYS: that makes every reversal pattern fail its context gate
    rather than fire on an unknown trend.
    """
    highs = [s for s in ctx.swings if s.kind is SwingKind.HIGH and s.confirmed_ts <= ctx.bar.ts]
    lows = [s for s in ctx.swings if s.kind is SwingKind.LOW and s.confirmed_ts <= ctx.bar.ts]
    if len(highs) < 2 or len(lows) < 2:
        return Trend.SIDEWAYS
    h1, h2 = highs[-2].price, highs[-1].price
    l1, l2 = lows[-2].price, lows[-1].price
    if h2 > h1 and l2 > l1:
        return Trend.UP
    if h2 < h1 and l2 < l1:
        return Trend.DOWN
    return Trend.SIDEWAYS


def _levels_near(
    ctx: BarContext, tolerance: Decimal
) -> list[tuple[str, Decimal]]:
    """Session levels this bar is actually touching, within `tolerance`.

    PAPA's ordering is location first, then the candle signal AT that
    location - a pattern in open space is explicitly not a setup. Only
    levels the backend actually reported are considered; a `None` level is
    absent, never zero.
    """
    candidates = [
        ("PDH", ctx.levels.previous_high),
        ("PDL", ctx.levels.previous_low),
        ("PDC", ctx.levels.previous_close),
        ("premarket_high", ctx.levels.premarket_high),
        ("premarket_low", ctx.levels.premarket_low),
        ("opening_range_high", ctx.levels.opening_range_high),
        ("opening_range_low", ctx.levels.opening_range_low),
    ]
    if ctx.vwap is not None:
        candidates.append(("vwap", ctx.vwap.vwap))

    bar = ctx.bar
    near: list[tuple[str, Decimal]] = []
    for name, price in candidates:
        if price is None:
            continue
        # The bar TOUCHED the level if the level sits inside its range,
        # or within tolerance of either extreme.
        if _low(bar) - tolerance <= price <= _high(bar) + tolerance:
            near.append((name, price))
    return near


def candle_reversal_setup(
    ctx: BarContext,
    *,
    atr_stop_buffer: Decimal = Decimal("0.30"),
    location_tolerance_atr: Decimal = Decimal("0.25"),
    require_location: bool = True,
) -> SetupSignal | None:
    """A candlestick reversal pattern, at a session level, against a trend.

    This is the SMM/PAPA half of the ASTA material wired into the engine.
    It enforces both of the material's gates rather than trading a shape:

      1. **Trend context** - "a reversal candle is significant only at the
         end of a trend; ignore it in the middle of a trend or range."
         `CandlePattern.is_signal_in_context` applies that, against the Dow
         trend from confirmed swings.
      2. **Location** - the pattern must occur AT a marked level (previous
         day's high/low/close, premarket extreme, opening range, or VWAP).
         A hammer in open space is not a setup.

    The stop goes beyond the pattern's own extreme plus an ATR buffer,
    which is the structural invalidation: if price trades through the low
    that produced the hammer, the reading was wrong.
    """
    if ctx.index < 2:
        return None
    if ctx.bar.open is None:
        # An intraday bar with no open cannot be read as a candle. Refused
        # quietly here (rather than raising) because one malformed bar
        # should not abort a whole replay - the pattern simply is not
        # detectable, which is the honest answer.
        return None

    trend = dow_trend(ctx)
    try:
        patterns = detect_at(ctx.bars, ctx.index)
    except ValueError:
        return None
    if not patterns:
        return None

    tolerance = (ctx.atr or Decimal(0)) * location_tolerance_atr
    near = _levels_near(ctx, tolerance)
    if require_location and not near:
        return None

    for pattern in patterns:
        if pattern.direction is PatternDirection.NEUTRAL:
            # A doji is indecision, not a direction. It can corroborate a
            # directional pattern but never carries a trade on its own.
            continue
        if not pattern.is_signal_in_context(trend):
            continue

        direction = (
            Direction.LONG
            if pattern.direction is PatternDirection.BULLISH
            else Direction.SHORT
        )
        extreme = _low(ctx.bar) if direction is Direction.LONG else _high(ctx.bar)

        score = 2  # the pattern itself
        evidence = {"pattern": pattern.name, "trend": trend.value}
        if near:
            score += 1
            evidence["location"] = ", ".join(name for name, _ in near)
        if ctx.confirm_bias is direction:
            score += 1
            evidence["cross_market"] = "confirms"
        if ctx.rsi is not None:
            exhausted = (
                ctx.rsi <= Decimal(35)
                if direction is Direction.LONG
                else ctx.rsi >= Decimal(65)
            )
            if exhausted:
                score += 1
                evidence["rsi"] = str(ctx.rsi.quantize(Decimal("0.1")))
        if any(p.name == "doji" for p in patterns):
            evidence["indecision"] = "doji also present"

        return SetupSignal(
            setup_name="candle_reversal",
            direction=direction,
            entry_index=ctx.index,
            entry_price=ctx.bar.close,
            stop_price=_stop_from_extreme(extreme, direction, ctx.atr, atr_stop_buffer),
            score=score,
            evidence=evidence,
        )
    return None


SetupDetector = Callable[[BarContext], SetupSignal | None]
"""What the engine needs from a setup: one bar's context in, an optional
signal out. Every detector below also takes keyword parameters with
defaults, which is compatible with this - and naming the type keeps the
registry from widening to `object`, where a caller of `SETUPS[name]`
loses every guarantee about what it is calling."""

# ---------------------------------------------------------------------------
# Phase 85 (D102): three setups derived from measuring 5-minute structure
# (`scripts/research_5m_structure.py`, `docs/RESEARCH_5M.md`).
#
# READ THIS BEFORE USING THEM. The measurement over 128 sessions of real
# TQQQ and QQQ 5-minute bars found NO edge in reversing at swing highs or
# lows: once a pivot is measured from the bar it actually becomes knowable
# on (three bars after it prints), the forward hour is 48-50% either way
# with a mean within ±0.15 ATR. The one asymmetry that survived, on both
# symbols, was small and in the CONTINUATION direction: a pullback on
# falling volume resumed 51% of the time against 47-49% for one on rising
# volume.
#
# These three encode exactly that, and nothing more optimistic than that.
# ---------------------------------------------------------------------------


def _volume(bar) -> Decimal:
    return Decimal(bar.volume or 0)


def _avg_volume(ctx: BarContext, start: int, end: int) -> Decimal | None:
    window = [b for b in ctx.bars[max(0, start) : end] if b.volume]
    if not window:
        return None
    return sum((_volume(b) for b in window), Decimal(0)) / len(window)


def quiet_pullback_setup(
    ctx: BarContext,
    *,
    impulse_bars: int = 6,
    pullback_bars: int = 3,
    min_impulse_atr: Decimal = Decimal("1.0"),
    atr_stop_buffer: Decimal = Decimal("0.30"),
) -> SetupSignal | None:
    """Trend continuation after a pullback on FALLING volume.

    The only asymmetry the 5-minute study found with a consistent sign on
    both symbols, and it is small: 51% continuation against 47-49% for a
    pullback on rising volume, a mean difference of about 0.15 ATR over
    ~2,900 samples. Traded here with the trend, never against it.

    Three conditions, all of which must hold:
      1. an impulse of at least `min_impulse_atr` over `impulse_bars`;
      2. the last `pullback_bars` moved AGAINST that impulse;
      3. average volume in the pullback is below the impulse's.

    The stop is the pullback's own extreme plus an ATR buffer: if price
    takes out the low the pullback made, the pullback was a reversal.
    """
    i = ctx.index
    if i < impulse_bars + pullback_bars + 1 or ctx.atr is None or ctx.atr <= 0:
        return None

    impulse_start = i - impulse_bars - pullback_bars
    impulse_end = i - pullback_bars
    leg = ctx.bars[impulse_end].close - ctx.bars[impulse_start].close
    if abs(leg) < min_impulse_atr * ctx.atr:
        return None

    pull = ctx.bar.close - ctx.bars[impulse_end].close
    if leg > 0 and pull >= 0:
        return None
    if leg < 0 and pull <= 0:
        return None

    impulse_vol = _avg_volume(ctx, impulse_start, impulse_end)
    pull_vol = _avg_volume(ctx, impulse_end, i + 1)
    if impulse_vol is None or pull_vol is None or pull_vol >= impulse_vol:
        return None

    direction = Direction.LONG if leg > 0 else Direction.SHORT
    window = ctx.bars[impulse_end : i + 1]
    extreme = min(_low(b) for b in window) if leg > 0 else max(_high(b) for b in window)
    stop = _stop_from_extreme(extreme, direction, ctx.atr, atr_stop_buffer)

    score = 2
    evidence = {
        "impulse_atr": f"{abs(leg) / ctx.atr:.2f}",
        "pullback_volume_ratio": f"{pull_vol / impulse_vol:.2f}",
    }
    if ctx.higher_tf_bias is direction:
        score += 1
        evidence["higher_tf_bias"] = direction.value
    if pull_vol < impulse_vol * Decimal("0.7"):
        score += 1
        evidence["volume_dry_up"] = "pullback under 70% of impulse volume"
    if ctx.vwap is not None:
        above = ctx.bar.close > ctx.vwap.vwap
        if (direction is Direction.LONG) == above:
            score += 1
            evidence["vwap_side"] = "with trend"

    return SetupSignal(
        setup_name="quiet_pullback",
        direction=direction,
        entry_index=i,
        entry_price=ctx.bar.close,
        stop_price=stop,
        score=score,
        evidence=evidence,
    )


def volume_climax_reversal_setup(
    ctx: BarContext,
    *,
    climax_multiple: Decimal = Decimal("2.0"),
    atr_stop_buffer: Decimal = Decimal("0.30"),
    location_tolerance_atr: Decimal = Decimal("0.25"),
) -> SetupSignal | None:
    """A session extreme made on CLIMACTIC volume, at a marked level, that
    the next bar rejects.

    This is the brief's "catch the top / the bottom", written as strictly
    as the concept allows: the bar must make the session's extreme so far,
    on at least `climax_multiple` times the session's average volume, sit
    within a quarter-ATR of a marked level, and be REJECTED by the
    following bar closing back inside its range.

    **The study found no edge here** (a swing extreme reversed 44-50% of
    the time whether or not it sat at a level). It is included because the
    brief asked for a top/bottom strategy and because a strategy that is
    measured and found wanting is worth more than one that is assumed;
    every backtest of it must be read with `docs/RESEARCH_5M.md` open.
    """
    i = ctx.index
    if i < 6 or ctx.atr is None or ctx.atr <= 0 or ctx.bar.open is None:
        return None

    session_avg = _avg_volume(ctx, 0, i)
    if session_avg is None or session_avg <= 0:
        return None
    if _volume(ctx.bar) < climax_multiple * session_avg:
        return None

    prior_high = max(_high(b) for b in ctx.bars[:i])
    prior_low = min(_low(b) for b in ctx.bars[:i])
    bar_high, bar_low = _high(ctx.bar), _low(ctx.bar)

    if bar_high > prior_high:
        direction = Direction.SHORT
        extreme = bar_high
        rejected = ctx.bar.close < (bar_high + bar_low) / 2
    elif bar_low < prior_low:
        direction = Direction.LONG
        extreme = bar_low
        rejected = ctx.bar.close > (bar_high + bar_low) / 2
    else:
        return None
    if not rejected:
        return None

    tolerance = ctx.atr * location_tolerance_atr
    near = _levels_near(ctx, tolerance)
    if not near:
        return None

    stop = _stop_from_extreme(extreme, direction, ctx.atr, atr_stop_buffer)
    score = 2
    evidence = {
        "volume_x_session_avg": f"{_volume(ctx.bar) / session_avg:.2f}",
        "location": ", ".join(name for name, _ in near),
        "rejection": "close back inside the bar's own range",
    }
    if ctx.rsi is not None and (
        (direction is Direction.LONG and ctx.rsi < 30)
        or (direction is Direction.SHORT and ctx.rsi > 70)
    ):
        score += 1
        evidence["rsi"] = f"{ctx.rsi:.1f}"
    if ctx.confirm_bias is direction:
        score += 1
        evidence["confirm_bias"] = direction.value

    return SetupSignal(
        setup_name="volume_climax_reversal",
        direction=direction,
        entry_index=i,
        entry_price=ctx.bar.close,
        stop_price=stop,
        score=score,
        evidence=evidence,
    )


def gap_fade_setup(
    ctx: BarContext,
    *,
    min_gap_pct: Decimal = Decimal("0.5"),
    max_bar_index: int = 12,
    atr_stop_buffer: Decimal = Decimal("0.30"),
) -> SetupSignal | None:
    """Fade an opening gap back toward the previous close.

    The gap is this platform's only visible trace of overnight news and
    corporate actions in bar data — it is NOT a news feed, and this setup
    does not claim to read news. It fires in the first hour, when a gap of
    at least `min_gap_pct` has stalled (the bar closes back against the gap
    direction) and the previous close is still unfilled.

    **The gap is measured in PERCENT, not ATR.** ATR(14) is not computable
    from fewer than 15 bars, and this setup only ever looks at the first 12
    bars of a session, so an ATR gate here can never pass — the first
    version of this detector produced exactly zero trades for that reason.
    Percent of the previous close is knowable at the open, which is when
    this decision has to be made.

    **The study found gaps ≥ 1 ATR faded 46-49% of the time** — a coin
    flip. Included on the same terms as `volume_climax_reversal`.
    """
    i = ctx.index
    if i < 2 or i > max_bar_index:
        return None
    prev_close = ctx.levels.previous_close
    if prev_close is None or prev_close <= 0 or ctx.bars[0].open is None:
        return None

    gap = ctx.bars[0].open - prev_close
    if abs(gap) / prev_close * 100 < min_gap_pct:
        return None
    # Already filled: nothing left to fade.
    if (gap > 0 and ctx.bar.close <= prev_close) or (gap < 0 and ctx.bar.close >= prev_close):
        return None

    direction = Direction.SHORT if gap > 0 else Direction.LONG
    stalling = (
        ctx.bar.close < ctx.bar.open if gap > 0 else ctx.bar.close > ctx.bar.open
    ) if ctx.bar.open is not None else False
    if not stalling:
        return None

    window = ctx.bars[: i + 1]
    extreme = max(_high(b) for b in window) if gap > 0 else min(_low(b) for b in window)
    stop = _stop_from_extreme(extreme, direction, ctx.atr, atr_stop_buffer)

    score = 2
    evidence = {
        "gap_pct": f"{abs(gap) / prev_close * 100:.2f}",
        "target": "previous close (unfilled)",
        "bar_of_session": str(i),
    }
    if ctx.vwap is not None:
        if (direction is Direction.SHORT and ctx.bar.close < ctx.vwap.vwap) or (
            direction is Direction.LONG and ctx.bar.close > ctx.vwap.vwap
        ):
            score += 1
            evidence["vwap_side"] = "already back through VWAP"
    if ctx.higher_tf_bias is direction:
        score += 1
        evidence["higher_tf_bias"] = direction.value

    return SetupSignal(
        setup_name="gap_fade",
        direction=direction,
        entry_index=i,
        entry_price=ctx.bar.close,
        stop_price=stop,
        score=score,
        evidence=evidence,
    )




# ---------------------------------------------------------------------------
# Strategy 3 / Section 27 - RSI divergence at a swing
# ---------------------------------------------------------------------------


def _bar_index_at(ctx: BarContext, ts) -> int | None:
    """Index of the bar that printed at `ts`, or None if it is not in this
    window. Linear because a session is a few hundred bars and a dict keyed
    on timestamps would have to be rebuilt every bar anyway."""
    for k in range(ctx.index, -1, -1):
        if ctx.bars[k].ts == ts:
            return k
    return None


def _rsi_at(ctx: BarContext, index: int, period: int) -> Decimal | None:
    closes = [b.close for b in ctx.bars[: index + 1]]
    try:
        return _indicator_rsi(closes, period)
    except InsufficientDataError:
        return None


def rsi_divergence_setup(
    ctx: BarContext,
    *,
    period: int = 14,
    atr_stop_buffer: Decimal = Decimal("0.30"),
    min_rsi_gap: Decimal = Decimal("2"),
    max_bars_since_swing: int = 40,
) -> SetupSignal | None:
    """Price makes a new extreme; the oscillator does not.

    Bullish: this bar's low is BELOW the last confirmed swing low while
    RSI here is HIGHER than RSI was at that swing. Bearish is the mirror.

    Two things make this honest rather than a curve-fit:

    **The comparison swing must be CONFIRMED.** A pivot is only
    identifiable `strength` bars after it printed, so comparing against the
    lowest low in a lookback - the easy version - compares against a pivot
    the market had not yet revealed and manufactures divergences that were
    not visible in real time. `last_confirmed_swing` is asked for one that
    was known at THIS bar's timestamp.

    **RSI at the swing is recomputed from the bars up to that swing**, not
    read off the current value. The current RSI is a fact about now; what
    the comparison needs is what the oscillator read then.

    `min_rsi_gap` exists because a divergence of 0.2 RSI points is noise
    wearing the name of a signal.
    """
    i = ctx.index
    if ctx.atr is None or ctx.atr <= 0 or i < period + 2:
        return None

    now_ts = ctx.bar.ts
    for kind, direction in (
        (SwingKind.LOW, Direction.LONG),
        (SwingKind.HIGH, Direction.SHORT),
    ):
        swing = last_confirmed_swing(ctx.swings, kind, as_of=now_ts)
        if swing is None:
            continue
        k = _bar_index_at(ctx, swing.ts)
        if k is None or k >= i or i - k > max_bars_since_swing:
            continue

        if direction is Direction.LONG:
            made_new_extreme = _low(ctx.bar) < swing.price
        else:
            made_new_extreme = _high(ctx.bar) > swing.price
        if not made_new_extreme:
            continue

        here = ctx.rsi if ctx.rsi is not None else _rsi_at(ctx, i, period)
        there = _rsi_at(ctx, k, period)
        if here is None or there is None:
            continue
        gap = here - there if direction is Direction.LONG else there - here
        if gap < min_rsi_gap:
            continue

        extreme = _low(ctx.bar) if direction is Direction.LONG else _high(ctx.bar)
        stop = _stop_from_extreme(extreme, direction, ctx.atr, atr_stop_buffer)

        score = 2
        evidence = {
            "swing_price": f"{swing.price}",
            "rsi_then": f"{there:.1f}",
            "rsi_now": f"{here:.1f}",
            "rsi_gap": f"{gap:.1f}",
            "bars_since_swing": str(i - k),
        }
        if direction is Direction.LONG and here < Decimal(40):
            score += 1
            evidence["oscillator_zone"] = "oversold"
        if direction is Direction.SHORT and here > Decimal(60):
            score += 1
            evidence["oscillator_zone"] = "overbought"
        if ctx.higher_tf_bias is direction:
            score += 1
            evidence["higher_tf_bias"] = direction.value
        if gap >= min_rsi_gap * 3:
            score += 1
            evidence["divergence_size"] = "wide"

        return SetupSignal(
            setup_name="rsi_divergence",
            direction=direction,
            entry_index=i,
            entry_price=ctx.bar.close,
            stop_price=stop,
            score=score,
            evidence=evidence,
        )
    return None


# ---------------------------------------------------------------------------
# Strategy 9 / Section 33 - Order block / fair value gap retest
# ---------------------------------------------------------------------------


def order_block_fvg_setup(
    ctx: BarContext,
    *,
    lookback: int = 20,
    min_gap_atr: Decimal = Decimal("0.25"),
    atr_stop_buffer: Decimal = Decimal("0.30"),
    max_bars_to_retest: int = 12,
) -> SetupSignal | None:
    """Entry on the retest of an unfilled three-bar imbalance.

    A bullish fair value gap is a three-bar window whose FIRST bar's high
    sits below its THIRD bar's low: price moved so quickly that the range
    between them never traded. The order block is the last opposite-colour
    candle before that move. The setup is not the gap itself - it is price
    coming BACK to the gap and holding it.

    The playbook scopes this as entry refinement on strategy 1 rather than
    a standalone edge, and that scoping is preserved here: alignment with
    the higher-timeframe bias is scored, and without it the signal still
    fires but carries the lower score. Making the bias mandatory would be
    a stronger claim than the playbook makes.

    **The gap must still be unfilled at entry.** A gap price has already
    traded back through is not an imbalance any more; entering on it is
    entering on a level the market has already resolved. The window
    between the gap and the retest is bounded for the same reason - a gap
    from forty bars ago is a fact about a different regime.
    """
    i = ctx.index
    if ctx.atr is None or ctx.atr <= 0 or i < 4:
        return None

    start = max(2, i - lookback)
    # Newest gap first: the most recent imbalance is the one price is
    # reacting to now.
    for g in range(i - 1, start - 1, -1):
        first, third = ctx.bars[g - 2], ctx.bars[g]
        if i - g > max_bars_to_retest:
            break

        bullish_gap = _high(first) < _low(third)
        bearish_gap = _low(first) > _high(third)
        if not (bullish_gap or bearish_gap):
            continue

        if bullish_gap:
            direction = Direction.LONG
            gap_low, gap_high = _high(first), _low(third)
        else:
            direction = Direction.SHORT
            gap_low, gap_high = _high(third), _low(first)

        size = gap_high - gap_low
        if size < min_gap_atr * ctx.atr:
            continue

        # Unfilled: no bar between the gap and the bar before this one
        # closed through the far side of it.
        between = ctx.bars[g + 1 : i]
        if direction is Direction.LONG and any(_low(b) < gap_low for b in between):
            continue
        if direction is Direction.SHORT and any(_high(b) > gap_high for b in between):
            continue

        # The retest: this bar traded into the gap and closed back out of
        # it on the correct side.
        if direction is Direction.LONG:
            touched = _low(ctx.bar) <= gap_high
            held = ctx.bar.close >= gap_low
        else:
            touched = _high(ctx.bar) >= gap_low
            held = ctx.bar.close <= gap_high
        if not (touched and held):
            continue

        extreme = _low(ctx.bar) if direction is Direction.LONG else _high(ctx.bar)
        origin = gap_low if direction is Direction.LONG else gap_high
        extreme = min(extreme, origin) if direction is Direction.LONG else max(extreme, origin)
        stop = _stop_from_extreme(extreme, direction, ctx.atr, atr_stop_buffer)

        score = 2
        evidence = {
            "gap_low": f"{gap_low}",
            "gap_high": f"{gap_high}",
            "gap_atr": f"{size / ctx.atr:.2f}",
            "bars_since_gap": str(i - g),
        }
        if ctx.higher_tf_bias is direction:
            score += 1
            evidence["higher_tf_bias"] = direction.value
        if ctx.vwap is not None:
            above = ctx.bar.close > ctx.vwap.vwap
            if (direction is Direction.LONG) == above:
                score += 1
                evidence["vwap_side"] = "with trend"
        if size >= min_gap_atr * 3 * ctx.atr:
            score += 1
            evidence["imbalance"] = "large"

        return SetupSignal(
            setup_name="order_block_fvg",
            direction=direction,
            entry_index=i,
            entry_price=ctx.bar.close,
            stop_price=stop,
            score=score,
            evidence=evidence,
        )
    return None


# ---------------------------------------------------------------------------
# Strategy 10 / Section 34 - Fibonacci confluence
# ---------------------------------------------------------------------------

_FIB_LEVELS = (Decimal("0.618"), Decimal("0.705"), Decimal("0.786"))
"""The playbook's discount/premium zone. 0.705 is the midpoint of the
other two and is included because the zone is what the setup trades, not
any one ratio - a detector that fired only at 0.618 to six decimals would
almost never fire at all."""


def fib_confluence_setup(
    ctx: BarContext,
    *,
    atr_stop_buffer: Decimal = Decimal("0.30"),
    zone_tolerance_atr: Decimal = Decimal("0.20"),
    confluence_tolerance_atr: Decimal = Decimal("0.35"),
    require_confluence: bool = True,
    max_bars_since_leg: int = 40,
) -> SetupSignal | None:
    """A retracement into the 0.618-0.786 zone of the last impulse leg,
    with a second, independent level agreeing.

    The leg is measured between the two most recent CONFIRMED swings, one
    of each kind. Using the running high and low of a lookback instead
    would measure a leg whose endpoints the market had not yet revealed -
    the same look-ahead trap `docs/RESEARCH_5M.md` records being caught in
    the structure study.

    **Confluence is required by default and it must be INDEPENDENT.** A
    Fibonacci level agreeing with a Fibonacci level is one observation
    counted twice. What counts here is a session level or VWAP - computed
    from the session clock and from volume respectively, neither of which
    knows anything about the retracement.
    """
    i = ctx.index
    if ctx.atr is None or ctx.atr <= 0:
        return None

    now_ts = ctx.bar.ts
    high = last_confirmed_swing(ctx.swings, SwingKind.HIGH, as_of=now_ts)
    low = last_confirmed_swing(ctx.swings, SwingKind.LOW, as_of=now_ts)
    if high is None or low is None:
        return None
    hi_i, lo_i = _bar_index_at(ctx, high.ts), _bar_index_at(ctx, low.ts)
    if hi_i is None or lo_i is None:
        return None
    if i - min(hi_i, lo_i) > max_bars_since_leg:
        return None
    span = high.price - low.price
    if span <= 0:
        return None

    # The leg runs from the older swing to the newer one; a retracement is
    # a move back toward the older one.
    if lo_i < hi_i:
        direction = Direction.LONG
        zone = [high.price - span * f for f in _FIB_LEVELS]
    else:
        direction = Direction.SHORT
        zone = [low.price + span * f for f in _FIB_LEVELS]

    zone_lo, zone_hi = min(zone), max(zone)
    pad = zone_tolerance_atr * ctx.atr
    price = ctx.bar.close
    if not (zone_lo - pad <= price <= zone_hi + pad):
        return None

    # The bar must have REACTED, not merely be sitting in the zone.
    if direction is Direction.LONG and ctx.bar.close <= ctx.bar.open:
        return None
    if direction is Direction.SHORT and ctx.bar.close >= ctx.bar.open:
        return None

    tol = confluence_tolerance_atr * ctx.atr
    confluences: list[str] = []
    candidates: list[tuple[str, Decimal | None]] = [
        ("previous_close", ctx.levels.previous_close),
        ("previous_high", ctx.levels.previous_high),
        ("previous_low", ctx.levels.previous_low),
        ("premarket_high", ctx.levels.premarket_high),
        ("premarket_low", ctx.levels.premarket_low),
        ("opening_range_high", ctx.levels.opening_range_high),
        ("opening_range_low", ctx.levels.opening_range_low),
        ("vwap", ctx.vwap.vwap if ctx.vwap is not None else None),
    ]
    for name, level in candidates:
        if level is not None and abs(level - price) <= tol:
            confluences.append(name)
    if require_confluence and not confluences:
        return None

    extreme = _low(ctx.bar) if direction is Direction.LONG else _high(ctx.bar)
    anchor = zone_lo if direction is Direction.LONG else zone_hi
    extreme = min(extreme, anchor) if direction is Direction.LONG else max(extreme, anchor)
    stop = _stop_from_extreme(extreme, direction, ctx.atr, atr_stop_buffer)

    score = 2
    evidence = {
        "leg_low": f"{low.price}",
        "leg_high": f"{high.price}",
        "zone": f"{zone_lo:.2f}-{zone_hi:.2f}",
        "confluence": ",".join(confluences) if confluences else "none",
    }
    if len(confluences) >= 2:
        score += 1
        evidence["confluence_count"] = str(len(confluences))
    if ctx.higher_tf_bias is direction:
        score += 1
        evidence["higher_tf_bias"] = direction.value
    if ctx.vwap is not None:
        above = price > ctx.vwap.vwap
        if (direction is Direction.LONG) == above:
            score += 1
            evidence["vwap_side"] = "with trend"

    return SetupSignal(
        setup_name="fib_confluence",
        direction=direction,
        entry_index=i,
        entry_price=ctx.bar.close,
        stop_price=stop,
        score=score,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Strategy 6 / Section 30 - Bollinger band confluence
# ---------------------------------------------------------------------------


def _bollinger(closes: Sequence[Decimal], period: int, mult: Decimal):
    """Middle, upper and lower, population sigma - the same convention the
    chart's own Bollinger uses (`apps/web/lib/indicators.ts`), so a band a
    trader is looking at and a band a setup fired on are the same band."""
    if len(closes) < period:
        return None
    window = list(closes[-period:])
    mean = sum(window, Decimal(0)) / period
    var = sum(((c - mean) ** 2 for c in window), Decimal(0)) / period
    sigma = var.sqrt()
    return mean, mean + mult * sigma, mean - mult * sigma


def bollinger_confluence_setup(
    ctx: BarContext,
    *,
    period: int = 20,
    mult: Decimal = Decimal("2"),
    atr_stop_buffer: Decimal = Decimal("0.30"),
    confluence_tolerance_atr: Decimal = Decimal("0.35"),
    require_confluence: bool = True,
) -> SetupSignal | None:
    """A close back INSIDE the band after a close outside it.

    Not "price touched the band". A band tag is the single most common
    thing an instrument does in a trend - a 3x ETF can ride the upper band
    for twenty bars - and trading every tag is trading against every trend.
    What is traded here is the rejection: the previous bar closed outside,
    this one closed back in.

    Confluence with a session level or VWAP is required by default for the
    same reason it is in `fib_confluence`: on its own, mean reversion at a
    band is the weakest claim in the playbook, and the band and the level
    are computed from unrelated inputs, so their agreement is evidence
    rather than restatement.
    """
    i = ctx.index
    if ctx.atr is None or ctx.atr <= 0 or i < period:
        return None

    closes = [b.close for b in ctx.bars[: i + 1]]
    now = _bollinger(closes, period, mult)
    prev = _bollinger(closes[:-1], period, mult)
    if now is None or prev is None:
        return None
    _, upper_now, lower_now = now
    _, upper_prev, lower_prev = prev

    previous = ctx.bars[i - 1]
    price = ctx.bar.close
    if previous.close < lower_prev and price > lower_now:
        direction = Direction.LONG
        band = lower_now
    elif previous.close > upper_prev and price < upper_now:
        direction = Direction.SHORT
        band = upper_now
    else:
        return None

    tol = confluence_tolerance_atr * ctx.atr
    confluences: list[str] = []
    for name, level in (
        ("previous_close", ctx.levels.previous_close),
        ("previous_high", ctx.levels.previous_high),
        ("previous_low", ctx.levels.previous_low),
        ("premarket_high", ctx.levels.premarket_high),
        ("premarket_low", ctx.levels.premarket_low),
        ("vwap", ctx.vwap.vwap if ctx.vwap is not None else None),
    ):
        if level is not None and abs(level - price) <= tol:
            confluences.append(name)
    if require_confluence and not confluences:
        return None

    extreme = _low(ctx.bars[i - 1]) if direction is Direction.LONG else _high(ctx.bars[i - 1])
    extreme = (
        min(extreme, _low(ctx.bar)) if direction is Direction.LONG else max(extreme, _high(ctx.bar))
    )
    stop = _stop_from_extreme(extreme, direction, ctx.atr, atr_stop_buffer)

    score = 2
    evidence = {
        "band": f"{band:.2f}",
        "prev_close": f"{previous.close}",
        "close": f"{price}",
        "confluence": ",".join(confluences) if confluences else "none",
    }
    if len(confluences) >= 2:
        score += 1
        evidence["confluence_count"] = str(len(confluences))
    if ctx.rsi is not None:
        if direction is Direction.LONG and ctx.rsi < Decimal(35):
            score += 1
            evidence["oscillator_zone"] = "oversold"
        if direction is Direction.SHORT and ctx.rsi > Decimal(65):
            score += 1
            evidence["oscillator_zone"] = "overbought"
    if ctx.higher_tf_bias is direction:
        score += 1
        evidence["higher_tf_bias"] = direction.value

    return SetupSignal(
        setup_name="bollinger_confluence",
        direction=direction,
        entry_index=i,
        entry_price=ctx.bar.close,
        stop_price=stop,
        score=score,
        evidence=evidence,
    )


SETUPS: dict[str, SetupDetector] = {
    "sweep_mss": sweep_mss_setup,
    "vwap_reversion": vwap_reversion_setup,
    "orb_failure": orb_failure_setup,
    "ema_reversal": ema_reversal_setup,
    "candle_reversal": candle_reversal_setup,
    "quiet_pullback": quiet_pullback_setup,
    "volume_climax_reversal": volume_climax_reversal_setup,
    "gap_fade": gap_fade_setup,
    # Phase 88 (D107) - the four the playbook scoped and nothing had built.
    "rsi_divergence": rsi_divergence_setup,
    "order_block_fvg": order_block_fvg_setup,
    "fib_confluence": fib_confluence_setup,
    "bollinger_confluence": bollinger_confluence_setup,
}
"""Registry, so a run can name which setups to enable and a caller can
add one without touching the engine."""
