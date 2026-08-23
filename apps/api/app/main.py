from contextlib import asynccontextmanager

from fastapi import FastAPI

from apps.api.app.api.routes.admin import router as admin_router
from apps.api.app.api.routes.marketdata import router as marketdata_router
from apps.api.app.api.routes.trades import router as trades_router
from apps.api.app.auth.routes.login import router as auth_router
from apps.api.app.core.config import get_settings
from apps.api.app.core.logging import configure_logging, get_logger
from apps.api.app.marketdata.providers.longbridge import build_longbridge_provider
from apps.api.app.marketdata.router import MarketDataRouter

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    longbridge = build_longbridge_provider(settings)
    app.state.market_data_router = MarketDataRouter([longbridge]) if longbridge else None
    logger.info(
        "trading_os_startup",
        trading_mode=settings.trading_mode.value,
        live_trading_enabled=settings.live_trading_enabled,
        emergency_stop_active=settings.emergency_stop_active,
        market_data_vendor="longbridge" if longbridge else "NOT_CONFIGURED",
    )
    yield


app = FastAPI(title="Trading OS API", version="0.1.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(trades_router)
app.include_router(admin_router)
app.include_router(marketdata_router)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "trading_mode": settings.trading_mode.value,
        "live_trading_enabled": settings.live_trading_enabled,
    }
