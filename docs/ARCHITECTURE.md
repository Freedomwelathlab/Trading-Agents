# Architecture

## Current (Phase 1-7)

```mermaid
flowchart LR
    client[Client] --> login["POST /auth/login"]
    login --> token[JWT access token]
    token --> route["POST /brokers/{id}/trades\nrequires Bearer token"]
    client --> health[GET /health]
    route --> settings[Settings\nfail-closed live gate\n+ risk limit defaults\n+ fail-closed JWT secret]
    route --> db[(Postgres/TimescaleDB)]

    vendors[No vendor wired yet\nD008] -.-> router[MarketDataRouter\nno-fabrication fallback]
    router -.-> snapshot[MarketSnapshot]
    snapshot -.would feed.-> proposal

    proposal[TradeProposal] --> persist[OMS submit_trade_and_record]
    persist --> oms[submit_trade]
    oms --> risk[Risk Engine\nevaluate_trade]
    risk -->|approved| broker[PaperBrokerAdapter\nvia PaperBrokerRegistry]
    risk -->|blocked| rejected[RiskDecision: rejected]
    broker --> fill[Fill]
    persist --> orders[(orders / fills\nappend-only)]
    route --> proposal
```

`apps/api/app/main.py` boots the app, logs startup mode, creates the
`PaperBrokerRegistry`, exposes `/health` and both routers (`/auth/login`,
the trades router). `apps/api/app/core/config.py` is the single source of
the execution-mode gate, default risk limits, emergency-stop flag, and now
also `jwt_secret_key` — fail-closed the same way, no default.
The trading path requires a Bearer token from `/auth/login` — verified
live against a running server (401 with no token, 200 with a real one, the
resulting order's `submitted_by_user_id` confirmed via SQL). The
market-data router (dotted lines, left) is built and tested but has no
provider plugged in and nothing yet calls it to build a `TradeProposal`;
the HTTP endpoint currently takes price/timestamp directly from the caller
instead (future work once a vendor is chosen, D008). The
`PaperBrokerAdapter`/`PaperBrokerRegistry` still don't persist
cash/position state (D005/D009) even though the `Order`/`Fill` decision
record now does — a restart resets accounts silently while the audit trail
survives. No registration endpoint or role-based authorization exists yet
(D010) — any authenticated user can trade on any broker_id they know.

## Target (per governing spec, not yet built)

```mermaid
flowchart TB
    subgraph analysts[Analyst layer - parallel]
        tech[Technical]
        fund[Fundamental]
        news[News]
        sent[Sentiment]
    end
    analysts --> research[Research debate\nbull vs bear]
    research --> trader[Trader agent\nproposal only]
    trader --> risk[Deterministic Risk Engine\nnon-LLM, fail-closed]
    risk -->|approved| portfolio[Portfolio Manager]
    risk -->|blocked| blocked[Trade blocked\nlogged, not silent]
    portfolio --> oms[OMS]
    oms --> broker[Broker Adapter\npaper or live]
```

The Risk Engine is the load-bearing boundary in this diagram — everything
above it can be wrong or down without capital risk; everything below it must
still fail closed if the Risk Engine itself is unreachable.

## Database

Postgres 16 + TimescaleDB. Current tables: `users`, `roles`, `assets`,
`brokers` (migration `0001_initial`). Money fields will use `NUMERIC`, never
float, once P&L/position tables exist (spec requirement, not yet
applicable — no such tables yet).

## Deployment

`docker-compose.yml` runs Postgres, Redis, and the API locally. No
staging/production deployment config exists yet.
