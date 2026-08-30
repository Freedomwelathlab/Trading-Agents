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


def test_the_snapshot_cycle_lock_defaults_to_enabled():
    """D047's default is ON, unlike the scheduler switch itself: "on" is the
    side that writes FEWER rows into an append-only table, and with a single
    worker it is a no-op that always acquires. An operator who enables the
    scheduler and thinks no further about it must get the multi-worker-safe
    behaviour, not the duplicate-row one."""
    settings = Settings(
        _env_file=None, jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256"
    )
    assert settings.portfolio_snapshot_cycle_lock_enabled is True


def test_the_snapshot_cycle_lock_can_be_disabled_to_restore_d030_behaviour():
    """The escape hatch must be reachable from configuration alone, since
    that is the only way a single-worker deployment can decline the extra
    pooled connection the lock holds for a cycle."""
    settings = Settings(
        _env_file=None,
        jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256",
        portfolio_snapshot_cycle_lock_enabled=False,
    )
    assert settings.portfolio_snapshot_cycle_lock_enabled is False
