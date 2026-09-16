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


SetupDetector = Callable[[BarContext], SetupSignal | None]
"""What the engine needs from a setup: one bar's context in, an optional
signal out. Every detector below also takes keyword parameters with
defaults, which is compatible with this - and naming the type keeps the
registry from widening to `object`, where a caller of `SETUPS[name]`
loses every guarantee about what it is calling."""

SETUPS: dict[str, SetupDetector] = {
    "sweep_mss": sweep_mss_setup,
    "vwap_reversion": vwap_reversion_setup,
    "orb_failure": orb_failure_setup,
    "ema_reversal": ema_reversal_setup,
}
"""Registry, so a run can name which setups to enable and a caller can
add one without touching the engine."""
