"""Builds the `RiskLimits` a trade is gated against, from configuration.

Extracted from `apps/api/app/api/routes/trades.py` in Phase 43
(docs/DECISIONS.md D058) so that the paper/live distinction is ONE pure,
directly unit-testable function rather than a branch buried in an async
route handler that needs a database, an authenticated user, and a broker
row to exercise.

The live and paper limit sets never mix: `live=True` reads only the
`live_risk_*` settings and `live=False` reads only the `risk_*` settings.
The three limits that are not money-scaled (stop requirement, market-data
freshness, duplicate-order window) are shared deliberately - they are
correctness and data-integrity rules, not risk appetite, and a live trade
must never be held to a *looser* version of them than a paper one.
"""

from apps.api.app.core.config import Settings
from apps.api.app.risk.models import RiskLimits


def build_risk_limits(settings: Settings, *, live: bool) -> RiskLimits:
    return RiskLimits(
        max_position_pct_of_equity=(
            settings.live_risk_max_position_pct_of_equity
            if live
            else settings.risk_max_position_pct_of_equity
        ),
        max_portfolio_exposure_pct_of_equity=(
            settings.live_risk_max_portfolio_exposure_pct_of_equity
            if live
            else settings.risk_max_portfolio_exposure_pct_of_equity
        ),
        max_risk_pct_of_equity_per_trade=(
            settings.live_risk_max_risk_pct_of_equity_per_trade
            if live
            else settings.risk_max_risk_pct_of_equity_per_trade
        ),
        require_stop_price=settings.risk_require_stop_price,
        max_market_data_age_seconds=settings.risk_max_market_data_age_seconds,
        duplicate_order_window_seconds=settings.risk_duplicate_order_window_seconds,
    )
