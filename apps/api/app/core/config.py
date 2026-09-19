from decimal import Decimal
from enum import Enum
from functools import lru_cache

from pydantic import Field, model_validator
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

    strategy_live_auto_execution_enabled: bool = False
    """Phase 69 (docs/DECISIONS.md D087). THE key that arms unattended live
    execution: a `mode='live'` deployment's scheduled runner places real
    orders with real money, with no per-trade human confirmation.

    Deliberately a THIRD key, separate from `trading_mode=live` and
    `live_trading_enabled`, and this separation is the whole point. Those
    two together arm the *interactive* live path (D058), where a human
    supplies `confirm: true` per order. A person who enables them to place
    one confirmed live trade by hand must not thereby, silently, also start
    an unsupervised robot. Arming the robot is its own decision, taken on
    its own switch.

    This replaces Phase 64's (D082) unconditional refusal, which existed
    because no such deliberate switch had yet been designed. D082's
    reasoning was never "live automation is impossible" - it was "there is
    no way to express that intent unambiguously, so refuse." This setting
    is that expression. The refusal branch survives verbatim for every
    configuration in which this is not explicitly, affirmatively on.

    Default false, and it is fail-closed twice over: `_validate()` refuses
    to start the app at all if this is true while any of the capital
    controls below is unset, or while the two D058 keys are not also on."""

    strategy_live_total_capital: Decimal | None = None
    """Phase 69 (D087). The capital the unattended live runner may have
    deployed, in `live_account_currency`. Not a starting balance and not a
    suggestion: the runner refuses to open a new position that would carry
    deployed cost basis past this number, recomputed from real fills every
    cycle, never from a remembered figure.

    **SCOPE: PER LIVE DEPLOYMENT, not per account.** This bound - and both
    loss breakers below - are evaluated against one deployment's own
    positions and its own P&L, because that is the only book this system
    can attribute cleanly: `Order.deployment_run_id` traces an order to the
    deployment that placed it, and nothing traces a position a human opened
    by hand or another deployment owns. So N concurrently ACTIVE live
    deployments can commit up to N times this number. Running one live
    deployment is the configuration this bound describes exactly; running
    several means doing that multiplication yourself. An account-wide
    ceiling is real future work, not something this setting quietly
    already does.

    Deliberately NOT derived from the live account's actual balance. An
    account may hold capital earmarked for something else entirely; this
    setting is how an operator says how much of it the robot is allowed to
    touch. `None` (the default) means the robot is unconfigured and will
    not run - there is no "unlimited" value, by design."""

    strategy_live_capital_per_trade: Decimal | None = None
    """Phase 69 (D087). The most cost basis one unattended live entry may
    commit, in `live_account_currency`.

    Applied as a CAP on the strategy's own `position_sizing`, not as a
    replacement for it: the runner sizes the position the strategy asked
    for, then takes the smaller of that and this. A strategy that wants
    less than this gets what it wants; a strategy that wants more is
    trimmed. Overriding outright would let a change to this number silently
    *increase* a conservative strategy's size, which is the wrong direction
    for a control whose job is to bound exposure."""

    strategy_live_max_open_positions: int = 5
    """Phase 69 (D087). Hard ceiling on concurrent open positions held by
    ONE live deployment (see the scope note on
    `strategy_live_total_capital`). A breach REJECTS a new entry; it never
    closes an existing one, and exits stay allowed so positions are never
    trapped open (same posture as `portfolio_max_open_positions`)."""

    strategy_live_max_daily_loss_pct: Decimal = Decimal("3")
    """Phase 69 (D087). Circuit breaker: percentage points of
    `strategy_live_total_capital` one live deployment may lose in one UTC
    day (scope note on `strategy_live_total_capital`) before the runner
    halts and PAUSES that deployment.

    A pause, never a liquidation. Auto-liquidating into whatever is
    happening on a day this breaker fires is how a bad hour becomes a
    realized loss - the positions stay, the robot stops opening new ones,
    and a human decides what to do. Re-arming is a human action (resume the
    deployment), which is the point of a breaker."""

    strategy_live_max_total_loss_pct: Decimal = Decimal("10")
    """Phase 69 (D087). The slower sibling of the daily breaker: cumulative
    net loss, as a percentage of `strategy_live_total_capital`, at which the
    runner halts and pauses that deployment. Same pause-never-liquidate
    posture, same human re-arm, same per-deployment scope.

    Deliberately a TOTAL-LOSS limit, not a drawdown-from-peak limit, and
    named for what it measures. A true drawdown breaker needs a stored
    high-water mark of equity over time; this system persists no such
    series for live deployments, and computing a peak from whatever history
    happens to be queryable would produce a number that drifts as old rows
    age out - a breaker whose threshold silently moves is worse than one
    that measures something simpler and says so."""

    backtest_fee_bps: Decimal = Decimal("10")
    """Phase 70 (D088). Round-turn-per-side trading fee charged in every
    engine_v2 backtest, in BASIS POINTS of the trade's notional (10 bps =
    0.10%), applied on BOTH the entry and the exit.

    **The default is deliberately not zero.** Before this phase the engine
    modelled no cost of any kind, which meant every backtest number this
    platform had ever produced described a frictionless market that does
    not exist. On an intraday strategy that trades often, costs are
    routinely the entire edge, so a zero default would let the most
    optimistic possible assumption be the one nobody had to choose.
    10 bps is a plausible retail crypto taker fee; it is a STARTING POINT
    an operator should replace with their own venue's real schedule, not a
    figure this system claims is accurate for any particular broker.

    Set to 0 only to deliberately reproduce a frictionless run - and read
    the result knowing that is what it is."""

    backtest_slippage_bps: Decimal = Decimal("5")
    """Phase 70 (D088). Assumed adverse price movement between a signal and
    its fill, in BASIS POINTS, applied to every simulated fill: a buy fills
    ABOVE the bar's close and a sell BELOW it, never the reverse.

    Modelled as a price adjustment rather than a cash deduction because
    that is what slippage physically is - you transact at a worse price,
    which also means an `all_in` entry can afford slightly fewer shares.
    Sizing is computed against the slipped price for exactly that reason,
    so a backtest never proposes a quantity the simulated account could not
    actually pay for.

    A flat bps figure is a SIMPLIFICATION and is documented as one: real
    slippage scales with order size relative to available liquidity, gaps
    wider in fast markets, and is worse for a large order than a small one.
    This constant models none of that. It exists so a backtest is
    pessimistic by default rather than silently perfect, not because a
    single number describes execution well."""

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

    portfolio_max_portfolio_volatility_pct: Decimal = Decimal("0.40")
    """Trade-path Portfolio Manager, Phase 62 (D079): ceiling on the
    PROJECTED post-trade annualized volatility of the whole book
    (sqrt(wᵀ Σ w) on post-trade market-value weights), as a fraction of 1.0.
    0.40 = 40% annualized: comfortably above a diversified equity book's
    long-run ~15-20% and above a single volatile large-cap's ~30-35%, so it
    binds only when a BUY concentrates the book into genuinely high-variance
    or tightly-correlated names - the case D079 added it for - rather than
    on ordinary trading. A BUY that pushes projected vol over this is sized
    DOWN by bisection, never hard-rejected.

    TRADE PATH ONLY. Every backtest engine omits `market_risk` and this
    check never runs there, so no backtest result moves (D079). The check
    also SKIPS itself (audited, non-binding) for any submission where a held
    or proposed symbol has too little ingested `market_data_bars` history to
    compute an honest covariance - on the normal local checkout, with no
    bars ingested, that means it never binds anything."""
    portfolio_max_position_correlation: Decimal = Decimal("0.80")
    """Trade-path Portfolio Manager, Phase 62 (D079): ceiling on the
    proposed symbol's MAXIMUM pairwise Pearson correlation (from daily
    `market_data_bars` returns) with any OTHER currently-held symbol, above
    which a NEW position is not opened. 0.80 is high on purpose: below it
    two names still diversify meaningfully, and this check only means to
    stop opening a position that is very nearly a duplicate of risk the
    book already carries. Correlation does not depend on quantity, so a
    breach is a REJECT, never a resize (exactly like max_open_positions).

    TRADE PATH ONLY, same as the volatility ceiling above: never consulted
    by any backtest, and SKIPPED (audited) rather than blocking whenever
    there is too little overlapping bar history between the proposed symbol
    and a held one to compute a correlation."""
    portfolio_market_risk_lookback_days: int = 365
    """Trade-path Portfolio Manager, Phase 62 (D079): trailing CALENDAR days
    of daily `market_data_bars` closes read to compute the volatility and
    correlation figures the two checks above compare against. 365 -> ~252
    trading days, a year of returns: the shortest window that gives a stable
    annualized volatility without reaching so far back that a regime change
    dominates it. TRADE PATH ONLY. Must be positive."""
    portfolio_market_risk_min_observations: int = 60
    """Trade-path Portfolio Manager, Phase 62 (D079): minimum overlapping
    daily returns a symbol (or a pair) must have inside the lookback window
    to be usable. ~3 trading months; below this a standard deviation or a
    correlation is too noisy to gate a real trade on, so the corresponding
    check is SKIPPED and recorded rather than run on a thin series - and a
    symbol with no ingested bars at all is simply never covered, which is
    why on a checkout with an unconfigured market-data vendor both Phase-62
    checks are inert. TRADE PATH ONLY. Must be greater than 1."""

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

    live_order_reconciler_enabled: bool = False
    """Opt-in switch for the LIVE ORDER RECONCILER loop (Phase 49,
    docs/DECISIONS.md D066) - the background job that asks the real broker
    what became of orders recorded as `submitted_unconfirmed` and resolves
    them to what the broker actually reports.

    Defaults to FALSE, the same fail-closed posture as
    `portfolio_snapshot_scheduler_enabled` above and for the same reason:
    this is unattended behaviour that reaches a real trading venue on a
    timer with no human in the loop.

    Enabling it alone does nothing at all. The reconciler short-circuits
    every cycle unless a live execution path actually exists - which still
    requires `trading_mode=live` AND `live_trading_enabled=true` AND the
    `LONGPORT_LIVE_*` credential trio, exactly as `build_live_broker_adapter()`
    demands. On the repository's committed defaults, turning this on
    produces a logged NOT_CONFIGURED skip and nothing else: no broker call,
    no database write."""

    live_order_reconciler_interval_seconds: int = 300
    """How often the reconciler polls when enabled. Five minutes by default
    - far shorter than the snapshot scheduler's hour, because an unresolved
    live order is an open question about real money rather than a point on
    a chart, and far longer than a retry loop, because the broker is a rate-
    limited third party and an unfilled order that has not resolved in five
    minutes will not resolve any faster for being asked twice as often.

    Must be positive; the reconciler refuses to construct otherwise (a zero
    interval would busy-loop a real trading venue's API, which is a good way
    to get credentials throttled or revoked)."""

    live_order_reconciler_cycle_lock_enabled: bool = True
    """Phase 49 (D066): before doing any work, each reconciliation cycle
    takes a non-blocking Postgres session-level advisory lock on its OWN key
    (`RECONCILER_LOCK_OBJID`, distinct from the snapshot job's), so running
    the API under several uvicorn/gunicorn workers means one worker polls
    the broker per interval instead of all of them.

    Defaults TRUE for the same reason `portfolio_snapshot_cycle_lock_enabled`
    does - "on" is the side that does less - and it matters more here: N
    workers each independently resolving the same order is N calls to a real
    venue's API and N racing UPDATEs against the same row."""

    strategy_runner_enabled: bool = False
    """Phase 63 (D081): opt-in switch for the scheduled strategy-deployment
    runner - the in-process task that re-evaluates every ACTIVE
    `StrategyDeployment` on an interval and submits PAPER trades through the
    normal RISK -> PORTFOLIO -> BROKER path.

    Defaults FALSE, the same fail-closed posture as every other background
    loop (the snapshot scheduler, the reconciler, live trading, the
    emergency stop) - and this one is if anything more consequential: it
    places real paper orders on a timer with no per-order human. Turning it
    on still does nothing until a deployment has been created AND explicitly
    approved by a person."""

    strategy_runner_interval_seconds: int = 300
    """Seconds between deployment-runner cycles. 5 minutes by default -
    strategies here evaluate on daily bars, so a shorter interval buys
    nothing but load; a much longer one delays acting on a fresh bar. A
    zero or negative value fails app startup (see the validator below)."""

    strategy_runner_cycle_lock_enabled: bool = True
    """Phase 63: each runner cycle takes a non-blocking Postgres advisory
    lock on its OWN key (`DEPLOYMENT_RUNNER_LOCK_OBJID`, distinct from the
    snapshot and reconciler keys) before doing any work, so under several
    workers one worker runs the deployments per interval rather than all of
    them each placing the strategy's order. Defaults TRUE - and it matters
    most of the three background jobs, because two workers each running an
    ACTIVE deployment means two real paper orders where the strategy asked
    for one."""

    strategy_runner_market_hours_gate_enabled: bool = True
    """Phase 63: reuse `MarketHoursGate` (D042) - on a UTC weekend the whole
    runner cycle no-ops before any deployment is enumerated. A weekend check
    only, not a fabricated exchange calendar; disable to run the cycle every
    interval regardless of day."""

    autotrade_runner_enabled: bool = True
    """Phase 81 (D098): the Autotrade Bot loop. Defaults TRUE — a departure
    from the other background loops, justified by where the gate sits: a
    bot cannot act until a person creates AND approves it, and the runner
    runs on PAPER brokers only (a live broker is refused with its own run
    status before any market data or order path is touched). With no
    approved bot the loop does nothing; with no market-data vendor it logs
    and writes nothing. Set false to switch the loop off entirely."""

    autotrade_runner_interval_seconds: int = 60
    """Seconds between bot cycles. The bot trades 5-minute bars and manages
    stops every cycle, so a minute is the useful granularity; a much
    shorter interval only re-reads the same bar."""

    autotrade_runner_cycle_lock_enabled: bool = True
    """Same cross-worker advisory-lock discipline as the deployment runner,
    on the bot runner's own key."""

    strategy_drift_min_round_trips: int = 10
    """Phase 66 (D084): a deployment's own closed round trips must reach
    this count before `evaluate_deployment_drift` will render a verdict at
    all - below it, the result is `INSUFFICIENT_DATA`, never a fabricated
    `NO_DRIFT`/`DRIFT_DETECTED` computed from a handful of trades. A win
    rate over 2-3 round trips is noise, not evidence; judging drift from it
    would risk auto-pausing (or falsely clearing) a deployment on a coin
    flip. Must be positive (see the validator below) - a zero or negative
    threshold would let the very first round trip decide the verdict."""

    strategy_drift_max_win_rate_deviation_pct: Decimal = Field(default=Decimal("30"), gt=0)
    """Phase 66 (D084): how many percentage points a deployment's actual win
    rate may diverge (absolute value) from its reference backtest's
    `win_rate_pct` before `evaluate_deployment_drift` calls it drift. 30
    points is deliberately generous - this compares a live, still-small
    paper-trading sample against a backtest computed over far more trades,
    and a tight threshold would flag ordinary small-sample noise as drift,
    pausing a deployment that is not actually broken. `gt=0`: a
    zero-or-negative deviation ceiling would call every non-identical win
    rate drift, which is not a meaningful signal."""

    strategy_drift_auto_pause_enabled: bool = False
    """Phase 66 (D084): opt-in switch for automatically pausing a deployment
    when `evaluate_deployment_drift` returns `DRIFT_DETECTED`. Defaults
    FALSE - the same fail-closed posture as every other automation in this
    codebase (the snapshot scheduler, the reconciler, the strategy runner
    itself, live trading). Drift is still detected and recorded in
    `strategy_drift_checks` every cycle regardless of this setting; when it
    is `False`, the audit row simply reads `action_taken="observed_only"`
    and an operator who agrees pauses the deployment by hand
    (`POST /deployments/{id}/pause`) rather than the system doing it for
    them. This mirrors docs/TRADING_SAFETY.md's standing pattern of
    auditing before acting."""

    owner_bootstrap_email: str | None = None
    """Phase 79 (D097). If set, startup widens this EXISTING account to the
    `owner` role (every permission) and grants it every broker, then demotes
    every other `admin:manage` holder to `trader`. It never creates an
    account. Exists for deployments where the operator can set an env var
    but has no shell (Railway); the same code path is
    `scripts/grant_owner.py`. Idempotent — safe to leave set."""

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

    email_provider_base_url: str | None = None
    email_provider_api_key: str | None = None
    email_provider_from_address: str | None = None
    """Phase 46 (docs/DECISIONS.md D063): transactional email delivery for
    the password-reset link. All three optional and all-or-nothing, exactly
    like the LONGPORT_* and LLM_PROVIDER_* trios above and for the same
    reason - email is genuinely optional to configure, and an absent vendor
    must render as NOT_CONFIGURED rather than as a silently-dropped message
    the app then claims to have sent.

    Deliberately vendor-name-agnostic (not RESEND_*/POSTMARK_*/SES_*). The
    wire contract is a generic transactional-email HTTP API:
    `POST {base_url}/emails`, `Authorization: Bearer {api_key}`, JSON body
    `{"from", "to": [...], "subject", "text"}`, any 2xx meaning accepted -
    which is Resend's shape and is close enough to several others to be
    reachable through a one-file adapter. See
    apps/api/app/notifications/transactional_email.py for the exact
    contract and apps/api/app/notifications/provider.py for the port any
    other vendor would implement instead.

    When these are unset the reset flow still works end to end: a token is
    still created, and an admin holding `admin:manage` can read the raw
    link out of POST /admin/users/{id}/password-reset. What never happens
    is the app reporting an email as sent when no vendor exists to send it
    (docs/TRADING_SAFETY.md's no-fabrication rule, applied to
    notifications). Never commit real values."""

    auth_password_reset_token_ttl_minutes: int = 30
    """Phase 46 (D063): how long an issued reset token stays redeemable.

    Thirty minutes, not hours: this token is a full account takeover in one
    string, it travels through email (a channel this app does not control
    and cannot revoke from), and the legitimate user is by construction
    sitting at the reset page right now. The expiry is stamped onto the row
    at creation, so changing this value never retroactively extends or
    revokes a link already in someone's inbox. Must be positive."""

    auth_password_reset_max_requests_per_hour: int = 5
    """Phase 46 (D063): how many reset tokens may be issued for ONE account
    per rolling hour, counted from `password_reset_tokens` itself.

    Counting persisted rows rather than an in-process tally is what makes
    this limit hold across uvicorn/gunicorn workers, and it costs no new
    table and no new dependency - the same reasoning D049 used to reject a
    Redis-backed counter for the login lockout.

    Tripping it never changes the response: the endpoint still answers the
    same generic 200, because a throttle that only fires for registered
    emails would itself be the enumeration oracle the generic response
    exists to prevent. Set to 0 to disable this layer."""

    auth_password_reset_max_requests_per_ip_per_hour: int = 20
    """Phase 46 (D063): a per-client-IP fixed-window cap on
    POST /auth/password-reset/request, answered with 429.

    This is the layer that bounds a flood of UNKNOWN emails, which the
    per-account limit above cannot see (an unknown email creates no row, on
    purpose - creating one would make storage itself an oracle).

    Honestly scoped: it is an in-process counter, so with N workers the
    effective ceiling is N times this number, and it resets on restart. It
    is a cost-imposing measure against casual abuse, not a security
    boundary - a real one belongs at the reverse proxy, which is also the
    only layer that can see the true client IP rather than whatever
    `X-Forwarded-For` claims (this app deliberately does not trust that
    header; see apps/api/app/auth/reset_throttle.py). Set to 0 to
    disable."""

    auth_password_reset_url_base: str = "http://localhost:3000/reset-password"
    """Phase 46 (D063): the front-end URL a reset link points at. The token
    is appended as `?token=...`.

    This is a link-construction detail only. Nothing about a token's
    validity depends on it - the backend never parses this URL back, and a
    token issued under one value redeems fine under another. The default is
    the local `next dev` origin so the flow works out of the box in
    development; a real deployment must set it to its own origin, or every
    emailed link will point at the operator's laptop."""

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
    reached. The lock expires on its own - there is still no unlock
    endpoint, so a duration long enough to require one would strand a
    legitimate user with no recourse. (Since Phase 46/D063 a successful
    password reset also clears the lock, which is a recovery path but not
    an unlock endpoint: it requires possession of a real reset link.)
    Fifteen minutes is short enough to
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
    def _enforce_live_auto_execution_is_fully_configured(self) -> "Settings":
        """Phase 69 (D087). Fail closed at STARTUP, not at the first cycle.

        An unattended robot that boots with half its capital controls unset
        and discovers the gap only when it is about to place its first real
        order is exactly the failure mode docs/TRADING_SAFETY.md's
        fail-closed rule exists to prevent. If the robot is armed, every
        bound it operates under must already be a real number, and the two
        D058 keys must already be on - arming automation on top of a live
        path that is itself disabled is incoherent.

        Nothing here authorizes anything. It only refuses to start in a
        configuration whose meaning is ambiguous.
        """
        if not self.strategy_live_auto_execution_enabled:
            return self

        if self.trading_mode is not TradingMode.LIVE or not self.live_trading_enabled:
            raise ValueError(
                "STRATEGY_LIVE_AUTO_EXECUTION_ENABLED=true requires TRADING_MODE=live "
                "and LIVE_TRADING_ENABLED=true. Refusing to arm unattended live "
                "execution on top of a live path that is not itself enabled."
            )

        missing = [
            name
            for name, value in (
                ("STRATEGY_LIVE_TOTAL_CAPITAL", self.strategy_live_total_capital),
                ("STRATEGY_LIVE_CAPITAL_PER_TRADE", self.strategy_live_capital_per_trade),
            )
            if value is None
        ]
        if missing:
            raise ValueError(
                f"STRATEGY_LIVE_AUTO_EXECUTION_ENABLED=true requires {' and '.join(missing)} "
                "to be set. Refusing to run an unattended live robot with no capital bound; "
                "there is deliberately no 'unlimited' default."
            )

        total = self.strategy_live_total_capital
        per_trade = self.strategy_live_capital_per_trade
        assert total is not None and per_trade is not None  # nosec - checked above
        if total <= 0 or per_trade <= 0:
            raise ValueError(
                "STRATEGY_LIVE_TOTAL_CAPITAL and STRATEGY_LIVE_CAPITAL_PER_TRADE must "
                "both be positive."
            )
        if per_trade > total:
            raise ValueError(
                f"STRATEGY_LIVE_CAPITAL_PER_TRADE ({per_trade}) exceeds "
                f"STRATEGY_LIVE_TOTAL_CAPITAL ({total}). One trade cannot be allowed to "
                "commit more than the robot's entire capital allowance."
            )
        if self.strategy_live_max_open_positions <= 0:
            raise ValueError("STRATEGY_LIVE_MAX_OPEN_POSITIONS must be positive.")
        if not (0 < self.strategy_live_max_daily_loss_pct <= 100):
            raise ValueError(
                "STRATEGY_LIVE_MAX_DAILY_LOSS_PCT must be in (0, 100]. A breaker set to "
                "zero or a negative value would halt immediately; above 100 it could "
                "never fire."
            )
        if not (0 < self.strategy_live_max_total_loss_pct <= 100):
            raise ValueError("STRATEGY_LIVE_MAX_TOTAL_LOSS_PCT must be in (0, 100].")
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
    def _enforce_positive_reconciler_interval(self) -> "Settings":
        """Same discipline as the snapshot interval above (Phase 49, D066),
        and the stakes are higher: a non-positive interval here would
        busy-loop a REAL trading venue's API rather than a market-data
        vendor's, which is how credentials get throttled or revoked. Caught
        at config load so it fails startup with a clear message rather than
        surfacing from inside the lifespan."""
        if self.live_order_reconciler_interval_seconds <= 0:
            raise ValueError(
                "LIVE_ORDER_RECONCILER_INTERVAL_SECONDS must be positive. A zero or "
                "negative interval would busy-loop the reconciliation cycle against a "
                "real broker's API."
            )
        if self.strategy_runner_interval_seconds <= 0:
            raise ValueError(
                "STRATEGY_RUNNER_INTERVAL_SECONDS must be positive. A zero or negative "
                "interval would busy-loop the deployment runner against the database and "
                "the paper broker (Phase 63, D081)."
            )
        if self.autotrade_runner_interval_seconds <= 0:
            raise ValueError(
                "AUTOTRADE_RUNNER_INTERVAL_SECONDS must be positive (Phase 81, D098)."
            )
        if self.strategy_drift_min_round_trips <= 0:
            raise ValueError(
                "STRATEGY_DRIFT_MIN_ROUND_TRIPS must be positive (Phase 66, D084). A zero "
                "or negative threshold would let evaluate_deployment_drift render a "
                "NO_DRIFT/DRIFT_DETECTED verdict from a tiny, noisy sample of round trips "
                "instead of the honest INSUFFICIENT_DATA."
            )
        return self

    @model_validator(mode="after")
    def _enforce_sane_market_risk_settings(self) -> "Settings":
        """Phase 62 (D079). Each of these fails in a direction that would
        surface later as a 500 from inside the trade route rather than as a
        clear startup error: a non-positive volatility ceiling or a
        correlation ceiling outside [0, 1] is rejected by `PortfolioLimits`
        when the route builds it, and a non-positive lookback or a
        min-observations <= 1 is rejected by `MarketRiskInputs`. Caught here
        so a bad config fails the app's boot with a readable message. These
        gate the trade path only; no backtest reads them."""
        if self.portfolio_max_portfolio_volatility_pct <= 0:
            raise ValueError(
                "PORTFOLIO_MAX_PORTFOLIO_VOLATILITY_PCT must be positive - it is a "
                "volatility ceiling as a fraction of 1.0 (D079)."
            )
        if not (0 <= self.portfolio_max_position_correlation <= 1):
            raise ValueError(
                "PORTFOLIO_MAX_POSITION_CORRELATION must be between 0 and 1 inclusive - "
                "it is a Pearson correlation ceiling (D079)."
            )
        if self.portfolio_market_risk_lookback_days <= 0:
            raise ValueError(
                "PORTFOLIO_MARKET_RISK_LOOKBACK_DAYS must be positive (D079)."
            )
        if self.portfolio_market_risk_min_observations <= 1:
            raise ValueError(
                "PORTFOLIO_MARKET_RISK_MIN_OBSERVATIONS must be greater than 1 - a "
                "standard deviation needs at least two observations (D079)."
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

    @model_validator(mode="after")
    def _enforce_sane_password_reset_settings(self) -> "Settings":
        """Phase 46 (D063). Every one of these fails in the dangerous
        direction if it is wrong, so all three are caught at startup rather
        than at the first reset request: a non-positive TTL issues tokens
        that are already expired (or, read the other way, a nonsense
        window), and a negative throttle is never a meaningful value - 0 is
        the documented way to disable a throttle deliberately."""
        if self.auth_password_reset_token_ttl_minutes <= 0:
            raise ValueError(
                "AUTH_PASSWORD_RESET_TOKEN_TTL_MINUTES must be positive. A zero or "
                "negative TTL would issue reset tokens that are expired on arrival."
            )
        if self.auth_password_reset_max_requests_per_hour < 0:
            raise ValueError(
                "AUTH_PASSWORD_RESET_MAX_REQUESTS_PER_HOUR must be >= 0. Use 0 to "
                "disable the per-account reset throttle deliberately."
            )
        if self.auth_password_reset_max_requests_per_ip_per_hour < 0:
            raise ValueError(
                "AUTH_PASSWORD_RESET_MAX_REQUESTS_PER_IP_PER_HOUR must be >= 0. Use 0 "
                "to disable the per-IP reset throttle deliberately."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    # jwt_secret_key has no default on purpose (see the field docstring) -
    # pydantic-settings fills it from the environment/.env at runtime, but
    # mypy can't see that, hence the ignore rather than a fake default.
    return Settings()  # type: ignore[call-arg]
