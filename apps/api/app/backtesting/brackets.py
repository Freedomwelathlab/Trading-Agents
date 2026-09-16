"""Bracket position simulation: stop, scaled targets, trail, time stop
(Phase 73, D091).

`engine_v2` models a position as a pair of events - enter flat-to-long,
exit long-to-flat - with the exit decided by the same rule vocabulary as
the entry. The intraday playbook does not work that way. Its risk section
defines a position by four things fixed AT ENTRY (a structural stop, a
1R partial, a 2R partial, and a trailed runner), and the trade ends
whenever price reaches one of them. Nothing in a rule comparing two
indicator series can express "exit 30% here and trail the rest".

So this simulates the bracket directly. Three conventions below decide
whether the numbers it produces are honest, and each is the pessimistic
choice rather than the convenient one.

**1. On a bar that touches both the stop and a target, the STOP fills
first.** A 5-minute OHLC bar records four numbers and no path; whether
price reached 1R before or after tagging the stop is genuinely unknowable
from it. Resolving that ambiguity in the trade's favour is the single
largest source of fictitious profit in intraday backtesting, and it grows
with volatility - so on a 3x leveraged ETF it would flatter these results
most of all. Assuming the worst understates a real edge; assuming the
best invents one.

**2. Every fill pays costs.** Entry, each partial, the trail and the time
stop all price through the same `CostModel` that `engine_v2` uses. A
strategy that scales out three times pays three spreads, and a model that
charges one makes over-trading look free - the exact error that made
`rsi-30-70` look profitable on BTC while losing money.

**3. An intraday position is flat by the closing bell.** There is no
overnight hold. TQQQ targets 3x the Nasdaq-100's DAILY move and its
multi-day path diverges from 3x the index by design, so carrying an
intraday signal overnight is not the same strategy with a longer horizon -
it is a different instrument's risk profile. The time stop is therefore
part of the model, not a safety net bolted on.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from apps.api.app.backtesting.costs import CostModel
from apps.api.app.marketdata.ohlcv import OHLCVBar
from apps.api.app.marketdata.sessions import is_regular_hours, session_date
from apps.api.app.marketdata.structure import Direction


class ExitReason(str, enum.Enum):  # noqa: UP042 (str mixin matches this codebase)
    """Why a slice of a position closed.

    Recorded per fill rather than per trade because a scaled exit has
    several, and the playbook's own measurement list asks for TP1 hit rate
    and stop-out rate separately - which is unanswerable if a trade is
    labelled only by how its last share left.
    """

    STOP = "stop"
    TARGET_1R = "target_1r"
    TARGET_2R = "target_2r"
    TRAIL = "trail"
    TIME_STOP = "time_stop"


@dataclass(frozen=True)
class BracketPlan:
    """The risk parameters of one strategy, fixed before any trade.

    Expressed in R and in ATR multiples rather than in dollars or percent
    so the same plan applies unchanged to any instrument - which is what
    makes a strategy defined here testable on a different ticker without
    re-tuning. The playbook's own stop section is explicit that fixed
    dollar stops fail across changing volatility.
    """

    risk_per_trade_pct: Decimal = Decimal("0.005")
    """Fraction of equity risked between entry and stop. The playbook's
    research range is 0.25%-0.50% and it says to test the lower end first;
    0.5% is the top of that range, chosen as the default only because it
    makes position sizes large enough for the whole-share flooring below
    to be visible rather than silently rounding every trade to zero."""

    atr_stop_buffer: Decimal = Decimal("0.30")
    """Volatility buffer beyond the structural level, in ATR(14) units.
    The playbook's starting range is 0.20-0.40."""

    tp1_r_multiple: Decimal = Decimal("1")
    tp2_r_multiple: Decimal = Decimal("2")
    tp1_fraction: Decimal = Decimal("0.33")
    tp2_fraction: Decimal = Decimal("0.33")
    """Scale-out sizes. The playbook says 25-40% at each; a third and a
    third leaves a third running, which is the shape it describes."""

    breakeven_after_tp1: bool = True
    """The playbook's "reduce risk at +1R". Moving the stop to entry after
    the first partial is the mechanical reading of it."""

    trail_atr_multiple: Decimal | None = Decimal("1.5")
    """ATR trail applied to the runner after TP2, from the most favourable
    close. `None` runs the remainder to the time stop instead. The
    playbook lists three trailing options and says to pick one and
    backtest it rather than switching mid-trade."""

    min_stop_atr_multiple: Decimal | None = Decimal("0.25")
    """Section 16's stop-quality test: reject a trade whose stop is "so
    tight that normal noise repeatedly hits it".

    ATR is the natural measure of that noise, so the floor is expressed
    against it rather than as a fixed percentage. Without this the engine
    happily took setups with a stop distance of 0.00% of price, where the
    result is decided by the spread rather than by the idea - the median
    stop on the first real run was 0.78% of price, the minimum 0.00%.

    `None` disables the test, so its contribution can be measured rather
    than assumed.
    """

    max_stop_atr_multiple: Decimal | None = Decimal("4")
    """The other half of the same test: a stop "so wide that required
    reward-to-risk becomes unrealistic". A 2R target beyond four ATRs
    needs an eight-ATR move inside one session."""

    max_position_pct_of_equity: Decimal = Decimal("0.10")
    """A ceiling on notional, independent of the risk calculation.

    Risk-based sizing divides by the stop distance, so a very tight stop
    asks for a very large position - on a 3x ETF that can exceed the whole
    account. This cap is the same 10%-of-equity limit the platform's Risk
    Engine enforces on any single order, applied here so a research
    backtest cannot report trades the live path would refuse. When it
    binds, the trade is TAKEN AT THE CAP and flagged, never silently
    dropped: a skipped trade would quietly remove exactly the highest-
    conviction setups from the statistics.
    """


@dataclass
class BracketFill:
    ts: datetime
    quantity: Decimal
    price: Decimal
    reason: ExitReason
    r_multiple: Decimal


@dataclass
class BracketTrade:
    """One completed round trip, with every partial recorded."""

    direction: Direction
    symbol: str
    entry_ts: datetime
    entry_price: Decimal
    initial_stop: Decimal
    quantity: Decimal
    risk_per_share: Decimal
    setup_name: str
    fills: list[BracketFill] = field(default_factory=list)
    size_was_capped: bool = False

    @property
    def exit_ts(self) -> datetime | None:
        return self.fills[-1].ts if self.fills else None

    @property
    def gross_pnl(self) -> Decimal:
        """Signed against direction, so a short that falls is a profit."""
        total = Decimal(0)
        for fill in self.fills:
            move = (
                fill.price - self.entry_price
                if self.direction is Direction.LONG
                else self.entry_price - fill.price
            )
            total += move * fill.quantity
        return total

    @property
    def r_multiple(self) -> Decimal:
        """The trade's result in units of its own initial risk.

        R is the playbook's unit of account throughout - its expectancy
        formula, its daily stop and its targets are all in R - because it
        is the only measure that compares a wide-stop trade with a tight
        one on equal terms.
        """
        risk = self.risk_per_share * self.quantity
        if risk <= 0:
            return Decimal(0)
        return self.gross_pnl / risk

    @property
    def hit_tp1(self) -> bool:
        return any(f.reason is ExitReason.TARGET_1R for f in self.fills)

    @property
    def hit_tp2(self) -> bool:
        return any(f.reason is ExitReason.TARGET_2R for f in self.fills)

    @property
    def stopped_out(self) -> bool:
        return any(f.reason is ExitReason.STOP for f in self.fills)




def _high(bar: OHLCVBar) -> Decimal:
    return bar.high if bar.high is not None else bar.close


def _low(bar: OHLCVBar) -> Decimal:
    return bar.low if bar.low is not None else bar.close



def stop_quality_ok(
    *, entry_price: Decimal, stop_price: Decimal, atr: Decimal | None, plan: BracketPlan
) -> bool:
    """Section 16's stop-quality test, applied before a trade is sized.

    Returns True when ATR is unknown: refusing every setup during warmup
    would silently drop the first bars of every session, which is where
    several of these setups live.
    """
    if atr is None or atr <= 0:
        return True
    distance = abs(entry_price - stop_price)
    if plan.min_stop_atr_multiple is not None and distance < plan.min_stop_atr_multiple * atr:
        return False
    if plan.max_stop_atr_multiple is not None and distance > plan.max_stop_atr_multiple * atr:
        return False
    return True


def position_size(
    *,
    equity: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    plan: BracketPlan,
    whole_shares: bool = True,
) -> tuple[Decimal, bool]:
    """Shares to trade, and whether the notional cap bound.

    `risk dollars / stop distance`, which is the playbook's formula
    verbatim. Flooring to whole shares matches what an equity venue will
    accept; the returned flag reports the cap so a caller can distinguish
    "sized as intended" from "sized down", rather than inferring it by
    recomputing.
    """
    distance = abs(entry_price - stop_price)
    if distance <= 0 or equity <= 0 or entry_price <= 0:
        return (Decimal(0), False)

    risk_dollars = equity * plan.risk_per_trade_pct
    raw = risk_dollars / distance

    capped = False
    max_shares = (equity * plan.max_position_pct_of_equity) / entry_price
    if raw > max_shares:
        raw = max_shares
        capped = True

    quantity = raw.to_integral_value(rounding="ROUND_FLOOR") if whole_shares else raw
    return (quantity, capped and quantity > 0)


def simulate_bracket(
    bars: Sequence[OHLCVBar],
    *,
    symbol: str,
    direction: Direction,
    entry_index: int,
    entry_price: Decimal,
    stop_price: Decimal,
    quantity: Decimal,
    atr: Decimal | None,
    plan: BracketPlan,
    costs: CostModel,
    setup_name: str,
    size_was_capped: bool = False,
) -> BracketTrade | None:
    """Replay `bars` from `entry_index + 1` until the bracket is resolved.

    Entry is priced at `entry_price` through the cost model; the replay
    begins on the FOLLOWING bar. Allowing the entry bar itself to resolve
    the trade would let a setup detected from a bar's close be filled and
    stopped inside that same close - information the strategy did not have
    when it decided.
    """
    if quantity <= 0:
        return None

    fill_entry = (
        costs.buy_price(entry_price)
        if direction is Direction.LONG
        else costs.sell_price(entry_price)
    )

    # R is measured from the price actually PAID to the stop, not from the
    # signal price the setup reported (Phase 73, D091).
    #
    # Measuring from the pre-cost price makes a stop-out lose more than 1R
    # and the error scales inversely with the stop distance: on a $39
    # instrument with a $0.05 stop, a 3bp round trip is about half the
    # entire risk budget, so a "1R stop" really loses 1.5R. Observed
    # directly on the first real TQQQ run - 37 of 239 trades lost worse
    # than a full stop and one reported -169R - which is not a strategy
    # result at all, it is the denominator being wrong.
    risk_per_share = abs(fill_entry - stop_price)
    if risk_per_share <= 0:
        return None

    trade = BracketTrade(
        direction=direction,
        symbol=symbol,
        entry_ts=bars[entry_index].ts,
        entry_price=fill_entry,
        initial_stop=stop_price,
        quantity=quantity,
        risk_per_share=risk_per_share,
        setup_name=setup_name,
        size_was_capped=size_was_capped,
    )

    sign = Decimal(1) if direction is Direction.LONG else Decimal(-1)
    tp1 = fill_entry + sign * plan.tp1_r_multiple * risk_per_share
    tp2 = fill_entry + sign * plan.tp2_r_multiple * risk_per_share

    remaining = quantity
    stop = stop_price
    took_tp1 = took_tp2 = False
    best_close = fill_entry
    entry_session = session_date(bars[entry_index].ts)

    def close_slice(bar: OHLCVBar, qty: Decimal, raw_price: Decimal, reason: ExitReason) -> Decimal:
        price = (
            costs.sell_price(raw_price)
            if direction is Direction.LONG
            else costs.buy_price(raw_price)
        )
        move = (
            price - fill_entry if direction is Direction.LONG else fill_entry - price
        )
        trade.fills.append(
            BracketFill(
                ts=bar.ts,
                quantity=qty,
                price=price,
                reason=reason,
                r_multiple=move / risk_per_share,
            )
        )
        return qty

    last_in_session = bars[entry_index]

    for bar in bars[entry_index + 1 :]:
        # --- Time stop: flat by the close of the entry session. ---
        #
        # Closed on the LAST IN-SESSION bar, not on the first bar that
        # falls outside it. Filling at the out-of-session bar would price
        # the exit after the overnight gap - so a position "closed at the
        # bell" would in fact capture the next morning's move, which is
        # exactly the overnight exposure this stop exists to remove. On a
        # 3x ETF that gap is where the largest moves live, and the error
        # would look like edge.
        if session_date(bar.ts) != entry_session or not is_regular_hours(bar.ts):
            close_slice(
                last_in_session, remaining, last_in_session.close, ExitReason.TIME_STOP
            )
            remaining = Decimal(0)
            break
        last_in_session = bar

        hit_stop = (
            _low(bar) <= stop if direction is Direction.LONG else _high(bar) >= stop
        )
        # Convention 1: the stop is resolved BEFORE any target on the same
        # bar. See this module's docstring - the bar records no path, and
        # resolving the ambiguity favourably is where fictitious profit
        # comes from.
        if hit_stop:
            close_slice(bar, remaining, stop, ExitReason.STOP)
            remaining = Decimal(0)
            break

        if not took_tp1:
            reached = (
                _high(bar) >= tp1 if direction is Direction.LONG else _low(bar) <= tp1
            )
            if reached:
                qty = (quantity * plan.tp1_fraction).to_integral_value(
                    rounding="ROUND_FLOOR"
                )
                qty = min(qty, remaining)
                if qty > 0:
                    close_slice(bar, qty, tp1, ExitReason.TARGET_1R)
                    remaining -= qty
                took_tp1 = True
                if plan.breakeven_after_tp1:
                    stop = fill_entry
                if remaining <= 0:
                    break

        if took_tp1 and not took_tp2:
            reached = (
                _high(bar) >= tp2 if direction is Direction.LONG else _low(bar) <= tp2
            )
            if reached:
                qty = (quantity * plan.tp2_fraction).to_integral_value(
                    rounding="ROUND_FLOOR"
                )
                qty = min(qty, remaining)
                if qty > 0:
                    close_slice(bar, qty, tp2, ExitReason.TARGET_2R)
                    remaining -= qty
                took_tp2 = True
                if remaining <= 0:
                    break

        # --- Trail the runner, only after the second target. ---
        if took_tp2 and plan.trail_atr_multiple is not None and atr:
            best_close = (
                max(best_close, bar.close)
                if direction is Direction.LONG
                else min(best_close, bar.close)
            )
            trailed = best_close - sign * plan.trail_atr_multiple * atr
            stop = max(stop, trailed) if direction is Direction.LONG else min(stop, trailed)

    if remaining > 0 and bars[entry_index + 1 :]:
        close_slice(bars[-1], remaining, bars[-1].close, ExitReason.TIME_STOP)

    return trade if trade.fills else None
