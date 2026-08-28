"""Unit tests for apps/api/app/backtesting/engine.py's orchestration -
a fake HistoryProvider (no real Longbridge credentials needed) feeding a
small, fully hand-computed close series through the REAL Risk Engine
(evaluate_trade) and the REAL PaperBrokerAdapter fill math. Every expected
number in test_hand_computed_single_round_trip is derived by hand in the
comment above it, not just asserted from whatever the code produces.
"""

from datetime import date
from decimal import Decimal

import pytest

from apps.api.app.backtesting.engine import run_backtest
from apps.api.app.backtesting.errors import InsufficientHistoryError, UnsupportedDateRangeError
from apps.api.app.backtesting.models import BacktestRequest
from apps.api.app.risk.models import RiskLimits


class FakeHistoryProvider:
    name = "fake-history"

    def __init__(self, closes: list[Decimal]) -> None:
        self._closes = closes

    async def get_daily_closes(self, symbol: str, *, count: int) -> list[Decimal]:
        # Mirrors the real contract: "up to count, may return fewer."
        return self._closes[-count:] if count <= len(self._closes) else self._closes


def _risk_limits() -> RiskLimits:
    return RiskLimits(
        max_position_pct_of_equity=Decimal("0.10"),
        max_portfolio_exposure_pct_of_equity=Decimal("0.50"),
        max_risk_pct_of_equity_per_trade=Decimal("0.01"),
        require_stop_price=False,
        max_market_data_age_seconds=86_400,
        duplicate_order_window_seconds=5,
    )


@pytest.mark.asyncio
async def test_hand_computed_single_round_trip() -> None:
    # 20 flat warmup closes at 100 (SMA(20) settles at exactly 100), then
    # 3 requested-window closes: 110 (crosses above -> BUY), 120 (HOLD),
    # 90 (crosses below -> SELL). See test_strategy.py's identical
    # SMA-crossover derivation style for how these three signals are
    # derived from the SMA(20) sequence; verified in code review below:
    #
    #   SMA(20) at the last warmup day = 100.
    #   SMA(20) at day 1 (close=110) = (19*100 + 110)/20 = 100.5
    #     -> prev_close(100) <= prev_sma(100) and curr_close(110) >
    #        curr_sma(100.5) => BUY
    #   SMA(20) at day 2 (close=120) = (18*100 + 110 + 120)/20 = 101.5
    #     -> prev_close(110) >= prev_sma(100.5) but curr_close(120) is not
    #        < curr_sma(101.5) => HOLD
    #   SMA(20) at day 3 (close=90) = (17*100 + 110 + 120 + 90)/20 = 101
    #     -> prev_close(120) >= prev_sma(101.5) and curr_close(90) <
    #        curr_sma(101) => SELL
    #
    # Starting cash 10,000. On the BUY day, an all-cash proposal
    # (floor(10000/110)=90 shares, notional 9,900) exceeds
    # max_position_pct_of_equity (10% of 10,000 = 1,000 notional), so the
    # Risk Engine blocks it and reports max_quantity_allowed =
    # floor(1000/110) = 9. The engine retries once at 9 shares (notional
    # 990) - approved. Fill: cash 10000 - 990 = 9010, holding 9 shares.
    #   Day1 equity = 9010 + 9*110 = 10,000 (no P&L yet, just deployed).
    #   Day2 equity (HOLD) = 9010 + 9*120 = 10,090.
    # On the SELL day, all 9 held shares are proposed - well within every
    # risk limit - and approved. Fill: cash 9010 + 9*90 = 9,820, position
    # back to 0.
    #   Day3 equity = 9,820 + 0 = 9,820.
    # total_return_pct = (9820 - 10000) / 10000 * 100 = -1.8
    # One completed round trip: entry 110, exit 90 -> a loss -> win_rate 0%.
    # max_drawdown_pct: equity curve [10000, 10090, 9820], running peak
    # [10000, 10090, 10090] -> drawdowns [0, 0, (10090-9820)/10090*100].
    warmup = [Decimal(100)] * 20
    window = [Decimal(110), Decimal(120), Decimal(90)]
    history = FakeHistoryProvider(warmup + window)

    request = BacktestRequest(
        symbol="TEST",
        start_date=date(2026, 8, 24),
        end_date=date(2026, 8, 26),
        starting_cash=Decimal(10_000),
    )

    result = await run_backtest(
        request,
        history_provider=history,
        risk_limits=_risk_limits(),
        today=date(2026, 8, 26),
    )

    assert [p.equity for p in result.equity_curve] == [
        Decimal(10_000),
        Decimal(10_090),
        Decimal(9_820),
    ]
    assert result.final_equity == Decimal(9_820)
    assert result.total_return_pct == Decimal("-1.8")
    assert result.num_trades == 1
    assert result.win_rate_pct == Decimal(0)
    expected_drawdown = (Decimal(10_090) - Decimal(9_820)) / Decimal(10_090) * Decimal(100)
    assert result.max_drawdown_pct == expected_drawdown


@pytest.mark.asyncio
async def test_flat_series_produces_no_trades_and_zero_return() -> None:
    closes = [Decimal(100)] * 23
    history = FakeHistoryProvider(closes)
    request = BacktestRequest(
        symbol="FLAT",
        start_date=date(2026, 8, 24),
        end_date=date(2026, 8, 26),
        starting_cash=Decimal(5_000),
    )

    result = await run_backtest(
        request, history_provider=history, risk_limits=_risk_limits(), today=date(2026, 8, 26)
    )

    assert result.num_trades == 0
    assert result.win_rate_pct == Decimal(0)
    assert result.total_return_pct == Decimal(0)
    assert result.final_equity == Decimal(5_000)
    assert all(p.equity == Decimal(5_000) for p in result.equity_curve)


@pytest.mark.asyncio
async def test_insufficient_history_raises_typed_error_not_a_shorter_series() -> None:
    # Only 10 closes total; SMA(20) warmup alone needs 20.
    history = FakeHistoryProvider([Decimal(100)] * 10)
    request = BacktestRequest(
        symbol="THIN",
        start_date=date(2026, 8, 24),
        end_date=date(2026, 8, 26),
        starting_cash=Decimal(1_000),
    )

    with pytest.raises(InsufficientHistoryError):
        await run_backtest(
            request, history_provider=history, risk_limits=_risk_limits(), today=date(2026, 8, 26)
        )


@pytest.mark.asyncio
async def test_end_date_not_today_is_rejected() -> None:
    history = FakeHistoryProvider([Decimal(100)] * 30)
    request = BacktestRequest(
        symbol="OLD",
        start_date=date(2020, 1, 1),
        end_date=date(2020, 1, 5),
        starting_cash=Decimal(1_000),
    )

    with pytest.raises(UnsupportedDateRangeError):
        await run_backtest(
            request, history_provider=history, risk_limits=_risk_limits(), today=date(2026, 8, 26)
        )


@pytest.mark.asyncio
async def test_equity_curve_dates_span_only_the_requested_window() -> None:
    warmup = [Decimal(100)] * 20
    window = [Decimal(101), Decimal(102), Decimal(103)]
    history = FakeHistoryProvider(warmup + window)
    request = BacktestRequest(
        symbol="DATES",
        start_date=date(2026, 8, 24),
        end_date=date(2026, 8, 26),
        starting_cash=Decimal(1_000),
    )

    result = await run_backtest(
        request, history_provider=history, risk_limits=_risk_limits(), today=date(2026, 8, 26)
    )

    assert [p.date for p in result.equity_curve] == [
        date(2026, 8, 24),
        date(2026, 8, 25),
        date(2026, 8, 26),
    ]
