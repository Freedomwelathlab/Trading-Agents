"""Phase 69 (D087): the startup validator for unattended live execution.

These assert that a half-configured robot cannot boot AT ALL, rather than
discovering its missing bounds at the moment it is about to place its first
real order. Every case below is a `ValidationError` from constructing
`Settings`, i.e. an app that refuses to start.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from apps.api.app.core.config import Settings, TradingMode

_ARMED = dict(
    trading_mode=TradingMode.LIVE,
    live_trading_enabled=True,
    strategy_live_auto_execution_enabled=True,
    strategy_live_total_capital=Decimal("10000"),
    strategy_live_capital_per_trade=Decimal("1000"),
)


def _settings(**overrides) -> Settings:
    """`_env_file=None` so a developer's own `.env` cannot influence a test
    about whether live automation is allowed to start."""
    return Settings(
        _env_file=None,
        jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256",
        **{**_ARMED, **overrides},
    )


def test_a_fully_configured_robot_constructs() -> None:
    settings = _settings()
    assert settings.strategy_live_auto_execution_enabled
    assert settings.strategy_live_total_capital == Decimal("10000")


def test_the_default_is_off_and_needs_no_capital_bounds() -> None:
    """The property that matters for every checkout of this repository: a
    default Settings constructs fine and the robot is off."""
    settings = Settings(
        _env_file=None, jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256"
    )
    assert settings.strategy_live_auto_execution_enabled is False
    assert settings.strategy_live_total_capital is None
    assert settings.strategy_live_capital_per_trade is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"live_trading_enabled": False, "trading_mode": TradingMode.PAPER},
        {"trading_mode": TradingMode.PAPER},
    ],
    ids=["both-off", "mode-not-live"],
)
def test_arming_without_the_interactive_live_keys_refuses_to_start(overrides) -> None:
    with pytest.raises(ValidationError, match="requires TRADING_MODE=live"):
        _settings(**overrides)


@pytest.mark.parametrize(
    "missing", ["strategy_live_total_capital", "strategy_live_capital_per_trade"]
)
def test_a_missing_capital_bound_refuses_to_start(missing: str) -> None:
    """There is deliberately no 'unlimited' default."""
    with pytest.raises(ValidationError, match="to be set"):
        _settings(**{missing: None})


@pytest.mark.parametrize(
    "overrides",
    [
        {"strategy_live_total_capital": Decimal("0")},
        {"strategy_live_capital_per_trade": Decimal("0")},
        {"strategy_live_total_capital": Decimal("-5")},
    ],
    ids=["zero-total", "zero-per-trade", "negative-total"],
)
def test_non_positive_capital_refuses_to_start(overrides) -> None:
    with pytest.raises(ValidationError, match="must .*be positive"):
        _settings(**overrides)


def test_a_per_trade_cap_above_total_capital_refuses_to_start() -> None:
    """One trade must never be allowed to commit more than the robot's
    entire allowance - a configuration that says otherwise is a typo, not
    an intention worth honouring."""
    with pytest.raises(ValidationError, match="exceeds"):
        _settings(
            strategy_live_total_capital=Decimal("1000"),
            strategy_live_capital_per_trade=Decimal("5000"),
        )


@pytest.mark.parametrize("value", [Decimal("0"), Decimal("-1"), Decimal("101")])
def test_an_out_of_range_daily_loss_breaker_refuses_to_start(value: Decimal) -> None:
    """Zero or negative would halt immediately; above 100 could never fire.
    Both are configurations whose author did not mean what they wrote."""
    with pytest.raises(ValidationError, match="STRATEGY_LIVE_MAX_DAILY_LOSS_PCT"):
        _settings(strategy_live_max_daily_loss_pct=value)


@pytest.mark.parametrize("value", [Decimal("0"), Decimal("101")])
def test_an_out_of_range_total_loss_breaker_refuses_to_start(value: Decimal) -> None:
    with pytest.raises(ValidationError, match="STRATEGY_LIVE_MAX_TOTAL_LOSS_PCT"):
        _settings(strategy_live_max_total_loss_pct=value)


def test_a_non_positive_open_position_ceiling_refuses_to_start() -> None:
    with pytest.raises(ValidationError, match="STRATEGY_LIVE_MAX_OPEN_POSITIONS"):
        _settings(strategy_live_max_open_positions=0)


def test_bad_capital_bounds_are_ignored_while_the_robot_is_off() -> None:
    """The validator gates only the ARMED configuration. Leaving stale or
    nonsensical capital values in a `.env` while the robot is off must not
    prevent an ordinary paper deployment from starting - the switch, not
    the leftovers, is what decides."""
    settings = Settings(
        _env_file=None,
        jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256",
        strategy_live_auto_execution_enabled=False,
        strategy_live_total_capital=Decimal("100"),
        strategy_live_capital_per_trade=Decimal("99999"),
    )
    assert settings.strategy_live_auto_execution_enabled is False
