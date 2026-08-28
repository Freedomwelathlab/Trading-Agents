"""POST /backtests (D025). A pure simulation - no broker_id in the path, no
BrokerGrant check, because this route structurally never touches a real
broker's persisted state (see apps/api/app/backtesting/engine.py's module
docstring): every run constructs its own in-memory PaperBrokerAdapter and
throws it away at the end of the request. Gated by get_current_user alone
(any authenticated, active user) rather than a new Permission or
require_broker_access - see docs/DECISIONS.md D025 for the reasoning.
"""

from fastapi import APIRouter, Depends, HTTPException

from apps.api.app.api.dependencies import get_history_provider
from apps.api.app.auth.dependencies import get_current_user
from apps.api.app.backtesting.engine import run_backtest
from apps.api.app.backtesting.errors import InsufficientHistoryError, UnsupportedDateRangeError
from apps.api.app.backtesting.models import BacktestRequest, BacktestResult
from apps.api.app.core.config import Settings, get_settings
from apps.api.app.db.models import User
from apps.api.app.marketdata.history_provider import HistoryProvider
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.risk.models import RiskLimits

router = APIRouter(prefix="/backtests", tags=["backtesting"])


@router.post("", response_model=BacktestResult)
async def run_backtest_endpoint(
    request: BacktestRequest,
    current_user: User = Depends(get_current_user),
    history_provider: HistoryProvider | None = Depends(get_history_provider),
    settings: Settings = Depends(get_settings),
) -> BacktestResult:
    del current_user  # required for the auth check only; unused otherwise

    if history_provider is None:
        raise HTTPException(status_code=400, detail="NOT_CONFIGURED: no history provider.")

    risk_limits = RiskLimits(
        max_position_pct_of_equity=settings.risk_max_position_pct_of_equity,
        max_portfolio_exposure_pct_of_equity=settings.risk_max_portfolio_exposure_pct_of_equity,
        max_risk_pct_of_equity_per_trade=settings.risk_max_risk_pct_of_equity_per_trade,
        require_stop_price=False,
        # v1's strategy has no stop-loss rule of its own - the SMA-cross-down
        # SELL signal is its exit, not a stop price. Requiring one here would
        # only force fabricating a value with no grounding in the strategy.
        # See docs/DECISIONS.md D025.
        max_market_data_age_seconds=settings.risk_max_market_data_age_seconds,
        duplicate_order_window_seconds=settings.risk_duplicate_order_window_seconds,
    )

    try:
        return await run_backtest(
            request, history_provider=history_provider, risk_limits=risk_limits
        )
    except UnsupportedDateRangeError as exc:
        raise HTTPException(status_code=400, detail=f"UNSUPPORTED_DATE_RANGE: {exc}") from None
    except InsufficientHistoryError as exc:
        raise HTTPException(status_code=400, detail=f"DATA_UNAVAILABLE: {exc}") from None
    except DataUnavailableError as exc:
        raise HTTPException(status_code=400, detail=f"DATA_UNAVAILABLE: {exc}") from None
    except VendorError as exc:
        raise HTTPException(status_code=502, detail=f"DATA_UNAVAILABLE: {exc}") from None
