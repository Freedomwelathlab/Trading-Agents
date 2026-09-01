"""Phase 43 (D058): the paper and live risk-limit sets must be built from
disjoint settings, so that neither can drift into the other.
"""

from decimal import Decimal

from apps.api.app.core.config import Settings
from apps.api.app.risk.limits import build_risk_limits


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"jwt_secret_key": "test-only-not-a-real-secret"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_the_live_defaults_are_the_conservative_ones_the_user_specified():
    limits = build_risk_limits(_settings(), live=True)
    assert limits.max_position_pct_of_equity == Decimal("0.05")
    assert limits.max_portfolio_exposure_pct_of_equity == Decimal("0.20")
    assert limits.max_risk_pct_of_equity_per_trade == Decimal("0.01")


def test_the_paper_defaults_are_unchanged_by_the_live_path_existing():
    limits = build_risk_limits(_settings(), live=False)
    assert limits.max_position_pct_of_equity == Decimal("0.10")
    assert limits.max_portfolio_exposure_pct_of_equity == Decimal("0.50")
    assert limits.max_risk_pct_of_equity_per_trade == Decimal("0.01")


def test_the_live_limits_are_strictly_tighter_than_or_equal_to_the_paper_ones():
    settings = _settings()
    paper = build_risk_limits(settings, live=False)
    live = build_risk_limits(settings, live=True)
    assert live.max_position_pct_of_equity < paper.max_position_pct_of_equity
    assert live.max_portfolio_exposure_pct_of_equity < paper.max_portfolio_exposure_pct_of_equity
    assert live.max_risk_pct_of_equity_per_trade <= paper.max_risk_pct_of_equity_per_trade


def test_retuning_the_live_limits_does_not_touch_the_paper_ones():
    settings = _settings(
        live_risk_max_position_pct_of_equity=Decimal("0.01"),
        live_risk_max_portfolio_exposure_pct_of_equity=Decimal("0.02"),
        live_risk_max_risk_pct_of_equity_per_trade=Decimal("0.003"),
    )
    paper = build_risk_limits(settings, live=False)
    assert paper.max_position_pct_of_equity == Decimal("0.10")
    assert paper.max_portfolio_exposure_pct_of_equity == Decimal("0.50")
    assert paper.max_risk_pct_of_equity_per_trade == Decimal("0.01")

    live = build_risk_limits(settings, live=True)
    assert live.max_position_pct_of_equity == Decimal("0.01")


def test_retuning_the_paper_limits_does_not_touch_the_live_ones():
    settings = _settings(
        risk_max_position_pct_of_equity=Decimal("0.90"),
        risk_max_portfolio_exposure_pct_of_equity=Decimal("0.95"),
        risk_max_risk_pct_of_equity_per_trade=Decimal("0.50"),
    )
    live = build_risk_limits(settings, live=True)
    assert live.max_position_pct_of_equity == Decimal("0.05")
    assert live.max_portfolio_exposure_pct_of_equity == Decimal("0.20")
    assert live.max_risk_pct_of_equity_per_trade == Decimal("0.01")


def test_the_non_money_rules_are_shared_so_live_is_never_held_to_a_looser_one():
    settings = _settings()
    paper = build_risk_limits(settings, live=False)
    live = build_risk_limits(settings, live=True)
    assert live.require_stop_price == paper.require_stop_price is True
    assert live.max_market_data_age_seconds == paper.max_market_data_age_seconds
    assert live.duplicate_order_window_seconds == paper.duplicate_order_window_seconds
