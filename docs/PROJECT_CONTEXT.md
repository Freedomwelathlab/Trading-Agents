# Project Context

## Mission

An AI-assisted, multi-asset trading platform, built fresh (not a fork) after
discovery found [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
is a research pipeline with no OMS, risk engine, or execution layer. Full
findings: [`../../ARCHITECTURE-DISCOVERY-REPORT.md`](../../ARCHITECTURE-DISCOVERY-REPORT.md).
Full spec: `../../MASTER CLAUDE CODE PROMPT — WALL STREET AI TRADING OPERATING SYSTEM.md`
(63 sections; treat as the governing document for anything not covered here).

## Current Architecture

Modular monolith, ports-and-adapters. Today: FastAPI app + Postgres/TimescaleDB
+ Redis + a first, narrow agent layer (TraderAgent, TechnicalAnalyst — D018,
D019). See [ARCHITECTURE.md](ARCHITECTURE.md) for the diagram and how this
compares to the full target.

## Major Decisions

See [DECISIONS.md](DECISIONS.md) — D001 through D019 so far; see
"Current Status" below and IMPLEMENTATION_STATUS.md for the running summary.

## Agent Organization

`TraderAgent` (D018) and `TechnicalAnalyst` (D019) exist — a proposal-only
agent and a single read-only analyst, both routed through
`AnthropicCompatibleProvider`. Not yet built: the full parallel analyst team
(fundamental/news/sentiment — blocked on real data providers,
`packages/data_providers/` is still an empty placeholder), research
(bull/bear debate), and Portfolio Manager. Do not assume those modules are
implemented.

## Trading Workflow (target, not yet built in full)

Data → Analysts (one exists: TechnicalAnalyst) → Research debate (not built)
→ Trader proposal (TraderAgent exists) → **Risk Engine (deterministic,
non-LLM, implemented)** → Portfolio (not built) → OMS (implemented) → Broker
adapter (paper only, implemented).

## Safety Rules

Non-negotiable, see [TRADING_SAFETY.md](TRADING_SAFETY.md). Already enforced
in code: `TradingMode.LIVE` cannot construct without `LIVE_TRADING_ENABLED=true`
(`apps/api/app/core/config.py`), and structured logs redact
secret-shaped keys (`apps/api/app/core/logging.py`).

## Technology Stack

FastAPI, Pydantic v2/Pydantic Settings, SQLAlchemy 2.0 async, asyncpg,
Alembic, Postgres 16 + TimescaleDB, Redis 7, structlog, pytest, ruff, mypy.
Frontend: Next.js 16 (App Router)/TypeScript/Tailwind v4 in `apps/web/`
(D020) — Next.js route handlers proxy every backend call server-side so
the JWT can live in an httpOnly cookie rather than browser-readable
storage; Vitest + React Testing Library for component tests.

## Current Status

Phases 1-17 complete: repo skeleton, deterministic risk engine, paper
broker + OMS, order/fill persistence, market-data routing, HTTP trade
submission, authentication, role-based authorization, per-broker access
grants, a minimal admin API (now including update/deactivate), persisted
paper-broker state, a real Longbridge market-data provider, market data
connected to trade submission, the first agent (a single `TraderAgent`),
the first (one-analyst) slice of the parallel analyst layer (a single
read-only `TechnicalAnalyst`), and the first frontend (`apps/web/`). See
[IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).
`POST /brokers/{broker_id}/trades` requires a Bearer token, a role
granting `trade:submit:paper`, AND an explicit grant for that specific
`broker_id` — all of which can be set up via `/admin/*` routes after one
bootstrap admin is created by direct SQL (D013). A user can now be
deactivated and a role's permissions changed via `PATCH /admin/users/{id}`
and `PATCH /admin/roles/{id}` (D016), taking effect on the affected
user's very next request — no re-login needed, since `get_current_user`
never trusts a cached JWT claim. A trader's cash and positions live in
`broker_accounts`/`broker_positions` (D014) and survive a process
restart. `GET /market-data/{symbol}/quote` (D015) is wired to Longbridge,
and `estimated_price` on a trade submission is now optional (D017) — if
the caller omits it, the same router supplies a live price and timestamp
together; if supplied, the caller's price remains fully authoritative,
unchanged from every phase before D017. Real paper-trading Longbridge
credentials were supplied and verified live 2026-08-24 (local, gitignored
`.env`, never committed) — both the read-only quote endpoint and an
omitted-price trade returned genuine live Longbridge prices, not just the
"not configured" path and fake-provider tests this was previously limited
to. `POST /brokers/{broker_id}/agent-trades` (D018) is the first
LLM-backed code in the codebase — a single `TraderAgent` proposes
side/quantity/a stop distance from a symbol + free-text directive, price
still always comes from the same live-quote path (never the agent), and
the result goes through the identical Risk Engine path a human-submitted
trade uses. OmniRoute was unreachable in this environment at
implementation time, so this route's "provider actually configured" path
is verified only against a fake provider in tests, not a real completion
yet. Phase 16 (D019) adds the first analyst: `TechnicalAnalyst.analyze()`
reads the same live quote `agent-trades` already fetches and writes a
short, structured, read-only `TechnicalRead` (`stance`/`summary`/
`confidence` — no side/quantity/price field). It computes no indicator
itself (RSI/MACD/etc must be deterministic code once real historical
price data exists — `packages/data_providers/` is still an empty
placeholder). When configured (same `LLM_PROVIDER_*` connection as
`TraderAgent`, not a separate one), its read is passed to
`TraderAgent.propose()` as optional prompt context; absent or failed, the
trade proceeds exactly as it did before Phase 16, no context appended,
never blocked. Only a `TechnicalAnalyst` was built this phase — no
`FundamentalAnalyst`/`NewsAnalyst`/`SentimentAnalyst`, since no real data
source for any of those is wired into this codebase (spec §57's
no-fabrication rule).

## Planned Work

Phase 18+: the rest of the parallel analyst layer (fundamental/news/
sentiment — each blocked on a real, wired data source per spec §57),
research debate, and Portfolio Manager per the governing spec's "Target"
architecture. Order not finalized. Also pending: re-verify D018/D019's
live-provider paths once OmniRoute (or another compatible endpoint) is
reachable; if a second analyst is ever added, revisit whether
parallel-execution/fan-out infrastructure across analysts is now
warranted (deliberately not built in Phase 16 — one analyst has nothing
to parallelize against). Frontend v2 candidates (D020): admin UI,
agent-trades UI, broker discovery (no such endpoint exists yet), session
refresh/expiry UX, a real Docker-backed e2e verification pass, Playwright
if a protectable flow emerges.

`apps/web/` (D020, Phase 17) is the first frontend — a minimal Next.js
app covering login, `/health` display, quote lookup, and paper-trade
submission, each rendering the backend's real response shape (including
`NOT_CONFIGURED:`/`NO_DATA_AVAILABLE:` sentinel details and a rejected
trade's `block_reason`) rather than any fabricated data. The JWT is
stored in an httpOnly cookie set by a Next.js route handler that proxies
`/auth/login`; all other authenticated calls go through further route
handlers (`/api/health`, `/api/quote/[symbol]`, `/api/trades/[brokerId]`)
that read the cookie server-side and forward the request — the browser
never holds the token directly. `npm run build` passes with zero type
errors; 7 Vitest component tests cover the quote/trade success and error
rendering paths. Not yet verified end-to-end against a running backend
(Docker Desktop's daemon was unreachable during the Phase 17 build) —
only `npm run dev` + curl against the Next.js app itself was confirmed;
a real Docker-backed login→quote→trade round-trip is still pending.

## Known Problems

None open. (`Freedomwelathlab/Trading-Agents` had its `main` accidentally
overwritten with an unrelated personal work-folder history and was briefly
public — resolved 2026-08-22 via force-push of clean history. Credential
rotation for the briefly-exposed `.env` files is the user's action, outside
what this repo's docs can verify.)

## Open Decisions

- Whether `packages/llm_providers` / `packages/data_providers` get vendored
  from TradingAgents now or deferred until the full parallel-analyst layer
  is built (currently leaning deferred — the single `TraderAgent` shipped
  in D018 has no need for either package yet).
- First agent: done (D018) — a single `TraderAgent` (side/quantity/stop
  distance only, never price), reachable via
  `POST /brokers/{broker_id}/agent-trades`, going through the same Risk
  Engine path as a human-submitted trade.
- First analyst: done (D019) — a single read-only `TechnicalAnalyst`
  (`stance`/`summary`/`confidence`, never price/side/quantity), wired as
  optional context into `TraderAgent.propose()`. Still open: fundamental/
  news/sentiment analysts (each blocked on a real wired data source),
  research debate, and Portfolio Manager from the governing spec's target
  architecture are not started; re-verifying D018/D019 against a real
  OmniRoute completion (only fake-provider-verified so far, OmniRoute
  unreachable at implementation time).
- Risk-engine rule set is decided and implemented (D004). Still open:
  duplicate-order detection and an explicit emergency-stop *source*
  (currently just a boolean parameter on `evaluate_trade`/`submit_trade` —
  where it reads from in production is undecided).
- Order/fill persistence: done (D006). HTTP endpoint: done (D009).
  Authentication: done (D010). Authorization: done (D011) — requires the
  `trade:submit:paper` permission. Per-broker access grants: done (D012).
  Minimal admin API: done (D013) — create users/roles, create+revoke
  broker grants, all gated by `admin:manage`. User/role update and
  deactivate: done (D016) — `PATCH /admin/users/{id}` and
  `PATCH /admin/roles/{id}`, closing D013's deliberate scope cut.
  Persisted paper-broker state: done (D014) — `PaperBrokerRegistry` is
  gone, cash/positions live in Postgres and survive a restart. Still
  open: no delete endpoints for users/roles (would orphan FKs — D016),
  and the very first admin user/role still requires one direct DB insert
  to bootstrap.
- Market data vendor: done (D015) — Longbridge, chosen by the user over
  IBKR/a free API. `GET /market-data/{symbol}/quote` works when
  `LONGPORT_APP_KEY`/`LONGPORT_APP_SECRET`/`LONGPORT_ACCESS_TOKEN` are all
  set; 503 `NOT_CONFIGURED` otherwise. Connected to trade submission: done
  (D017) — `estimated_price` is optional on `POST /brokers/{id}/trades`;
  omitted, a live quote supplies both price and `market_data_as_of`
  together; supplied, the caller's price stays fully authoritative and
  the vendor is never consulted, unchanged from every phase before D017.

## Important Constraints

Everything in [TRADING_SAFETY.md](TRADING_SAFETY.md), plus: no live trading
credentials in any fixture or dev file (spec §51); never fabricate market
data, fills, P&L, or broker responses (spec §57) — show `NOT CONFIGURED` /
`DATA_UNAVAILABLE` instead.
