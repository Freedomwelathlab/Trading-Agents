from decimal import Decimal
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

    emergency_stop_active: bool = False
    """Global kill switch, flippable without a redeploy. Checked on every
    trade submission (spec §46's 'emergency stop' control)."""

    paper_broker_starting_cash: Decimal = Decimal(100_000)
    """Starting cash for a paper broker the first time an order references
    it. Not persisted per-broker yet - see docs/DECISIONS.md."""

    risk_max_position_pct_of_equity: Decimal = Decimal("0.10")
    risk_max_portfolio_exposure_pct_of_equity: Decimal = Decimal("0.50")
    risk_max_risk_pct_of_equity_per_trade: Decimal = Decimal("0.01")
    risk_require_stop_price: bool = True
    risk_max_market_data_age_seconds: int = 300
    risk_duplicate_order_window_seconds: int = 5
    """See docs/DECISIONS.md D024 for why 5s was chosen over a shorter or
    longer window."""

    portfolio_max_symbol_pct_of_equity: Decimal = Decimal("0.25")
    """Trade-path Portfolio Manager (D029): cap on any ONE symbol's
    post-trade market value as a share of equity. Deliberately looser than
    risk_max_position_pct_of_equity (0.10), because the two measure
    different things: 0.10 caps a single order's notional, 0.25 caps the
    aggregate position that repeated compliant orders can accumulate."""
    portfolio_min_cash_reserve_pct_of_equity: Decimal = Decimal("0.05")
    """Trade-path Portfolio Manager (D029): cash floor as a share of equity,
    so the book is never fully invested. The Risk Engine's buying-power
    check only asks whether cash covers one notional; this keeps a reserve
    across all of them."""
    portfolio_max_open_positions: int = 20
    """Trade-path Portfolio Manager (D029): cap on distinct held symbols -
    an honest position count, not a real diversification measure (this repo
    stores no sector or correlation data to compute one from)."""

    portfolio_snapshot_scheduler_enabled: bool = False
    """Opt-in switch for the automatic portfolio snapshot loop (Phase 27,
    docs/DECISIONS.md D030). Defaults to FALSE deliberately: this is
    unattended behavior that reads real broker state and writes real
    append-only history on a timer with no human in the loop, so it follows
    the same fail-closed, explicitly-enabled posture as
    `live_trading_enabled` and the market-data/LLM credential trios below.
    Phase 25/D027's manual `POST /brokers/{id}/portfolio/snapshots` remains
    the only capture path unless this is set. See
    apps/api/app/portfolio/scheduler.py."""

    portfolio_snapshot_interval_seconds: int = 3600
    """How often the snapshot loop runs when enabled. One hour by default -
    frequent enough to build a usable intraday equity curve, far below any
    market-data vendor's rate limits at this portfolio size, and coarse
    enough that a full day of an unattended process adds ~24 rows per
    broker rather than thousands. Must be positive; the scheduler refuses
    to construct otherwise (a zero interval would busy-loop the DB and the
    vendor)."""

    longport_app_key: str | None = None
    longport_app_secret: str | None = None
    longport_access_token: str | None = None
    """All three optional, no default value pretending to be a real one -
    market data is genuinely optional to configure (spec Sec57: absence
    must render as NOT_CONFIGURED, not a fabricated feed). All three must
    be set together or the Longbridge provider isn't built at all - see
    apps/api/app/marketdata/providers/longbridge.py and docs/DECISIONS.md D015."""

    llm_provider_base_url: str | None = None
    llm_provider_api_key: str | None = None
    llm_provider_model: str | None = None
    """All three optional, same all-or-nothing posture as the Longbridge
    trio above - the agent layer is genuinely optional to configure
    (docs/AGENT_POLICY.md, D018). Deliberately provider-name-agnostic
    (not e.g. OMNIROUTE_*): docs/MODEL_ROUTING.md requires model/provider
    config to never be hard-coded, so any Anthropic-Messages-API-compatible
    endpoint works here, OmniRoute included. See
    apps/api/app/agents/anthropic_compatible.py and docs/DECISIONS.md D018."""

    jwt_secret_key: str
    """No default, deliberately - an app that can silently boot with a
    built-in JWT secret is a worse failure mode than one that refuses to
    start without configuration, same posture as the live-trading gate
    below. Set via .env; see .env.example for how to generate one."""
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 30

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

    @model_validator(mode="after")
    def _enforce_positive_snapshot_interval(self) -> "Settings":
        """Validated at config load, not just at scheduler construction, so a
        bad interval fails the app's startup with a clear message instead of
        surfacing later as a ValueError from inside the lifespan."""
        if self.portfolio_snapshot_interval_seconds <= 0:
            raise ValueError(
                "PORTFOLIO_SNAPSHOT_INTERVAL_SECONDS must be positive. A zero or "
                "negative interval would busy-loop the snapshot cycle against the "
                "database and the market data vendor."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    # jwt_secret_key has no default on purpose (see the field docstring) -
    # pydantic-settings fills it from the environment/.env at runtime, but
    # mypy can't see that, hence the ignore rather than a fake default.
    return Settings()  # type: ignore[call-arg]
