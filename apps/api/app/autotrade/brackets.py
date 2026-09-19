"""Per-position exit rules for the bot (Phase 81, D098).

Pure functions over a small state object so every rule is testable
without a database or a broker. The bot calls `initial_bracket` once at
entry and `manage` once per cycle per open position, then acts on the
returned decision through the OMS.

All percentages are PERCENT figures as the operator typed them (1.5 means
1.5%), converted here and nowhere else.

Long-only, matching the scanner and the paper broker.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from apps.api.app.db.models import AutotradeExitReason

_HUNDRED = Decimal(100)


@dataclass(frozen=True)
class BracketState:
    entry_price: Decimal
    initial_stop_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal | None
    peak_price: Decimal
    take_profit_armed: bool


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
    *, entry_price: Decimal, structural_stop: Decimal, rules: ExitRules
) -> BracketState:
    """The stop and target a new long is opened with.

    Stop: the setup's own structural stop (`auto`), or under `max` the
    TIGHTER of that and `entry * (1 - max%)` — an operator's maximum loss
    is a ceiling, never a licence to widen a structural stop.

    Target: 2R (`auto`, the playbook's TP2), or under `min` the FARTHER
    of 2R and `entry * (1 + min%)` — a minimum take-profit is a floor.
    """
    stop = structural_stop
    cap = _pct(rules.stop_loss_max_pct)
    if rules.stop_loss_mode == "max" and cap is not None:
        stop = max(stop, entry_price * (Decimal(1) - cap))
    if stop >= entry_price:
        # A stop at or above entry is not a stop. Fall back to the
        # operator's cap if one exists, else refuse via a zero-width
        # bracket the caller must reject.
        stop = entry_price * (Decimal(1) - cap) if cap else entry_price

    risk = entry_price - stop
    target = entry_price + risk * 2
    floor = _pct(rules.take_profit_min_pct)
    if rules.take_profit_mode == "min" and floor is not None:
        target = max(target, entry_price * (Decimal(1) + floor))

    return BracketState(
        entry_price=entry_price,
        initial_stop_price=stop,
        stop_price=stop,
        take_profit_price=target,
        peak_price=entry_price,
        take_profit_armed=False,
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
    """
    # 1. Hard stop on the bar's low.
    if bar_low <= state.stop_price:
        reason = (
            AutotradeExitReason.TRAILING_STOP
            if state.stop_price > state.initial_stop_price
            else AutotradeExitReason.STOP_LOSS
        )
        return BracketDecision(state, reason)

    peak = max(state.peak_price, bar_high)
    armed = state.take_profit_armed
    trail_tp = _pct(rules.trailing_take_profit_pct)

    # 2. Target.
    if state.take_profit_price is not None and bar_high >= state.take_profit_price:
        if trail_tp is None:
            return BracketDecision(replace(state, peak_price=peak), AutotradeExitReason.TAKE_PROFIT)
        armed = True

    # 3. Trailing take profit: armed, and price has given back the
    #    allowance from the peak.
    if armed and trail_tp is not None and bar_close <= peak * (Decimal(1) - trail_tp):
        return BracketDecision(
            replace(state, peak_price=peak, take_profit_armed=True),
            AutotradeExitReason.TRAILING_TAKE_PROFIT,
        )

    # 4. Trailing stop ratchet — only ever upward.
    stop = state.stop_price
    trail = _pct(rules.trailing_stop_pct)
    if trail is not None:
        stop = max(stop, peak * (Decimal(1) - trail))

    new_state = replace(state, stop_price=stop, peak_price=peak, take_profit_armed=armed)

    # 5. Flat at session end — an intraday bot holds nothing overnight.
    if session_ending:
        return BracketDecision(new_state, AutotradeExitReason.SESSION_END)

    return BracketDecision(new_state, None)
