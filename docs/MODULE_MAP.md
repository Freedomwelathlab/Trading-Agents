# Module Map

System-level view. "—" means not built yet; do not assume it exists.

| Module | Responsibility | Inputs | Outputs | Depends on | Status |
|---|---|---|---|---|---|
| API (`apps/api`) | HTTP entrypoint, settings, logging, trade submission, admin | HTTP requests | JSON | DB, OMS, Risk Engine, Broker Registry, Auth, Agents | Phase 1, 6, 7, 10, 13, 15, 16, 18 |
| Database | Persist users/roles/assets/brokers/orders/fills | ORM calls | Rows | Postgres | Phase 1, 4 |
| Frontend | — | — | — | API | Not started |
| Agents | Draft a trade idea (side/quantity/stop distance) from a symbol + directive; never authoritative for price/risk | Symbol, directive text | `TradeIdea` (structured, validated) | LLM provider (Anthropic Messages API-compatible), Risk Engine (validates the idea downstream) | Phase 15 (done) — single `TraderAgent` only (D018), reachable via `POST /brokers/{broker_id}/agent-trades` |
| Analysts (parallel layer) | Read-only market commentary — Phase 16/18 has one: `TechnicalAnalyst`, narrating a live quote plus (when available) real computed SMA/RSI | Symbol, live price, quote timestamp, optional real indicator values | `TechnicalRead` (`stance`/`summary`/`confidence`, structured, validated) — no side/quantity/price field | LLM provider (same connection as Agents); History Provider (optional, feeds real indicators — Phase 18); consumed as optional prompt context by `TraderAgent.propose()`, never a trusted or required input | Phase 16, 18 (done) — single `TechnicalAnalyst` only (D019/D021); no fundamental/news/sentiment analyst exists — no real data source for them yet (`packages/data_providers/` still empty) |
| Research (debate) | — | — | — | Agents, Analysts | Not started |
| Risk Engine | Deterministic trade validation, fail-closed | Trade proposal, account state | Approve/block | Portfolio, market data | Phase 2 (done) |
| Portfolio | Track positions/P&L | Fills, prices | Position/P&L state | Risk Engine, Broker | Not started — paper broker tracks cash/positions itself for now, no separate portfolio module |
| Execution/OMS | Order lifecycle, structurally enforces risk gate | Approved trade | Fill or rejection | Risk Engine, Broker Adapter | Phase 3-4, 6, 11 (done) — paper broker, persisted to `orders`/`fills` (decision audit) AND `broker_accounts`/`broker_positions` (current cash/positions), reachable via `POST /brokers/{broker_id}/trades`; survives a restart |
| Market Data | Vendor routing, no-fabrication sentinels; historical price series + deterministic indicators (D021) | Vendor APIs | Normalized snapshots; daily closes, `sma()`/`rsi()` | Longbridge (`longport` SDK) | Phase 5, 12, 14, 18 (done) — Longbridge wired (D015), reachable via `GET /market-data/{symbol}/quote`; connected to trade submission for an omitted `estimated_price` (D017); `HistoryProvider`/`LongbridgeHistoryProvider` add real daily closes for deterministic SMA/RSI (D021), verified live against real Longbridge data |
| Backtesting | — | — | — | Market Data, Risk Engine | Not started |
| Reporting | — | — | — | Portfolio | Not started |
| Redis | Caching (future) | — | — | — | Provisioned in docker-compose, unused by app code |
| Auth | Password hashing, JWT tokens, `get_current_user`, `require_permission`, `require_broker_access` | Email/password | Bearer token / authenticated+authorized `User` scoped to a `broker_id` | Database (`users`, `roles`, `broker_grants` tables) | Phase 7-10, 13 (done) — admin API exists incl. update/deactivate (D013, D016), bootstrap admin still needs SQL |
| Observability | Structured logging with redaction | Log calls | JSON logs | — | Phase 1 (logging only; no metrics/tracing) |
