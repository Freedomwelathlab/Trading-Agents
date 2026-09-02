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


def test_missing_jwt_secret_key_fails_closed(monkeypatch: pytest.MonkeyPatch):
    """`_env_file=None` only suppresses the .env file, not the process
    environment — pydantic-settings still reads JWT_SECRET_KEY from there.
    Any shell (or CI job) that exports it therefore made this test assert
    the opposite of what it claims, so the absence has to be arranged
    explicitly rather than assumed. See docs/DECISIONS.md D052."""
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
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


def test_the_login_lockout_defaults_to_five_attempts_and_fifteen_minutes():
    """D049's defaults are part of the security posture, not an arbitrary
    pair - an operator who never sets these must still get a real lockout."""
    settings = Settings(
        _env_file=None, jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256"
    )
    assert settings.auth_max_failed_login_attempts == 5
    assert settings.auth_lockout_duration_minutes == 15


def test_the_login_lockout_can_be_disabled_with_zero_attempts():
    settings = Settings(
        _env_file=None,
        jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256",
        auth_max_failed_login_attempts=0,
    )
    assert settings.auth_max_failed_login_attempts == 0


def test_a_negative_failed_login_threshold_fails_at_config_load():
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256",
            auth_max_failed_login_attempts=-1,
        )


def test_a_non_positive_lockout_duration_fails_while_the_lockout_is_enabled():
    """A zero-minute lock would set and expire in the same instant, which
    reads as protection in the config file and is none in practice."""
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256",
            auth_max_failed_login_attempts=5,
            auth_lockout_duration_minutes=0,
        )


def test_no_email_provider_is_configured_by_default(monkeypatch: pytest.MonkeyPatch):
    """D063's NOT_CONFIGURED default. The reset flow still works without
    these - it just hands the link to an admin instead of emailing it - so
    the meaningful assertion is that all three are absent together, which
    is what makes build_email_provider return None."""
    for name in (
        "EMAIL_PROVIDER_BASE_URL",
        "EMAIL_PROVIDER_API_KEY",
        "EMAIL_PROVIDER_FROM_ADDRESS",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(
        _env_file=None, jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256"
    )
    assert settings.email_provider_base_url is None
    assert settings.email_provider_api_key is None
    assert settings.email_provider_from_address is None


def test_the_password_reset_defaults_are_a_short_ttl_and_real_throttles():
    """These defaults are the posture, not placeholders: an operator who
    never sets them must still get a 30-minute single-use link and both
    throttle layers switched on."""
    settings = Settings(
        _env_file=None, jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256"
    )
    assert settings.auth_password_reset_token_ttl_minutes == 30
    assert settings.auth_password_reset_max_requests_per_hour == 5
    assert settings.auth_password_reset_max_requests_per_ip_per_hour == 20


def test_a_non_positive_reset_token_ttl_fails_at_config_load():
    """A zero or negative TTL issues links that are expired on arrival —
    caught at startup rather than discovered by a user who cannot reset."""
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256",
            auth_password_reset_token_ttl_minutes=0,
        )


def test_a_negative_reset_throttle_fails_at_config_load():
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256",
            auth_password_reset_max_requests_per_hour=-1,
        )


def test_a_negative_per_ip_reset_throttle_fails_at_config_load():
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256",
            auth_password_reset_max_requests_per_ip_per_hour=-1,
        )
