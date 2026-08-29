# Code Map

Read this before exploring the repo. Update it when a module's shape changes.

## Settings & execution mode

Purpose: single source of truth for `TRADING_MODE` / `LIVE_TRADING_ENABLED`,
fails closed at construction.
Main file: `apps/api/app/core/config.py`
Interface: `get_settings() -> Settings`, `Settings.trading_mode: TradingMode`
Tests: `tests/test_config.py`
Important: any new code that reads trading mode must go through
`get_settings()`, never re-parse env vars directly.

## Logging

Purpose: structured JSON logs with automatic secret redaction.
Main file: `apps/api/app/core/logging.py`
Interface: `configure_logging()`, `get_logger(name)`
Tests: `tests/test_logging.py`
Important: `_redact_secrets` matches on key name (regex), not value shape —
if a new module logs a field that looks like a credential under an
unexpected key name, check the pattern still catches it.

## Database layer

Purpose: async SQLAlchemy engine/session + ORM models.
Main files: `apps/api/app/db/base.py` (engine, session, `Base`),
`apps/api/app/db/models.py` (`User`, `Role`, `Asset`, `Broker`, `BrokerGrant`,
`BrokerAccount`, `BrokerPosition`, `Order`, `Fill`)
Dependencies: `apps.api.app.core.config`, `apps.api.app.risk.models` (reuses `Side`)
Tests: migration round-trip (manual, docker-compose) + `tests/db/test_order_persistence.py`
(integration, real Postgres)
Important: `Broker.kind` (paper/live) is the structural mechanism meant to
keep live and paper credentials from ever occupying the same row. Any future
broker-credential table must preserve this separation, not merge it. Every
enum column MUST go through the `_pg_enum()` helper at the top of
`models.py` — plain `Enum(SomeEnum)` sends the member *name* to Postgres,
not its `.value`, and the DB's enum type only accepts the lowercase values
the migration created (see D007). `Order`/`Fill` are append-only by
convention (no code path updates or deletes a row) — enforced by discipline,
not a DB trigger, so don't add one. `Role.permissions` is a flat
`text[]` — see the Auth section below and D011. `BrokerGrant` is a
`(user_id, broker_id)` join table (unique constraint) — see D012.
`BrokerAccount`/`BrokerPosition` are, unlike `Order`/`Fill`, mutable
current-state (not append-only) — see D014; `BrokerPosition` only keeps
rows for nonzero quantities.
`apps/api/app/db/base.py`'s engine is created once at module import and its
asyncpg connections are loop-bound — async tests must share one event loop
(`pyproject.toml`'s `asyncio_default_test_loop_scope = "session"`), not the
pytest-asyncio default of one loop per test (see D007).

## FastAPI app

Purpose: HTTP entrypoint.
Main file: `apps/api/app/main.py`
Interface: `GET /health`
Tests: `tests/test_health.py`

## Migrations

Purpose: schema history.
Location: `migrations/` (Alembic, async env)
Current head: `0009_orders_portfolio_decision` (`0001` users/roles/assets/
brokers, `0002` orders/fills, `0003` adds `orders.submitted_by_user_id`,
`0004` adds `roles.permissions`, `0005` adds `broker_grants`, `0006` adds
`broker_accounts`/`broker_positions`, `0007` adds the composite
`orders(broker_id, symbol, submitted_at)` index for the duplicate-order
query, `0008` adds `portfolio_snapshots`/`portfolio_snapshot_positions`,
`0009` adds the four nullable `orders.portfolio_*` audit columns — D029)
Important: enum columns use `create_type=False` on the Python-side ENUM
object to avoid a double-CREATE-TYPE error against `create_table` — see the
comment history in `0001_initial.py` if adding a new enum column.

## Risk Engine

Purpose: deterministic validation of a proposed trade — the boundary spec
Sec3/Sec46 require between any LLM and a real order.
Main files: `apps/api/app/risk/models.py` (`TradeProposal`, `AccountState`,
`RiskLimits`, `RecentOrder`, `RiskDecision`, `BlockReason`),
`apps/api/app/risk/engine.py` (`evaluate_trade()`)
Interface: `evaluate_trade(proposal, account, limits, *, emergency_stop_active=False, now=None, recent_orders=None) -> RiskDecision`
Dependencies: none — pure function, no LLM/network/DB/I-O import. This is
load-bearing, not incidental: don't add an import here that could make the
engine unable to run with every external service down. `recent_orders`
(D024, duplicate-order detection) is the one place external, DB-sourced
data reaches the engine — as plain `RecentOrder` data the caller queried
and handed in, never a DB call the engine makes itself; see
`apps/api/app/oms/persistence.py`'s `get_recent_filled_orders()` for the
one query that produces it.
Tests: `tests/risk/test_engine.py` (23 tests, including one that proves the
engine still blocks a bad trade when a stubbed LLM provider raises, and 9
covering duplicate-order detection — D024)
Important: every rejection is a typed `BlockReason`, never a bare `False` —
add a new enum member rather than reusing an existing reason for a new rule.
`RiskLimits` has no defaults; an unconfigured limit is a bug to fix, not a
value to fall back to. The engine never resizes an order itself
(`max_quantity_allowed` is informational only) — a caller must submit a new,
explicit proposal, keeping the audit trail honest about what was actually
approved. Duplicate-order detection (D024): a proposal matching a FILLED
order on the same broker — same symbol, side, quantity, and
estimated_price — within `RiskLimits.duplicate_order_window_seconds`
(default 5s, `Settings.risk_duplicate_order_window_seconds`) is blocked
with `BlockReason.DUPLICATE_ORDER`; REJECTED orders are deliberately never
compared against — see D024 for the full reasoning.

## Execution (broker adapters)

Purpose: broker-agnostic port + a deterministic paper implementation.
Main files: `apps/api/app/execution/broker.py` (`BrokerAdapter` Protocol,
`OrderRequest`, `Fill`), `apps/api/app/execution/paper_broker.py`
(`PaperBrokerAdapter`), `apps/api/app/execution/persistence.py`
(`load_paper_broker`, `save_paper_broker` — see D014)
Dependencies: `apps.api.app.risk.models` (reuses `Side`, `AccountState`),
`apps.api.app.db.models` (`BrokerAccount`, `BrokerPosition`, persistence only)
Tests: `tests/execution/test_paper_broker.py` (pure, no DB),
`tests/execution/test_persistence.py` (DB-backed load/save round-trips)
Important: `PaperBrokerAdapter` itself is still pure in-memory (no DB
import) — persistence is a wrapping layer around it (D014), same pattern
as D006's `submit_trade`/`submit_trade_and_record` split. Market orders
only; limit orders raise rather than fake a fill. No short selling.
`get_account_state()` requires a mark for every open position and raises
rather than guessing a stale/missing price. **`load_paper_broker()` locks
the `broker_accounts` row with `SELECT ... FOR UPDATE`** — that lock must
stay held until `save_paper_broker()` + one final commit, in the same
request; this is why `submit_trade_and_record()` no longer commits
internally (see the OMS section below).

## OMS

Purpose: the one sanctioned path from a `TradeProposal` to a `BrokerAdapter`
call — application code must go through `submit_trade()`, never call a
broker adapter directly, or the risk gate becomes optional by accident.
Main file: `apps/api/app/oms/service.py`
Interface: `submit_trade(proposal, account, limits, broker, *, emergency_stop_active=False, now=None, recent_orders=None, portfolio=None, portfolio_limits=None) -> OMSResult`
Dependencies: `apps.api.app.risk.engine`,
`apps.api.app.portfolio_manager.manager` (D029),
`apps.api.app.execution.broker`
Tests: `tests/oms/test_service.py` — includes a spy broker adapter proving a
rejected proposal never reaches `submit_order()`;
`tests/oms/test_service_portfolio_manager.py` (5 tests, D029) covers the
Portfolio Manager's placement in this path.

Since D029 `submit_trade()` also runs the trade-path Portfolio Manager
between the risk gate and the broker call. `portfolio`/`portfolio_limits`
are optional: omitting them skips it entirely and restores pre-D029
behaviour (safe, because it can only ever shrink or stop a trade the Risk
Engine already approved). `OMSResult` gained `portfolio_decision` and
`effective_quantity` — the latter is the quantity actually submitted to
the broker, which differs from `proposal.quantity` on a MODIFY.

`apps/api/app/oms/persistence.py`'s `submit_trade_and_record()` wraps
`submit_trade()` with append-only Order/Fill persistence — see the Database
layer section and D006. Use `submit_trade` for pure/unit-tested logic,
`submit_trade_and_record` anywhere an audit trail is needed (everywhere
reachable from outside the process). **It deliberately does not commit**
(only `flush()`es) — the caller (`trades.py`) commits once after also
calling `save_paper_broker()`, so `load_paper_broker()`'s row lock (D014)
covers the whole trade, not just the order/fill write.

## Execution context

Purpose: structural guarantee that a research/backtest code path can't hold
a live credential (`ResearchContext | PaperContext | LiveContext`).
Main file: `apps/api/app/core/execution_context.py`
Interface: `build_execution_context(settings, *, broker_id=None)`
Tests: `tests/core/test_execution_context.py`
Important: `LiveContext` construction re-checks `live_trading_enabled` even
though `Settings` already enforces it — deliberate defense in depth
(spec §46), not redundant code to simplify away.

## Auth

Purpose: identity (who) and permission checks (what they may do).
Main files: `apps/api/app/auth/security.py` (`hash_password`,
`verify_password`, `create_access_token`, `decode_access_token`),
`apps/api/app/auth/dependencies.py` (`get_current_user`,
`require_permission`), `apps/api/app/auth/permissions.py` (`Permission`
enum), `apps/api/app/auth/routes/login.py` (`POST /auth/login`),
`apps/api/app/auth/schemas.py` (`TokenResponse`)
Dependencies: `apps.api.app.core.config` (`jwt_secret_key` etc.),
`apps.api.app.db.models.User`/`Role`
Tests: `tests/auth/test_security.py` (8 unit tests),
`tests/auth/test_authorization.py` (3 unit tests against the
`require_permission` checker directly), `tests/api/test_auth.py`
(4 integration tests against real Postgres)
Important: **no public registration endpoint exists** — users and roles are
created either via the `/admin/*` routes (see the HTTP API layer section
below and D013) or, for the very first admin, by direct DB insert.
Authorization is *permission-based via `Role.permissions`*, not
per-resource for trading — a user whose role grants `SUBMIT_PAPER_TRADE`
still needs a `BrokerGrant` for the specific broker (D012).
`Permission.ADMIN` ("admin:manage") gates every `/admin/*` route.
`get_current_user` eager-loads
`User.role` via `selectinload` — lazy-loading it later in an async context
would raise, not silently work. `require_permission(Permission.X)` returns
403 for a missing permission, distinct from `get_current_user`'s 401 for
no/invalid identity — don't collapse that distinction.
`SUBMIT_LIVE_TRADE` is defined but enforced nowhere; there's no live
execution path for it to gate — granting it today would be a permission
that does nothing, not a shortcut to live trading. `Settings.jwt_secret_key`
has no default, matching `live_trading_enabled`'s fail-closed posture —
the app will not boot without one configured. The login route deliberately
hashes against a dummy bcrypt hash when the email doesn't exist
(`login.py`'s `_DUMMY_HASH`, generated at import time, not hand-written) to
avoid a timing side-channel that would let an attacker enumerate emails —
don't "simplify" that early-return away.

## HTTP API layer

Purpose: the FastAPI-facing surface — DTOs, route handlers,
request-scoped dependency wiring. Separate from the domain models
(risk/oms/execution) so the HTTP contract can evolve independently.
Main files: `apps/api/app/api/schemas.py` (`TradeSubmissionRequest` —
`estimated_price` optional as of D017, `TradeSubmissionResponse`,
`AgentTradeRequest`/`AgentTradeResponse` as of D018),
`apps/api/app/api/dependencies.py`
(`require_broker_access`, `AuthorizedBroker`, `get_market_data_router`,
`get_trader_agent`, `get_technical_analyst`, `get_history_provider` (D021)),
`apps/api/app/api/routes/trades.py` (`router`:
`POST /brokers/{broker_id}/trades`; `agent_router`:
`POST /brokers/{broker_id}/agent-trades` — both registered separately in
`main.py`; shares `_resolve_live_quote`/`_execute_trade`/
`_require_paper_broker` helpers between the two routes)
Dependencies: `apps.api.app.oms.persistence`, `apps.api.app.execution.persistence`,
`apps.api.app.marketdata.router` (D017), `apps.api.app.agents.trader` (D018),
`apps.api.app.db.models`, `apps.api.app.core.config`, `apps.api.app.auth.dependencies`
Tests: `tests/api/test_trades.py` (18 integration tests against a real
Postgres instance, using `httpx.AsyncClient` + `ASGITransport` — see the
Important note below, and D009; 4 of the 18 cover D017's omitted-price
path via `app.dependency_overrides[get_market_data_router]`),
`tests/api/test_agent_trades.py` (5 integration tests for the
agent-trades route, reusing `test_trades.py`'s fixtures/helpers via
direct import rather than duplicating them)
Important: this route is the *only* sanctioned way to reach
`submit_trade_and_record()` from outside the process. `require_broker_access`
(`apps/api/app/api/dependencies.py`, D012) is the single dependency that
decides whether a request may touch a given `broker_id` at all — identity
→ global `trade:submit:paper` permission (D010, D011) → broker existence
(404) → a specific `BrokerGrant` row (403) — and hands the route back an
`AuthorizedBroker(user, broker)` so the route no longer looks up the
broker itself. Every persisted order still records `submitted_by_user_id`.
`estimated_price` is optional (D017): supplied, it's authoritative and
`get_market_data_router` is never called; omitted, the route fetches one
`MarketSnapshot` and takes both price and `market_data_as_of` from it
together, never mixing a live price with a caller-supplied timestamp.
**Testing an async endpoint that also touches the DB directly in the same
test must use `httpx.AsyncClient(transport=ASGITransport(app=app), ...)`,
never FastAPI's `TestClient`** — `TestClient` runs the app in a separate
thread with its own event loop, which crashes against the shared, loop-bound
DB engine the same way D007's bug did. See `tests/api/test_trades.py`'s
`api_client()` helper for the pattern (manually driving
`app.router.lifespan_context(app)` since `AsyncClient` doesn't do that
automatically the way `TestClient` does).

## Portfolio Manager (trade path)

Purpose: spec §18's Portfolio Manager — the portfolio-level construction
gate between the Risk Engine and the OMS's broker call (D029). Decides
APPROVE / MODIFY (shrink) / REJECT for a proposal the Risk Engine has
already approved, using portfolio-wide state a per-trade check cannot see.
**Not** the same thing as the `apps/api/app/portfolio/` reporting module
below — different package, different purpose, no shared code.
Main files: `apps/api/app/portfolio_manager/models.py` (`PortfolioAction`,
`PortfolioConstraint`, `PortfolioHolding`, `PortfolioState`,
`PortfolioLimits`, `PortfolioCheck`, `PortfolioDecision` — Pydantic, all
`Decimal`), `apps/api/app/portfolio_manager/manager.py`
(`decide()`, `portfolio_state_from_positions()`)
Interface: `decide(proposal, portfolio, limits) -> PortfolioDecision`
Dependencies: `apps.api.app.risk.models` (`TradeProposal`, `Side`) only.
**No LLM, no network call, no I/O** — same discipline as
`apps/api/app/risk/engine.py`, and enforced by the same absence of any
client import.
Tests: `tests/portfolio_manager/test_manager.py` (13 pure unit tests),
`tests/oms/test_service_portfolio_manager.py` (5, wiring),
`tests/db/test_portfolio_decision_persistence.py` (4, real Postgres),
`tests/api/test_trades_portfolio_manager.py` (4, real Postgres over HTTP)
Important: a MODIFY produces a *new* proposal that `submit_trade()` sends
back through `evaluate_trade()` before any broker call — the Portfolio
Manager can never hand a quantity to a broker that the Risk Engine has not
approved. Constraints are `symbol_concentration`, `cash_reserve` and
`max_open_positions` (limits from `Settings.portfolio_*`); spec §18's
correlation/sector/volatility/return/drawdown items are deliberately
unimplemented for want of sector data and a persisted return series (D029).
`REQUEST_MORE_RESEARCH` exists in the enum but is never emitted. A failing
check only blocks when the trade *worsens* that measure, so a de-risking
sell is never refused because of a pre-existing breach. Decisions persist
to `orders.portfolio_action` / `portfolio_binding_constraint` /
`portfolio_detail` / `portfolio_requested_quantity` (migration `0009`);
null there means "never ran", never "approved".

## Portfolio module + route

Purpose: deterministic, read-only position/P&L reporting (D022), plus
persisted append-only snapshot history (D027) — closes
`docs/MODULE_MAP.md`'s previously-empty Portfolio row. No LLM. The
read/compute path (`GET /brokers/{broker_id}/portfolio`) makes no writes
and needs no new tables; the snapshot-history path
(`POST .../portfolio/snapshots`, `GET .../portfolio/history`) adds two
new append-only tables. Phase 27 (D030) added a second, opt-in write
trigger: a background scheduler that sources its own real marks from
`MarketDataRouter` instead of a request body.
Main files: `apps/api/app/portfolio/models.py` (`PortfolioSnapshot`,
`PortfolioPosition` — Pydantic, all `Decimal`), `apps/api/app/portfolio/errors.py`
(`MissingMarkError`, `BrokerAccountNotFoundError`), `apps/api/app/portfolio/snapshot.py`
(`compute_portfolio_snapshot()`, `replay_symbol_fills()` — the latter a
pure function, no DB/I/O, directly unit-testable), `apps/api/app/api/routes/portfolio.py`
(`router`: `GET /brokers/{broker_id}/portfolio`,
`POST /brokers/{broker_id}/portfolio/snapshots`,
`GET /brokers/{broker_id}/portfolio/history`),
`apps/api/app/portfolio/persistence.py` (`persist_portfolio_snapshot()` —
the single append-only write path shared by the manual POST route and the
scheduler, extracted in D030 so the two cannot drift),
`apps/api/app/portfolio/scheduler.py` (D030 — `PortfolioSnapshotScheduler`
(asyncio task, started/stopped by the lifespan in
`apps/api/app/main.py`), `run_snapshot_cycle()`,
`capture_scheduled_snapshot()`, `resolve_marks()`,
`eligible_broker_ids()`, `open_position_symbols()`, and the typed
`ScheduledSnapshotOutcome`/`ScheduledSnapshotStatus`/`SnapshotCycleResult`
results)
Dependencies: `apps.api.app.db.models` (`BrokerAccount`, `BrokerPosition`,
`Order`, `Fill` — read-only for the GET; `PortfolioSnapshotRow`/
`PortfolioSnapshotPositionRow` — written by the POST, read by the history
GET), `apps.api.app.api.dependencies.require_broker_access`,
`apps.api.app.auth.permissions.Permission.VIEW_PORTFOLIO` (introduced in
D022, reused unchanged by D027's two new routes — see D027 for why no new
permission was added)
Tests: `tests/portfolio/test_snapshot.py` (6 pure unit tests for
`replay_symbol_fills()`'s average-cost-basis math, no DB),
`tests/api/test_portfolio.py` (23 integration tests against real
Postgres, reusing `tests/api/test_trades.py`'s fixtures via direct import
— 15 pre-D027 covering the GET, 8 new covering the POST/history routes),
`tests/portfolio/test_scheduler.py` (8 unit tests — mark resolution
through a real `MarketDataRouter` with a fake only at the vendor
boundary, the interval guards, and the scheduler-disabled-by-default
guard), `tests/api/test_snapshot_scheduler.py` (12 integration tests
against real Postgres — capture, each typed skip reason, "one unpriceable
symbol blocks the whole snapshot", a mixed multi-broker cycle, and the
running asyncio task writing a real row on its timer)
Important: `Permission.VIEW_PORTFOLIO` is deliberately separate from
`Permission.SUBMIT_PAPER_TRADE` — this module is read-only with respect
to capital/orders (persisting a snapshot doesn't change that — see D027),
so it doesn't require trade-submission rights, only its own weaker
permission plus the same `require_broker_access` grant check every
broker-scoped route uses. Current marks travel as a JSON body on the
GET/POST (`{"marks": {symbol: price}}`, same shape as
`TradeSubmissionRequest.marks`) since a query string can't cleanly carry
an arbitrary symbol->price map. Realized P&L and `avg_cost` use
average-cost basis (not FIFO/LIFO — this schema has no per-lot data to
support that; see D022's alternatives), replayed from `orders`/`fills` in
`filled_at` order; current open quantity comes from `BrokerPosition` (the
execution layer's own authoritative current state, D014), not the fills
replay, so a snapshot's position sizes always match what the execution
layer itself believes it holds. A missing mark for a currently-held
symbol is 400 `DATA_UNAVAILABLE:`, never a guessed price — on the POST,
that also means nothing is persisted (the DB write only happens after
`compute_portfolio_snapshot()` succeeds). `PortfolioSnapshotPositionRow`
is a child table of `PortfolioSnapshotRow`, not a JSON column — see
D027's Reason for the full reasoning (queryability of per-symbol history,
e.g. "AAPL's position history," and consistency with how Order/Fill
already model this schema's other per-symbol time series).

Phase 27 (D030) note: the scheduler is the first background component in
this codebase. It is **disabled by default**
(`PORTFOLIO_SNAPSHOT_SCHEDULER_ENABLED=false`,
`PORTFOLIO_SNAPSHOT_INTERVAL_SECONDS=3600`); with the defaults nothing is
constructed in the lifespan at all. Because it has no HTTP caller to
supply `marks`, it fetches every held symbol's mark from the existing
`MarketDataRouter` and — this is the load-bearing part — writes
**nothing** for a broker whose holdings it cannot fully price, returning a
typed `ScheduledSnapshotOutcome` and logging
`portfolio_snapshot_skipped` at warning level with the exact unpriced
symbols. It never substitutes avg_cost, zero, a prior mark, or
`Settings.paper_broker_starting_cash` (the last of which the read-only
GET route does fall back to — deliberately not mirrored here; see D030).
A broker with no open positions needs no marks and is captured normally
even when market data is NOT_CONFIGURED.

## Backtesting module + route

Purpose: replays one hard-coded SMA(20)-crossover strategy against real
historical closes through the real Risk Engine and paper-broker fill math
(D025) — closes `docs/MODULE_MAP.md`'s previously-empty Backtesting row.
No writes to any broker's persisted state, no LLM.
Main files: `apps/api/app/backtesting/strategy.py` (`Signal`,
`generate_signals()` — pure, reuses `marketdata/indicators.sma()`),
`apps/api/app/backtesting/metrics.py` (`RoundTrip`,
`compute_total_return_pct()`, `compute_max_drawdown_pct()`,
`compute_win_rate_pct()` — pure), `apps/api/app/backtesting/models.py`
(`BacktestRequest`, `EquityPoint`, `BacktestResult` — Pydantic, all
`Decimal`), `apps/api/app/backtesting/errors.py`
(`InsufficientHistoryError`, `UnsupportedDateRangeError`),
`apps/api/app/backtesting/engine.py` (`run_backtest()` — the
orchestration: fetches closes, generates signals, runs each signal
through `evaluate_trade()` and a fresh `PaperBrokerAdapter`),
`apps/api/app/api/routes/backtests.py` (`router`: `POST /backtests`)
Dependencies: `apps.api.app.marketdata.history_provider.HistoryProvider`
(D021, via `get_history_provider`), `apps.api.app.risk.engine.evaluate_trade`
(D004), `apps.api.app.execution.paper_broker.PaperBrokerAdapter` (D014),
`apps.api.app.auth.dependencies.get_current_user`
Tests: `tests/backtesting/test_metrics.py` (10 pure unit tests, hand-
computed), `tests/backtesting/test_strategy.py` (6 pure unit tests, a
hand-derived crossover sequence), `tests/backtesting/test_engine.py` (5
unit tests against a fake `HistoryProvider`, a hand-computed single round
trip through the real Risk Engine/`PaperBrokerAdapter`), `tests/api/
test_backtests.py` (7 integration tests against real Postgres and a fake
`HistoryProvider`)
Important: no `broker_id`, no `Permission`, no `BrokerGrant` — gated by
`get_current_user` alone, since the endpoint structurally never touches a
real broker's state (see D025 for the full reasoning). `end_date` in the
request must equal today (UTC); `HistoryProvider.get_daily_closes()`
(D021) only returns the most recent N closes as of now, with no
timestamps attached, so an arbitrary past window can't be honestly
served — see D025's "Consequences" for why extending `HistoryProvider`
itself is explicit future work, not solved here. `RiskLimits` for a
backtest set `require_stop_price=False` (the strategy's exit is its SELL
signal, not a stop price) but otherwise reuse `Settings`' real risk
limits unchanged.

## Market data route

Purpose: read-only `GET /market-data/{symbol}/quote`, the HTTP surface
for the Longbridge provider (D015).
Main files: `apps/api/app/api/schemas_marketdata.py` (`QuoteResponse`),
`apps/api/app/api/routes/marketdata.py`
Dependencies: `apps.api.app.api.dependencies.get_market_data_router`
(returns `app.state.market_data_router`, which is `None` when no vendor
built at startup), `apps.api.app.auth.dependencies.get_current_user`
Important: requires *any* authenticated user (`get_current_user`) — no
special permission, since it's read-only and not a trading action. As of
D017, `POST /brokers/{broker_id}/trades` uses this same
`get_market_data_router`/`MarketDataRouter` when a trade omits
`estimated_price` — this endpoint lets a caller preview that price
without submitting a trade.

## Admin routes

Purpose: the minimal HTTP surface that replaces raw SQL for creating,
updating, and deactivating users and roles, and for granting/revoking
broker access (D013, D016). Not a general admin panel.
Main files: `apps/api/app/api/schemas_admin.py` (request/response DTOs —
`CreateUserResponse` deliberately never includes a password or hash;
`UpdateUserRequest`/`UpdateRoleRequest` use Pydantic's `model_fields_set`
convention so an omitted key means "leave untouched" and an explicit
`null` means "clear"), `apps/api/app/api/routes/admin.py`
(`POST /admin/users`, `PATCH /admin/users/{id}`, `POST /admin/roles`,
`PATCH /admin/roles/{id}`, `POST /admin/broker-grants`,
`DELETE /admin/broker-grants/{id}`)
Dependencies: `apps.api.app.auth.dependencies` (`require_permission`),
`apps.api.app.auth.security` (`hash_password`), `apps.api.app.db.models`
Tests: `tests/api/test_admin.py` (16 integration tests against real
Postgres — 9 covering create/grant/revoke, including one that grants then
revokes broker access via the API and confirms trade authorization
actually flips both ways; 7 covering the D016 PATCH endpoints, including
deactivating a user and confirming their already-issued token stops
working immediately, and PATCHing a role's permissions and confirming
every holder's already-issued token reflects it immediately)
Important: the whole router is gated by a single
`Depends(require_permission(Permission.ADMIN))` passed to `APIRouter(...,
dependencies=[...])` — every route inherits it without each function
needing its own parameter for it. **No delete for users/roles, no
listing endpoints** — deliberate scope cut (D013/D016), not an oversight;
deleting a `User`/`Role` row would orphan `orders.submitted_by_user_id`/
`users.role_id`, so `is_active=false` (user) or clearing `permissions`
(role) is the supported way to neutralize one instead. **The very first
admin user and role still require one direct DB insert** — there's no
user holding `admin:manage` to call these routes with the first time.

## Market data

Purpose: vendor routing with an explicit fallback chain and a hard
no-fabrication floor.
Main files: `apps/api/app/marketdata/models.py` (`MarketSnapshot`),
`apps/api/app/marketdata/provider.py` (`MarketDataProvider` Protocol,
`DataUnavailableError`, `VendorError`), `apps/api/app/marketdata/router.py`
(`MarketDataRouter`, `NoDataAvailableError`),
`apps/api/app/marketdata/providers/longbridge.py`
(`LongbridgeMarketDataProvider`, `build_longbridge_provider`)
Dependencies: `longport` (only imported inside `build_longbridge_provider()`,
never by the provider class or by tests — see D015)
Tests: `tests/marketdata/` (9 tests against in-process fake providers),
`tests/marketdata/providers/test_longbridge.py` (8 tests against a fake
`LongbridgeQuoteClient` — never the real SDK, real credentials, or a
network call)
Important: Longbridge is the first concrete `MarketDataProvider` (D015,
closing D008). `build_longbridge_provider(settings)` returns `None`
unless `LONGPORT_APP_KEY`/`LONGPORT_APP_SECRET`/`LONGPORT_ACCESS_TOKEN`
are *all* set — a partial configuration is treated the same as none, not
guessed at. `GET /market-data/{symbol}/quote` exposes it, and as of D017
`POST /brokers/{broker_id}/trades` also uses it when `estimated_price` is
omitted. Only `DataUnavailableError` and `VendorError` trigger fallback
to the next provider; any other exception propagates immediately rather
than being silently treated as "try the next one." When every provider
fails, `NoDataAvailableError`'s message is prefixed `NO_DATA_AVAILABLE:`
by convention — grep for that prefix, don't invent a different sentinel
elsewhere in the codebase. If you add a second provider (e.g. IBKR),
verify its actual SDK surface by installing the real package and
introspecting it directly (`dir()`/`inspect.signature()`) before writing
code against it — two web sources gave conflicting answers for
Longbridge's own SDK, so don't trust documentation alone.

**Historical prices / indicators (D021)**: `apps/api/app/marketdata/history_provider.py`
(`HistoryProvider` Protocol — a price *series*, `get_daily_closes(symbol,
count) -> list[Decimal]` oldest-first, distinct from
`MarketDataProvider`'s single quote), `apps/api/app/marketdata/indicators.py`
(`sma()`, `rsi()`, `InsufficientDataError` — pure functions, no LLM, no
I/O, per `docs/TOKEN_POLICY.md`'s mandatory-deterministic list),
`apps/api/app/marketdata/providers/longbridge.py`'s
`LongbridgeHistoryProvider`/`build_longbridge_history_provider`
(`AsyncQuoteContext.candlesticks(symbol, Period.Day, count,
AdjustType.NoAdjust)`, verified by introspection like the quote provider
— the provider sorts candles by timestamp itself, never trusting the
SDK's return order). `sma()`/`rsi()` raise `InsufficientDataError` rather
than padding when too few closes exist — callers must handle a short
series explicitly. Tests: `tests/marketdata/test_indicators.py` (9),
`tests/marketdata/providers/test_longbridge_history.py` (5, fake client),
`tests/api/test_history_provider_wiring.py` (4, DB-backed).

## Agents

Purpose: the first LLM-backed code in this codebase — a single
`TraderAgent` plus (Phase 16, D019) a single read-only `TechnicalAnalyst`,
not the full analyst/research-debate/portfolio-manager stack the
governing spec eventually wants (D018/D019).
Main files: `apps/api/app/agents/provider.py` (`LLMProvider` Protocol,
`LLMProviderError` — mirrors `marketdata/provider.py`'s shape),
`apps/api/app/agents/anthropic_compatible.py`
(`AnthropicCompatibleProvider`, `build_llm_provider`),
`apps/api/app/agents/parsing.py` (`extract_json_object` — shared by both
agents below, D019), `apps/api/app/agents/trader.py` (`TradeIdea`,
`AgentOutputError`, `TraderAgent`, `build_trader_agent`,
`stop_price_from_distance`), `apps/api/app/agents/technical_analyst.py`
(`TechnicalRead`, `Stance`, `AnalystOutputError`, `TechnicalAnalyst`,
`build_technical_analyst`)
Dependencies: `httpx` (the only network client `AnthropicCompatibleProvider`
uses — not imported by either agent or by tests)
Tests: `tests/agents/test_trader.py` (8 unit tests against a fake
`LLMProvider`), `tests/agents/test_technical_analyst.py` (7 unit tests
against a fake `LLMProvider`), `tests/api/test_agent_trades.py`
(5 integration tests against real Postgres for `POST /brokers/{id}/agent-trades`),
`tests/api/test_technical_analyst_wiring.py` (3 integration tests against
real Postgres proving the optional analyst wiring — no analyst configured
still succeeds without context, a configured analyst's read reaches the
trader agent's prompt, a failing analyst never blocks the trade),
`tests/api/test_history_provider_wiring.py` (4 integration tests, D021 —
see the Market data section above) — none of these are real network
calls, though D021 was separately verified against the real Longbridge
API directly (see D021's Status in `docs/DECISIONS.md`).
Important: `build_llm_provider(settings)` returns `None` unless
`LLM_PROVIDER_BASE_URL`/`_API_KEY`/`_MODEL` are *all* set — same
all-or-nothing posture as Longbridge (D015). The client is deliberately
provider-name-agnostic (not `OMNIROUTE_*`) per `docs/MODEL_ROUTING.md`;
any endpoint speaking the Anthropic Messages API works. `TradeIdea` has
**no price field at all** — the agent proposes `side`/`quantity`/a stop
distance only, price always comes from the same live-quote path D017
uses (`apps/api/app/api/routes/trades.py`'s `_resolve_live_quote`), and
`stop_price_from_distance()` (deterministic, not agent output) converts
the proposed distance into a real stop price against that live price.
The resulting `TradeProposal` goes through the identical
`submit_trade_and_record()` → Risk Engine path a human-submitted trade
uses — no separate or weaker validation for agent-originated trades.
Malformed/unparseable LLM output raises `AgentOutputError`, surfaced as
502 `AGENT_OUTPUT_INVALID:`, never a fabricated trade. OmniRoute was
unreachable in this environment at implementation time, so only the
NOT_CONFIGURED path and fake-provider-backed logic are verified directly
here — not a real vendor completion.
`TechnicalAnalyst` (D019, Phase 16) is read-only by construction:
`TechnicalRead` has **no side/quantity/price field at all**, only
`stance`/`summary`/`confidence` — there is no order-submission path for
this agent to bypass. It shares the same `build_llm_provider(settings)`
connection as `TraderAgent` (no second `LLM_PROVIDER_*` set) since there's
exactly one analyst this phase — `build_technical_analyst(llm_provider)`
returns `None` under the identical NOT_CONFIGURED convention. It never
computes an indicator itself (RSI/MACD/etc must be deterministic code per
`docs/TOKEN_POLICY.md`) — its system prompt explicitly forbids claiming
to calculate or invent one. As of D021, when a `HistoryProvider` is
configured (see the Market data section above), `agent-trades` computes
real `SMA(20)`/`RSI(14)` values deterministically and passes them to
`analyze(indicator_context=...)`, which the analyst may narrate but never
recompute; with no history available it still falls back to commentary
on the one live quote alone, same as D019's original behavior. Wired
into `POST /brokers/{id}/agent-trades` as **optional** context for
`TraderAgent.propose(technical_context=...)` — absence (not configured)
or a raised `AnalystOutputError` (bad/unparseable LLM output) both
silently omit the context rather than blocking the trade or fabricating
a read; only a genuinely successful, schema-valid `TechnicalRead` is
ever passed through.

## Packages (placeholders, not yet populated)

`packages/llm_providers/`, `packages/data_providers/` — destinations for
Apache-2.0-licensed code vendored from TradingAgents once the full
analyst/research-debate layer (beyond the single `TraderAgent` above)
starts. Currently just READMEs. See root `NOTICE`.

## Frontend

Purpose: Phase 17 (D020) first frontend slice — login, health status,
quote lookup, paper-trade submission — extended in Phase 20 (D023) with
an admin UI and an agent-trades UI, and in Phase 24 (D026) with a
real-time portfolio view, each rendering the backend's real response,
never fabricated data.
Root: `apps/web/` — Next.js 16 (App Router), TypeScript, Tailwind v4.

`apps/web/lib/backend.ts` — server-only helpers: `backendBaseUrl()`
(reads `API_BASE_URL`, defaults to `http://localhost:8000`), `backendUrl(path)`,
the shared `AUTH_COOKIE_NAME` constant, and `backendGetWithBody(path,
headers, body)` (D026) — issues a real GET request carrying a JSON body
via Node's core `http`/`https` module, since Node's built-in `fetch`
(undici) throws on any GET/HEAD with a body; used only by the portfolio
route handler below. Never imported from a client component.

`apps/web/proxy.ts` — Next.js 16 proxy convention (replaces the
deprecated `middleware.ts`). `proxy(request)` redirects `/dashboard/*` to
`/login` when the auth cookie is absent. Only checks presence, not
validity — an expired/invalid token still surfaces as the backend's real
401 through the route handlers below, never guessed here.

`apps/web/app/api/auth/login/route.ts` — `POST`, takes `{email, password}`
JSON from the browser, forwards it to the backend as OAuth2
form-encoded (`username=<email>&password=<password>`) per the API
contract, and on success sets the JWT as an httpOnly cookie
(`AUTH_COOKIE_NAME`). On failure, passes through the backend's actual
status and body.
`apps/web/app/api/auth/logout/route.ts` — `POST`, clears the cookie.
`apps/web/app/api/health/route.ts` — `GET`, proxies `GET /health`
(no auth required by contract; still proxied so the browser only ever
talks same-origin).
`apps/web/app/api/quote/[symbol]/route.ts` — `GET`, reads the JWT from
the cookie, calls `GET /market-data/{symbol}/quote` with
`Authorization: Bearer`, passes through the backend's exact status and
body (including 503 `NOT_CONFIGURED:` / 404 `NO_DATA_AVAILABLE:`
details). 401 if the cookie is missing.
`apps/web/app/api/trades/[brokerId]/route.ts` — `POST`, same
cookie-to-Bearer pattern, proxies `POST /brokers/{broker_id}/trades`,
passes through the full `TradeSubmissionResponse` body and status
unmodified.
`apps/web/app/api/agent-trades/[brokerId]/route.ts` — `POST` (D023), same
cookie-to-Bearer pattern, proxies `POST /brokers/{broker_id}/agent-trades`,
passes through the full `AgentTradeResponse` body and status unmodified
(including the 400 `NOT_CONFIGURED:` / 502 `AGENT_OUTPUT_INVALID:`
sentinels).
`apps/web/app/api/admin/users/route.ts` — `POST` (D023), proxies
`POST /admin/users`.
`apps/web/app/api/admin/users/[userId]/route.ts` — `PATCH` (D023),
proxies `PATCH /admin/users/{user_id}`.
`apps/web/app/api/admin/roles/route.ts` — `POST` (D023), proxies
`POST /admin/roles`.
`apps/web/app/api/admin/roles/[roleId]/route.ts` — `PATCH` (D023),
proxies `PATCH /admin/roles/{role_id}`.
`apps/web/app/api/admin/broker-grants/route.ts` — `POST` (D023), proxies
`POST /admin/broker-grants`.
`apps/web/app/api/admin/broker-grants/[grantId]/route.ts` — `DELETE`
(D023), proxies `DELETE /admin/broker-grants/{grant_id}`; re-returns a
204 as `new NextResponse(null, { status: 204 })` since a 204 must carry
no JSON body.
`apps/web/app/api/portfolio/[brokerId]/route.ts` — `POST` (D026), reads
the JWT from the cookie, then calls `backendGetWithBody()` to issue the
real backend call as a `GET /brokers/{broker_id}/portfolio` carrying
`{"marks": {...}}` as a JSON body (D022's contract), passing through the
full `PortfolioSnapshot` body and status unmodified, including 400
`DATA_UNAVAILABLE:` for a missing mark, 403 for missing
`VIEW_PORTFOLIO`/no broker grant, and 404 for an unknown broker.

`apps/web/app/login/page.tsx` — client component, email/password form
posting to `/api/auth/login`; on success routes to `/dashboard`; renders
the real error detail from a failed login (network failure vs backend
401 are distinguished in the UI copy but both render, never silently).
`apps/web/app/dashboard/page.tsx` — server component shell (gated by
`proxy.ts`) composing three client components.
`apps/web/components/HealthStatus.tsx` — fetches `/api/health` on mount,
renders `status`/`trading_mode`/`live_trading_enabled` or a real error.
`apps/web/components/QuoteLookup.tsx` — symbol input, fetches
`/api/quote/[symbol]`, renders the quote or the backend's exact error
`detail` string (with HTTP status prefixed) on 503/404/other failure.
`apps/web/components/TradeForm.tsx` — broker ID + symbol/side/quantity/
optional estimated_price/stop_price form, posts to
`/api/trades/[brokerId]`, renders the full response including a
rejected trade's `block_reason`.
`apps/web/components/LogoutButton.tsx` — posts to `/api/auth/logout`,
redirects to `/login`.
`apps/web/app/page.tsx` — server component, redirects `/` to
`/dashboard` or `/login` based on cookie presence.
`apps/web/components/AgentTradeForm.tsx` (D023) — broker ID + symbol +
directive + a `SYMBOL=price, SYMBOL2=price2` marks field parsed
client-side into the `marks` object, posts to
`/api/agent-trades/[brokerId]`, renders the full `AgentTradeResponse`
(`side`/`quantity`/`rationale` plus the trade-response fields) or the
real `NOT_CONFIGURED:`/`AGENT_OUTPUT_INVALID:` detail on failure.
`apps/web/app/admin/page.tsx` (D023) — reachable by any authenticated
user (gated on cookie presence only, same as `/dashboard`); composes six
forms, two each for users/roles/broker-grants. Never hides itself based
on a client-side permission guess — the real `admin:manage` gate is the
backend's 403 on submit.
`apps/web/components/admin/UsersAdmin.tsx` (D023) — `CreateUserForm`
(email/password/role/active, posts to `/api/admin/users`) and
`UpdateUserForm` (user ID + opt-in checkboxes to change active status
and/or role, posts to `/api/admin/users/[userId]`), both rendering the
real response or error detail.
`apps/web/components/admin/RolesAdmin.tsx` (D023) — `CreateRoleForm`
(name/description/comma-separated permissions, posts to
`/api/admin/roles`) and `UpdateRoleForm` (role ID + opt-in checkboxes to
change description and/or replace permissions, posts to
`/api/admin/roles/[roleId]`).
`apps/web/components/admin/BrokerGrantsAdmin.tsx` (D023) —
`CreateBrokerGrantForm` (user ID + broker ID, posts to
`/api/admin/broker-grants`) and `DeleteBrokerGrantForm` (grant ID, posts
`DELETE` to `/api/admin/broker-grants/[grantId]`, renders a real 204
confirmation or a real error, e.g. 404 for an unknown grant).
`apps/web/components/PortfolioView.tsx` (D026) — broker ID + a
`SYMBOL=price, SYMBOL2=price2` marks field parsed client-side, posts to
`/api/portfolio/[brokerId]`, renders the full `PortfolioSnapshot`
(`cash`, a positions table with every
`symbol`/`quantity`/`avg_cost`/`current_value`/`unrealized_pnl`/
`realized_pnl`, and the three totals) or the real
`DATA_UNAVAILABLE:`/403/404 detail on failure. Composed onto
`/dashboard` alongside the existing components. Real-time snapshot
only — no historical chart (needs Phase 25's persisted snapshots).

Tests: `apps/web/test/QuoteLookup.test.tsx` (4 tests — success render,
503 NOT_CONFIGURED detail rendered verbatim, 404 NO_DATA_AVAILABLE
detail rendered verbatim, network failure renders a real error and never
fabricated data), `apps/web/test/TradeForm.test.tsx` (3 tests — rejected
trade's `block_reason` rendered legibly, filled trade's fill
price/quantity rendered, a real 403 detail rendered),
`apps/web/test/AgentTradeForm.test.tsx` (5 tests, D023 — filled/rejected
rendering, real `NOT_CONFIGURED:`/`AGENT_OUTPUT_INVALID:` sentinels,
network failure), `apps/web/test/UsersAdmin.test.tsx` (6 tests, D023),
`apps/web/test/RolesAdmin.test.tsx` (4 tests, D023),
`apps/web/test/BrokerGrantsAdmin.test.tsx` (6 tests, D023) — the admin
tests cover success, a real 403 (non-admin caller), a real 404/409 where
applicable, and network failure. `apps/web/test/PortfolioView.test.tsx`
(6 tests, D026) — a real rendered snapshot (cash, a full position row,
all three totals), an empty-positions render, the real 400
`DATA_UNAVAILABLE:` sentinel for a missing mark, a real 403, a real 404,
and network failure. Vitest + React Testing Library + jsdom
(`apps/web/vitest.config.ts`, `apps/web/test/setup.ts`), chosen over
Playwright for this phase's scope — see D020.

Important: the JWT never reaches browser-readable storage — it's set as
an httpOnly cookie by `/api/auth/login` and read only inside route
handlers running server-side. Every route handler that talks to the
backend catches its own network failure and returns a
`DATA_UNAVAILABLE:`-prefixed detail rather than letting the client see a
generic Next.js error page. `API_BASE_URL` (server-side env var, see
`apps/web/.env.example`) is the only backend location the app knows.

## Not yet present

The parallel analyst layer, research debate, Portfolio Manager. Frontend
candidates remaining after Phase 20/D023 and Phase 24/D026 (`/admin/*`
UI, agent-trades UI, and the portfolio view are now built — see
`docs/PROJECT_CONTEXT.md`'s Planned Work): a historical performance chart
on the portfolio view (blocked on Phase 25's persisted snapshots), broker
discovery UI, session refresh/expiry UX, Playwright e2e, a
users/roles/grants listing UI. Do not import from paths that don't exist
yet.
