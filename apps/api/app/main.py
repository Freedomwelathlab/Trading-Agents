from contextlib import asynccontextmanager

from fastapi import FastAPI

from apps.api.app.agents.anthropic_compatible import build_llm_provider
from apps.api.app.agents.technical_analyst import build_technical_analyst
from apps.api.app.agents.trader import build_trader_agent
from apps.api.app.api.routes.admin import router as admin_router
from apps.api.app.api.routes.backtests import router as backtests_router
from apps.api.app.api.routes.brokers import router as brokers_router
from apps.api.app.api.routes.emergency_stop import router as emergency_stop_router
from apps.api.app.api.routes.emergency_stop import status_router as emergency_stop_status_router
from apps.api.app.api.routes.health import router as health_router
from apps.api.app.api.routes.marketdata import router as marketdata_router
from apps.api.app.api.routes.portfolio import router as portfolio_router
from apps.api.app.api.routes.trades import agent_router as agent_trades_router
from apps.api.app.api.routes.trades import router as trades_router
from apps.api.app.auth.routes.login import router as auth_router
from apps.api.app.auth.routes.session import router as session_router
from apps.api.app.core.config import get_settings
from apps.api.app.core.logging import configure_logging, get_logger
from apps.api.app.db.base import get_session_factory
from apps.api.app.marketdata.providers.longbridge import (
    build_longbridge_history_provider,
    build_longbridge_provider,
)
from apps.api.app.marketdata.router import MarketDataRouter
from apps.api.app.portfolio.cycle_lock import SnapshotCycleLock
from apps.api.app.portfolio.market_hours import MarketHoursGate
from apps.api.app.portfolio.scheduler import PortfolioSnapshotScheduler

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    longbridge = build_longbridge_provider(settings)
    app.state.market_data_router = MarketDataRouter([longbridge]) if longbridge else None
    # D021: same credential gate as the quote provider, but a distinct
    # capability (a price series, not one quote) - see
    # apps/api/app/marketdata/history_provider.py.
    app.state.history_provider = build_longbridge_history_provider(settings)

    llm_provider = build_llm_provider(settings)
    app.state.trader_agent = build_trader_agent(llm_provider)
    # Phase 16 (D019): same underlying LLM_PROVIDER_* connection as
    # TraderAgent - there's exactly one analyst this phase, so a second,
    # separately-configured provider would be unused parallel infra with
    # nothing to parallelize against (see D019's "future work" note).
    app.state.technical_analyst = build_technical_analyst(llm_provider)

    # Phase 27 (D030): automatic portfolio snapshots. Opt-in and off by
    # default - when disabled, nothing is constructed and no task runs, so
    # the app behaves exactly as it did through Phase 25/D027's
    # manual-only capture. The scheduler is handed the same
    # MarketDataRouter built above (possibly None / NOT_CONFIGURED); it
    # never sources a price any other way.
    app.state.portfolio_snapshot_scheduler = None
    if settings.portfolio_snapshot_scheduler_enabled:
        scheduler = PortfolioSnapshotScheduler(
            get_session_factory(),
            market_data_router=app.state.market_data_router,
            interval_seconds=settings.portfolio_snapshot_interval_seconds,
            # Phase 35 (D042): weekend gating, on by default. A weekend
            # cycle becomes an in-process no-op instead of a full round of
            # DB queries and vendor quote calls.
            market_hours_gate=MarketHoursGate(
                enabled=settings.portfolio_snapshot_market_hours_gate_enabled
            ),
            # Phase 36 (D044): average unless explicitly configured
            # otherwise. Whichever method is used is recorded on every row,
            # so GET .../history never mixes incomparable P&L silently.
            cost_basis_method=settings.portfolio_snapshot_cost_basis_method,
            # Phase 38 (D047): cross-worker mutual exclusion, on by
            # default. This lifespan runs once per uvicorn/gunicorn
            # worker, so without it every worker would append its own row
            # per interval to an append-only series. With one worker the
            # lock is always won and the cycle is unchanged.
            cycle_lock=SnapshotCycleLock(
                enabled=settings.portfolio_snapshot_cycle_lock_enabled
            ),
        )
        scheduler.start()
        app.state.portfolio_snapshot_scheduler = scheduler

    logger.info(
        "trading_os_startup",
        trading_mode=settings.trading_mode.value,
        live_trading_enabled=settings.live_trading_enabled,
        # D039: this is the FALLBACK default only - the authoritative
        # emergency-stop state lives in emergency_stop_events and is read
        # per trade submission, so this startup line must not be read as
        # "the stop is/isn't currently on".
        emergency_stop_settings_default=settings.emergency_stop_active,
        market_data_vendor="longbridge" if longbridge else "NOT_CONFIGURED",
        history_provider="longbridge" if app.state.history_provider else "NOT_CONFIGURED",
        llm_provider="configured" if llm_provider else "NOT_CONFIGURED",
        technical_analyst="configured" if app.state.technical_analyst else "NOT_CONFIGURED",
        portfolio_snapshot_scheduler=(
            f"enabled:{settings.portfolio_snapshot_interval_seconds}s"
            if settings.portfolio_snapshot_scheduler_enabled
            else "DISABLED"
        ),
        portfolio_snapshot_market_hours_gate=(
            "weekend_utc"
            if settings.portfolio_snapshot_market_hours_gate_enabled
            else "DISABLED"
        ),
        portfolio_snapshot_cost_basis_method=(
            settings.portfolio_snapshot_cost_basis_method.value
        ),
        portfolio_snapshot_cycle_lock=(
            "pg_advisory" if settings.portfolio_snapshot_cycle_lock_enabled else "DISABLED"
        ),
    )
    yield

    if app.state.portfolio_snapshot_scheduler is not None:
        await app.state.portfolio_snapshot_scheduler.stop()


app = FastAPI(title="Trading OS API", version="0.1.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(session_router)
app.include_router(trades_router)
app.include_router(agent_trades_router)
app.include_router(admin_router)
# D039: registered before nothing in particular but kept next to the admin
# router it shares a prefix with. The status route is a separate router
# because it is authentication-only, not admin:manage-gated.
app.include_router(emergency_stop_router)
app.include_router(emergency_stop_status_router)
app.include_router(marketdata_router)
app.include_router(portfolio_router)
app.include_router(backtests_router)
app.include_router(brokers_router)
# Phase 41 (D054): liveness (`/health`, unchanged contract) and the new
# readiness probe (`/health/ready`) moved out of this module into their own
# router - see apps/api/app/api/routes/health.py for why the two are
# separate endpoints rather than one endpoint that checks the database.
app.include_router(health_router)
