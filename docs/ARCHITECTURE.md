# Architecture

## Current (Phase 1-9)

```mermaid
flowchart LR
    client[Client] --> login["POST /auth/login"]
    login --> token[JWT access token]
    token --> perm["require_permission\ntrade:submit:paper"]
    perm -->|403 if missing| grantcheck["require_broker_access\nbroker exists? grant exists?"]
    grantcheck -->|404 / 403| route["POST /brokers/{id}/trades"]
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
The trading path requires a Bearer token from `/auth/login`, a role
granting `trade:submit:paper`, AND an explicit `BrokerGrant` for that
specific `broker_id` — verified live against a running server with two
real broker rows: the same trader got 200 on the one they were granted
and 403 on the one they weren't (the resulting order's
`submitted_by_user_id` also confirmed via SQL). The market-data router
(dotted lines, left) is built and tested but has no provider plugged in
and nothing yet calls it to build a `TradeProposal`; the HTTP endpoint
currently takes price/timestamp directly from the caller instead (future
work once a vendor is chosen, D008). The
`PaperBrokerAdapter`/`PaperBrokerRegistry` still don't persist
cash/position state (D005/D009) even though the `Order`/`Fill` decision
record now does — a restart resets accounts silently while the audit
trail survives. No admin endpoint exists for any of users/roles/grants
(D010/D011/D012) — all created by direct DB insert.

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
`brokers` (`0001`), `orders`, `fills` (`0002`), `broker_grants` (`0005`).
Money columns (`orders`/`fills`) already use `NUMERIC`, never float.

## Deployment

`docker-compose.yml` runs Postgres, Redis, and the API locally. No
staging/production deployment config exists yet.
