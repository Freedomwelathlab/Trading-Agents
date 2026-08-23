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

Phases 1-13 complete: repo skeleton, deterministic risk engine, paper
broker + OMS, order/fill persistence, market-data routing, HTTP trade
submission, authentication, role-based authorization, per-broker access
grants, a minimal admin API (now including update/deactivate), persisted
paper-broker state, and a real Longbridge market-data provider. See
[IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).
`POST /brokers/{broker_id}/trades` requires a Bearer token, a role
granting `trade:submit:paper`, AND an explicit grant for that specific
`broker_id` — all of which can be set up via `/admin/*` routes after one
bootstrap admin is created by direct SQL (D013). A user can now be
deactivated and a role's permissions changed via `PATCH /admin/users/{id}`
and `PATCH /admin/roles/{id}` (D016), taking effect on the affected
user's very next request — no re-login needed, since `get_current_user`
never trusts a cached JWT claim. A trader's cash and positions live in
`broker_accounts`/`broker_positions` (D014) and survive a process
restart. `GET /market-data/{symbol}/quote` (D015) is wired to Longbridge
but not yet connected to trade-proposal construction — an explicit open
decision, not an oversight (no real Longbridge credentials exist in this
environment, so only the "not configured" path and the routing logic
against a fake client were verified directly).

## Planned Work

Phase 14+: connecting the market-data router to trade-proposal
construction (D015), agent/LLM layer, frontend. Order not finalized.

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
  broker grants, all gated by `admin:manage`. User/role update and
  deactivate: done (D016) — `PATCH /admin/users/{id}` and
  `PATCH /admin/roles/{id}`, closing D013's deliberate scope cut.
  Persisted paper-broker state: done (D014) — `PaperBrokerRegistry` is
  gone, cash/positions live in Postgres and survive a restart. Still
  open: no delete endpoints for users/roles (would orphan FKs — D016),
  and the very first admin user/role still requires one direct DB insert
  to bootstrap.
- Market data vendor: done (D015) — Longbridge, chosen by the user over
  IBKR/a free API. `GET /market-data/{symbol}/quote` works when
  `LONGPORT_APP_KEY`/`LONGPORT_APP_SECRET`/`LONGPORT_ACCESS_TOKEN` are all
  set; 503 `NOT_CONFIGURED` otherwise. Still open: whether/how a live
  quote should feed `TradeSubmissionRequest.estimated_price`/
  `market_data_as_of` when the caller omits them, versus always requiring
  an explicit caller-supplied price as today — a load-bearing semantic
  choice about what's authoritative for a fill, deliberately not decided
  as a side effect of wiring the vendor.

## Important Constraints

Everything in [TRADING_SAFETY.md](TRADING_SAFETY.md), plus: no live trading
credentials in any fixture or dev file (spec §51); never fabricate market
data, fills, P&L, or broker responses (spec §57) — show `NOT CONFIGURED` /
`DATA_UNAVAILABLE` instead.
