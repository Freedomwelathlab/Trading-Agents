# Module Map

System-level view. "—" means not built yet; do not assume it exists.

| Module | Responsibility | Inputs | Outputs | Depends on | Status |
|---|---|---|---|---|---|
| API (`apps/api`) | HTTP entrypoint, settings, logging | HTTP requests | JSON | DB | Phase 1 |
| Database | Persist users/roles/assets/brokers | ORM calls | Rows | Postgres | Phase 1 |
| Frontend | — | — | — | API | Not started |
| Agents | — | — | — | LLM providers, market data | Not started |
| Research (debate) | — | — | — | Agents | Not started |
| Risk Engine | Deterministic trade validation, fail-closed | Trade proposal, account state | Approve/block | Portfolio, market data | Phase 2 (done) |
| Portfolio | Track positions/P&L | Fills, prices | Position/P&L state | Risk Engine, Broker | Not started — paper broker tracks cash/positions itself for now, no separate portfolio module |
| Execution/OMS | Order lifecycle, structurally enforces risk gate | Approved trade | Fill or rejection | Risk Engine, Broker Adapter | Phase 3-4 (done) — paper broker, persisted to `orders`/`fills`. No HTTP entrypoint yet. |
| Market Data | Vendor routing, no-fabrication sentinels | Vendor APIs | Normalized snapshots | External vendors | Not started |
| Backtesting | — | — | — | Market Data, Risk Engine | Not started |
| Reporting | — | — | — | Portfolio | Not started |
| Redis | Caching (future) | — | — | — | Provisioned in docker-compose, unused by app code |
| Auth | — | — | — | Database (`users`, `roles` tables exist) | Tables exist, no auth logic |
| Observability | Structured logging with redaction | Log calls | JSON logs | — | Phase 1 (logging only; no metrics/tracing) |
