"""Pure unit tests for the average-cost-basis fill replay
(replay_symbol_fills) - no DB, hand-verified scenarios. See
apps/api/app/portfolio/snapshot.py's module docstring for the method.
"""

from decimal import Decimal

from apps.api.app.portfolio.snapshot import replay_symbol_fills
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
