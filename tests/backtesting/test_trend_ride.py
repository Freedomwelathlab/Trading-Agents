"""Trend ride tests: the `trend_pullback` setup and the structure-trail
exit (Phase 101, D120).

What is pinned here is causality and the opt-in boundary, not a result:

* the setup fires on the bar a higher low becomes KNOWN, never on the bar
  it printed and never again afterwards;
* the trailed stop moves only on a swing's confirmation bar, and only for
  the bars after it;
* a default `BracketPlan()` behaves exactly as it did before Phase 101.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from apps.api.app.backtesting.brackets import BracketPlan, ExitReason, simulate_bracket
from apps.api.app.backtesting.costs import CostModel
from apps.api.app.backtesting.intraday_engine import IntradayRunConfig, run_intraday_backtest
from apps.api.app.backtesting.setups import SETUPS, BarContext, trend_pullback_setup
from apps.api.app.marketdata.sessions import SessionLevels, session_date
from apps.api.app.marketdata.structure import Direction, SwingKind, SwingPoint, find_swings

NY = ZoneInfo("America/New_York")
T0 = datetime(2026, 6, 17, 13, 30, tzinfo=UTC)  # 09:30 New York
FREE = CostModel.frictionless()


class Bar:
    def __init__(self, ts, *, high, low, close, open=None, volume=1_000):
        self.ts = ts
        self.open = Decimal(str(open)) if open is not None else None
        self.high = Decimal(str(high))
        self.low = Decimal(str(low))
        self.close = Decimal(str(close))
        self.volume = volume


def at(i: int) -> datetime:
    return T0 + timedelta(minutes=5 * i)


def flat_bars(n: int, price: float = 100.0) -> list[Bar]:
    return [Bar(at(i), high=price + 0.5, low=price - 0.5, close=price) for i in range(n)]


def swing(kind: SwingKind, i: int, price: float, *, confirm: int | None = None) -> SwingPoint:
    return SwingPoint(
        kind=kind,
        ts=at(i),
        price=Decimal(str(price)),
        confirmed_ts=at(i + 2 if confirm is None else confirm),
    )


def ctx(bars, swings, index, *, atr="1") -> BarContext:
    return BarContext(
        bars=bars[: index + 1],
        index=index,
        levels=SessionLevels(session_date=T0.date()),
        swings=swings,
        vwap=None,
        atr=Decimal(atr),
        rsi=None,
        ema_fast=None,
        ema_slow=None,
    )


H, L = SwingKind.HIGH, SwingKind.LOW

UPTREND = [
    swing(L, 1, 95),
    swing(H, 3, 100),
    swing(L, 5, 97),
    swing(H, 7, 105),
    swing(L, 9, 101),  # the higher low; confirmed at bar 11
]
"""Higher highs (100 -> 105) and higher lows (97 -> 101); the newest low
retraced (105 - 101) / (105 - 97) = 0.5 of the impulse before it."""

DOWNTREND = [
    swing(H, 1, 105),
    swing(L, 3, 100),
    swing(H, 5, 103),
    swing(L, 7, 95),
    swing(H, 9, 99),  # the lower high; confirmed at bar 11
]


# --------------------------------------------------------------------------
# The setup


def test_fires_long_on_the_bar_the_higher_low_is_confirmed():
    bars = flat_bars(14, 102)
    signal = trend_pullback_setup(ctx(bars, UPTREND, 11))
    assert signal is not None
    assert signal.setup_name == "trend_pullback"
    assert signal.direction is Direction.LONG
    assert signal.entry_price == bars[11].close
    # Stop: the confirmed low less the 0.30 ATR default buffer.
    assert signal.stop_price == Decimal("101") - Decimal("0.30")
    assert signal.evidence["pullback_depth"] == "0.500"


def test_is_silent_before_the_pivot_is_knowable():
    """At bar 10 the low at bar 9 has printed but is not yet a swing: it
    needs two bars after it. Acting on it here would be look-ahead."""
    bars = flat_bars(14, 102)
    assert trend_pullback_setup(ctx(bars, UPTREND, 10)) is None


def test_a_swing_confirmed_in_the_future_is_invisible():
    """Same pivot, but its confirmation arrives a bar later than the one
    being decided on. The detector must gate on `confirmed_ts`, not `ts`."""
    late = [*UPTREND[:-1], swing(L, 9, 101, confirm=12)]
    bars = flat_bars(14, 102)
    assert trend_pullback_setup(ctx(bars, late, 11)) is None
    assert trend_pullback_setup(ctx(bars, late, 12)) is not None


def test_fires_once_per_pullback_not_on_every_later_bar():
    bars = flat_bars(14, 102)
    assert trend_pullback_setup(ctx(bars, UPTREND, 12)) is None
    assert trend_pullback_setup(ctx(bars, UPTREND, 13)) is None


def test_mirrored_short_on_a_confirmed_lower_high():
    bars = flat_bars(14, 97)
    signal = trend_pullback_setup(ctx(bars, DOWNTREND, 11))
    assert signal is not None
    assert signal.direction is Direction.SHORT
    assert signal.stop_price == Decimal("99") + Decimal("0.30")


def test_direction_can_be_restricted():
    bars = flat_bars(14, 97)
    assert trend_pullback_setup(ctx(bars, DOWNTREND, 11), directions=(Direction.LONG,)) is None


def test_no_trend_no_signal():
    """A higher low without a higher high is not an uptrend."""
    flat_highs = [
        swing(L, 1, 95),
        swing(H, 3, 105),
        swing(L, 5, 97),
        swing(H, 7, 104),
        swing(L, 9, 101),
    ]
    assert trend_pullback_setup(ctx(flat_bars(14, 102), flat_highs, 11)) is None


def test_pullback_depth_band_is_enforced():
    bars = flat_bars(14, 102)
    assert trend_pullback_setup(ctx(bars, UPTREND, 11), max_depth=Decimal("0.4")) is None
    assert trend_pullback_setup(ctx(bars, UPTREND, 11), min_depth=Decimal("0.6")) is None


def test_more_trend_legs_need_more_history():
    bars = flat_bars(14, 102)
    assert trend_pullback_setup(ctx(bars, UPTREND, 11), min_trend_legs=2) is None


def test_a_support_filter_refuses_a_level_it_cannot_measure():
    """Too few bars for EMA21: the level is unknown, so the pullback cannot
    be shown to have held at it."""
    bars = flat_bars(14, 102)
    assert trend_pullback_setup(ctx(bars, UPTREND, 11), support="ema21") is None


def test_the_setup_is_registered():
    assert SETUPS["trend_pullback"] is trend_pullback_setup


def zigzag(start: float, cycles: int, *, up: int = 4, down: int = 2, step: float = 1.0):
    closes, p = [], start
    for _ in range(cycles):
        for _ in range(up):
            p += step
            closes.append(p)
        for _ in range(down):
            p -= step
            closes.append(p)
    return closes


def session_bars(day, closes, *, warmup: int = 16, price: float = 100.0):
    base = datetime(*day, 9, 30, tzinfo=NY).astimezone(UTC)
    specs = [(price, price, price)] * warmup + [(c + 0.25, c - 0.25, c) for c in closes]
    return [
        Bar(base + timedelta(minutes=5 * i), high=h, low=lo, close=c, open=c)
        for i, (h, lo, c) in enumerate(specs)
    ]


def test_with_real_swings_it_fires_only_on_confirmation_bars():
    """End to end with `find_swings`: every signal is on a bar where a swing
    low was confirmed, and never on the pivot bar itself."""
    bars = session_bars((2026, 6, 17), zigzag(100, 8))
    swings = find_swings(bars, strength=2)
    confirmations = {s.confirmed_ts for s in swings if s.kind is SwingKind.LOW}
    pivots = {s.ts for s in swings if s.kind is SwingKind.LOW}
    fired = []
    for i in range(len(bars)):
        c = BarContext(
            bars=bars[: i + 1], index=i, levels=SessionLevels(session_date=T0.date()),
            swings=swings, vwap=None, atr=Decimal("1"), rsi=None, ema_fast=None, ema_slow=None,
        )
        if (sig := trend_pullback_setup(c)) is not None:
            fired.append(bars[i].ts)
            assert sig.direction is Direction.LONG
    assert fired
    assert set(fired) <= confirmations
    assert not set(fired) & pivots


# --------------------------------------------------------------------------
# The structure-trail exit


def ride(bars, swings, *, direction=Direction.LONG, entry=100, stop=98, plan=None, atr="1"):
    return simulate_bracket(
        bars, symbol="TQQQ.US", direction=direction, entry_index=0,
        entry_price=Decimal(str(entry)), stop_price=Decimal(str(stop)),
        quantity=Decimal(100), atr=Decimal(atr),
        plan=plan or BracketPlan(structure_trail=True), costs=FREE,
        setup_name="test", swings=swings,
    )


def trail_bars():
    return [
        Bar(at(0), high=100.2, low=99.8, close=100),
        Bar(at(1), high=103, low=100.5, close=102),
        Bar(at(2), high=102.5, low=101, close=102),  # the pivot low: 101
        Bar(at(3), high=102.6, low=100.8, close=102),  # below 101 BEFORE confirmation
        Bar(at(4), high=103, low=100.9, close=102.8),  # confirmation bar, also below 101
        Bar(at(5), high=103, low=100.5, close=101),  # below 101 AFTER confirmation
        Bar(at(6), high=101, low=99, close=100),
    ]


def test_the_stop_ratchets_only_after_the_confirmation_bar():
    swings = [swing(L, 2, 101, confirm=4)]
    trade = ride(trail_bars(), swings)
    assert trade is not None
    assert len(trade.fills) == 1
    fill = trade.fills[0]
    # Bars 3 and 4 traded below 101 and did NOT exit: the swing was not yet
    # known (bar 3) or became known only at that bar's close (bar 4).
    assert fill.ts == at(5)
    assert fill.reason is ExitReason.STRUCTURE_TRAIL
    assert fill.price == Decimal("101")
    assert fill.r_multiple == Decimal("0.5")


def test_the_initial_stop_is_a_plain_stop():
    bars = [
        Bar(at(0), high=100.2, low=99.8, close=100),
        Bar(at(1), high=100.5, low=97.5, close=98),
    ]
    trade = ride(bars, [])
    assert trade.fills[0].reason is ExitReason.STOP
    assert trade.fills[0].price == Decimal("98")


def test_a_gap_through_the_stop_fills_at_the_open():
    bars = [
        Bar(at(0), high=100.2, low=99.8, close=100),
        Bar(at(1), high=97.2, low=96.5, close=97, open=97),
    ]
    trade = ride(bars, [])
    assert trade.fills[0].price == Decimal("97")


def test_a_confirmed_lower_low_is_a_structure_break():
    """With a 1-ATR buffer the trailed stop sits at 100, so a lower swing
    low at 100.5 does not touch it - but once CONFIRMED it is a lower low,
    and the ride ends at that bar's close."""
    bars = [
        Bar(at(0), high=100.2, low=99.8, close=100),
        Bar(at(1), high=103, low=101.5, close=102.5),
        Bar(at(2), high=102.5, low=101, close=102),  # low 101
        Bar(at(3), high=103, low=101.6, close=102.5),
        Bar(at(4), high=103.5, low=102, close=103),  # confirms 101 -> stop 100
        Bar(at(5), high=103, low=101, close=101.5),
        Bar(at(6), high=101.5, low=100.5, close=101),  # low 100.5 (lower low)
        Bar(at(7), high=101.6, low=100.7, close=101.2),
        Bar(at(8), high=101.8, low=100.8, close=101.4),  # confirms 100.5
        Bar(at(9), high=105, low=101, close=104),
    ]
    swings = [swing(L, 2, 101, confirm=4), swing(L, 6, 100.5, confirm=8)]
    plan = BracketPlan(structure_trail=True, structure_trail_buffer_atr=Decimal("1"))
    trade = ride(bars, swings, plan=plan)
    fill = trade.fills[0]
    assert fill.reason is ExitReason.STRUCTURE_BREAK
    assert fill.ts == at(8)
    assert fill.price == Decimal("101.4")


def test_the_stop_is_never_loosened():
    """A confirmed higher low BELOW the current stop leaves the stop alone."""
    bars = trail_bars()[:2] + [
        Bar(at(2), high=102, low=99, close=101.5),
        Bar(at(3), high=102, low=100.8, close=101.8),
        Bar(at(4), high=102, low=99.2, close=101.8),
        Bar(at(5), high=102, low=97.9, close=98),
    ]
    swings = [swing(L, 2, 97.5, confirm=4)]  # below the 98 stop
    trade = ride(bars, swings)
    assert trade.fills[0].reason is ExitReason.STOP
    assert trade.fills[0].price == Decimal("98")


def test_short_ratchets_on_confirmed_lower_highs():
    bars = [
        Bar(at(0), high=100.2, low=99.8, close=100),
        Bar(at(1), high=99.5, low=97, close=98),
        Bar(at(2), high=99, low=97.5, close=98),  # pivot high 99
        Bar(at(3), high=99.2, low=97.4, close=98),  # above 99 before confirmation
        Bar(at(4), high=98.5, low=97, close=97.2),  # confirmation
        Bar(at(5), high=99.5, low=97, close=99.2),  # through 99 after it
    ]
    swings = [swing(H, 2, 99, confirm=4)]
    trade = ride(bars, swings, direction=Direction.SHORT, stop=102)
    fill = trade.fills[0]
    assert fill.reason is ExitReason.STRUCTURE_TRAIL
    assert fill.ts == at(5)
    assert fill.price == Decimal("99")


def test_break_on_close_ignores_a_wick_through_the_trailed_level():
    swings = [swing(L, 2, 101, confirm=4)]
    bars = trail_bars()[:5] + [
        Bar(at(5), high=103, low=100.5, close=102),  # wick through 101, closes above
        Bar(at(6), high=102, low=100, close=100.5),  # closes through 101
    ]
    plan = BracketPlan(structure_trail=True, structure_break_on_close=True)
    trade = ride(bars, swings, plan=plan)
    fill = trade.fills[0]
    assert fill.ts == at(6)
    assert fill.reason is ExitReason.STRUCTURE_TRAIL
    assert fill.price == Decimal("100.5")


def test_optional_atr_trail_takes_profit_when_the_run_fades():
    bars = [Bar(at(0), high=100.2, low=99.8, close=100)] + [
        Bar(at(i), high=100 + i + 0.2, low=100 + i - 0.2, close=100 + i) for i in range(1, 6)
    ] + [Bar(at(6), high=105, low=103.5, close=103.8)]
    plan = BracketPlan(structure_trail=True, structure_atr_trail_multiple=Decimal("1"))
    trade = ride(bars, [], plan=plan)
    fill = trade.fills[0]
    assert fill.reason is ExitReason.TRAIL
    assert fill.price == Decimal("104")  # best close 105 less 1 ATR


def test_structure_ride_is_flat_by_the_bell_unless_held_overnight():
    day1 = session_bars((2026, 6, 17), zigzag(100, 10), warmup=0)
    day2 = session_bars((2026, 6, 18), zigzag(120, 4), warmup=0, price=120)
    bars = day1 + day2
    swings = find_swings(bars, strength=2)
    entry_price = float(day1[20].close)

    def go(overnight: bool):
        return simulate_bracket(
            bars, symbol="TQQQ.US", direction=Direction.LONG, entry_index=20,
            entry_price=Decimal(str(entry_price)), stop_price=Decimal(str(entry_price - 3)),
            quantity=Decimal(10), atr=Decimal("1"),
            plan=BracketPlan(structure_trail=True, hold_overnight=overnight),
            costs=FREE, setup_name="test", swings=swings,
        )

    intraday = go(False)
    assert intraday.fills[-1].reason is ExitReason.TIME_STOP
    assert session_date(intraday.exit_ts) == session_date(day1[0].ts)
    held = go(True)
    assert session_date(held.exit_ts) == session_date(day2[0].ts)


# --------------------------------------------------------------------------
# Default behaviour is unchanged


def test_default_plan_has_the_structure_exit_switched_off():
    plan = BracketPlan()
    assert plan.structure_trail is False
    assert plan.hold_overnight is False
    assert plan.structure_atr_trail_multiple is None


def test_default_plan_ignores_swings_and_still_scales_out():
    bars = [
        Bar(at(0), high=100.2, low=99.8, close=100),
        Bar(at(1), high=101.2, low=100.1, close=101),  # TP1 at 101 (1R on a $1 stop)
        Bar(at(2), high=102.2, low=101, close=102),  # TP2 at 102
        Bar(at(3), high=102.5, low=101.8, close=102.2),
    ]
    swings = [swing(L, 1, 100.1, confirm=3)]
    common = dict(
        symbol="TQQQ.US", direction=Direction.LONG, entry_index=0,
        entry_price=Decimal("100"), stop_price=Decimal("99"), quantity=Decimal(99),
        atr=Decimal("1"), plan=BracketPlan(), costs=FREE, setup_name="test",
    )
    without = simulate_bracket(bars, **common)
    with_swings = simulate_bracket(bars, swings=swings, **common)
    assert [(f.ts, f.price, f.reason) for f in without.fills] == [
        (f.ts, f.price, f.reason) for f in with_swings.fills
    ]
    assert without.hit_tp1 and without.hit_tp2


# --------------------------------------------------------------------------
# Engine wiring


def _engine_bars():
    day1 = session_bars((2026, 6, 17), zigzag(100, 10))
    last = float(day1[-1].close)
    day2 = session_bars((2026, 6, 18), zigzag(last, 6), warmup=0, price=last)
    return day1, day2


def _run(bars, *, overnight: bool):
    return run_intraday_backtest(
        bars,
        IntradayRunConfig(
            symbol="TEST",
            setups=("trend_pullback",),
            plan=BracketPlan(structure_trail=True, hold_overnight=overnight),
            costs=FREE,
            allow_directions=(Direction.LONG,),
        ),
    )


def test_engine_rides_a_trend_pullback_to_the_bell():
    day1, day2 = _engine_bars()
    result = _run(day1 + day2, overnight=False)
    assert result.trades
    first = result.trades[0]
    assert first.setup_name == "trend_pullback"
    assert first.direction is Direction.LONG
    assert session_date(first.exit_ts) == session_date(first.entry_ts)
    assert first.fills[-1].reason is ExitReason.TIME_STOP


def test_engine_can_hold_a_ride_overnight_and_blocks_new_entries_meanwhile():
    day1, day2 = _engine_bars()
    result = _run(day1 + day2, overnight=True)
    assert result.trades
    first = result.trades[0]
    assert session_date(first.exit_ts) == session_date(day2[0].ts)
    # One position at a time, across the overnight gap too.
    for earlier, later in zip(result.trades, result.trades[1:], strict=False):
        assert later.entry_ts > earlier.exit_ts
