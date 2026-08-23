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

Phase 1 (repo skeleton) and Phase 2 (deterministic risk engine) complete.
See [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).

## Planned Work

Phase 3+: broker adapter (paper first), OMS wired to the risk engine,
market-data service, agent/LLM layer, frontend. Order not finalized.

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
- Risk-engine rule set is now decided and implemented (D004) for the
  position-size/exposure/per-trade-risk/freshness/stop-price rules. Still
  open: duplicate-order detection and an explicit emergency-stop *source*
  (currently just a boolean parameter — where it reads from in Phase 3 is
  undecided).

## Important Constraints

Everything in [TRADING_SAFETY.md](TRADING_SAFETY.md), plus: no live trading
credentials in any fixture or dev file (spec §51); never fabricate market
data, fills, P&L, or broker responses (spec §57) — show `NOT CONFIGURED` /
`DATA_UNAVAILABLE` instead.
