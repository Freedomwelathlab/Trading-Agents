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
`apps/api/app/db/models.py` (`User`, `Role`, `Asset`, `Broker`, `Order`, `Fill`)
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
not a DB trigger, so don't add one.
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
Current head: `0001_initial` (users, roles, assets, brokers)
Important: enum columns use `create_type=False` on the Python-side ENUM
object to avoid a double-CREATE-TYPE error against `create_table` — see the
comment history in `0001_initial.py` if adding a new enum column. Current
head: `0002_orders_fills` (adds `orders`, `fills`).

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
(`PaperBrokerAdapter`)
Dependencies: `apps.api.app.risk.models` (reuses `Side`, `AccountState`)
Tests: `tests/execution/test_paper_broker.py`
Important: in-memory only, not persisted — see `docs/DECISIONS.md` D005.
Market orders only; limit orders raise rather than fake a fill. No short
selling. `get_account_state()` requires a mark for every open position and
raises rather than guessing a stale/missing price.

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
`submit_trade_and_record` anywhere an audit trail is needed (which, once an
HTTP endpoint exists, should be everywhere reachable from outside the
process).

## Execution context

Purpose: structural guarantee that a research/backtest code path can't hold
a live credential (`ResearchContext | PaperContext | LiveContext`).
Main file: `apps/api/app/core/execution_context.py`
Interface: `build_execution_context(settings, *, broker_id=None)`
Tests: `tests/core/test_execution_context.py`
Important: `LiveContext` construction re-checks `live_trading_enabled` even
though `Settings` already enforces it — deliberate defense in depth
(spec §46), not redundant code to simplify away.

## Market data

Purpose: vendor routing with an explicit fallback chain and a hard
no-fabrication floor.
Main files: `apps/api/app/marketdata/models.py` (`MarketSnapshot`),
`apps/api/app/marketdata/provider.py` (`MarketDataProvider` Protocol,
`DataUnavailableError`, `VendorError`), `apps/api/app/marketdata/router.py`
(`MarketDataRouter`, `NoDataAvailableError`)
Dependencies: none beyond stdlib/pydantic — no HTTP client, no vendor SDK
Tests: `tests/marketdata/` (9 tests, all against in-process fake providers)
Important: **no concrete `MarketDataProvider` is implemented or wired** —
see `docs/DECISIONS.md` D008. Only `DataUnavailableError` and `VendorError`
trigger fallback to the next provider; any other exception propagates
immediately rather than being silently treated as "try the next one." When
every provider fails, `NoDataAvailableError`'s message is prefixed
`NO_DATA_AVAILABLE:` by convention — grep for that prefix, don't invent a
different sentinel elsewhere in the codebase.

## Packages (placeholders, not yet populated)

`packages/llm_providers/`, `packages/data_providers/` — destinations for
Apache-2.0-licensed code vendored from TradingAgents once the agent/data
layer phase starts. Currently just READMEs. See root `NOTICE`.

## Not yet present

Order/fill persistence, market-data service, agent/LLM layer, frontend,
auth. Do not import from paths that don't exist yet.
