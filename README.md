# Trading OS — AI Trading Operating System

A modular, multi-asset AI-assisted trading platform. Built fresh (not a fork)
per the discovery findings in [`ARCHITECTURE-DISCOVERY-REPORT.md`](../ARCHITECTURE-DISCOVERY-REPORT.md),
vendoring select reusable components from
[TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) (Apache-2.0, see `NOTICE`).

## Non-negotiable safety rules (governing spec §3, §46, §56, §57, §62)

- An LLM is never the sole authority to submit a live order. AI decision,
  deterministic risk validation, and order execution are separate layers.
- The system fails closed: if safety cannot be verified, the trade is blocked.
- `TRADING_MODE` defaults to `research`; `LIVE_TRADING_ENABLED` defaults to
  `false`. Enabling live trading requires an explicit, out-of-band
  confirmation step that does not exist yet — it is intentionally not built.
- No fabricated prices, fills, P&L, or broker responses. Unconfigured or
  unavailable data is surfaced as `NOT CONFIGURED` / `DATA_UNAVAILABLE`, never
  invented.

## Status: Phase 1 — repo & data-layer skeleton

This is the foundation only: FastAPI skeleton, typed settings with a
fail-closed live-mode gate, secret-redacting structured logging, SQLAlchemy
2.0 async models (`users`, `roles`, `assets`, `brokers`), and the first
Alembic migration. **No agents, no risk engine, no broker adapters, and no
order path exist yet.**

## Local development

```bash
cp .env.example .env
docker compose up -d postgres redis
pip install -e ".[dev]"
alembic upgrade head
uvicorn apps.api.app.main:app --reload
```

Run tests: `pytest`. Lint: `ruff check .`. Type-check: `mypy apps packages`.

## Layout

```
apps/api/            FastAPI service
  app/core/          settings, logging
  app/db/            SQLAlchemy models + session
migrations/          Alembic migrations
packages/            vendored/reusable libraries (llm_providers, data_providers — placeholders)
tests/               pytest suite
```
