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

Phase 1 complete: repo skeleton, DB models (users/roles/assets/brokers),
first migration, `/health` endpoint. See [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).

## Planned Work

Phase 2: deterministic risk engine. Phase 3+: broker adapter (paper first),
OMS, market-data service, agent/LLM layer, frontend. Order not finalized
beyond Phase 2 — the spec's MVP acceptance test is the risk engine blocking a
bad trade with every LLM stubbed to raise, which is why it comes before any
agent code.

## Known Problems

- `Freedomwelathlab/Trading-Agents` had its `main` accidentally overwritten
  with an unrelated personal work-folder history (2026-08-22); repo was
  briefly public with that content. Remediation (force-push clean history +
  credential rotation) — check current status before assuming this is fixed.

## Open Decisions

- Exact risk-engine rule set (position sizing formula, exposure limit
  shape) — proposed as a plan before Phase 2 code, not decided yet.
- Whether `packages/llm_providers` / `packages/data_providers` get vendored
  from TradingAgents now or deferred until the agent layer phase (currently
  leaning deferred — no consumer exists yet).

## Important Constraints

Everything in [TRADING_SAFETY.md](TRADING_SAFETY.md), plus: no live trading
credentials in any fixture or dev file (spec §51); never fabricate market
data, fills, P&L, or broker responses (spec §57) — show `NOT CONFIGURED` /
`DATA_UNAVAILABLE` instead.
