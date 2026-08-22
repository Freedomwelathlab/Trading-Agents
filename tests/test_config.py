import pytest

from apps.api.app.core.config import Settings, TradingMode


def test_default_mode_is_research_and_safe():
    settings = Settings(_env_file=None)
    assert settings.trading_mode is TradingMode.RESEARCH
    assert settings.live_trading_enabled is False


def test_live_mode_requires_enable_flag():
    with pytest.raises(ValueError, match="LIVE_TRADING_ENABLED"):
        Settings(_env_file=None, trading_mode=TradingMode.LIVE, live_trading_enabled=False)


def test_live_mode_with_flag_set_is_allowed_to_construct():
    settings = Settings(_env_file=None, trading_mode=TradingMode.LIVE, live_trading_enabled=True)
    assert settings.trading_mode is TradingMode.LIVE
