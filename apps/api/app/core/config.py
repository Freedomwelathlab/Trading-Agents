from enum import Enum
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(str, Enum):  # noqa: UP042 (str mixin kept for pydantic/env-var interop)
    """Execution context. Spec §3/§46: an LLM alone must never reach LIVE.

    RESEARCH: analysis only, no order path exists.
    PAPER: simulated broker/exchange, no real money moves.
    LIVE: real broker, real money — gated by LIVE_TRADING_ENABLED and the
    explicit confirmation flow in the risk/execution layer (not built yet).
    """

    RESEARCH = "research"
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"

    trading_mode: TradingMode = TradingMode.RESEARCH
    live_trading_enabled: bool = False

    database_url: str = "postgresql+asyncpg://trading_os:trading_os@localhost:5432/trading_os"
    redis_url: str = "redis://localhost:6379/0"

    @model_validator(mode="after")
    def _enforce_fail_closed_live_gate(self) -> "Settings":
        """Fail closed: LIVE mode requires the explicit enable flag (spec §46/§56).

        This does not by itself authorize live trading — it only prevents the
        app from booting into a LIVE-labeled mode with the safety flag off,
        which would be an inconsistent and dangerous configuration.
        """
        if self.trading_mode is TradingMode.LIVE and not self.live_trading_enabled:
            raise ValueError(
                "TRADING_MODE=live requires LIVE_TRADING_ENABLED=true. "
                "Refusing to start in an inconsistent live-but-disabled state."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
