"""Both-direction trading (Phase 87, D106).

Everything here is a mirror of a long rule, and every one of them is a
place where leaving the long formula in would produce a number that looks
right. A stop below entry on a short can never trigger; a trailing stop
that ratchets up on a short loosens instead of tightens; the long P&L
formula reports a winning short as a loss of the same size. Each test
below pins one of those.
"""

from decimal import Decimal

import pytest

from apps.api.app.autotrade.brackets import ExitRules, initial_bracket, manage
from apps.api.app.autotrade.service import EXTENDED_HOURS_SCORE_PREMIUM, _extended_hours_bar
from apps.api.app.db.models import AutotradeExitReason
from apps.api.app.execution.broker import OrderRequest
from apps.api.app.execution.paper_broker import (
    InsufficientFundsError,
    InsufficientPositionError,
    PaperBrokerAdapter,
)
from apps.api.app.marketdata.structure import Direction
from apps.api.app.risk.models import Side

D = Decimal


def rules(**over) -> ExitRules:
    base = dict(
        stop_loss_mode="auto",
        stop_loss_max_pct=None,
        trailing_stop_pct=None,
        take_profit_mode="auto",
        take_profit_min_pct=None,
        trailing_take_profit_pct=None,
    )
    base.update(over)
    return ExitRules(**base)  # type: ignore[arg-type]


# --- brackets --------------------------------------------------------------


def test_a_short_opens_with_its_stop_above_entry_and_its_target_below():
    b = initial_bracket(
        entry_price=D("100"), structural_stop=D("102"), rules=rules(),
        direction=Direction.SHORT,
    )
    assert b.stop_price == D("102")
    # Risk is 2, so 2R is 4 BELOW entry.
    assert b.take_profit_price == D("96")


def test_a_short_stop_cap_tightens_toward_entry_rather_than_away_from_it():
    # The operator's cap is 1%, i.e. 101 — nearer entry than the structural
    # 105, so it wins. `max()` here (the long rule) would pick 105 and quietly
    # let the trade risk five times what the operator allowed.
    b = initial_bracket(
        entry_price=D("100"), structural_stop=D("105"),
        rules=rules(stop_loss_mode="max", stop_loss_max_pct=D("1")),
        direction=Direction.SHORT,
    )
    assert b.stop_price == D("101")


def test_a_short_stop_on_the_wrong_side_of_entry_is_replaced_by_the_cap():
    b = initial_bracket(
        entry_price=D("100"), structural_stop=D("98"),
        rules=rules(stop_loss_mode="max", stop_loss_max_pct=D("2")),
        direction=Direction.SHORT,
    )
    assert b.stop_price == D("102")


def test_a_short_stops_out_on_the_bars_high_not_its_low():
    b = initial_bracket(
        entry_price=D("100"), structural_stop=D("102"), rules=rules(),
        direction=Direction.SHORT,
    )
    d = manage(
        b, bar_high=D("102.5"), bar_low=D("99"), bar_close=D("101"),
        rules=rules(), session_ending=False,
    )
    assert d.exit_reason is AutotradeExitReason.STOP_LOSS


def test_a_short_takes_profit_on_the_bars_low():
    b = initial_bracket(
        entry_price=D("100"), structural_stop=D("102"), rules=rules(),
        direction=Direction.SHORT,
    )
    d = manage(
        b, bar_high=D("100.5"), bar_low=D("95.5"), bar_close=D("96"),
        rules=rules(), session_ending=False,
    )
    assert d.exit_reason is AutotradeExitReason.TAKE_PROFIT


def test_a_shorts_trailing_stop_ratchets_down_and_never_back_up():
    r = rules(trailing_stop_pct=D("1"))
    # A wide structural stop on purpose: with a 2-point stop the 2R target
    # sits at 96 and the first bar below would exit on the target before the
    # trail was ever exercised.
    b = initial_bracket(
        entry_price=D("100"), structural_stop=D("104"), rules=r,
        direction=Direction.SHORT,
    )
    # Price falls to 95: the trail sits 1% above the lowest low.
    first = manage(
        b, bar_high=D("99"), bar_low=D("95"), bar_close=D("95.5"),
        rules=r, session_ending=False,
    )
    assert first.exit_reason is None
    assert first.state.stop_price == D("95") * D("1.01")

    # Price comes back up without touching the stop: the trough is
    # unchanged, so the stop must not loosen back toward 102.
    second = manage(
        first.state, bar_high=D("95.8"), bar_low=D("95.4"), bar_close=D("95.7"),
        rules=r, session_ending=False,
    )
    assert second.state.stop_price == first.state.stop_price


def test_a_long_is_untouched_by_all_of_this():
    b = initial_bracket(entry_price=D("100"), structural_stop=D("98"), rules=rules())
    assert b.direction is Direction.LONG
    assert b.stop_price == D("98")
    assert b.take_profit_price == D("104")
    d = manage(
        b, bar_high=D("101"), bar_low=D("97.9"), bar_close=D("98"),
        rules=rules(), session_ending=False,
    )
    assert d.exit_reason is AutotradeExitReason.STOP_LOSS


# --- paper broker ----------------------------------------------------------


def sell(symbol="X", qty="10") -> OrderRequest:
    return OrderRequest(symbol=symbol, side=Side.SELL, quantity=D(qty))


def test_shorting_stays_refused_unless_the_caller_asked_for_it():
    broker = PaperBrokerAdapter(starting_cash=D("10000"))
    with pytest.raises(InsufficientPositionError):
        broker.submit_order(sell(), market_price=D("50"))


def test_a_short_opens_a_negative_position_and_credits_the_proceeds():
    broker = PaperBrokerAdapter(starting_cash=D("10000"), allow_short=True)
    broker.submit_order(sell(qty="10"), market_price=D("50"))
    assert broker.positions["X"] == D("-10")
    assert broker.cash == D("10500")

    # Buying it back closes the position and pays for it.
    broker.submit_order(
        OrderRequest(symbol="X", side=Side.BUY, quantity=D("10")), market_price=D("45")
    )
    assert broker.positions["X"] == D("0")
    # 10,000 + 500 proceeds - 450 to buy back = 10,050: fifty dollars of
    # profit on ten shares falling five points.
    assert broker.cash == D("10050")


def test_a_short_is_collateralised_at_full_cash_and_refused_otherwise():
    broker = PaperBrokerAdapter(starting_cash=D("100"), allow_short=True)
    with pytest.raises(InsufficientFundsError) as err:
        broker.submit_order(sell(qty="10"), market_price=D("50"))
    assert "100% cash" in str(err.value)


def test_selling_what_is_held_does_not_count_as_a_short():
    # 10 held, selling 12: only the 2 that go past the holding need
    # collateral, not the whole 12.
    broker = PaperBrokerAdapter(
        starting_cash=D("101"), positions={"X": D("10")}, allow_short=True
    )
    broker.submit_order(sell(qty="12"), market_price=D("50"))
    assert broker.positions["X"] == D("-2")


def test_an_account_holding_a_short_values_it_as_a_liability():
    broker = PaperBrokerAdapter(starting_cash=D("10000"), allow_short=True)
    broker.submit_order(sell(qty="10"), market_price=D("50"))
    state = broker.get_account_state(marks={"X": D("60")})
    # Cash 10,500 less a 600 liability: the short moved against us.
    assert state.equity == D("9900")
    # Exposure is the absolute size of the position, not its signed value.
    assert state.current_exposure == D("600")


# --- the extended-hours bar ------------------------------------------------


def spec(market_type: str, min_score: int = 3, explicit: int | None = None):
    from apps.api.app.autotrade.service import BotSpec

    return BotSpec(
        name="b", broker_id=None, watchlist_id=None, symbols=["X"],  # type: ignore[arg-type]
        market_type=market_type, bar_interval="5m", max_trades_per_session=1,
        max_trades_per_day=1, capital_per_trade=D("100"), strategy_mode="auto",
        setups=[], min_score=min_score, stop_loss_mode="auto", stop_loss_max_pct=None,
        trailing_stop_pct=None, take_profit_mode="auto", take_profit_min_pct=None,
        trailing_take_profit_pct=None, news_blackout_minutes=0,
        extended_hours_min_score=explicit,
    )


def test_a_regular_only_bot_stores_no_extended_hours_bar():
    # The field would never be read for this bot, and a number nobody reads
    # is a number somebody eventually trusts.
    assert _extended_hours_bar(spec("regular")) is None


@pytest.mark.parametrize("market_type", ["auto", "pre_market", "post_market"])
def test_a_bot_that_may_trade_extended_hours_gets_a_stricter_default(market_type):
    assert _extended_hours_bar(spec(market_type, min_score=3)) == 3 + EXTENDED_HOURS_SCORE_PREMIUM


def test_the_operators_own_figure_always_wins_including_a_looser_one():
    assert _extended_hours_bar(spec("auto", min_score=3, explicit=1)) == 1


def test_the_default_cannot_exceed_the_scale_it_is_measured_on():
    # Scores run 0-10; min_score 9 plus the premium would be 11, a bar no
    # signal can clear, which would silently disable extended hours instead
    # of tightening it.
    assert _extended_hours_bar(spec("auto", min_score=9)) == 10
