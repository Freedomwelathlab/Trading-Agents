# Module Map

System-level view. "—" means not built yet; do not assume it exists.

| Module | Responsibility | Inputs | Outputs | Depends on | Status |
|---|---|---|---|---|---|
| API (`apps/api`) | HTTP entrypoint, settings, logging, trade submission | HTTP requests | JSON | DB, OMS, Risk Engine, Broker Registry, Auth | Phase 1, 6, 7 |
| Database | Persist users/roles/assets/brokers/orders/fills | ORM calls | Rows | Postgres | Phase 1, 4 |
| Frontend | — | — | — | API | Not started |
| Agents | — | — | — | LLM providers, market data | Not started |
| Research (debate) | — | — | — | Agents | Not started |
| Risk Engine | Deterministic trade validation, fail-closed | Trade proposal, account state | Approve/block | Portfolio, market data | Phase 2 (done) |
| Portfolio | Track positions/P&L | Fills, prices | Position/P&L state | Risk Engine, Broker | Not started — paper broker tracks cash/positions itself for now, no separate portfolio module |
| Execution/OMS | Order lifecycle, structurally enforces risk gate | Approved trade | Fill or rejection | Risk Engine, Broker Adapter | Phase 3-4, 6 (done) — paper broker, persisted to `orders`/`fills`, reachable via `POST /brokers/{broker_id}/trades` |
| Market Data | Vendor routing, no-fabrication sentinels | Vendor APIs | Normalized snapshots | External vendors | Phase 5 (done) — routing/normalization contract only, no vendor wired (D008) |
| Backtesting | — | — | — | Market Data, Risk Engine | Not started |
| Reporting | — | — | — | Portfolio | Not started |
| Redis | Caching (future) | — | — | — | Provisioned in docker-compose, unused by app code |
| Auth | Password hashing, JWT tokens, `get_current_user` | Email/password | Bearer token / authenticated `User` | Database (`users` table) | Phase 7 (done) — authentication only, no registration, no authorization/roles |
| Observability | Structured logging with redaction | Log calls | JSON logs | — | Phase 1 (logging only; no metrics/tracing) |
