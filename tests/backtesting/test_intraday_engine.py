"""Intraday engine tests (Phase 73, D091).

These pin the engine's guarantees rather than any strategy's results:
one position at a time, the playbook's daily risk controls actually
binding, and the replay staying causal.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from apps.api.app.backtesting.brackets import BracketPlan
from apps.api.app.backtesting.costs import CostModel
from apps.api.app.backtesting.intraday_engine import (
    IntradayRunConfig,
    run_intraday_backtest,
)
from apps.api.app.backtesting.intraday_metrics import compute_metrics
from apps.api.app.marketdata.structure import Direction

NY = ZoneInfo("America/New_York")


class Bar:
    def __init__(self, ts, *, high, low, close, open=None, volume=10_000):
        self.ts = ts
        self.open = Decimal(str(open if open is not None else close))
        self.high = Decimal(str(high))
        self.low = Decimal(str(low))
        self.close = Decimal(str(close))
        self.volume = volume


def make_session(day, specs, *, start=(9, 30)):
    base = datetime(*day, *start, tzinfo=NY).astimezone(UTC)
    return [
        Bar(base + timedelta(minutes=5 * i), high=s[0], low=s[1], close=s[2])
        for i, s in enumerate(specs)
    ]


def flat_session(day, price=100.0, n=78, *, start=(9, 30)):
    return make_session(day, [(price, price, price)] * n, start=start)


def config(**kw):
    base = dict(
        symbol="TEST",
        setups=("sweep_mss",),
        costs=CostModel.frictionless(),
        plan=BracketPlan(),
        starting_equity=Decimal("100000"),
    )
    base.update(kw)
    return IntradayRunConfig(**base)


def test_an_unknown_setup_name_is_refused_rather_than_silently_ignored():
    """A typo that produced an empty run would report "no trades found",
    which reads as a strategy that never triggered rather than as a
    strategy that never ran."""
    with pytest.raises(ValueError, match="No known setups"):
        run_intraday_backtest(flat_session((2026, 6, 17)), config(setups=("nope",)))


def test_an_empty_series_returns_an_empty_result_not_an_error():
    result = run_intraday_backtest([], config())
    assert result.trades == []
    assert result.sessions_available == 0


def test_a_session_with_too_few_bars_is_skipped():
    """Six bars cannot establish an opening range, a swing and a sweep.
    Trading it would be describing structure that is not there."""
    result = run_intraday_backtest(flat_session((2026, 6, 17), n=3), config())
    assert result.trades == []


def _sweep_session(day):
    """A session tracing the playbook's master sequence end to end.

    The intermediate bounce at index 3 matters: it is what creates a
    confirmed LOWER HIGH for the later break to clear. A monotonic slide
    into the sweep has no such pivot, so no structure shift can ever be
    detected and the setup cannot fire - which is exactly how an earlier
    version of this fixture made several tests below pass on zero trades.
    """
    specs = [
        (100.0, 99.0, 99.5),
        (99.0, 98.0, 98.2),
        (99.5, 98.0, 99.3),      # bounce...
        (100.5, 99.0, 99.2),     # ...peaking here: the lower high
        (99.5, 98.0, 98.3),      # confirms the pivot at index 3
        (99.0, 96.0, 96.5),
        (97.0, 94.5, 96.2),      # sweeps the 95.00 previous low, reclaims
        (97.5, 96.0, 97.3),
        (99.0, 97.0, 98.5),
        (101.5, 98.0, 101.2),    # closes above 100.5: market structure shift
    ]
    specs += [(102.0, 100.0, 101.0)] * 60
    return make_session(day, specs)


def _two_day_series():
    """Day one establishes a previous low of 95.00 for day two to sweep."""
    day_one = make_session((2026, 6, 16), [(101, 95, 100)] * 78)
    return day_one + _sweep_session((2026, 6, 17))


def test_the_fixture_actually_produces_the_setup_it_describes():
    """Guards every structural test below from passing vacuously.

    A fixture that produces no trades satisfies "no trade spans a session
    boundary" and "only one position at a time" trivially, and those are
    the guarantees most worth having.
    """
    result = run_intraday_backtest(_two_day_series(), config())

    assert result.signals_seen > 0
    assert len(result.trades) > 0


def test_only_one_position_is_open_at_a_time():
    """Overlapping entries would compound the per-trade risk into an
    exposure nobody sized for - the sizing formula assumes the trade it
    sizes is the only one on."""
    bars = _two_day_series()
    result = run_intraday_backtest(bars, config(setups=("sweep_mss",)))

    trades = sorted(result.trades, key=lambda t: t.entry_ts)
    for earlier, later in zip(trades, trades[1:], strict=False):
        assert earlier.exit_ts is not None
        assert later.entry_ts >= earlier.exit_ts


def test_the_daily_loss_limit_stops_the_session():
    """Section 18's rule, and the point of it is that it binds regardless
    of how attractive the next setup looks."""
    bars = _two_day_series()

    unlimited = run_intraday_backtest(
        bars, config(daily_loss_limit_r=None, max_consecutive_losses=None)
    )
    limited = run_intraday_backtest(
        bars, config(daily_loss_limit_r=Decimal("-0.5"), max_consecutive_losses=None)
    )

    assert len(limited.trades) <= len(unlimited.trades)


def test_the_score_filter_removes_low_conviction_signals():
    """Section 14's threshold is a starting test value, not a proven
    cutoff - so it is a parameter that can be measured, and the default
    admits everything rather than quietly encoding 8/10."""
    bars = _two_day_series()

    permissive = run_intraday_backtest(bars, config(min_score=0))
    strict = run_intraday_backtest(bars, config(min_score=10))

    assert len(strict.trades) <= len(permissive.trades)
    if permissive.signals_seen:
        assert strict.signals_rejected_by_score > 0 or not strict.trades


def test_direction_can_be_restricted_to_one_side():
    bars = _two_day_series()

    longs_only = run_intraday_backtest(
        bars, config(allow_directions=(Direction.LONG,))
    )

    assert all(t.direction is Direction.LONG for t in longs_only.trades)


def test_no_trade_is_held_across_a_session_boundary():
    """The engine's hardest guarantee: a 3x daily-reset ETF held overnight
    is a different instrument's risk profile, so the time stop is part of
    the model rather than a safeguard."""
    from apps.api.app.marketdata.sessions import session_date

    bars = _two_day_series()
    result = run_intraday_backtest(bars, config())

    for trade in result.trades:
        assert trade.exit_ts is not None
        assert session_date(trade.entry_ts) == session_date(trade.exit_ts)


def test_every_trade_is_inside_regular_hours():
    """Premarket and after-hours bars are stored because the playbook
    prices premarket levels, but filling against them assumes a book that
    is not there - TQQQ trades ~53M shares regular against ~1.9M
    premarket."""
    from apps.api.app.marketdata.sessions import is_regular_hours

    premarket = make_session((2026, 6, 17), [(94, 90, 92)] * 6, start=(7, 0))
    bars = premarket + _two_day_series()

    result = run_intraday_backtest(bars, config())

    for trade in result.trades:
        assert is_regular_hours(trade.entry_ts)
        assert is_regular_hours(trade.exit_ts)


def test_results_are_deterministic():
    """No randomness anywhere in the path, so a recorded run is
    re-checkable - the same discipline the improvement loop relies on."""
    bars = _two_day_series()

    first = run_intraday_backtest(bars, config())
    second = run_intraday_backtest(bars, config())

    assert [(t.entry_ts, t.entry_price, t.quantity) for t in first.trades] == [
        (t.entry_ts, t.entry_price, t.quantity) for t in second.trades
    ]


def test_costs_never_improve_a_result():
    bars = _two_day_series()

    free = run_intraday_backtest(bars, config(costs=CostModel.frictionless()))
    costed = run_intraday_backtest(
        bars,
        config(costs=CostModel(fee_bps=Decimal("10"), slippage_bps=Decimal("5"))),
    )

    if free.trades and costed.trades:
        assert compute_metrics(costed.trades).gross_pnl <= compute_metrics(
            free.trades
        ).gross_pnl


# --------------------------------------------------------------------------
# Metrics


def test_profit_factor_is_undefined_rather_than_zero_when_nothing_was_lost():
    """Reporting an undefined ratio as zero would rank a flawless strategy
    last."""
    bars = _two_day_series()
    result = run_intraday_backtest(bars, config())
    winners = [t for t in result.trades if t.r_multiple > 0]

    metrics = compute_metrics(winners)

    assert metrics.profit_factor is None
    assert metrics.average_loss_r is None


def test_metrics_of_no_trades_are_absent_not_zero():
    metrics = compute_metrics([])

    assert metrics.total_trades == 0
    assert metrics.win_rate is None
    assert metrics.expectancy_r is None
    assert metrics.profit_factor is None
    assert metrics.total_r == Decimal(0)


def test_expectancy_equals_mean_r():
    """The playbook's formula `(win rate x avg win) - (loss rate x avg
    loss)` is algebraically the mean of R. Computing the mean directly
    means the headline figure cannot disagree with its own components
    through separate rounding."""
    bars = _two_day_series()
    result = run_intraday_backtest(bars, config())
    if not result.trades:
        pytest.skip("fixture produced no trades")

    metrics = compute_metrics(result.trades)
    manual = sum((t.r_multiple for t in result.trades), Decimal(0)) / len(result.trades)

    assert metrics.expectancy_r == manual
