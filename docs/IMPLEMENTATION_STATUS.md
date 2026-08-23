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
- Phase 4: order/fill persistence (2026-08-23). Migration `0002` adds
  append-only `orders`/`fills` tables (NUMERIC money columns, denormalized
  `symbol` — see D006). `apps/api/app/oms/persistence.py` —
  `submit_trade_and_record()` wraps (doesn't modify) `submit_trade()` and
  writes exactly one new `Order` row per call, never an update. Verified
  end-to-end against a real Postgres/TimescaleDB container: migration
  upgrade/downgrade/upgrade round-trip, then 3 integration tests exercising
  the actual insert/query path. Found and fixed two real, previously-latent
  bugs during this verification — see D007: (1) `Enum()` columns were
  sending the Python enum member name instead of its value, silently broken
  since Phase 1 but never triggered until a row was actually inserted; (2)
  the async test suite had a cross-event-loop connection bug on Windows,
  fixed by pinning pytest-asyncio to a session-scoped loop. 8 new tests,
  35/35 total passing, ruff+mypy clean (20 source files).

## In Progress

Nothing currently mid-implementation.

## Blocked

Nothing currently blocked.

## Planned

Phase 5+ (order not finalized): HTTP endpoint that actually calls
`submit_trade_and_record()` (nothing external can submit a trade yet —
everything is exercised only by tests), market-data service with vendor
routing, agent/LLM layer, frontend.

## Technical Debt

None yet — codebase is small enough that debt hasn't accumulated.

## Optimization Tasks

Deferred until an agent/LLM layer exists — see [AI_OPTIMIZATION.md](AI_OPTIMIZATION.md).
Building a token router/cache with nothing to route would be premature
optimization.

## Tests

35 tests, all passing: fail-closed live-mode gate (3), log redaction (1),
health endpoint (1), risk engine (14), paper broker (5), OMS (3), execution
context (5), order/fill persistence (3, DB-backed against real Postgres).

## Known Issues

None open.
