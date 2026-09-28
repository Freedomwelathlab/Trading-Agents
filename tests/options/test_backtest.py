"""The modelled options backtest (Phase 100, D119).

Pure unit tests over synthetic closes: no database, no network. What they
pin is what would make a backtest quietly lie - look-ahead in the
volatility input, a modelled price reported as a real one, costs that are
not charged, a structure whose "max loss" is not its max loss, and trades
that straddle a window boundary.
"""

import math
import random
from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.api.app.options.backtest import (
    DEFAULT_IV_RV_MULTIPLIER,
    MODELLED_NOTE,
    DailyClose,
    ExitReason,
    ObservedChain,
    ObservedQuote,
    OptionsBacktestConfig,
    OptionStrategy,
    PricingSource,
    derive_iv_rv_multiplier,
    modelled_expiry,
    prepare_series,
    run_options_backtest,
    trade_stats,
)
from apps.api.app.options.pricing import OptionRight
from apps.api.app.options.strategies import select_strikes
from apps.api.app.options.structures import StructureKind


def business_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def path(n: int = 160, *, drift: float = 0.0, vol: float = 0.03, seed: int = 7,
         start: date = date(2025, 1, 6), px: float = 100.0) -> list[DailyClose]:
    rng = random.Random(seed)
    out = []
    for d in business_days(start, n):
        out.append(DailyClose(d, px))
        px *= math.exp(drift + rng.gauss(0, vol))
    return out


def cfg(strategy: OptionStrategy, **over) -> OptionsBacktestConfig:
    base = dict(strategy=strategy, target_delta=0.30, dte_days=14, rv_window=5)
    base.update(over)
    return OptionsBacktestConfig(**base)  # type: ignore[arg-type]


# --- labelling -------------------------------------------------------------------


def test_every_trade_without_snapshots_is_labelled_modelled():
    result = run_options_backtest(path(), cfg(OptionStrategy.BULL_PUT), iv_rv_multiplier=1.2)
    assert result.trades
    assert {t.pricing for t in result.trades} == {PricingSource.MODELLED}
    assert result.pricing_basis == "MODELLED"
    assert result.note == MODELLED_NOTE and "not historical" in result.note.lower()
    assert result.to_dict(include_trades=False)["pricing_basis"] == "MODELLED"


# --- no look-ahead -------------------------------------------------------------------


def test_volatility_at_a_close_uses_only_returns_up_to_that_close():
    closes = path(60)
    series = prepare_series(closes, 5)
    rets = [math.log(closes[i].close / closes[i - 1].close) for i in range(1, 60)]
    import statistics

    assert series.rv[:5] == (None,) * 5
    expected = statistics.stdev(rets[5 - 5 : 5]) * math.sqrt(252)
    assert series.rv[5] == pytest.approx(expected)


def test_changing_the_future_does_not_change_a_past_entry():
    base = path(120)
    shocked = base[:80] + [DailyClose(c.day, c.close * 3) for c in base[80:]]
    config = cfg(OptionStrategy.LONG_CALL, dte_days=7)
    a = run_options_backtest(base, config, iv_rv_multiplier=1.2)
    b = run_options_backtest(shocked, config, iv_rv_multiplier=1.2)
    early_a = [t for t in a.trades if t.exit_date < base[79].day]
    early_b = [t for t in b.trades if t.exit_date < base[79].day]
    assert early_a and early_a == early_b


# --- economics ---------------------------------------------------------------------


def test_a_long_call_makes_money_in_a_steady_rally_and_a_long_put_does_not():
    rally = path(160, drift=0.01, vol=0.01)
    call = run_options_backtest(rally, cfg(OptionStrategy.LONG_CALL), iv_rv_multiplier=1.0)
    put = run_options_backtest(rally, cfg(OptionStrategy.LONG_PUT), iv_rv_multiplier=1.0)
    assert call.stats.total_pnl > 0 > put.stats.total_pnl


def test_a_bought_leg_pays_mid_plus_the_modelled_half_spread():
    result = run_options_backtest(path(), cfg(OptionStrategy.LONG_CALL), iv_rv_multiplier=1.2)
    for t in result.trades:
        mid = t.legs[0].entry_price
        half = max(Decimal("0.02"), Decimal("0.03") * mid)
        assert abs(t.entry_premium - (mid + half)) <= Decimal("0.011")


def test_commissions_are_charged_to_open_and_to_close_but_not_on_a_worthless_expiry():
    result = run_options_backtest(
        path(160),
        cfg(OptionStrategy.LONG_CALL, profit_target_pct=None, stop_loss_pct=None),
        iv_rv_multiplier=1.2,
    )
    expired = [t for t in result.trades if t.exit_reason is ExitReason.EXPIRY]
    assert expired
    for t in expired:
        expected = Decimal("0.65") if t.legs[0].exit_price == 0 else Decimal("1.30")
        assert t.commissions == expected
        # P&L is intrinsic minus what was paid, x100, minus commissions (the
        # per-share figures are shown rounded to cents, hence the tolerance).
        assert abs(t.pnl - ((t.exit_value - t.entry_premium) * 100 - t.commissions)) <= 1


def test_a_credit_spreads_max_loss_is_width_minus_credit_plus_commissions():
    result = run_options_backtest(path(), cfg(OptionStrategy.BULL_PUT), iv_rv_multiplier=1.2)
    for t in result.trades:
        width = abs(t.legs[0].strike - t.legs[1].strike)
        credit = -t.entry_premium
        assert credit > 0
        expected = (width - credit) * 100 + Decimal("0.65") * 2 * 2
        assert abs(t.max_loss - expected) <= Decimal("0.51")
        assert t.return_on_risk == pytest.approx(float(t.pnl / t.max_loss))


def test_a_debit_spreads_max_loss_is_the_debit_plus_commissions():
    result = run_options_backtest(path(), cfg(OptionStrategy.BULL_CALL, target_delta=0.5),
                                  iv_rv_multiplier=1.2)
    assert result.trades
    for t in result.trades:
        assert t.entry_premium > 0
        expected = t.entry_premium * 100 + Decimal("0.65") * 2 * 2
        assert abs(t.max_loss - expected) <= Decimal("0.51")
        long_leg = next(leg for leg in t.legs if leg.is_long)
        short_leg = next(leg for leg in t.legs if not leg.is_long)
        assert long_leg.strike < short_leg.strike


def test_an_iron_condor_has_four_legs_in_the_right_order():
    result = run_options_backtest(path(), cfg(OptionStrategy.IRON_CONDOR), iv_rv_multiplier=1.2)
    assert result.trades
    for t in result.trades:
        puts = sorted((leg for leg in t.legs if leg.right is OptionRight.PUT),
                      key=lambda leg: leg.strike)
        calls = sorted((leg for leg in t.legs if leg.right is OptionRight.CALL),
                       key=lambda leg: leg.strike)
        assert [leg.is_long for leg in puts] == [True, False]
        assert [leg.is_long for leg in calls] == [False, True]
        assert puts[1].strike < calls[0].strike
        assert t.entry_premium < 0


def test_a_stop_exits_at_the_close_that_breaches_it():
    result = run_options_backtest(
        path(160, vol=0.05),
        cfg(OptionStrategy.BULL_PUT, stop_loss_pct=0.5, profit_target_pct=None),
        iv_rv_multiplier=1.2,
    )
    stopped = [t for t in result.trades if t.exit_reason is ExitReason.STOP_LOSS]
    assert stopped
    for t in stopped:
        # Evaluated on the close-able price, so the realised loss is at
        # least the stop (and can be more: a gap fills at the gapped price).
        assert (t.exit_value - t.entry_premium) <= Decimal("0.5") * t.entry_premium + Decimal(
            "0.011"
        )


# --- windows and expiry ------------------------------------------------------------


def test_entries_respect_the_window_and_the_last_bar_closes_everything():
    closes = path(160)
    start, end = closes[40].day, closes[100].day
    result = run_options_backtest(
        closes, cfg(OptionStrategy.LONG_PUT, dte_days=30, profit_target_pct=None,
                    stop_loss_pct=None),
        iv_rv_multiplier=1.2, start=start, end=end,
    )
    assert result.trades
    assert all(start <= t.entry_date < end for t in result.trades)
    assert all(t.exit_date <= end for t in result.trades)
    assert result.trades[-1].exit_reason is ExitReason.WINDOW_END


def test_an_expiry_with_no_bar_settles_on_the_last_close_before_it():
    days = business_days(date(2025, 2, 24), 12)  # Mon 24 Feb .. Tue 11 Mar
    closes = [DailyClose(d, 100.0 + i) for i, d in enumerate(days)]
    friday = date(2025, 3, 7)
    assert friday in days
    closes = [c for c in closes if c.day != friday]  # a holiday Friday
    entry = date(2025, 3, 3)
    config = cfg(OptionStrategy.LONG_CALL, dte_days=4, rv_window=2, target_delta=0.5,
                 profit_target_pct=None, stop_loss_pct=None)
    assert modelled_expiry(entry, 4) == friday
    result = run_options_backtest(closes, config, iv_rv_multiplier=1.2, start=entry,
                                  end=date(2025, 3, 11))
    first = result.trades[0]
    assert first.entry_date == entry and first.expiry == friday
    assert first.exit_reason is ExitReason.EXPIRY
    assert first.exit_date == friday
    thursday_close = next(c.close for c in closes if c.day == date(2025, 3, 6))
    assert first.exit_spot == Decimal(str(thursday_close)).quantize(Decimal("0.01"))
    assert first.legs[0].exit_price == (first.exit_spot - first.legs[0].strike).max(Decimal(0))


# --- observed prices ------------------------------------------------------------------


def _observed_setup():
    days = business_days(date(2025, 3, 3), 20)
    # Alternating closes: realised vol is non-zero at every bar, and the
    # expiry close (an even index) is exactly 100.
    closes = [DailyClose(d, 100.0 + (i % 2)) for i, d in enumerate(days)]
    entry = days[5]
    expiry = entry + timedelta(days=11)  # a Friday: 2025-03-10 + 11 = 2025-03-21
    assert expiry.weekday() == 4
    quotes = tuple(
        ObservedQuote(expiry=expiry, right=OptionRight.CALL, strike=Decimal(k),
                      bid=Decimal(bid), ask=Decimal(ask), iv=Decimal("0.5"), delta=Decimal(dl))
        for k, bid, ask, dl in [
            ("95", "5.90", "6.10", "0.78"),
            ("100", "2.40", "2.60", "0.51"),
            ("105", "0.80", "0.90", "0.24"),
        ]
    )
    chain = ObservedChain(trade_date=entry, spot=Decimal("100"), quotes=quotes)
    return closes, entry, expiry, chain


def test_a_snapshot_on_the_entry_day_supplies_real_contracts_and_real_prices():
    closes, entry, expiry, chain = _observed_setup()
    config = cfg(OptionStrategy.LONG_CALL, target_delta=0.5, dte_days=10, rv_window=2,
                 profit_target_pct=None, stop_loss_pct=None)
    result = run_options_backtest(closes, config, iv_rv_multiplier=1.2, start=entry,
                                  end=closes[-1].day, observed={entry: chain})
    t = result.trades[0]
    assert t.expiry == expiry and t.legs[0].strike == Decimal("100")  # delta 0.51
    assert t.legs[0].entry_price == Decimal("2.50")   # the real mid
    assert t.entry_premium == Decimal("2.60")          # bought at the real ask
    # Held to expiry: settlement is exact, so the whole trade is OBSERVED.
    assert t.exit_reason is ExitReason.EXPIRY
    assert t.pricing is PricingSource.OBSERVED
    assert result.pricing_basis == "MODELLED+OBSERVED"


def test_a_real_entry_with_a_modelled_exit_is_mixed_not_observed():
    closes, entry, _expiry, chain = _observed_setup()
    config = cfg(OptionStrategy.LONG_CALL, target_delta=0.5, dte_days=10, rv_window=2,
                 profit_target_pct=None, stop_loss_pct=None, exit_dte=10)
    result = run_options_backtest(closes, config, iv_rv_multiplier=1.2, start=entry,
                                  end=closes[-1].day, observed={entry: chain})
    t = result.trades[0]
    assert t.exit_reason is ExitReason.DTE_EXIT
    assert t.pricing is PricingSource.MIXED


def test_a_snapshot_without_usable_quotes_falls_back_to_the_model():
    closes, entry, expiry, _chain = _observed_setup()
    one_sided = ObservedChain(
        trade_date=entry, spot=Decimal("100"),
        quotes=(ObservedQuote(expiry=expiry, right=OptionRight.CALL, strike=Decimal("100"),
                              bid=None, ask=Decimal("2.6"), delta=Decimal("0.5")),),
    )
    config = cfg(OptionStrategy.LONG_CALL, target_delta=0.5, dte_days=10, rv_window=2)
    result = run_options_backtest(closes, config, iv_rv_multiplier=1.2, start=entry,
                                  end=closes[-1].day, observed={entry: one_sided})
    assert result.trades[0].pricing is PricingSource.MODELLED


# --- the multiplier ---------------------------------------------------------------------


def test_no_snapshot_means_the_default_multiplier_and_says_so():
    m = derive_iv_rv_multiplier(prepare_series(path(60), 20), {})
    assert m.value == DEFAULT_IV_RV_MULTIPLIER
    assert "default" in m.source


def test_a_snapshot_measures_atm_iv_over_realised_vol():
    closes = path(60)
    series = prepare_series(closes, 20)
    day = closes[40].day
    expiry = day + timedelta(days=30)
    spot = Decimal(str(round(closes[40].close, 2)))
    atm = spot.quantize(Decimal(1))
    chain = ObservedChain(
        trade_date=day, spot=spot,
        quotes=(
            ObservedQuote(expiry, OptionRight.CALL, atm, Decimal(1), Decimal(2),
                          iv=Decimal("0.60")),
            ObservedQuote(expiry, OptionRight.PUT, atm, Decimal(1), Decimal(2),
                          iv=Decimal("0.70")),
            ObservedQuote(expiry, OptionRight.CALL, atm + 10, Decimal(1), Decimal(2),
                          iv=Decimal("0.99")),
        ),
    )
    m = derive_iv_rv_multiplier(series, {day: chain})
    rv = series.rv[40]
    assert rv is not None
    assert m.value == pytest.approx(0.65 / rv)
    assert "measured" in m.source


def test_a_snapshot_day_without_a_bar_is_skipped_not_filled():
    closes = path(60)
    series = prepare_series(closes, 20)
    orphan = closes[-1].day + timedelta(days=30)
    chain = ObservedChain(orphan, Decimal(100), (
        ObservedQuote(orphan + timedelta(days=30), OptionRight.CALL, Decimal(100), Decimal(1),
                      Decimal(2), iv=Decimal("0.5")),
    ))
    m = derive_iv_rv_multiplier(series, {orphan: chain})
    assert m.value == DEFAULT_IV_RV_MULTIPLIER
    assert m.samples[0]["used"] is False


# --- configuration and stats ------------------------------------------------------------


def test_a_daily_backtest_refuses_zero_dte():
    with pytest.raises(ValueError):
        OptionsBacktestConfig(strategy=OptionStrategy.LONG_CALL, dte_days=0)


def test_default_wings_reproduce_the_playbooks_credit_pair():
    c = OptionsBacktestConfig(strategy=OptionStrategy.BULL_PUT, target_delta=0.30)
    assert c.resolved_wing_delta == pytest.approx(0.15)
    d = OptionsBacktestConfig(strategy=OptionStrategy.BULL_CALL, target_delta=0.60)
    assert d.resolved_wing_delta == pytest.approx(0.35)


def test_empty_stats_are_none_not_zero():
    s = trade_stats([])
    assert s.trades == 0 and s.win_rate is None and s.expectancy is None
    assert s.expectancy_r is None


def test_drawdown_is_peak_to_trough_of_cumulative_pnl():
    result = run_options_backtest(path(160, vol=0.05), cfg(OptionStrategy.LONG_CALL),
                                  iv_rv_multiplier=1.2)
    cum = peak = dd = Decimal(0)
    for t in result.trades:
        cum += t.pnl
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    assert result.stats.max_drawdown == dd
    assert result.stats.total_pnl == cum


def test_select_strikes_accepts_research_deltas_and_defaults_to_the_playbook():
    default = select_strikes(kind=StructureKind.BULL_PUT, spot=100.0, dte_years=14 / 365,
                             volatility=0.5)
    same = select_strikes(kind=StructureKind.BULL_PUT, spot=100.0, dte_years=14 / 365,
                          volatility=0.5, long_delta=0.15, short_delta=0.30)
    wider = select_strikes(kind=StructureKind.BULL_PUT, spot=100.0, dte_years=14 / 365,
                           volatility=0.5, long_delta=0.05, short_delta=0.30)
    assert default == same
    assert wider[0] < default[0] and wider[1] == default[1]
