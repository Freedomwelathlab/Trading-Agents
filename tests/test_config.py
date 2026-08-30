import pytest

from apps.api.app.core.config import Settings, TradingMode
from apps.api.app.portfolio.models import CostBasisMethod


def test_default_mode_is_research_and_safe():
    settings = Settings(
        _env_file=None, jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256"
    )
    assert settings.trading_mode is TradingMode.RESEARCH
    assert settings.live_trading_enabled is False


def test_live_mode_requires_enable_flag():
    with pytest.raises(ValueError, match="LIVE_TRADING_ENABLED"):
        Settings(
            _env_file=None,
            jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256",
            trading_mode=TradingMode.LIVE,
            live_trading_enabled=False,
        )


def test_live_mode_with_flag_set_is_allowed_to_construct():
    settings = Settings(
        _env_file=None,
        jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256",
        trading_mode=TradingMode.LIVE,
        live_trading_enabled=True,
    )
    assert settings.trading_mode is TradingMode.LIVE


def test_missing_jwt_secret_key_fails_closed():
    with pytest.raises(ValueError, match="jwt_secret_key"):
        Settings(_env_file=None)


def test_the_snapshot_scheduler_defaults_to_average_cost_basis():
    """D044 made the scheduler's cost-basis method configurable but did NOT
    change its behaviour: an unconfigured deployment keeps writing exactly
    the average-cost history D030 shipped."""
    settings = Settings(
        _env_file=None, jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256"
    )
    assert settings.portfolio_snapshot_cost_basis_method is CostBasisMethod.AVERAGE


def test_an_unrecognised_scheduler_cost_basis_method_fails_at_config_load():
    """Typing the setting as the real enum is what makes this a startup
    failure rather than a run of scheduled rows labelled with a method
    nothing can read back."""
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256",
            portfolio_snapshot_cost_basis_method="hifo",
        )
