from contextlib import asynccontextmanager

from fastapi import FastAPI

from apps.api.app.api.routes.trades import router as trades_router
from apps.api.app.core.config import get_settings
from apps.api.app.core.logging import configure_logging, get_logger
from apps.api.app.execution.registry import PaperBrokerRegistry

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.paper_broker_registry = PaperBrokerRegistry(
        starting_cash=settings.paper_broker_starting_cash
    )
    logger.info(
        "trading_os_startup",
        trading_mode=settings.trading_mode.value,
        live_trading_enabled=settings.live_trading_enabled,
        emergency_stop_active=settings.emergency_stop_active,
    )
    yield


app = FastAPI(title="Trading OS API", version="0.1.0", lifespan=lifespan)
app.include_router(trades_router)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "trading_mode": settings.trading_mode.value,
        "live_trading_enabled": settings.live_trading_enabled,
    }
