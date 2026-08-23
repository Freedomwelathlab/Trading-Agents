# Implementation Status

Update this after meaningful implementation work — not for every commit.

## Completed

- Phase 1: repo skeleton, typed `Settings` with fail-closed live-mode gate,
  secret-redacting structlog, SQLAlchemy 2.0 async models
  (`users`/`roles`/`assets`/`brokers`), migration `0001_initial`
  (verified: upgrade/downgrade/upgrade round-trip against real
  Postgres/TimescaleDB), `GET /health`, CI (ruff/mypy/pytest/migration
  check/secret scan), docker-compose. 5/5 tests passing, ruff+mypy clean.
- Bootstrap: documentation set + root `CLAUDE.md` (2026-08-23).
- Phase 2: deterministic risk engine (2026-08-23). `apps/api/app/risk/`
  (`models.py`, `engine.py`) — pure function, zero LLM/network/I-O
  dependency by construction. Rules enforced: emergency stop, market-data
  freshness, required stop price, non-zero stop distance, max
  single-position size (% of equity), max portfolio exposure (% of
  equity), max per-trade risk (% of equity ÷ stop distance), buying-power
  check. Every rejection returns a typed `BlockReason`, never a bare
  `False`. 14 tests including one that operationalizes the discovery
  report's MVP acceptance criterion (blocks a bad trade with an LLM stub
  that raises). 19/19 total tests passing, ruff+mypy clean.
- Phase 3: paper broker adapter + OMS (2026-08-23).
  `apps/api/app/execution/{broker,paper_broker}.py` — `BrokerAdapter`
  Protocol + deterministic in-memory `PaperBrokerAdapter` (market orders
  only, no shorting, requires a mark for every open position, never
  fabricates a fill price). `apps/api/app/oms/service.py` —
  `submit_trade()`, the single sanctioned path from a proposal to a broker
  call; it always calls the risk engine first and a rejected proposal
  never reaches the broker (proven with a spy adapter in tests). Also
  added `apps/api/app/core/execution_context.py` — typed
  `ResearchContext | PaperContext | LiveContext` so a research code path
  can't structurally hold a live credential. 13 new tests, 32/32 total
  passing, ruff+mypy clean (19 source files).

## In Progress

Nothing currently mid-implementation.

## Blocked

Nothing currently blocked.

## Planned

Phase 4+ (order not finalized): order/fill persistence (see D005 — paper
broker is in-memory only today), market-data service with vendor routing,
agent/LLM layer, frontend.

## Technical Debt

None yet — codebase is small enough that debt hasn't accumulated.

## Optimization Tasks

Deferred until an agent/LLM layer exists — see [AI_OPTIMIZATION.md](AI_OPTIMIZATION.md).
Building a token router/cache with nothing to route would be premature
optimization.

## Tests

32 tests, all passing: fail-closed live-mode gate (3), log redaction (1),
health endpoint (1), risk engine (14), paper broker (5), OMS (3), execution
context (5). No DB-backed integration tests yet — order/fill persistence
doesn't exist (D005).

## Known Issues

None open.
