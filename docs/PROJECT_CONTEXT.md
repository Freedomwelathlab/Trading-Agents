# Project Context

## Mission

An AI-assisted, multi-asset trading platform, built fresh (not a fork) after
discovery found [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
is a research pipeline with no OMS, risk engine, or execution layer. Full
findings: [`../../ARCHITECTURE-DISCOVERY-REPORT.md`](../../ARCHITECTURE-DISCOVERY-REPORT.md).
Full spec: `../../MASTER CLAUDE CODE PROMPT — WALL STREET AI TRADING OPERATING SYSTEM.md`
(63 sections; treat as the governing document for anything not covered here).

## Current Architecture

Modular monolith, ports-and-adapters. Today: FastAPI app + Postgres/TimescaleDB
+ Redis, no agent layer yet. See [ARCHITECTURE.md](ARCHITECTURE.md).

## Major Decisions

See [DECISIONS.md](DECISIONS.md) — D001 (build fresh, vendor reusable
TradingAgents pieces) and D002 (typed execution-mode gate) so far.

## Agent Organization

Not built yet. Planned per spec: analyst team (technical/fundamental/news/
sentiment) → research (bull/bear debate) → trader → deterministic risk gate →
portfolio manager → OMS → broker adapter. None of this exists in code today —
do not assume agent modules are implemented.

## Trading Workflow (target, not yet built)

Data → Analysts → Research debate → Trader proposal → **Risk Engine
(deterministic, non-LLM)** → Portfolio → OMS → Broker adapter.

## Safety Rules

Non-negotiable, see [TRADING_SAFETY.md](TRADING_SAFETY.md). Already enforced
in code: `TradingMode.LIVE` cannot construct without `LIVE_TRADING_ENABLED=true`
(`apps/api/app/core/config.py`), and structured logs redact
secret-shaped keys (`apps/api/app/core/logging.py`).

## Technology Stack

FastAPI, Pydantic v2/Pydantic Settings, SQLAlchemy 2.0 async, asyncpg,
Alembic, Postgres 16 + TimescaleDB, Redis 7, structlog, pytest, ruff, mypy.
Frontend (Next.js/TypeScript, per spec) not started.

## Current Status

Phases 1-11 complete: repo skeleton, deterministic risk engine, paper
broker + OMS, order/fill persistence, market-data routing skeleton, HTTP
trade submission, authentication, role-based authorization, per-broker
access grants, a minimal admin API, persisted paper-broker state. See
[IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).
`POST /brokers/{broker_id}/trades` requires a Bearer token, a role
granting `trade:submit:paper`, AND an explicit grant for that specific
`broker_id` — all of which can be set up via `/admin/*` routes after one
bootstrap admin is created by direct SQL (D013). A trader's cash and
positions now live in `broker_accounts`/`broker_positions` (D014) and
survive a process restart — verified by killing and restarting a live
server mid-test and confirming cumulative cash/positions carried over.
No real market-data vendor is wired (D008) — open decision for the user.

## Planned Work

Phase 12+: a concrete market-data vendor adapter (once chosen), user/role
update+deactivate endpoints (D013's deliberately-cut scope), agent/LLM
layer, frontend. Order not finalized.

## Known Problems

None open. (`Freedomwelathlab/Trading-Agents` had its `main` accidentally
overwritten with an unrelated personal work-folder history and was briefly
public — resolved 2026-08-22 via force-push of clean history. Credential
rotation for the briefly-exposed `.env` files is the user's action, outside
what this repo's docs can verify.)

## Open Decisions

- Whether `packages/llm_providers` / `packages/data_providers` get vendored
  from TradingAgents now or deferred until the agent layer phase (currently
  leaning deferred — no consumer exists yet).
- Risk-engine rule set is decided and implemented (D004). Still open:
  duplicate-order detection and an explicit emergency-stop *source*
  (currently just a boolean parameter on `evaluate_trade`/`submit_trade` —
  where it reads from in production is undecided).
- Order/fill persistence: done (D006). HTTP endpoint: done (D009).
  Authentication: done (D010). Authorization: done (D011) — requires the
  `trade:submit:paper` permission. Per-broker access grants: done (D012).
  Minimal admin API: done (D013) — create users/roles, create+revoke
  broker grants, all gated by `admin:manage`. Persisted paper-broker
  state: done (D014) — `PaperBrokerRegistry` is gone, cash/positions
  live in Postgres and survive a restart. Still open: no update or
  deactivate for users/roles (D013's deliberate scope cut), and the very
  first admin user/role still requires one direct DB insert to bootstrap.
- **Which market data vendor to wire is an open decision requiring user
  input (D008).** The routing/normalization contract is built and tested;
  no concrete provider exists. Options include a paid data vendor, a free
  public API, or the Longbridge/IBKR connections already available
  elsewhere in this user's tooling (those are MCP tools reachable in chat,
  not currently something this FastAPI service calls — wiring one in would
  need its own auth/rate-limit handling inside this app).

## Important Constraints

Everything in [TRADING_SAFETY.md](TRADING_SAFETY.md), plus: no live trading
credentials in any fixture or dev file (spec §51); never fabricate market
data, fills, P&L, or broker responses (spec §57) — show `NOT CONFIGURED` /
`DATA_UNAVAILABLE` instead.
