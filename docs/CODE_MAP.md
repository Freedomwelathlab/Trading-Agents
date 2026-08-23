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
`apps/api/app/db/models.py` (`User`, `Role`, `Asset`, `Broker`)
Dependencies: `apps.api.app.core.config`
Tests: none yet at the DB layer (models are exercised via migration
round-trip, not unit tests — see `migrations/versions/0001_initial.py`)
Important: `Broker.kind` (paper/live) is the structural mechanism meant to
keep live and paper credentials from ever occupying the same row. Any future
broker-credential table must preserve this separation, not merge it.

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

## Packages (placeholders, not yet populated)

`packages/llm_providers/`, `packages/data_providers/` — destinations for
Apache-2.0-licensed code vendored from TradingAgents once the agent/data
layer phase starts. Currently just READMEs. See root `NOTICE`.

## Not yet present

Risk engine, OMS, broker adapters, market-data service, agent/LLM layer,
frontend, auth. Do not import from paths that don't exist yet.
