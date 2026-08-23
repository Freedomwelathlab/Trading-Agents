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

## In Progress

Nothing currently mid-implementation.

## Blocked

Nothing currently blocked. (Prior GitHub push-hygiene incident — remote
`main` accidentally overwritten with an unrelated personal work-folder
history, briefly public — resolved 2026-08-22 via force-push of clean
history. User still owns rotating credentials in the `.env` files that were
exposed; that's outside what code/docs can verify.)

## Planned

Phase 3+ (order not finalized): broker adapter (paper first), OMS wired to
the risk engine, market-data service with vendor routing, agent/LLM layer,
frontend. The risk engine currently has no caller — nothing in Phase 3 may
submit an order without going through `evaluate_trade()` first.

## Technical Debt

None yet — codebase is small enough that debt hasn't accumulated.

## Optimization Tasks

Deferred until an agent/LLM layer exists — see [AI_OPTIMIZATION.md](AI_OPTIMIZATION.md).
Building a token router/cache with nothing to route would be premature
optimization.

## Tests

19 tests, all passing: fail-closed live-mode gate (3), log redaction (1),
health endpoint (1), risk engine (14). No integration tests yet (nothing to
integrate — risk engine has no caller).

## Known Issues

None open.
