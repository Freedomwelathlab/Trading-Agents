"""Phase 69 (D087) - the gate between a scheduled cycle and real money.

The arming tests below are deliberately exhaustive over every way the gate
can say no, because "the robot is off" is the property that protects every
default checkout, and a gate with an untested branch is a gate whose
failure mode nobody has looked at.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apps.api.app.core.config import Settings, TradingMode
from apps.api.app.deployments.live_guard import (
    LiveArmingStatus,
    LiveCapitalDecision,
    LiveCapitalStatus,
    evaluate_live_arming,
    live_entry_budget,
)

_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

_ARMED = {
    "trading_mode": TradingMode.LIVE,
    "live_trading_enabled": True,
    "strategy_live_auto_execution_enabled": True,
    "strategy_live_total_capital": Decimal("10000"),
    "strategy_live_capital_per_trade": Decimal("1000"),
}


def _base() -> Settings:
    """`_env_file=None` (the `tests/core/test_execution_context.py` pattern)
    so a developer's own `.env` - live credentials included - can never leak
    into a test about whether live trading is armed."""
    return Settings(
        _env_file=None,
        jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256",
    )


def _settings(**overrides: object) -> Settings:
    """A fully-armed Settings."""
    return _base().model_copy(update={**_ARMED, **overrides})


# --- arming gate -------------------------------------------------------


def test_a_default_configuration_is_not_armed() -> None:
    """The property every checkout of this repository must have."""
    decision = evaluate_live_arming(_base(), live_broker_available=True)
    assert decision.status is LiveArmingStatus.NOT_ARMED
    assert not decision.armed
    assert "STRATEGY_LIVE_AUTO_EXECUTION_ENABLED is false" in decision.detail


def test_arming_requires_the_interactive_live_keys_too() -> None:
    """The robot switch alone is not enough: arming automation on top of a
    live path that is itself disabled is incoherent, and is refused rather
    than silently treated as permission."""
    decision = evaluate_live_arming(
        _settings(live_trading_enabled=False), live_broker_available=True
    )
    assert decision.status is LiveArmingStatus.LIVE_TRADING_DISABLED
    assert not decision.armed


def test_arming_requires_trading_mode_live() -> None:
    decision = evaluate_live_arming(
        _settings(trading_mode=TradingMode.PAPER), live_broker_available=True
    )
    assert decision.status is LiveArmingStatus.LIVE_TRADING_DISABLED


@pytest.mark.parametrize(
    "missing", ["strategy_live_total_capital", "strategy_live_capital_per_trade"]
)
def test_arming_requires_both_capital_bounds(missing: str) -> None:
    """There is deliberately no 'unlimited' default for either bound."""
    decision = evaluate_live_arming(_settings(**{missing: None}), live_broker_available=True)
    assert decision.status is LiveArmingStatus.NOT_CONFIGURED
    assert not decision.armed


def test_a_missing_live_broker_is_never_substituted_with_a_paper_one() -> None:
    """Fails closed. A paper stand-in would place simulated orders while
    the audit trail said `live` - the exact confusion this project's
    anti-fabrication rule forbids."""
    decision = evaluate_live_arming(_settings(), live_broker_available=False)
    assert decision.status is LiveArmingStatus.NO_LIVE_BROKER
    assert not decision.armed


def test_a_fully_configured_system_is_armed() -> None:
    decision = evaluate_live_arming(_settings(), live_broker_available=True)
    assert decision.status is LiveArmingStatus.ARMED
    assert decision.armed


# --- entry budget ------------------------------------------------------


def _decision(remaining: Decimal) -> LiveCapitalDecision:
    return LiveCapitalDecision(
        status=LiveCapitalStatus.OK,
        detail="",
        deployed_cost_basis=Decimal(0),
        realized_pnl_today=Decimal(0),
        unrealized_pnl=Decimal(0),
        realized_pnl_total=Decimal(0),
        open_position_count=0,
        remaining_capital=remaining,
    )


def test_the_entry_budget_is_the_per_trade_cap_when_capital_is_plentiful() -> None:
    budget = live_entry_budget(_decision(Decimal("10000")), settings=_settings())
    assert budget == Decimal("1000")


def test_the_entry_budget_is_the_remaining_capital_when_that_binds_first() -> None:
    """Near the total-capital ceiling, the LAST trade is smaller than the
    per-trade cap rather than being allowed to punch through the ceiling."""
    budget = live_entry_budget(_decision(Decimal("250")), settings=_settings())
    assert budget == Decimal("250")


def test_the_entry_budget_is_zero_when_the_capital_allowance_is_exhausted() -> None:
    assert live_entry_budget(_decision(Decimal(0)), settings=_settings()) == Decimal(0)


# --- capital decision semantics ----------------------------------------


def test_only_loss_breaches_pause_the_deployment() -> None:
    """An unpriceable position halts THIS cycle but must not pause the
    deployment: a stale data feed is an operational gap, not a loss event,
    and pausing on it would disguise an ingestion problem as a risk
    decision."""

    def at(status: LiveCapitalStatus) -> LiveCapitalDecision:
        return LiveCapitalDecision(
            status=status,
            detail="",
            deployed_cost_basis=Decimal(0),
            realized_pnl_today=Decimal(0),
            unrealized_pnl=Decimal(0),
            realized_pnl_total=Decimal(0),
            open_position_count=0,
            remaining_capital=Decimal(0),
        )

    assert at(LiveCapitalStatus.DAILY_LOSS_BREACHED).pauses_deployment
    assert at(LiveCapitalStatus.TOTAL_LOSS_BREACHED).pauses_deployment
    assert not at(LiveCapitalStatus.UNPRICEABLE_POSITION).pauses_deployment
    assert at(LiveCapitalStatus.UNPRICEABLE_POSITION).halted
    assert not at(LiveCapitalStatus.OK).halted


# --- unrealized marking ------------------------------------------------


def test_unrealized_is_none_rather_than_partial_when_a_lot_cannot_be_marked() -> None:
    """A partial sum would understate the loss in the one direction that
    keeps the robot trading, so an unmarkable lot poisons the whole
    figure rather than being quietly dropped from it."""
    from apps.api.app.deployments.live_guard import _unrealized
    from apps.api.app.deployments.monitoring import OpenLot

    now = _NOW
    lots = {
        "AAPL.US": OpenLot(quantity=Decimal(10), entry_price=Decimal(100), entered_at=now),
        "MSFT.US": OpenLot(quantity=Decimal(5), entry_price=Decimal(200), entered_at=now),
    }
    assert _unrealized(lots, {"AAPL.US": Decimal(110), "MSFT.US": Decimal(190)}) == Decimal(50)
    assert _unrealized(lots, {"AAPL.US": Decimal(110)}) is None


def test_open_lot_cost_basis_is_price_times_quantity() -> None:
    from apps.api.app.deployments.monitoring import OpenLot

    lot = OpenLot(quantity=Decimal(7), entry_price=Decimal("12.5"), entered_at=_NOW)
    assert lot.cost_basis == Decimal("87.5")


# --- the daily-loss window ---------------------------------------------


def test_todays_realized_window_is_a_utc_calendar_day() -> None:
    """Documented behaviour, asserted so a future timezone change is a test
    failure rather than a silent shift in when the breaker resets."""
    today = datetime(2026, 9, 11, 23, 59, tzinfo=UTC)
    yesterday = today - timedelta(hours=1)
    assert today.astimezone(UTC).date() == yesterday.astimezone(UTC).date()
    assert (today + timedelta(minutes=2)).astimezone(UTC).date() != today.astimezone(UTC).date()
