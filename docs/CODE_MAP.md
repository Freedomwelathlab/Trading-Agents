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
Current head: `0006_broker_state` (`0001` users/roles/assets/brokers,
`0002` orders/fills, `0003` adds `orders.submitted_by_user_id`, `0004` adds
`roles.permissions`, `0005` adds `broker_grants`, `0006` adds
`broker_accounts`/`broker_positions`)
Important: enum columns use `create_type=False` on the Python-side ENUM
object to avoid a double-CREATE-TYPE error against `create_table` — see the
comment history in `0001_initial.py` if adding a new enum column.

## Risk Engine

Purpose: deterministic validation of a proposed trade — the boundary spec
Sec3/Sec46 require between any LLM and a real order.
Main files: `apps/api/app/risk/models.py` (`TradeProposal`, `AccountState`,
`RiskLimits`, `RiskDecision`, `BlockReason`), `apps/api/app/risk/engine.py`
(`evaluate_trade()`)
Interface: `evaluate_trade(proposal, account, limits, *, emergency_stop_active=False, now=None) -> RiskDecision`
Dependencies: none — pure function, no LLM/network/DB/I-O import. This is
load-bearing, not incidental: don't add an import here that could make the
engine unable to run with every external service down.
Tests: `tests/risk/test_engine.py` (14 tests, including one that proves the
engine still blocks a bad trade when a stubbed LLM provider raises)
Important: every rejection is a typed `BlockReason`, never a bare `False` —
add a new enum member rather than reusing an existing reason for a new rule.
`RiskLimits` has no defaults; an unconfigured limit is a bug to fix, not a
value to fall back to. The engine never resizes an order itself
(`max_quantity_allowed` is informational only) — a caller must submit a new,
explicit proposal, keeping the audit trail honest about what was actually
approved.

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
Interface: `submit_trade(proposal, account, limits, broker, *, emergency_stop_active=False, now=None) -> OMSResult`
Dependencies: `apps.api.app.risk.engine`, `apps.api.app.execution.broker`
Tests: `tests/oms/test_service.py` — includes a spy broker adapter proving a
rejected proposal never reaches `submit_order()`.

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
Main files: `apps/api/app/api/schemas.py` (`TradeSubmissionRequest`,
`TradeSubmissionResponse`), `apps/api/app/api/dependencies.py`
(`require_broker_access`, `AuthorizedBroker`),
`apps/api/app/api/routes/trades.py` (`POST /brokers/{broker_id}/trades`)
Dependencies: `apps.api.app.oms.persistence`, `apps.api.app.execution.persistence`,
`apps.api.app.db.models`, `apps.api.app.core.config`, `apps.api.app.auth.dependencies`
Tests: `tests/api/test_trades.py` (14 integration tests against a real
Postgres instance, using `httpx.AsyncClient` + `ASGITransport` — see the
Important note below, and D009)
Important: this route is the *only* sanctioned way to reach
`submit_trade_and_record()` from outside the process. `require_broker_access`
(`apps/api/app/api/dependencies.py`, D012) is the single dependency that
decides whether a request may touch a given `broker_id` at all — identity
→ global `trade:submit:paper` permission (D010, D011) → broker existence
(404) → a specific `BrokerGrant` row (403) — and hands the route back an
`AuthorizedBroker(user, broker)` so the route no longer looks up the
broker itself. Every persisted order still records `submitted_by_user_id`.
**Testing an async endpoint that also touches the DB directly in the same
test must use `httpx.AsyncClient(transport=ASGITransport(app=app), ...)`,
never FastAPI's `TestClient`** — `TestClient` runs the app in a separate
thread with its own event loop, which crashes against the shared, loop-bound
DB engine the same way D007's bug did. See `tests/api/test_trades.py`'s
`api_client()` helper for the pattern (manually driving
`app.router.lifespan_context(app)` since `AsyncClient` doesn't do that
automatically the way `TestClient` does).

## Market data route

Purpose: read-only `GET /market-data/{symbol}/quote`, the HTTP surface
for the Longbridge provider (D015).
Main files: `apps/api/app/api/schemas_marketdata.py` (`QuoteResponse`),
`apps/api/app/api/routes/marketdata.py`
Dependencies: `apps.api.app.api.dependencies.get_market_data_router`
(returns `app.state.market_data_router`, which is `None` when no vendor
built at startup), `apps.api.app.auth.dependencies.get_current_user`
Important: requires *any* authenticated user (`get_current_user`) — no
special permission, since it's read-only and not a trading action.
Deliberately separate from the trades endpoint; see the Market data
section above for why the two aren't connected yet.

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
guessed at. `GET /market-data/{symbol}/quote` exposes it but **nothing in
the trading path calls it** — `POST /brokers/{broker_id}/trades` still
takes price/timestamp from the caller, unchanged since Phase 6; whether
to connect the two is an explicit open decision (D015), not a gap to
"complete." Only `DataUnavailableError` and `VendorError` trigger
fallback to the next provider; any other exception propagates
immediately rather than being silently treated as "try the next one."
When every provider fails, `NoDataAvailableError`'s message is prefixed
`NO_DATA_AVAILABLE:` by convention — grep for that prefix, don't invent a
different sentinel elsewhere in the codebase. If you add a second
provider (e.g. IBKR), verify its actual SDK surface by installing the
real package and introspecting it directly (`dir()`/`inspect.signature()`)
before writing code against it — two web sources gave conflicting
answers for Longbridge's own SDK, so don't trust documentation alone.

## Packages (placeholders, not yet populated)

`packages/llm_providers/`, `packages/data_providers/` — destinations for
Apache-2.0-licensed code vendored from TradingAgents once the agent/data
layer phase starts. Currently just READMEs. See root `NOTICE`.

## Not yet present

Order/fill persistence, market-data service, agent/LLM layer, frontend,
auth. Do not import from paths that don't exist yet.
