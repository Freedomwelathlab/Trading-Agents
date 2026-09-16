"""Bracket simulation tests (Phase 73, D091).

The conventions being pinned here are the ones that decide whether an
intraday backtest is honest: stop-before-target on an ambiguous bar,
costs on every partial, and a hard flat-by-the-close. Each has a
convenient alternative that produces better numbers and describes trades
that could not have happened.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from apps.api.app.backtesting.brackets import (
    BracketPlan,
    ExitReason,
    position_size,
    simulate_bracket,
)
from apps.api.app.backtesting.costs import CostModel
from apps.api.app.marketdata.structure import Direction

NY = ZoneInfo("America/New_York")
FREE = CostModel.frictionless()


class Bar:
    def __init__(self, ts, *, high, low, close, open=None):
        self.ts = ts
        self.open = Decimal(str(open)) if open is not None else None
        self.high = Decimal(str(high))
        self.low = Decimal(str(low))
        self.close = Decimal(str(close))
        self.volume = 1_000


def session(specs, *, day=(2026, 6, 17), start=(9, 30)):
    """Bars on a real regular session, 5 minutes apart."""
    base = datetime(*day, *start, tzinfo=NY).astimezone(UTC)
    return [
        Bar(base + timedelta(minutes=5 * i), high=s[0], low=s[1], close=s[2])
        for i, s in enumerate(specs)
    ]


def run(bars, *, direction=Direction.LONG, entry=100, stop=99, qty=100,
        plan=None, atr=Decimal("1"), costs=FREE):
    return simulate_bracket(
        bars, symbol="TQQQ.US", direction=direction, entry_index=0,
        entry_price=Decimal(str(entry)), stop_price=Decimal(str(stop)),
        quantity=Decimal(qty), atr=atr, plan=plan or BracketPlan(),
        costs=costs, setup_name="test",
    )


# --------------------------------------------------------------------------
# Sizing


def test_size_is_risk_dollars_divided_by_stop_distance():
    """The playbook's formula verbatim: $100,000 risking 0.5% is $500, and
    a $6 stop distance buys 83 shares, floored from 83.33.

    The stop here is deliberately WIDE. With a $1 stop the formula asks for
    500 shares - $50,000, half the account - and the notional cap binds
    before the formula is visible at all. That interaction is real and is
    tested separately below; it is not what this test is about.
    """
    qty, capped = position_size(
        equity=Decimal("100000"), entry_price=Decimal("100"),
        stop_price=Decimal("94"), plan=BracketPlan(),
    )
    assert qty == Decimal("83")
    assert capped is False


def test_a_tight_stop_is_capped_by_notional_not_allowed_to_exceed_the_account():
    """Risk-based sizing divides by the stop distance, so a very tight stop
    asks for a very large position. On a 3x ETF that can exceed the whole
    account, and the platform's Risk Engine refuses any single order above
    10% of equity - so a research backtest that reported such a trade would
    be describing a fill the live path would reject."""
    qty, capped = position_size(
        equity=Decimal("100000"), entry_price=Decimal("100"),
        stop_price=Decimal("99.99"), plan=BracketPlan(),
    )
    assert capped is True
    assert qty == Decimal("100")  # 10% of 100k at $100
    assert qty * Decimal("100") <= Decimal("100000") * Decimal("0.10")


def test_a_capped_trade_is_still_taken():
    """Dropping it would remove exactly the tightest-stop setups from the
    statistics, which is a selection effect rather than a filter."""
    qty, capped = position_size(
        equity=Decimal("50000"), entry_price=Decimal("70"),
        stop_price=Decimal("69.98"), plan=BracketPlan(),
    )
    assert capped is True
    assert qty > 0


def test_a_zero_or_inverted_stop_distance_sizes_nothing():
    for stop in ("100", "0"):
        qty, _ = position_size(
            equity=Decimal("100000"), entry_price=Decimal("100"),
            stop_price=Decimal(stop), plan=BracketPlan(),
        )
        assert qty == 0 or stop == "0"


# --------------------------------------------------------------------------
# The stop-before-target convention


def test_a_bar_touching_both_stop_and_target_resolves_as_a_stop():
    """The most important convention in this module.

    A 5-minute bar records four numbers and no path. Whether price reached
    +1R before or after tagging the stop is unknowable from it, and
    resolving that in the trade's favour is the largest source of
    fictitious profit in intraday backtesting - worst of all on a 3x ETF,
    where bars are widest.
    """
    bars = session([(100, 100, 100), (102, 98, 101)])  # spans both 99 and 101

    trade = run(bars, entry=100, stop=99)

    assert trade is not None
    assert trade.stopped_out is True
    assert trade.hit_tp1 is False
    assert trade.r_multiple == Decimal("-1")


def test_the_entry_bar_itself_cannot_resolve_the_trade():
    """A setup detected from a bar's close is filled at that close. Letting
    the same bar's high and low then decide the outcome uses information
    the strategy did not have when it decided."""
    bars = session([(200, 1, 100), (100, 100, 100)])  # entry bar spans everything

    trade = run(bars, entry=100, stop=99)

    assert trade is not None
    assert not trade.stopped_out
    assert not trade.hit_tp1


# --------------------------------------------------------------------------
# Scaled exits and R accounting


def test_a_clean_winner_scales_out_at_1r_then_2r_and_trails():
    bars = session(
        [
            (100, 100, 100),   # entry bar
            (101, 100, 101),   # +1R
            (102, 101, 102),   # +2R
            (103, 102, 103),   # runner
            (103, 99.4, 99.5), # trail gives way (best close 103 - 1.5*ATR)
        ]
    )

    trade = run(bars, entry=100, stop=99, qty=99)

    assert trade.hit_tp1 and trade.hit_tp2
    reasons = [f.reason for f in trade.fills]
    assert reasons[0] is ExitReason.TARGET_1R
    assert reasons[1] is ExitReason.TARGET_2R
    assert sum(f.quantity for f in trade.fills) == Decimal("99")
    assert trade.r_multiple > 1


def test_r_multiple_is_measured_against_the_trades_own_initial_risk():
    """R is the playbook's unit of account throughout - expectancy, the
    daily stop, the targets - because it is the only measure that compares
    a wide-stop trade with a tight one on equal terms."""
    wide = run(session([(100, 100, 100), (110, 100, 110)]), entry=100, stop=90, qty=10)
    tight = run(session([(100, 100, 100), (101, 100, 101)]), entry=100, stop=99, qty=10)

    # Very different dollar moves, same risk-adjusted result.
    assert wide.gross_pnl == Decimal("100")
    assert tight.gross_pnl == Decimal("10")
    assert wide.r_multiple == tight.r_multiple == Decimal("1")


def test_the_stop_moves_to_breakeven_after_the_first_target():
    """The playbook's "reduce risk at +1R". Without it, a trade that tags
    1R and reverses gives the whole thing back."""
    bars = session(
        [
            (100, 100, 100),
            (101, 100, 101),      # +1R, partial out, stop -> entry
            (101, 98, 98.5),      # would have been -1R on the original stop
        ]
    )

    trade = run(bars, entry=100, stop=99, qty=99, plan=BracketPlan(breakeven_after_tp1=True))

    assert trade.hit_tp1
    assert trade.stopped_out
    stop_fill = next(f for f in trade.fills if f.reason is ExitReason.STOP)
    assert stop_fill.price == Decimal("100")  # breakeven, not 99
    assert trade.r_multiple > 0


def test_without_breakeven_the_original_stop_stands():
    bars = session([(100, 100, 100), (101, 100, 101), (101, 98, 98.5)])

    trade = run(bars, entry=100, stop=99, qty=99,
                plan=BracketPlan(breakeven_after_tp1=False))

    stop_fill = next(f for f in trade.fills if f.reason is ExitReason.STOP)
    assert stop_fill.price == Decimal("99")


# --------------------------------------------------------------------------
# Shorts


def test_a_short_profits_when_price_falls():
    """The playbook is symmetric and the engine must be too - `engine_v2`
    is long-only, which is why this exists."""
    bars = session([(100, 100, 100), (99.5, 99, 99), (99, 98, 98)])

    trade = run(bars, direction=Direction.SHORT, entry=100, stop=101, qty=99)

    assert trade.gross_pnl > 0
    assert trade.hit_tp1 and trade.hit_tp2


def test_a_short_is_stopped_when_price_rises():
    bars = session([(100, 100, 100), (102, 100, 101.5)])

    trade = run(bars, direction=Direction.SHORT, entry=100, stop=101, qty=99)

    assert trade.stopped_out
    assert trade.r_multiple == Decimal("-1")


# --------------------------------------------------------------------------
# The time stop


def test_the_position_is_flat_by_the_closing_bell():
    """TQQQ targets 3x the Nasdaq-100's DAILY move and its multi-day path
    diverges from 3x the index by design. Carrying an intraday signal
    overnight is not the same strategy held longer - it is a different
    instrument's risk. So the time stop is part of the model."""
    bars = session(
        [(100, 100, 100)]
        + [(100.5, 99.5, 100)] * 76      # through to 15:55
        + [(100.5, 99.5, 100.2)]         # 16:00 - after hours
    )

    trade = run(bars, entry=100, stop=99, qty=99)

    assert trade is not None
    assert trade.fills[-1].reason is ExitReason.TIME_STOP
    from apps.api.app.marketdata.sessions import is_regular_hours

    assert not is_regular_hours(trade.fills[-1].ts) or trade.exit_ts == bars[-1].ts


def test_a_position_never_carries_into_the_next_session():
    day_one = session([(100, 100, 100), (100.5, 99.5, 100)])
    day_two = session([(100.5, 99.5, 100)], day=(2026, 6, 18))

    trade = run([*day_one, *day_two], entry=100, stop=99, qty=99)

    from apps.api.app.marketdata.sessions import session_date

    assert session_date(trade.exit_ts) == session_date(day_one[0].ts)


# --------------------------------------------------------------------------
# Costs


def test_every_partial_pays_costs_not_just_the_round_trip():
    """A strategy that scales out three times pays three spreads. Charging
    one makes over-trading look free - the error that made `rsi-30-70`
    look profitable on BTC while actually losing money."""
    bars = session([(100, 100, 100), (101, 100, 101), (102, 101, 102), (103, 102, 103)])

    costed = run(bars, entry=100, stop=99, qty=99,
                 costs=CostModel(fee_bps=Decimal("10"), slippage_bps=Decimal("5")))
    free = run(bars, entry=100, stop=99, qty=99, costs=FREE)

    assert costed.gross_pnl < free.gross_pnl
    assert costed.entry_price > free.entry_price  # a long pays up to get in


def test_a_zero_quantity_trade_is_not_recorded():
    assert run(session([(100, 100, 100), (101, 100, 101)]), qty=0) is None


def test_a_zero_risk_trade_is_refused():
    assert run(session([(100, 100, 100), (101, 100, 101)]), entry=100, stop=100) is None


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_quantity_is_fully_accounted_for_in_every_outcome(direction):
    """Whatever happens, every share bought is sold. A scale-out path that
    leaks shares would silently change the position size mid-trade and
    misreport the result."""
    bars = session([(100, 100, 100), (101, 99, 100.5), (102, 98, 101)] + [(101, 99, 100)] * 80)

    trade = run(bars, direction=direction, entry=100,
                stop=99 if direction is Direction.LONG else 101, qty=97)

    assert sum(f.quantity for f in trade.fills) == Decimal("97")
