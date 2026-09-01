from decimal import Decimal
from enum import Enum
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# apps/api/app/portfolio/models.py is pure Pydantic/enum - no config, no DB,
# no I/O - so importing it here cannot cycle back. Typing the setting below
# as the real enum rather than `str` is what makes a typo'd
# PORTFOLIO_SNAPSHOT_COST_BASIS_METHOD fail at app startup instead of
# silently persisting a method name nothing can read back (D044).
from apps.api.app.portfolio.models import CostBasisMethod


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

    health_readiness_timeout_seconds: float = 3.0
    """Hard wall-clock cap on the database check behind `GET /health/ready`
    (Phase 41, docs/DECISIONS.md D054).

    A readiness probe that can hang is worse than one that fails: an
    orchestrator polling a probe which never answers gets a timeout at ITS
    layer, on ITS schedule, and in the meantime the probe is holding a
    pooled connection that real requests want. Bounding the check here
    means a wedged database produces a fast, explicit 503 with
    `reason: "timeout"` rather than a hung request.

    Deliberately shorter than any sane orchestrator probe timeout (K8s
    `timeoutSeconds` defaults to 1s but is commonly raised to 5-10s), so
    this app is the thing that decides the check failed, not the caller.
    Does NOT apply to `GET /health`, which performs no I/O at all."""

    emergency_stop_active: bool = False
    """BOOTSTRAP DEFAULT ONLY as of Phase 33 (docs/DECISIONS.md D039).

    The authoritative state of spec §46's emergency stop is a persisted
    row in `emergency_stop_events`, read on every trade submission by
    apps/api/app/safety/emergency_stop.py and flipped live through
    POST /admin/emergency-stop - no `.env` edit, no restart. This setting
    is consulted if and only if that table is still empty, i.e. the switch
    has never been flipped on this database; once one row exists, changing
    this value has no effect on the trade path."""

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

    live_risk_max_position_pct_of_equity: Decimal = Decimal("0.05")
    live_risk_max_portfolio_exposure_pct_of_equity: Decimal = Decimal("0.20")
    live_risk_max_risk_pct_of_equity_per_trade: Decimal = Decimal("0.01")
    """Phase 43 (docs/DECISIONS.md D058): the risk limits applied to a LIVE
    trade, deliberately kept SEPARATE from the `risk_*` settings above
    rather than replacing or reinterpreting them.

    Two reasons they are separate rather than one shared set:

      1. Paper trading's behaviour must be *provably* unchanged by the
         existence of a live path. Sharing one set of numbers would mean
         any future tightening for live money silently retunes every paper
         backtest and every existing test's expectations.
      2. Real money warrants tighter defaults than a simulator. These are
         5% of equity per position and 20% total exposure, against paper's
         10%/50% - half and two-fifths respectively. Per-trade risk stays
         at 1%: that limit is already a conservative, well-understood
         fraction, and loosening OR tightening it would change position
         sizing for a reason unrelated to "this is real money."

    Only `apps/api/app/api/routes/trades.py`'s live branch reads these; the
    paper branch reads the `risk_*` set and nothing else. Neither branch can
    see the other's numbers, which is what makes claim (1) structural
    rather than a convention.

    These are DEFAULTS, not a licence: they gate a trade only after
    `TRADING_MODE=live`, `LIVE_TRADING_ENABLED=true`, the live credential
    trio, the `trade:submit:live` permission, the per-request `confirm`
    flag, the emergency stop, and the Portfolio Manager have all been
    satisfied."""

    live_account_currency: str = "USD"
    """Which currency's cash balance on the live Longbridge account is
    treated as this account's cash. A live Longbridge account can hold
    several currencies; the adapter refuses to value the account at all if
    this one is absent, rather than substituting another currency's balance
    at an unknown rate (spec Sec57)."""

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

    portfolio_snapshot_cost_basis_method: CostBasisMethod = CostBasisMethod.AVERAGE
    """Phase 36 (docs/DECISIONS.md D044): which cost-basis method the
    snapshot scheduler computes and records its rows under.

    Defaults to AVERAGE - the scheduler's behaviour is unchanged unless
    this is set, which matters because the scheduler writes append-only
    history unattended. Changing it mid-life does NOT rewrite existing
    rows and is not meant to: each row records the method it was captured
    under, so a series that changes method stays honest and readable
    rather than silently mixing incomparable realized-P&L figures. A
    client should still group by `cost_basis_method` before drawing one
    curve across such a change.

    Only average/fifo/lifo are accepted; anything else fails app startup
    rather than falling back."""

    portfolio_snapshot_market_hours_gate_enabled: bool = True
    """Phase 35 (docs/DECISIONS.md D042): when the snapshot scheduler is
    enabled, skip cycles that fall on a Saturday or Sunday in UTC instead
    of firing a full round of DB queries and vendor quote calls whose only
    product would be an unchanged after-hours row (or a logged skip).

    Defaults TRUE - unlike the scheduler switch above, which is fail-closed
    at false, this one's safer state is ON, because "on" is the side that
    does *less*: fewer vendor calls, fewer meaningless rows. Set it false
    to restore D030's unconditional wall-clock firing, which is the right
    choice for testing and for anyone snapshotting 24/7 instruments.

    This is a WEEKEND gate only. It knows nothing about public holidays,
    half-days, or per-exchange session times, and does not pretend to -
    see apps/api/app/portfolio/market_hours.py for why a hardcoded
    exchange calendar was rejected rather than approximated."""

    portfolio_snapshot_cycle_lock_enabled: bool = True
    """Phase 38 (docs/DECISIONS.md D047): before doing any work, each
    snapshot cycle takes a non-blocking Postgres session-level advisory
    lock, so that running the API under more than one uvicorn/gunicorn
    worker produces ONE snapshot row per interval instead of one per
    worker. This closes the multi-worker consequence D030 recorded.

    Defaults TRUE, for the same reason the market-hours gate does: "on" is
    the side that writes fewer rows into an append-only table, and with a
    single worker there is never a contender, so the lock is always
    acquired and the cycle behaves exactly as it did before Phase 38 (at
    the cost of one pooled connection and two trivial statements per
    cycle).

    Set false to restore D030's unguarded behaviour, in which no lock
    statement is issued at all. Only do that with a single worker: with
    several, it reinstates the duplicate-row bug. No new dependency backs
    this - see apps/api/app/portfolio/cycle_lock.py for why Postgres
    advisory locks were chosen over Redis (provisioned but unwired), a
    leader-election library, or a scheduler library."""

    longport_app_key: str | None = None
    longport_app_secret: str | None = None
    longport_access_token: str | None = None
    """All three optional, no default value pretending to be a real one -
    market data is genuinely optional to configure (spec Sec57: absence
    must render as NOT_CONFIGURED, not a fabricated feed). All three must
    be set together or the Longbridge provider isn't built at all - see
    apps/api/app/marketdata/providers/longbridge.py and docs/DECISIONS.md D015."""

    longport_live_app_key: str | None = None
    longport_live_app_secret: str | None = None
    longport_live_access_token: str | None = None
    """Phase 43 (D058): LIVE TRADING credentials for the Longbridge trade
    context. Same all-or-nothing posture as the read-only quote trio above
    and deliberately DISTINCT from it - a quote credential must never be
    able to become a trading credential by accident, which is exactly what
    reusing `longport_app_key` here would have allowed.

    These alone authorize nothing: `build_live_broker_adapter()` also
    requires `trading_mode=live` AND `live_trading_enabled=true`, so
    setting these three on a default (`LIVE_TRADING_ENABLED=false`)
    deployment constructs no trade context and opens no connection.
    Never commit real values; keep them in a local, gitignored `.env`."""

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

    auth_max_failed_login_attempts: int = 5
    """Phase 39 (docs/DECISIONS.md D049): how many CONSECUTIVE failed
    password attempts against one existing, active account before that
    account is locked for `auth_lockout_duration_minutes`. A successful
    login resets the counter to zero, so this only ever fires on a run of
    failures, never on an ordinary user who mistypes once a week.

    Five is the threshold because it is far above the realistic
    human-typo rate for a password manager era login and far below the
    number of guesses that makes an online dictionary attack worthwhile;
    combined with a 15-minute lock it caps an attacker at 20 guesses an
    hour per account. Set to 0 to disable the lockout entirely (the route
    then behaves exactly as it did before Phase 39) - negative values are
    rejected at startup."""

    auth_lockout_duration_minutes: int = 15
    """Phase 39 (docs/DECISIONS.md D049): how long an account stays locked
    once `auth_max_failed_login_attempts` consecutive failures are
    reached. The lock expires on its own - there is no unlock endpoint and
    no email flow, so a duration long enough to require one would strand a
    legitimate user with no recourse. Fifteen minutes is short enough to
    be a nuisance rather than a lockout-as-denial-of-service against the
    real account holder, and long enough to make sustained guessing
    pointless. Must be positive whenever the lockout is enabled."""

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

    @model_validator(mode="after")
    def _enforce_sane_login_lockout(self) -> "Settings":
        """A negative attempt threshold or a non-positive lock duration is
        always a configuration mistake, and both fail in the dangerous
        direction (no lockout at all, or a lock that expires the instant it
        is set). Caught at startup rather than at the first failed login."""
        if self.auth_max_failed_login_attempts < 0:
            raise ValueError(
                "AUTH_MAX_FAILED_LOGIN_ATTEMPTS must be >= 0. Use 0 to disable the "
                "login lockout deliberately; a negative value is never meaningful."
            )
        if self.auth_max_failed_login_attempts > 0 and self.auth_lockout_duration_minutes <= 0:
            raise ValueError(
                "AUTH_LOCKOUT_DURATION_MINUTES must be positive when "
                "AUTH_MAX_FAILED_LOGIN_ATTEMPTS is greater than 0. A zero or negative "
                "duration would lock an account and immediately unlock it, which is "
                "no protection at all."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    # jwt_secret_key has no default on purpose (see the field docstring) -
    # pydantic-settings fills it from the environment/.env at runtime, but
    # mypy can't see that, hence the ignore rather than a fake default.
    return Settings()  # type: ignore[call-arg]
