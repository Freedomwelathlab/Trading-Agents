"""Per-position exit rules for the bot (Phase 81, D098).

Pure functions over a small state object so every rule is testable
without a database or a broker. The bot calls `initial_bracket` once at
entry and `manage` once per cycle per open position, then acts on the
returned decision through the OMS.

All percentages are PERCENT figures as the operator typed them (1.5 means
1.5%), converted here and nowhere else.

**Both directions since Phase 87 (D106).** A short is not a long with the
signs flipped in the caller: the stop sits ABOVE entry, the target BELOW,
the trail ratchets DOWN from the lowest low rather than up from the
highest high, and "did this bar touch the stop" reads the bar's HIGH
instead of its low. Each of those is a place where reusing the long branch
yields a number that looks like a stop and can never trigger. The
direction is therefore carried in `BracketState` and each comparison is
written out per direction rather than folded into a sign multiplier — a
multiplier is compact and unreadable at exactly the moment somebody is
checking whether a live stop is on the correct side of the price.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from apps.api.app.db.models import AutotradeExitReason
from apps.api.app.marketdata.structure import Direction

_HUNDRED = Decimal(100)


@dataclass(frozen=True)
class BracketState:
    entry_price: Decimal
    initial_stop_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal | None
    peak_price: Decimal
    """The most favourable price seen since entry: the highest high for a
    long, the LOWEST low for a short. Named for the long case because that
    is what the column has always held, and renaming a live column is not
    worth a migration for a comment's sake."""
    take_profit_armed: bool
    direction: Direction = Direction.LONG


@dataclass(frozen=True)
class ExitRules:
    stop_loss_mode: str  # auto | max
    stop_loss_max_pct: Decimal | None
    trailing_stop_pct: Decimal | None
    take_profit_mode: str  # auto | min
    take_profit_min_pct: Decimal | None
    trailing_take_profit_pct: Decimal | None


@dataclass(frozen=True)
class BracketDecision:
    state: BracketState
    exit_reason: AutotradeExitReason | None

    @property
    def should_exit(self) -> bool:
        return self.exit_reason is not None


def _pct(p: Decimal | None) -> Decimal | None:
    return None if p is None else p / _HUNDRED


def initial_bracket(
    *,
    entry_price: Decimal,
    structural_stop: Decimal,
    rules: ExitRules,
    direction: Direction = Direction.LONG,
) -> BracketState:
    """The stop and target a new position is opened with.

    Stop: the setup's own structural stop (`auto`), or under `max` the
    TIGHTER of that and the operator's maximum-loss distance — an
    operator's maximum loss is a ceiling, never a licence to widen a
    structural stop. "Tighter" means nearer entry in both directions:
    `max()` for a long, `min()` for a short.

    Target: 2R (`auto`, the playbook's TP2), or under `min` the FARTHER of
    2R and the operator's minimum-gain distance — a minimum take-profit is
    a floor. "Farther" flips the same way.

    A stop on the WRONG SIDE of entry is not a stop. With no operator cap
    to fall back on this returns a zero-width bracket, which the caller
    must reject rather than open a position whose exit can never trigger.
    """
    cap = _pct(rules.stop_loss_max_pct)
    floor = _pct(rules.take_profit_min_pct)
    stop = structural_stop

    if direction is Direction.LONG:
        if rules.stop_loss_mode == "max" and cap is not None:
            stop = max(stop, entry_price * (Decimal(1) - cap))
        if stop >= entry_price:
            stop = entry_price * (Decimal(1) - cap) if cap else entry_price
        risk = entry_price - stop
        target = entry_price + risk * 2
        if rules.take_profit_mode == "min" and floor is not None:
            target = max(target, entry_price * (Decimal(1) + floor))
    else:
        if rules.stop_loss_mode == "max" and cap is not None:
            stop = min(stop, entry_price * (Decimal(1) + cap))
        if stop <= entry_price:
            stop = entry_price * (Decimal(1) + cap) if cap else entry_price
        risk = stop - entry_price
        target = entry_price - risk * 2
        if rules.take_profit_mode == "min" and floor is not None:
            target = min(target, entry_price * (Decimal(1) - floor))

    return BracketState(
        entry_price=entry_price,
        initial_stop_price=stop,
        stop_price=stop,
        take_profit_price=target,
        peak_price=entry_price,
        take_profit_armed=False,
        direction=direction,
    )


def manage(
    state: BracketState,
    *,
    bar_high: Decimal,
    bar_low: Decimal,
    bar_close: Decimal,
    rules: ExitRules,
    session_ending: bool,
) -> BracketDecision:
    """One cycle of management on a completed bar.

    Order of checks is deliberate and matches `simulate_bracket`: the stop
    is tested first (a bar that touches both stop and target is treated as
    a stop — the conservative reading), then the target, then the trails,
    then the session-end flat.

    For a SHORT each of those reads the other side of the bar: the stop
    triggers on the high, the target on the low, the favourable extreme is
    the lowest low, and the stop ratchets DOWN.
    """
    is_long = state.direction is Direction.LONG
    stop_touched = bar_low <= state.stop_price if is_long else bar_high >= state.stop_price
    trailed = (
        state.stop_price > state.initial_stop_price
        if is_long
        else state.stop_price < state.initial_stop_price
    )

    # 1. Hard stop.
    if stop_touched:
        reason = AutotradeExitReason.TRAILING_STOP if trailed else AutotradeExitReason.STOP_LOSS
        return BracketDecision(state, reason)

    peak = max(state.peak_price, bar_high) if is_long else min(state.peak_price, bar_low)
    armed = state.take_profit_armed
    trail_tp = _pct(rules.trailing_take_profit_pct)

    # 2. Target.
    target_touched = state.take_profit_price is not None and (
        bar_high >= state.take_profit_price if is_long else bar_low <= state.take_profit_price
    )
    if target_touched:
        if trail_tp is None:
            return BracketDecision(replace(state, peak_price=peak), AutotradeExitReason.TAKE_PROFIT)
        armed = True

    # 3. Trailing take profit: armed, and price has given back the
    #    allowance from the favourable extreme.
    if armed and trail_tp is not None:
        gave_back = (
            bar_close <= peak * (Decimal(1) - trail_tp)
            if is_long
            else bar_close >= peak * (Decimal(1) + trail_tp)
        )
        if gave_back:
            return BracketDecision(
                replace(state, peak_price=peak, take_profit_armed=True),
                AutotradeExitReason.TRAILING_TAKE_PROFIT,
            )

    # 4. Trailing stop ratchet — only ever toward the position's favour.
    stop = state.stop_price
    trail = _pct(rules.trailing_stop_pct)
    if trail is not None:
        stop = (
            max(stop, peak * (Decimal(1) - trail))
            if is_long
            else min(stop, peak * (Decimal(1) + trail))
        )

    new_state = replace(state, stop_price=stop, peak_price=peak, take_profit_armed=armed)

    # 5. Flat at session end — an intraday bot holds nothing overnight.
    if session_ending:
        return BracketDecision(new_state, AutotradeExitReason.SESSION_END)

    return BracketDecision(new_state, None)
