"""Pure unit tests for the cost-basis fill replay (replay_symbol_fills) -
no DB, hand-verified scenarios. See apps/api/app/portfolio/snapshot.py's
module docstring for each method.

The first block is the D022 average-cost suite, unchanged: it doubles as
the backward-compatibility guard for Phase 34 (D041), since every one of
those tests still calls replay_symbol_fills() with no `method` argument.
The second block covers the FIFO/LIFO lot tracking D041 added.
"""

from decimal import Decimal

from apps.api.app.portfolio.models import CostBasisMethod
from apps.api.app.portfolio.snapshot import (
    replay_symbol_fills,
    replay_symbol_fills_average,
)
from apps.api.app.risk.models import Side


def test_a_single_buy_sets_avg_cost_and_no_realized_pnl():
    avg_cost, realized_pnl = replay_symbol_fills(
        [(Side.BUY, Decimal(10), Decimal(100))]
    )
    assert avg_cost == Decimal(100)
    assert realized_pnl == Decimal(0)


def test_two_buys_at_different_prices_average_the_cost():
    # 10 @ 100 then 10 @ 110 -> (1000 + 1100) / 20 = 105.
    avg_cost, realized_pnl = replay_symbol_fills(
        [
            (Side.BUY, Decimal(10), Decimal(100)),
            (Side.BUY, Decimal(10), Decimal(110)),
        ]
    )
    assert avg_cost == Decimal(105)
    assert realized_pnl == Decimal(0)


def test_a_partial_sell_realizes_pnl_against_avg_cost_without_changing_it():
    # 10 @ 50, sell 4 @ 60 -> realized (60-50)*4 = 40; avg_cost unchanged
    # under average-cost basis (not FIFO/LIFO).
    avg_cost, realized_pnl = replay_symbol_fills(
        [
            (Side.BUY, Decimal(10), Decimal(50)),
            (Side.SELL, Decimal(4), Decimal(60)),
        ]
    )
    assert avg_cost == Decimal(50)
    assert realized_pnl == Decimal(40)


def test_a_losing_sell_produces_negative_realized_pnl():
    avg_cost, realized_pnl = replay_symbol_fills(
        [
            (Side.BUY, Decimal(10), Decimal(50)),
            (Side.SELL, Decimal(10), Decimal(40)),
        ]
    )
    assert avg_cost == Decimal(50)
    assert realized_pnl == Decimal(-100)


def test_a_full_hand_verified_scenario_across_buys_and_sells():
    # Buy 10 @ 100 -> avg 100, held 10
    # Buy 10 @ 110 -> avg (1000+1100)/20 = 105, held 20
    # Sell 5 @ 120 -> realized += (120-105)*5 = 75, held 15
    # Sell 15 @ 90 -> realized += (90-105)*15 = -225, held 0
    # Total realized = 75 - 225 = -150; avg_cost stays 105 (last buy avg,
    # unchanged by sells).
    avg_cost, realized_pnl = replay_symbol_fills(
        [
            (Side.BUY, Decimal(10), Decimal(100)),
            (Side.BUY, Decimal(10), Decimal(110)),
            (Side.SELL, Decimal(5), Decimal(120)),
            (Side.SELL, Decimal(15), Decimal(90)),
        ]
    )
    assert avg_cost == Decimal(105)
    assert realized_pnl == Decimal(-150)


def test_no_fills_gives_zero_avg_cost_and_zero_realized_pnl():
    avg_cost, realized_pnl = replay_symbol_fills([])
    assert avg_cost == Decimal(0)
    assert realized_pnl == Decimal(0)


# --- Phase 34 / D041: FIFO and LIFO cost-basis methods -------------------
#
# Every scenario below is hand-verified in its comment. The headline case
# is test_the_three_methods_disagree_on_the_same_history, which is the
# whole justification for offering the choice: one fill history, three
# different-but-individually-correct realized-P&L figures.

MULTI_LOT_HISTORY = [
    (Side.BUY, Decimal(10), Decimal(100)),
    (Side.BUY, Decimal(10), Decimal(110)),
    (Side.SELL, Decimal(15), Decimal(120)),
]
"""Buy 10 @ 100, buy 10 @ 110, sell 15 @ 120. Chosen because the sell
straddles both lots, so which lot it consumes first actually changes the
answer - a sell contained inside a single lot would let all three methods
agree and would prove nothing."""


def test_the_default_method_is_average_and_matches_the_bare_call():
    # The backward-compatibility contract: calling with no `method` must be
    # identical to the pre-D041 function, and to asking for AVERAGE.
    assert replay_symbol_fills(MULTI_LOT_HISTORY) == replay_symbol_fills(
        MULTI_LOT_HISTORY, method=CostBasisMethod.AVERAGE
    )
    assert replay_symbol_fills(MULTI_LOT_HISTORY) == replay_symbol_fills_average(
        MULTI_LOT_HISTORY
    )


def test_the_three_methods_disagree_on_the_same_history():
    # AVERAGE: avg = (10*100 + 10*110) / 20 = 105.
    #          realized = (120 - 105) * 15 = 225.
    #          basis stays 105 (sells never move an average basis).
    # FIFO:    the sell of 15 consumes the 10 @ 100 lot, then 5 of the 110.
    #          realized = (120-100)*10 + (120-110)*5 = 200 + 50 = 250.
    #          open lots left: 5 @ 110 -> basis 110.
    # LIFO:    the sell of 15 consumes the 10 @ 110 lot, then 5 of the 100.
    #          realized = (120-110)*10 + (120-100)*5 = 100 + 100 = 200.
    #          open lots left: 5 @ 100 -> basis 100.
    average = replay_symbol_fills(MULTI_LOT_HISTORY, method=CostBasisMethod.AVERAGE)
    fifo = replay_symbol_fills(MULTI_LOT_HISTORY, method=CostBasisMethod.FIFO)
    lifo = replay_symbol_fills(MULTI_LOT_HISTORY, method=CostBasisMethod.LIFO)

    assert average == (Decimal(105), Decimal(225))
    assert fifo == (Decimal(110), Decimal(250))
    assert lifo == (Decimal(100), Decimal(200))

    # Three genuinely distinct realized-P&L figures, not three aliases.
    assert len({average[1], fifo[1], lifo[1]}) == 3


def test_fifo_and_lifo_agree_when_only_one_lot_is_ever_open():
    # With a single lot there is no ordering choice to make, so FIFO and
    # LIFO must coincide - and here they also coincide with AVERAGE, since
    # one lot's price *is* the average. Buy 10 @ 50, sell 4 @ 60 ->
    # realized (60-50)*4 = 40; 6 @ 50 still open -> basis 50.
    fills = [(Side.BUY, Decimal(10), Decimal(50)), (Side.SELL, Decimal(4), Decimal(60))]
    for method in CostBasisMethod:
        assert replay_symbol_fills(fills, method=method) == (
            Decimal(50),
            Decimal(40),
        ), method


def test_fifo_and_lifo_realize_the_same_total_once_every_lot_is_closed():
    # Ordering only decides *when* each lot's gain lands, never the total.
    # Fully closing the book must therefore reconcile all three methods:
    # bought 20 for 1000 + 1100 = 2100, sold 20 for 15*120 + 5*90 = 2250,
    # so total realized is 150 whichever order the lots were consumed in.
    fills = [
        (Side.BUY, Decimal(10), Decimal(100)),
        (Side.BUY, Decimal(10), Decimal(110)),
        (Side.SELL, Decimal(15), Decimal(120)),
        (Side.SELL, Decimal(5), Decimal(90)),
    ]
    for method in CostBasisMethod:
        _basis, realized = replay_symbol_fills(fills, method=method)
        assert realized == Decimal(150), method


def test_a_closed_out_position_reports_no_lot_basis_under_fifo_and_lifo():
    # Flat means no held quantity, so there is no basis to report - unlike
    # AVERAGE, which carries its last running average (105) forward. The
    # divergence is deliberate; see replay_symbol_fills_lots' docstring.
    fills = [
        (Side.BUY, Decimal(10), Decimal(100)),
        (Side.BUY, Decimal(10), Decimal(110)),
        (Side.SELL, Decimal(20), Decimal(120)),
    ]
    assert replay_symbol_fills(fills, method=CostBasisMethod.FIFO)[0] == Decimal(0)
    assert replay_symbol_fills(fills, method=CostBasisMethod.LIFO)[0] == Decimal(0)
    assert replay_symbol_fills(fills, method=CostBasisMethod.AVERAGE)[0] == Decimal(105)


def test_three_lots_consumed_in_opposite_orders():
    # Buy 5 @ 10, buy 5 @ 20, buy 5 @ 30, then sell 10 @ 40.
    # FIFO: (40-10)*5 + (40-20)*5 = 150 + 100 = 250; 5 @ 30 left -> 30.
    # LIFO: (40-30)*5 + (40-20)*5 = 50 + 100 = 150; 5 @ 10 left -> 10.
    # AVERAGE: avg 20 -> (40-20)*10 = 200; basis stays 20.
    fills = [
        (Side.BUY, Decimal(5), Decimal(10)),
        (Side.BUY, Decimal(5), Decimal(20)),
        (Side.BUY, Decimal(5), Decimal(30)),
        (Side.SELL, Decimal(10), Decimal(40)),
    ]
    assert replay_symbol_fills(fills, method=CostBasisMethod.FIFO) == (
        Decimal(30),
        Decimal(250),
    )
    assert replay_symbol_fills(fills, method=CostBasisMethod.LIFO) == (
        Decimal(10),
        Decimal(150),
    )
    assert replay_symbol_fills(fills, method=CostBasisMethod.AVERAGE) == (
        Decimal(20),
        Decimal(200),
    )


def test_a_partially_consumed_lot_keeps_its_own_price_for_the_remainder():
    # FIFO: buy 10 @ 100, buy 10 @ 110, sell 5 @ 130 eats half the first
    # lot -> realized (130-100)*5 = 150. Remaining lots: 5 @ 100 and
    # 10 @ 110 -> basis (500 + 1100) / 15 = 1600/15.
    fills = [
        (Side.BUY, Decimal(10), Decimal(100)),
        (Side.BUY, Decimal(10), Decimal(110)),
        (Side.SELL, Decimal(5), Decimal(130)),
    ]
    basis, realized = replay_symbol_fills(fills, method=CostBasisMethod.FIFO)
    assert realized == Decimal(150)
    assert basis == Decimal(1600) / Decimal(15)


def test_selling_past_the_open_lots_opens_a_short_rather_than_inventing_a_basis():
    # Buy 5 @ 100, sell 8 @ 120. Only 5 shares were ever bought, so only
    # those 5 can be realized: (120-100)*5 = 100. The extra 3 open a short
    # lot at 120 - never realized against a fabricated zero cost.
    # Buying 3 back @ 90 then realizes (120-90)*3 = 90, total 190, flat.
    fills = [(Side.BUY, Decimal(5), Decimal(100)), (Side.SELL, Decimal(8), Decimal(120))]
    for method in (CostBasisMethod.FIFO, CostBasisMethod.LIFO):
        basis, realized = replay_symbol_fills(fills, method=method)
        assert realized == Decimal(100), method
        assert basis == Decimal(120), method  # the open short lot's own price

    covered = [*fills, (Side.BUY, Decimal(3), Decimal(90))]
    for method in (CostBasisMethod.FIFO, CostBasisMethod.LIFO):
        basis, realized = replay_symbol_fills(covered, method=method)
        assert realized == Decimal(190), method
        assert basis == Decimal(0), method  # flat again


def test_no_fills_is_zero_under_every_method():
    for method in CostBasisMethod:
        assert replay_symbol_fills([], method=method) == (Decimal(0), Decimal(0)), method


def test_buys_only_give_the_same_basis_under_every_method():
    # With no sells there is nothing to order, so all three must agree:
    # (10*100 + 10*110) / 20 = 105, and no realized P&L.
    fills = [
        (Side.BUY, Decimal(10), Decimal(100)),
        (Side.BUY, Decimal(10), Decimal(110)),
    ]
    for method in CostBasisMethod:
        assert replay_symbol_fills(fills, method=method) == (
            Decimal(105),
            Decimal(0),
        ), method
