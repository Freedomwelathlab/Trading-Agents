"""The transaction-cost model (Phase 70, D088).

The central property asserted here is the IDENTITY the module's docstring
claims: folding a fee and a slippage allowance into one adjusted execution
price produces exactly the cash outcome that slipping the price and then
deducting the fee separately would. Everything else in `engine_v2` rests on
that being true rather than approximately true, because the separately
reported `total_fees` / `total_slippage` figures are computed one way while
the equity curve is produced the other.
"""

from decimal import Decimal

import pytest

from apps.api.app.backtesting.costs import CostModel


def _model(fee: str = "10", slippage: str = "5") -> CostModel:
    return CostModel(fee_bps=Decimal(fee), slippage_bps=Decimal(slippage))


# -------------------------------------------------------------- direction


def test_a_buy_fills_above_the_close_and_a_sell_below_it() -> None:
    # The whole point: costs are ADVERSE in both directions. A model that
    # got the sign right on one side and wrong on the other would make
    # round trips look profitable purely from trading.
    model = _model()
    mid = Decimal(100)
    assert model.buy_price(mid) > mid
    assert model.sell_price(mid) < mid


def test_the_adjustment_is_the_sum_of_both_rates() -> None:
    # 10 + 5 = 15 bps = 0.15%. 100 * 1.0015 = 100.15, 100 * 0.9985 = 99.85.
    model = _model()
    assert model.buy_price(Decimal(100)) == Decimal("100.15")
    assert model.sell_price(Decimal(100)) == Decimal("99.85")


def test_a_frictionless_model_changes_no_price() -> None:
    # This is what makes CostModel.frictionless() a faithful reproduction of
    # the engine's pre-Phase-70 behaviour, which every pre-existing
    # engine_v2 test relies on to still be describing the same engine.
    model = CostModel.frictionless()
    assert model.buy_price(Decimal("123.45")) == Decimal("123.45")
    assert model.sell_price(Decimal("123.45")) == Decimal("123.45")
    assert model.costs_for(quantity=Decimal(10), mid=Decimal(100)) == (
        Decimal(0),
        Decimal(0),
    )


# -------------------------------------------------------------- identity


@pytest.mark.parametrize(
    ("fee", "slippage", "quantity", "mid"),
    [
        ("10", "5", "10", "100"),
        ("0", "25", "3", "41234.56"),  # slippage only, BTC-scale price
        ("7.5", "0", "1", "0.01"),     # fee only, sub-cent price
        ("2.5", "2.5", "1000", "19.99"),
        ("0", "0", "7", "50"),
    ],
)
def test_the_price_adjustment_equals_the_reported_fee_plus_slippage(
    fee: str, slippage: str, quantity: str, mid: str
) -> None:
    """The identity the module rests on, over a spread of realistic scales.

    `engine_v2` produces its equity curve by filling at the adjusted price,
    but reports `total_fees` / `total_slippage` from `costs_for`. If these
    two disagreed by even a rounding step, the reported costs would not add
    up to the return the run actually produced - and a reader reconciling
    them would be chasing a discrepancy in the tooling rather than in the
    strategy. Decimal arithmetic makes exact equality the right assertion
    here; a tolerance would hide precisely the drift this guards against.
    """
    model = CostModel(fee_bps=Decimal(fee), slippage_bps=Decimal(slippage))
    qty, price = Decimal(quantity), Decimal(mid)
    charged_fee, charged_slippage = model.costs_for(quantity=qty, mid=price)

    mid_notional = qty * price
    assert qty * model.buy_price(price) == mid_notional + charged_fee + charged_slippage
    assert qty * model.sell_price(price) == mid_notional - charged_fee - charged_slippage


def test_fees_and_slippage_are_reported_separately_not_collapsed() -> None:
    # Interchangeable in the arithmetic, not in what an operator can do
    # about them - a fee is a venue's schedule, slippage is liquidity.
    fee, slippage = _model(fee="10", slippage="5").costs_for(
        quantity=Decimal(10), mid=Decimal(100)
    )
    assert fee == Decimal(1)          # 1000 notional * 10bps
    assert slippage == Decimal("0.5")  # 1000 notional * 5bps
    assert fee != slippage


def test_costs_are_computed_off_the_unadjusted_close() -> None:
    # Charging the fee off the already-slipped price would fold a sliver of
    # slippage into the fee and break the identity above. 1000 * 10bps = 1
    # exactly, not 1.0005.
    fee, _ = _model(fee="10", slippage="5").costs_for(
        quantity=Decimal(10), mid=Decimal(100)
    )
    assert fee == Decimal(1)


# ------------------------------------------------------------ refusals


def test_a_negative_rate_is_refused_at_construction() -> None:
    # A negative cost is a rebate, which this engine does not model. Caught
    # at construction rather than producing a backtest that quietly PAYS the
    # strategy to trade - the single most flattering bug this module could
    # have.
    with pytest.raises(ValueError, match="must not be negative"):
        CostModel(fee_bps=Decimal("-1"), slippage_bps=Decimal(0))
    with pytest.raises(ValueError, match="must not be negative"):
        CostModel(fee_bps=Decimal(0), slippage_bps=Decimal("-0.01"))


def test_a_sell_price_is_floored_at_zero_rather_than_going_negative() -> None:
    # Degenerate - nothing realistic reaches a 10,000bps cost - but a
    # negative fill price would propagate nonsense into equity rather than
    # failing anywhere visible.
    model = CostModel(fee_bps=Decimal(20_000), slippage_bps=Decimal(0))
    assert model.sell_price(Decimal(100)) == Decimal(0)


def test_the_model_is_frozen() -> None:
    # The two rates must not change partway through a replay, or the equity
    # curve would describe no single set of assumptions at all.
    model = _model()
    with pytest.raises(Exception):  # noqa: B017 - dataclasses.FrozenInstanceError
        model.fee_bps = Decimal(0)  # type: ignore[misc]
