"""Phase 102 (D122): paper option economics, the risk-gate mapping and the
bot's leg selection. Pure - no database, no network."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from apps.api.app.options.paper_orders import (
    LegSide,
    LegSpec,
    OptionOrderError,
    OptionStructureType,
    haircut_fill,
    mid_value,
    price_close,
    price_open,
    realized_pnl,
    settlement_value,
    structure_key,
    validate_structure,
)
from apps.api.app.options.pricing import OptionRight
from apps.api.app.options.risk_gate import (
    evaluate_option_close,
    evaluate_option_open,
    option_limits,
)
from apps.api.app.options_bot.engine import market_open, pnl_pct
from apps.api.app.options_bot.learning import aggregate
from apps.api.app.options_bot.selection import SelectionRefused, build_candidate, pick_expiry
from apps.api.app.risk.models import AccountState, BlockReason, RiskLimits
from tests.options.chain_fixtures import make_chain

C, P = OptionRight.CALL, OptionRight.PUT
B, S = LegSide.BUY, LegSide.SELL
T = OptionStructureType
NOW = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)  # a Monday, 11:00 ET
EXPIRY = date(2026, 10, 16)
D = Decimal


def chain(**kw):
    return make_chain(EXPIRY, NOW - timedelta(seconds=60), **kw)


# --- fills -------------------------------------------------------------------


def test_haircut_moves_k_half_spreads_against_the_trade_and_rounds_against_the_account():
    assert haircut_fill(B, bid=D("1.00"), ask=D("1.10"), k=D("0.5")) == D("1.08")  # 1.075 up
    assert haircut_fill(S, bid=D("1.00"), ask=D("1.10"), k=D("0.5")) == D("1.02")  # 1.025 down
    assert haircut_fill(B, bid=D("1.00"), ask=D("1.10"), k=D("1")) == D("1.10")  # the ask
    assert haircut_fill(S, bid=D("1.00"), ask=D("1.10"), k=D("0")) == D("1.05")  # the mid
    with pytest.raises(ValueError):
        haircut_fill(B, bid=D("1"), ask=D("2"), k=D("1.5"))


@pytest.mark.parametrize(
    "override",
    [{"bid": None}, {"ask": None}, {"bid": D("0")}, {"bid": D("2.10"), "ask": D("2.00")}],
    ids=["no-bid", "no-ask", "zero-bid", "crossed"],
)
def test_a_leg_without_a_two_sided_market_is_data_unavailable_never_guessed(override):
    ch = chain(overrides={(C, D(52)): override})
    with pytest.raises(OptionOrderError, match="^DATA_UNAVAILABLE"):
        price_open(T.BULL_CALL, [LegSpec(C, D(50), B), LegSpec(C, D(52), S)], ch, k=D("0.5"))


def test_an_unlisted_strike_is_data_unavailable():
    with pytest.raises(OptionOrderError, match="^DATA_UNAVAILABLE"):
        price_open(T.LONG_CALL, [LegSpec(C, D("50.5"), B)], chain(), k=D("0.5"))


# --- shapes ------------------------------------------------------------------


@pytest.mark.parametrize(
    "structure,legs",
    [
        (T.LONG_CALL, [LegSpec(C, D(50), S)]),
        (T.BULL_PUT, [LegSpec(P, D(46), S)]),  # protective put missing
        (T.BEAR_CALL, [LegSpec(C, D(52), S), LegSpec(P, D(50), B)]),  # a put is no cover
        (T.IRON_CONDOR, [LegSpec(P, D(44), S), LegSpec(P, D(46), S), LegSpec(C, D(54), S),
                         LegSpec(C, D(57), B)]),
        (T.CASH_SECURED_PUT, [LegSpec(C, D(55), S)]),
    ],
)
def test_naked_shorts_are_refused_whatever_they_are_called(structure, legs):
    with pytest.raises(OptionOrderError, match="^NAKED_SHORT_REFUSED"):
        validate_structure(structure, legs)


def test_shape_errors_are_specific():
    with pytest.raises(OptionOrderError, match="^BAD_STRUCTURE"):
        validate_structure(T.BULL_CALL, [LegSpec(C, D(52), B), LegSpec(C, D(50), S)])
    with pytest.raises(OptionOrderError, match="^BAD_STRUCTURE"):  # long below the short
        validate_structure(T.BEAR_CALL, [LegSpec(C, D(52), S), LegSpec(C, D(50), B)])
    with pytest.raises(OptionOrderError, match="^BAD_STRUCTURE"):
        validate_structure(T.LONG_CALL, [LegSpec(C, D(50), B), LegSpec(C, D(50), B)])
    with pytest.raises(OptionOrderError, match="^BAD_STRUCTURE"):
        validate_structure(
            T.IRON_CONDOR,
            [LegSpec(P, D(44), B), LegSpec(P, D(52), S), LegSpec(C, D(50), S),
             LegSpec(C, D(56), B)],
        )
    validate_structure(T.CASH_SECURED_PUT, [LegSpec(P, D(45), S)])  # allowed


# --- economics ---------------------------------------------------------------


def test_bull_call_debit_is_its_max_loss_and_capital():
    econ = price_open(
        T.BULL_CALL, [LegSpec(C, D(50), B), LegSpec(C, D(52), S)], chain(), k=D("0.5")
    )
    # 50C mid 2.00 -> buy 2.01 (2.005 up); 52C mid 1.80 -> sell 1.79 (1.795 down)
    assert [leg.fill_price for leg in econ.legs] == [D("2.01"), D("1.79")]
    assert econ.net_price == D("0.22")
    assert econ.max_loss_per_contract == D("22.00")
    assert econ.max_profit_per_contract == D("178.00")
    assert econ.capital_per_contract == econ.max_loss_per_contract


def test_bull_put_credit_reserves_width_less_credit():
    econ = price_open(T.BULL_PUT, [LegSpec(P, D(46), S), LegSpec(P, D(44), B)], chain(),
                      k=D("0.5"))
    # 46P mid 1.60 -> sell 1.59; 44P mid 1.40 -> buy 1.41; credit 0.18
    assert econ.net_price == D("-0.18") and econ.is_credit
    assert econ.max_loss_per_contract == D("182.00")
    assert econ.max_profit_per_contract == D("18.00")


def test_iron_condor_reserves_the_wider_wing_less_credit():
    legs = [LegSpec(P, D(44), B), LegSpec(P, D(46), S), LegSpec(C, D(54), S),
            LegSpec(C, D(57), B)]
    econ = price_open(T.IRON_CONDOR, legs, chain(), k=D("0.5"))
    # credit = 1.59 - 1.41 + 1.59 - 1.31 = 0.46; wider wing 3
    assert econ.net_price == D("-0.46")
    assert econ.max_loss_per_contract == D("254.00")


def test_cash_secured_put_reserves_strike_less_premium():
    econ = price_open(T.CASH_SECURED_PUT, [LegSpec(P, D(45), S)], chain(), k=D("0.5"))
    assert econ.net_price == D("-1.49")
    assert econ.max_loss_per_contract == D("4351.00")


def test_a_debit_at_or_above_the_width_is_refused():
    ch = chain(overrides={(C, D(51)): {"bid": D("0.10"), "ask": D("0.12")}})
    with pytest.raises(OptionOrderError, match="^BAD_PRICE"):
        price_open(T.BULL_CALL, [LegSpec(C, D(50), B), LegSpec(C, D(51), S)], ch, k=D("0.5"))


def test_close_settlement_and_pnl_are_signed_like_entry():
    legs = [
        {"right": "put", "strike": "46", "side": "sell"},
        {"right": "put", "strike": "44", "side": "buy"},
    ]
    _, exit_value = price_close(legs, chain(), k=D("0.5"))
    # buy back 46P at 1.61, sell 44P at 1.39 -> -0.22 per share
    assert exit_value == D("-0.22")
    assert mid_value(legs, chain()) == D("-0.20")
    assert realized_pnl(entry_net_price=D("-0.18"), exit_value=exit_value, quantity=2) == D("-8.00")
    # at expiry: both OTM keeps the credit; both ITM loses width - credit
    assert settlement_value(legs, D("50")) == 0
    assert realized_pnl(entry_net_price=D("-0.18"), exit_value=settlement_value(legs, D("40")),
                        quantity=1) == D("-182.00")


def test_structure_key_is_order_independent():
    a = structure_key("X.US", T.BULL_CALL, EXPIRY, [LegSpec(C, D(50), B), LegSpec(C, D(52), S)])
    b = structure_key("X.US", T.BULL_CALL, EXPIRY, [LegSpec(C, D(52), S), LegSpec(C, D(50), B)])
    assert a == b


# --- risk gate ---------------------------------------------------------------

LIMITS = option_limits(
    RiskLimits(
        max_position_pct_of_equity=D("0.10"),
        max_portfolio_exposure_pct_of_equity=D("0.50"),
        max_risk_pct_of_equity_per_trade=D("0.01"),
        require_stop_price=True,
        max_market_data_age_seconds=300,
        duplicate_order_window_seconds=5,
    ),
    max_quote_age_seconds=1800,
)
ACCOUNT = AccountState(equity=D(100_000), cash=D(100_000), current_exposure=D(0))


def _open(**over):
    kw = dict(
        structure_key="k", quantity=10, max_loss_per_contract=D("22"), account=ACCOUNT,
        limits=LIMITS, emergency_stop_active=False, now=NOW,
        quote_as_of=NOW - timedelta(minutes=15),
    )
    kw.update(over)
    return evaluate_option_open(**kw)


def test_a_fifteen_minute_delayed_quote_passes_and_no_stop_price_is_demanded():
    assert LIMITS.require_stop_price is False
    assert _open().approved


def test_per_trade_risk_is_the_max_loss_and_names_what_fits():
    d = _open(quantity=50)  # 1,100 of max loss against a 1,000 (1%) budget
    assert not d.approved and d.reason is BlockReason.EXCEEDS_PER_TRADE_RISK
    assert d.max_quantity_allowed == D(45)


def test_open_risk_ceiling_counts_capital_already_reserved():
    busy = AccountState(equity=D(100_000), cash=D(55_000), current_exposure=D(49_990))
    d = _open(account=busy)
    assert d.reason is BlockReason.EXCEEDS_PORTFOLIO_EXPOSURE


def test_the_engine_gates_run_for_options_too():
    assert _open(emergency_stop_active=True).reason is BlockReason.EMERGENCY_STOP_ACTIVE
    assert _open(quote_as_of=NOW - timedelta(hours=2)).reason is BlockReason.MARKET_DATA_STALE
    poor = AccountState(equity=D(100_000), cash=D(100), current_exposure=D(0))
    assert _open(account=poor).reason is BlockReason.INSUFFICIENT_BUYING_POWER


def test_a_close_is_only_stopped_by_the_emergency_stop_or_stale_data():
    ok = evaluate_option_close(limits=LIMITS, emergency_stop_active=False, now=NOW,
                               quote_as_of=NOW - timedelta(minutes=10))
    assert ok.approved
    assert evaluate_option_close(limits=LIMITS, emergency_stop_active=True, now=NOW,
                                 quote_as_of=NOW).reason is BlockReason.EMERGENCY_STOP_ACTIVE
    assert evaluate_option_close(limits=LIMITS, emergency_stop_active=False, now=NOW,
                                 quote_as_of=NOW - timedelta(hours=1)).reason is (
        BlockReason.MARKET_DATA_STALE
    )


# --- bot selection -----------------------------------------------------------


def test_pick_expiry_is_the_nearest_inside_the_band():
    today = date(2026, 10, 5)
    listed = [date(2026, 10, 6), date(2026, 10, 9), date(2026, 10, 16), date(2026, 11, 20)]
    assert pick_expiry(listed, today=today, dte_min=3, dte_max=14) == date(2026, 10, 9)
    with pytest.raises(SelectionRefused):
        pick_expiry(listed, today=today, dte_min=50, dte_max=60)


def test_bull_put_anchors_the_short_leg_on_vendor_delta():
    cand = build_candidate(T.BULL_PUT, chain(), target_delta=D("0.30"), spread_width=D(2))
    assert cand.legs == (LegSpec(P, D(46), S), LegSpec(P, D(44), B))
    assert abs(cand.anchor_delta) == D("0.30")


def test_iron_condor_selection():
    cand = build_candidate(T.IRON_CONDOR, chain(), target_delta=D("0.20"), spread_width=D(2))
    assert [(leg.right, leg.strike, leg.side) for leg in cand.legs] == [
        (P, D(42), B), (P, D(44), S), (C, D(56), S), (C, D(58), B)
    ]


def test_selection_refuses_illiquid_or_greekless_legs():
    wide = chain(overrides={(P, D(46)): {"bid": D("1.40"), "ask": D("1.80")}})
    # the 46 put is no longer two-sided-tight: the anchor moves or the filter refuses
    thin = chain(overrides={(P, D(44)): {"open_interest": 10}})
    with pytest.raises(SelectionRefused, match="liquidity"):
        build_candidate(T.BULL_PUT, thin, target_delta=D("0.30"), spread_width=D(2))
    with pytest.raises(SelectionRefused, match="liquidity"):
        build_candidate(T.BULL_PUT, wide, target_delta=D("0.30"), spread_width=D(2))
    no_greeks = chain(overrides={(P, D(k)): {"delta": None} for k in range(40, 61)})
    with pytest.raises(SelectionRefused, match="delta"):
        build_candidate(T.LONG_PUT, no_greeks, target_delta=D("0.30"), spread_width=None)


def test_market_gate_and_pnl_pct():
    assert market_open(NOW)
    assert not market_open(datetime(2026, 10, 4, 15, 0, tzinfo=UTC))  # Sunday
    assert not market_open(datetime(2026, 10, 5, 21, 0, tzinfo=UTC))  # after 16:00 ET
    assert pnl_pct(pnl=D("9"), entry_net_price=D("-0.18"), quantity=1) == D("50.0000")


def test_learning_aggregate_on_nothing_is_none_not_zero():
    s = aggregate("bull_put", [])
    assert s.trades == 0 and s.win_rate is None and s.expectancy is None
