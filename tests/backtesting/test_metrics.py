"""Unit tests for the pure equity-curve math in
apps/api/app/backtesting/metrics.py - every expected value below is
computed by hand in the test, not just "runs without crashing"."""

from decimal import Decimal

from apps.api.app.backtesting.metrics import (
    RoundTrip,
    compute_max_drawdown_pct,
    compute_total_return_pct,
    compute_win_rate_pct,
)


def test_total_return_pct_positive() -> None:
    # (1200 - 1000) / 1000 * 100 = 20
    assert compute_total_return_pct(
        starting_cash=Decimal(1000), final_equity=Decimal(1200)
    ) == Decimal(20)


def test_total_return_pct_negative() -> None:
    # (800 - 1000) / 1000 * 100 = -20
    assert compute_total_return_pct(
        starting_cash=Decimal(1000), final_equity=Decimal(800)
    ) == Decimal(-20)


def test_total_return_pct_flat() -> None:
    assert compute_total_return_pct(
        starting_cash=Decimal(1000), final_equity=Decimal(1000)
    ) == Decimal(0)


def test_max_drawdown_pct_empty_curve() -> None:
    assert compute_max_drawdown_pct([]) == Decimal(0)


def test_max_drawdown_pct_monotonic_up_is_zero() -> None:
    assert compute_max_drawdown_pct([Decimal(100), Decimal(110), Decimal(120)]) == Decimal(0)


def test_max_drawdown_pct_hand_computed() -> None:
    # peak sequence: 1000, 1000, 1000, 1200, 1200
    # drawdown at each point: 0, (1000-750)/1000*100=25, (1000-900)/1000*100=10,
    #   0, (1200-600)/1200*100=50
    curve = [Decimal(1000), Decimal(750), Decimal(900), Decimal(1200), Decimal(600)]
    assert compute_max_drawdown_pct(curve) == Decimal(50)


def test_max_drawdown_pct_single_point() -> None:
    assert compute_max_drawdown_pct([Decimal(500)]) == Decimal(0)


def test_win_rate_pct_no_round_trips() -> None:
    assert compute_win_rate_pct([]) == Decimal(0)


def test_win_rate_pct_hand_computed() -> None:
    # 3 wins (exit > entry) out of 4 round trips = 75%
    round_trips = [
        RoundTrip(entry_price=Decimal(100), exit_price=Decimal(120)),  # win
        RoundTrip(entry_price=Decimal(100), exit_price=Decimal(90)),  # loss
        RoundTrip(entry_price=Decimal(50), exit_price=Decimal(60)),  # win
        RoundTrip(entry_price=Decimal(80), exit_price=Decimal(85)),  # win
    ]
    assert compute_win_rate_pct(round_trips) == Decimal(75)


def test_win_rate_pct_tie_counts_as_not_a_win() -> None:
    round_trips = [RoundTrip(entry_price=Decimal(100), exit_price=Decimal(100))]
    assert compute_win_rate_pct(round_trips) == Decimal(0)


def test_win_rate_pct_all_wins() -> None:
    round_trips = [
        RoundTrip(entry_price=Decimal(10), exit_price=Decimal(11)),
        RoundTrip(entry_price=Decimal(10), exit_price=Decimal(12)),
    ]
    assert compute_win_rate_pct(round_trips) == Decimal(100)
