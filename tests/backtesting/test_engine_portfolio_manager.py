"""The backtest engine's integration of the trade-path Portfolio Manager
(D035, closing the honest gap D029 recorded).

Mirrors tests/oms/test_service_portfolio_manager.py's structure for the
live path: the same two structural properties are pinned here, in the
backtest context - the Portfolio Manager only ever sees a proposal the Risk
Engine has already approved, and a quantity IT produced is re-gated by the
Risk Engine before any (simulated) fill.

Everything runs against a fake HistoryProvider (no vendor credentials
needed), the REAL Risk Engine, the REAL Portfolio Manager and the REAL
PaperBrokerAdapter fill math. Every expected number below is hand-derived
in the comment above it.
"""

from datetime import date
from decimal import Decimal

import pytest

from apps.api.app.backtesting import engine as engine_module
from apps.api.app.backtesting.engine import run_backtest
from apps.api.app.backtesting.models import BacktestRequest
from apps.api.app.portfolio_manager.models import PortfolioLimits
from apps.api.app.risk.models import RiskDecision, RiskLimits

# The same close series test_engine.py's hand-computed round trip uses:
# 20 flat warmup closes at 100 (SMA(20) settles at exactly 100), then
# 110 (crosses above -> BUY), 120 (HOLD), 90 (crosses below -> SELL).
WARMUP = [Decimal(100)] * 20
WINDOW = [Decimal(110), Decimal(120), Decimal(90)]
TODAY = date(2026, 8, 26)


class FakeHistoryProvider:
    name = "fake-history"

    def __init__(self, closes: list[Decimal]) -> None:
        self._closes = closes

    async def get_daily_closes(self, symbol: str, *, count: int) -> list[Decimal]:
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


def _request() -> BacktestRequest:
    return BacktestRequest(
        symbol="TEST",
        start_date=date(2026, 8, 24),
        end_date=TODAY,
        starting_cash=Decimal(10_000),
    )


async def _run(portfolio_limits: PortfolioLimits | None):
    return await run_backtest(
        _request(),
        history_provider=FakeHistoryProvider(WARMUP + WINDOW),
        risk_limits=_risk_limits(),
        portfolio_limits=portfolio_limits,
        today=TODAY,
    )


@pytest.mark.asyncio
async def test_loose_limits_reproduce_pre_change_behaviour_exactly() -> None:
    """Baseline. With limits loose enough that nothing can ever bind, the
    run must be byte-for-byte the pre-D035 run (no portfolio_limits at
    all): the Portfolio Manager is a gate, and an unbinding gate must be
    invisible in the numbers.

    The one trade in this series is 9 shares at 110 = 990 notional against
    10,000 equity, so with a 100% per-symbol cap, a 0% cash reserve and a
    20-position cap, no constraint can be breached by any leg.
    """
    loose = PortfolioLimits(
        max_symbol_pct_of_equity=Decimal(1),
        min_cash_reserve_pct_of_equity=Decimal(0),
        max_open_positions=20,
    )

    without = await _run(None)
    with_loose = await _run(loose)

    assert with_loose.model_dump() == without.model_dump()
    # ...and it is the same run test_engine.py hand-derived: one round
    # trip, entry 110, exit 90, final equity 9,820.
    assert with_loose.final_equity == Decimal(9_820)
    assert with_loose.num_trades == 1
    assert with_loose.portfolio_modified_trades == 0
    assert with_loose.portfolio_modify_risk_blocked_trades == 0
    assert with_loose.portfolio_rejected_trades == 0


@pytest.mark.asyncio
async def test_a_modified_quantity_is_what_actually_gets_simulated_filled() -> None:
    """A 5% per-symbol cap binds and the modified quantity - not the
    risk-approved one - is what the paper broker actually fills.

    Hand-derived: the BUY day's all-cash proposal is floor(10000/110) = 90
    shares (9,900 notional), which the Risk Engine blocks on
    EXCEEDS_MAX_POSITION_SIZE (10% of 10,000 = 1,000) reporting
    max_quantity_allowed = floor(1000/110) = 9. The engine retries at 9
    (990 notional) and the Risk Engine approves. The Portfolio Manager then
    sees a 5%-of-equity symbol cap = 500, held value 0, so headroom 500 ->
    floor(500/110) = 4 shares: MODIFY 9 -> 4. Re-gated at 4 (440 notional,
    inside every risk limit) and filled.
      Fill: cash 10,000 - 440 = 9,560, holding 4.
      Day1 equity = 9,560 + 4*110 = 10,000
      Day2 equity = 9,560 + 4*120 = 10,040
    The SELL day proposes all 4 held shares; the Portfolio Manager cannot
    block a sell (it lowers the symbol's value and raises cash), so it
    fills: cash 9,560 + 4*90 = 9,920.
      Day3 equity = 9,920
    total_return_pct = (9920 - 10000)/10000*100 = -0.8
    """
    result = await _run(
        PortfolioLimits(
            max_symbol_pct_of_equity=Decimal("0.05"),
            min_cash_reserve_pct_of_equity=Decimal(0),
            max_open_positions=20,
        )
    )

    assert [p.equity for p in result.equity_curve] == [
        Decimal(10_000),
        Decimal(10_040),
        Decimal(9_920),
    ]
    assert result.final_equity == Decimal(9_920)
    assert result.total_return_pct == Decimal("-0.8")
    assert result.num_trades == 1
    assert result.portfolio_modified_trades == 1
    assert result.portfolio_modify_risk_blocked_trades == 0
    assert result.portfolio_rejected_trades == 0


@pytest.mark.asyncio
async def test_a_portfolio_rejection_blocks_the_fill_and_the_run_continues() -> None:
    """A 99% cash reserve makes any buy impossible to size into compliance,
    so the Portfolio Manager REJECTS rather than modifies (no quantity >= 1
    satisfies it: headroom is 10,000 - 9,900 = 100, floor(100/110) = 0).

    The Risk Engine approved that same 9-share proposal, so this rejection
    is unambiguously the portfolio's. Nothing fills, the position stays
    flat, the later SELL signal finds nothing to sell, and the run
    completes normally with a flat equity curve rather than aborting.
    """
    result = await _run(
        PortfolioLimits(
            max_symbol_pct_of_equity=Decimal(1),
            min_cash_reserve_pct_of_equity=Decimal("0.99"),
            max_open_positions=20,
        )
    )

    assert [p.equity for p in result.equity_curve] == [Decimal(10_000)] * 3
    assert result.final_equity == Decimal(10_000)
    assert result.total_return_pct == Decimal(0)
    assert result.num_trades == 0
    assert result.portfolio_rejected_trades == 1
    assert result.portfolio_modified_trades == 0
    assert result.portfolio_modify_risk_blocked_trades == 0


@pytest.mark.asyncio
async def test_a_modified_quantity_is_re_evaluated_by_the_risk_engine(monkeypatch) -> None:
    """The load-bearing safety invariant, in the backtest context: the
    quantity the Portfolio Manager produced is itself passed through
    evaluate_trade() before any fill.

    Unlike the live path's equivalent test, this cannot be demonstrated
    with a genuinely non-monotone risk check: the only such check is D024's
    duplicate-order rule, and the backtest engine passes no `recent_orders`
    (it has no order history to pass - nothing is persisted, D025). So the
    re-gate is observed directly instead, by wrapping the real
    evaluate_trade the engine calls and recording every quantity it was
    asked about. The real engine still decides every call; only the
    observation is added.
    """
    seen: list[Decimal] = []
    real_evaluate = engine_module.evaluate_trade

    def spy(proposal, account, limits, **kwargs):
        seen.append(proposal.quantity)
        return real_evaluate(proposal, account, limits, **kwargs)

    monkeypatch.setattr(engine_module, "evaluate_trade", spy)

    result = await _run(
        PortfolioLimits(
            max_symbol_pct_of_equity=Decimal("0.05"),
            min_cash_reserve_pct_of_equity=Decimal(0),
            max_open_positions=20,
        )
    )

    # BUY day: 90 (risk-blocked), 9 (risk-approved), then 4 - the
    # Portfolio Manager's own output, re-gated. Then the SELL day's 4.
    assert seen == [Decimal(90), Decimal(9), Decimal(4), Decimal(4)]
    assert result.portfolio_modified_trades == 1
    assert result.num_trades == 1


@pytest.mark.asyncio
async def test_a_modified_quantity_the_re_gate_rejects_never_fills(monkeypatch) -> None:
    """The other half of the same invariant: if the mandatory re-gate
    rejects the resized quantity, nothing reaches the (simulated) broker at
    all - the resize is not waved through on the strength of the first
    approval.

    The real Risk Engine cannot reject 4 shares here (every check it
    applies in a backtest is monotone in quantity), so the rejection is
    injected: the real engine answers every call except the one for the
    Portfolio Manager's modified quantity of 4.
    """
    real_evaluate = engine_module.evaluate_trade

    def reject_the_resize(proposal, account, limits, **kwargs):
        if proposal.quantity == Decimal(4):
            return RiskDecision(approved=False, reason=None, detail="injected re-gate rejection")
        return real_evaluate(proposal, account, limits, **kwargs)

    monkeypatch.setattr(engine_module, "evaluate_trade", reject_the_resize)

    result = await _run(
        PortfolioLimits(
            max_symbol_pct_of_equity=Decimal("0.05"),
            min_cash_reserve_pct_of_equity=Decimal(0),
            max_open_positions=20,
        )
    )

    assert result.num_trades == 0
    assert [p.equity for p in result.equity_curve] == [Decimal(10_000)] * 3
    assert result.portfolio_modified_trades == 1
    assert result.portfolio_modify_risk_blocked_trades == 1
    assert result.portfolio_rejected_trades == 0
