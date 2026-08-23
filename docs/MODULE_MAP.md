# Module Map

System-level view. "—" means not built yet; do not assume it exists.

| Module | Responsibility | Inputs | Outputs | Depends on | Status |
|---|---|---|---|---|---|
| API (`apps/api`) | HTTP entrypoint, settings, logging, trade submission, admin | HTTP requests | JSON | DB, OMS, Risk Engine, Broker Registry, Auth | Phase 1, 6, 7, 10, 13 |
| Database | Persist users/roles/assets/brokers/orders/fills | ORM calls | Rows | Postgres | Phase 1, 4 |
| Frontend | — | — | — | API | Not started |
| Agents | — | — | — | LLM providers, market data | Not started |
| Research (debate) | — | — | — | Agents | Not started |
| Risk Engine | Deterministic trade validation, fail-closed | Trade proposal, account state | Approve/block | Portfolio, market data | Phase 2 (done) |
| Portfolio | Track positions/P&L | Fills, prices | Position/P&L state | Risk Engine, Broker | Not started — paper broker tracks cash/positions itself for now, no separate portfolio module |
| Execution/OMS | Order lifecycle, structurally enforces risk gate | Approved trade | Fill or rejection | Risk Engine, Broker Adapter | Phase 3-4, 6, 11 (done) — paper broker, persisted to `orders`/`fills` (decision audit) AND `broker_accounts`/`broker_positions` (current cash/positions), reachable via `POST /brokers/{broker_id}/trades`; survives a restart |
| Market Data | Vendor routing, no-fabrication sentinels | Vendor APIs | Normalized snapshots | Longbridge (`longport` SDK) | Phase 5, 12 (done) — Longbridge wired (D015), reachable via `GET /market-data/{symbol}/quote`; not yet connected to trade submission |
| Backtesting | — | — | — | Market Data, Risk Engine | Not started |
| Reporting | — | — | — | Portfolio | Not started |
| Redis | Caching (future) | — | — | — | Provisioned in docker-compose, unused by app code |
| Auth | Password hashing, JWT tokens, `get_current_user`, `require_permission`, `require_broker_access` | Email/password | Bearer token / authenticated+authorized `User` scoped to a `broker_id` | Database (`users`, `roles`, `broker_grants` tables) | Phase 7-10, 13 (done) — admin API exists incl. update/deactivate (D013, D016), bootstrap admin still needs SQL |
| Observability | Structured logging with redaction | Log calls | JSON logs | — | Phase 1 (logging only; no metrics/tracing) |
