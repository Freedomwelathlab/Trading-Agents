"""The FX spread cost model, financing, and the intraday engine on the FX
clock (Phase 103, D123)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from apps.api.app.backtesting.brackets import (
    BracketFill,
    BracketPlan,
    BracketTrade,
    ExitReason,
    _charge_financing,
    simulate_bracket,
)
from apps.api.app.backtesting.costs import CostModel, FxSpreadCostModel, rollover_days
from apps.api.app.backtesting.intraday_engine import IntradayRunConfig, run_intraday_backtest
from apps.api.app.marketdata.sessions import FX_CALENDAR
from apps.api.app.marketdata.structure import Direction

NY = ZoneInfo("America/New_York")
LDN = ZoneInfo("Europe/London")


# --- cost model --------------------------------------------------------------


def test_fills_are_mid_plus_or_minus_half_a_spread_in_pips():
    eur = FxSpreadCostModel.for_symbol("EURUSD.FX")
    assert eur.half_spread_pips == Decimal("0.5")
    assert eur.buy_price(Decimal("1.10000")) == Decimal("1.10005")
    assert eur.sell_price(Decimal("1.10000")) == Decimal("1.09995")
    # Pips, not bps: the same half-pip is 100x larger in price on a JPY pair.
    jpy = FxSpreadCostModel.for_symbol("USDJPY.FX", half_spread_pips=Decimal("0.5"))
    assert jpy.buy_price(Decimal("150.000")) == Decimal("150.005")
    assert eur.costs_for(quantity=Decimal(10000), mid=Decimal("1.1")) == (
        Decimal(0), Decimal("0.50000")
    )


def test_negative_spread_is_refused():
    with pytest.raises(ValueError):
        FxSpreadCostModel(pair="EURUSD", pip_size=Decimal("0.0001"), half_spread_pips=Decimal(-1))


def ny(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=NY)


def test_rollover_days_charge_wednesday_triple_and_nothing_intraday():
    assert rollover_days(ny(2025, 6, 2, 9), ny(2025, 6, 2, 16, 55)) == 0  # intraday
    assert rollover_days(ny(2025, 6, 2, 9), ny(2025, 6, 3, 9)) == 1  # Mon roll
    assert rollover_days(ny(2025, 6, 4, 9), ny(2025, 6, 5, 9)) == 3  # Wed roll
    # Friday into Monday: only Friday's roll; there is none on Sunday's open.
    assert rollover_days(ny(2025, 6, 6, 9), ny(2025, 6, 9, 9)) == 1
    assert rollover_days(ny(2025, 6, 2, 9), ny(2025, 6, 9, 9)) == 7  # a full week
    assert rollover_days(ny(2025, 6, 2, 17), ny(2025, 6, 2, 18)) == 0  # opened ON the roll


def test_financing_is_off_by_default_and_charged_per_slice_when_set():
    base = FxSpreadCostModel.for_symbol("EURUSD.FX")
    rated = FxSpreadCostModel.for_symbol(
        "EURUSD.FX", financing_annual_rate_long=Decimal("0.0365")
    )
    trade = BracketTrade(
        direction=Direction.LONG, symbol="EURUSD.FX", entry_ts=ny(2025, 6, 2, 9),
        entry_price=Decimal("1"), initial_stop=Decimal("0.99"), quantity=Decimal(1000),
        risk_per_share=Decimal("0.01"), setup_name="t",
    )
    trade.fills = [
        BracketFill(ny(2025, 6, 2, 12), Decimal(500), Decimal("1.01"), ExitReason.TARGET_1R,
                    Decimal(1)),
        BracketFill(ny(2025, 6, 3, 12), Decimal(500), Decimal("1.01"), ExitReason.TIME_STOP,
                    Decimal(1)),
    ]
    _charge_financing(trade, base)
    assert trade.financing == 0
    _charge_financing(trade, rated)
    # Only the second half was held across Monday's roll: 500 * 1 * 3.65% / 365.
    assert trade.financing == Decimal("0.05")
    assert trade.gross_pnl == Decimal("10") - Decimal("0.05")
    _charge_financing(trade, CostModel.frictionless())  # equity model: untouched
    assert trade.financing == Decimal("0.05")


# --- engine on the FX clock --------------------------------------------------


class Bar:
    def __init__(self, ts, *, high, low, close, open=None):
        self.ts = ts.astimezone(UTC)
        self.open = Decimal(str(open if open is not None else close))
        self.high = Decimal(str(high))
        self.low = Decimal(str(low))
        self.close = Decimal(str(close))
        self.volume = None  # spot FX: no volume, ever


def _session(day: date, closes):
    start = datetime(day.year, day.month, day.day, 8, 0, tzinfo=LDN)
    return [
        Bar(start + timedelta(minutes=5 * i), high=c + 0.0003, low=c - 0.0003, close=c)
        for i, c in enumerate(closes)
    ]


def test_a_bracket_is_flat_before_the_17_00_new_york_roll():
    day = date(2025, 6, 4)
    closes = [1.1000 + 0.00001 * i for i in range(12 * 14)]
    bars = _session(day, closes)
    last_regular = [b for b in bars if FX_CALENDAR.is_regular_hours(b.ts)][-1].ts
    # After the roll: the next session's (Asian, blacked-out) bars.
    bars.append(Bar(datetime(2025, 6, 4, 17, 30, tzinfo=NY), high=1.2, low=1.2, close=1.2))
    trade = simulate_bracket(
        bars, symbol="EURUSD.FX", direction=Direction.LONG, entry_index=0,
        entry_price=Decimal("1.1000"), stop_price=Decimal("1.0900"), quantity=Decimal(1000),
        atr=None, plan=BracketPlan(trail_atr_multiple=None), costs=FxSpreadCostModel.for_symbol(
            "EURUSD.FX"), setup_name="t", calendar=FX_CALENDAR,
    )
    assert trade is not None
    assert trade.fills[-1].reason is ExitReason.TIME_STOP
    assert trade.exit_ts == last_regular
    assert trade.exit_ts < datetime(2025, 6, 4, 17, 0, tzinfo=NY)
    assert trade.financing == 0


def test_volume_gated_setups_never_fire_on_fx_bars():
    bars = []
    for i, day in enumerate((date(2025, 6, 2), date(2025, 6, 3), date(2025, 6, 4))):
        # A strong impulse then a pullback - the shape quiet_pullback wants,
        # minus the volume it needs to tell a quiet pullback from a loud one.
        path = [1.10 + 0.0004 * k for k in range(40)] + [1.116 - 0.0002 * k for k in range(8)]
        path += [1.114] * 60
        bars += _session(day, [p + 0.001 * i for p in path])
    for setup in ("quiet_pullback", "volume_climax_reversal", "vwap_reversion"):
        result = run_intraday_backtest(
            bars,
            IntradayRunConfig(
                symbol="EURUSD.FX", setups=(setup,), calendar=FX_CALENDAR,
                costs=FxSpreadCostModel.for_symbol("EURUSD.FX"),
            ),
        )
        assert result.signals_seen == 0, setup
        assert result.trades == []
    # The same bars DO reach the detectors: sessions were found and replayed.
    assert result.sessions_available == 3
