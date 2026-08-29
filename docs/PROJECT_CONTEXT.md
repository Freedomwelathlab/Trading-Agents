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
non-LLM, implemented)** → Portfolio Manager (deterministic, non-LLM,
implemented — Phase 26/D029) → OMS (implemented) → Broker adapter (paper
only, implemented).

## Safety Rules

Non-negotiable, see [TRADING_SAFETY.md](TRADING_SAFETY.md). Already enforced
in code: `TradingMode.LIVE` cannot construct without `LIVE_TRADING_ENABLED=true`
(`apps/api/app/core/config.py`), and structured logs redact
secret-shaped keys (`apps/api/app/core/logging.py`).

## Technology Stack

FastAPI, Pydantic v2/Pydantic Settings, SQLAlchemy 2.0 async, asyncpg,
Alembic, Postgres 16 + TimescaleDB, Redis 7, structlog, pytest, ruff, mypy.
Frontend: Next.js 16 (App Router)/TypeScript/Tailwind v4 in `apps/web/`
(D020, extended D023) — Next.js route handlers proxy every backend call
server-side so the JWT can live in an httpOnly cookie rather than
browser-readable storage; Vitest + React Testing Library for component
tests.

## Current Status

Phases 1-26 complete (19-22 built in parallel worktrees, merged
2026-08-28; phase 23 built in its own worktree alongside sibling
phase-24/25 work; phase 26 in its own worktree alongside sibling
phase-27/28 work): repo skeleton, deterministic risk engine, paper broker
+ OMS, order/fill persistence, market-data routing, HTTP trade
submission, authentication, role-based authorization, per-broker access
grants, a minimal admin API (now including update/deactivate), persisted
paper-broker state, a real Longbridge market-data provider, market data
connected to trade submission, the first agent (a single `TraderAgent`),
the first (one-analyst) slice of the parallel analyst layer (a single
read-only `TechnicalAnalyst`), the first frontend (`apps/web/`), real
historical prices feeding that analyst's narration, a real-time Portfolio
module (D022), a frontend v2 (admin UI + agent-trades UI, D023),
deterministic duplicate-order detection in the Risk Engine (D024), and a
backtesting engine replaying one hard-coded SMA(20)-crossover strategy
through the real Risk Engine and paper-broker fill math (D025). See
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
no-fabrication rule). Phase 18 (D021) closes the "no historical price
data" gap D019 flagged: `HistoryProvider`/`LongbridgeHistoryProvider`
fetch real daily closes via the same Longbridge SDK, and
`marketdata/indicators.py`'s pure `sma()`/`rsi()` compute real values
from them — the LLM never calculates, only narrates. Verified live
against the real Longbridge API directly (not just fakes): real
`AAPL.US` closes produced genuine `SMA(20)`/`RSI(14)` values.

Phase 19 (D022) builds the first Portfolio module — deterministic,
read-only, no LLM: `compute_portfolio_snapshot()` reads
`broker_accounts`/`broker_positions` (current cash/quantities) and
replays `orders`/`fills` per symbol for average-cost-basis `avg_cost`/
realized P&L, exposed via `GET /brokers/{broker_id}/portfolio` (a new,
weaker `Permission.VIEW_PORTFOLIO` rather than reusing
`SUBMIT_PAPER_TRADE`). This is the read-only reporting gap
`docs/MODULE_MAP.md` flagged, not the trade-path "Portfolio Manager" the
governing spec's Target architecture places between the Risk Engine and
OMS — that component still doesn't exist and Phase 19 doesn't touch the
trade path at all. Real-time snapshot only at the time; no persisted
history yet, so backtesting/alerts/performance-attribution-over-time were
explicit future work, not built speculatively.

Phase 25 (D027) closes that persisted-history gap: `POST
/brokers/{broker_id}/portfolio/snapshots` computes a snapshot via the
same `compute_portfolio_snapshot()` and persists it as an append-only row
(`portfolio_snapshots` + a child `portfolio_snapshot_positions` table,
chosen over a JSON column for queryability - see D027); `GET
/brokers/{broker_id}/portfolio/history` reads them back,
oldest-to-newest, paginated. Still gated by the same
`Permission.VIEW_PORTFOLIO` - no new permission was needed (D027).
Phase 25 was deliberately manual-only (a snapshot existed only because a
caller explicitly POSTed it); Phase 27 (D030) closes that gap with an
opt-in, default-OFF in-process asyncio scheduler
(`apps/api/app/portfolio/scheduler.py`, started by the FastAPI lifespan,
`PORTFOLIO_SNAPSHOT_SCHEDULER_ENABLED` /
`PORTFOLIO_SNAPSHOT_INTERVAL_SECONDS`, no new dependency). Since a
scheduler has no caller to supply `marks`, it sources a real quote for
every held symbol from the existing `MarketDataRouter` (D015/D017) and
**skips the broker for that cycle, writing nothing**, if any held symbol
cannot be priced - returning a typed `ScheduledSnapshotOutcome` and
logging the unpriced symbols at warning level rather than substituting
any price (spec Sec57). A cash-only broker needs no marks and is captured
normally. Capture runs the same `compute_portfolio_snapshot()` and the
same (newly extracted) `persist_portfolio_snapshot()` the manual POST
uses, so scheduled and manual rows are indistinguishable in the history.
Live-verified with the real app + real Postgres: a flat broker was
auto-snapshotted every cycle with real numbers from real fills, while a
broker holding `AAPL.US` was skipped every cycle
(`skipped_market_data_not_configured`) with an empty history - no
Longbridge credentials exist in this environment, so the real-vendor
*capture* path is covered by integration tests (real router, fake only at
the vendor boundary) rather than live. Backtesting/alerts/performance-attribution
themselves are still not built; Phase 25 only gives them a real time
series to eventually read.

Phase 23 (D025) closes that "backtesting" future-work note with the
one-strategy v1 it deliberately deferred to: `apps/api/app/backtesting/`
replays a single hard-coded SMA(20)-crossover strategy against real
historical closes (`HistoryProvider`, D021) through the real Risk Engine
(`evaluate_trade`, D004) and a fresh in-memory `PaperBrokerAdapter` per
run (D014) — structurally never touching `broker_accounts`/
`broker_positions`. `POST /backtests` (gated by `get_current_user` alone
— no broker scoping, no new `Permission`, since it moves no real capital)
returns a `BacktestResult` with a full equity curve, total return, trade
count, win rate, and max drawdown, every formula hand-verified in tests.
`HistoryProvider`'s "most recent N closes as of now" contract (no
per-close timestamps, no arbitrary historical window) means `end_date`
must equal today — a real "backtest an arbitrary past quarter" feature
needs `HistoryProvider` itself extended first, noted as explicit future
work rather than solved by fabricating dates on real prices. Live-verified
against real Longbridge data: a real `AAPL.US` backtest produced a
genuine equity curve with one real, risk-gated trade.

Phase 31 (D037) puts a **frontend on that endpoint**, closing the gap
D035 recorded honestly at the time ("`apps/web/` still does not render
the backtest response at all, let alone the new counters").
`components/BacktestPanel.tsx` on `/dashboard`, behind
`app/api/backtests/route.ts` — the same route-handler-proxy +
httpOnly-cookie pattern every other authenticated feature uses, minus a
`brokerId` segment because the endpoint is deliberately not broker-scoped
(D025). It renders the real `BacktestResult`: a hand-rolled inline-SVG
equity curve in `PortfolioHistoryChart.tsx`'s style (no new npm
dependency, one vertex per real trading day), the headline metrics, and
all three of D035's Portfolio Manager counters — kept distinct, always
shown even at zero, captioned to say that Risk Engine rejections are a
separate gate counted by none of them. Every real error sentinel is
surfaced verbatim and no error path ever draws a curve. No backend change
was needed. Live-verified only as far as this environment honestly
allows: with no market-data vendor credentials readable, the real chain
(browser -> route handler -> containerized API) rendered a real
`NOT_CONFIGURED: no history provider.` and a real 422; the success path
is component-test-covered against mocked responses, not a live run.

Phase 26 (D029) builds the trade-path **Portfolio Manager** — the box
every prior phase's docs kept flagging as missing. `apps/api/app/
portfolio_manager/` is a pure `decide(proposal, portfolio, limits) ->
PortfolioDecision` (no LLM, no network, no I/O), run inside
`oms/service.py`'s `submit_trade()` after the Risk Engine approves a
proposal and before the broker call. It can APPROVE, shrink (MODIFY), or
REJECT, enforcing per-symbol concentration vs equity, a cash-reserve
floor, and a distinct-open-positions cap — the aggregate-portfolio
questions a per-trade risk check structurally cannot see. Spec §18's
correlation, sector concentration, volatility, expected return and
drawdown are deliberately not implemented: no sector column and no
persisted per-symbol return series exist, and approximating them would be
fabrication. A MODIFY's resized proposal is re-run through the Risk
Engine before any broker call, so no quantity this system produces —
including one it produced itself — reaches a broker ungated;
`apps/api/app/risk/engine.py` was not modified. Every decision persists
to `orders.portfolio_*` (migration `0009`) as spec §18's audit record.
This is a different package from the read-only reporting `portfolio/`
module (D022/D027) and shares no code with it.

Phase 32 (D038) closes the frontend half of D029's own recorded gap.
`apps/web/components/PortfolioVerdict.tsx` is one shared panel rendered by
both `TradeForm` and `AgentTradeForm`, additive to the existing risk-verdict
block rather than a replacement: a MODIFY shows the binding constraint and
both quantities (requested vs actually filled) so a resize is legible; a
REJECT is violet and labelled "not the Risk Engine", deliberately distinct
from a risk rejection's amber; an APPROVE adds nothing; and a null or
missing `portfolio_action` renders as nothing at all — never as approval,
since null means the Portfolio Manager never ran. The result block's colour
is now keyed on `approved` (the risk verdict) rather than on `status`, so
amber always means the Risk Engine said no and the D029 combination
`approved: true` + `status: "rejected"` is no longer attributed to the
wrong gate. No backend change was needed; the four fields already existed.

## Planned Work

Phase 29+: the rest of the parallel analyst layer (fundamental/news/
sentiment — each blocked on a real, wired data source per spec §57) and
research debate. Order not finalized. Both the trade-path Portfolio
Manager (Phase 26/D029) and automatic/scheduled portfolio snapshotting
(Phase 27/D030) are now done — remaining follow-ons: `backtesting/` has
gated on the Portfolio Manager since Phase 30/D035, the backtest panel
shows its aggregate counters since Phase 31/D037, and the trade forms in
`apps/web/` now render the Portfolio Manager's verdict for a live trade
too (Phase 32/D038, additive alongside the existing risk verdict). The
scheduler's interval is
plain wall-clock (not market-hours-aware), and each API worker process
runs its own independent scheduler loop (so >1 worker would multiply
snapshot rows). Also pending: re-verify D018/D019's
live-provider paths once OmniRoute (or another compatible endpoint) is
reachable; if a second analyst is ever added, revisit whether
parallel-execution/fan-out infrastructure across analysts is now
warranted (deliberately not built in Phase 16 — one analyst has nothing
to parallelize against). Frontend candidates remaining after Phase
20/D023, Phase 24/D026, Phase 28/D031/D032 and Phase 29/D034 (admin UI,
agent-trades UI, the portfolio view, a historical performance chart, a
users/roles/grants listing UI, session expiry UX, and broker discovery
are now all built): Playwright e2e coverage. Playwright was deliberately
skipped in Phase 28 because it needs a new tool dependency, and remains
open pending explicit permission to add one.

Phase 29 (D034) added broker discovery: `GET /brokers` and
`GET /brokers/{broker_id}`, authentication-only and scoped to the
calling user's own `BrokerGrant` rows (grants define what is
discoverable), plus a `BrokerDiscovery` UI that pre-fills a real granted
broker id into the trade/portfolio forms. Pre-filling authorizes
nothing — `require_broker_access` still re-checks every request.

`apps/web/` (D020, Phase 17; extended Phase 20/D023, Phase 24/D026) is
the frontend — login, `/health` display, quote lookup, paper-trade
submission, agent-trade proposal, an admin UI for every `/admin/*`
endpoint, and a real-time portfolio view against Phase 19/D022's
`GET /brokers/{id}/portfolio`, each rendering the backend's real response
shape (including
`NOT_CONFIGURED:`/`NO_DATA_AVAILABLE:`/`AGENT_OUTPUT_INVALID:`/
`DATA_UNAVAILABLE:` sentinel details, a rejected trade's `block_reason`,
and a non-admin's real 403) rather than any fabricated data. The JWT is
stored in an httpOnly cookie
set by a Next.js route handler that proxies `/auth/login`; every other
authenticated call goes through its own route handler that reads the
cookie server-side and forwards the request — the browser never holds
the token directly. The `/admin` page is reachable by any authenticated
user (gated on cookie presence, same as `/dashboard`) — the frontend
never hides the admin nav based on a guess about the viewer's
permissions, since the real `admin:manage` gate is the backend's 403 on
submit, which every admin form renders honestly. `npm run build` passes
with zero type errors; 28 Vitest component tests (7 original + 21 new)
cover success, error-sentinel, 403/404/409, and network-failure
rendering paths across every form. Verified end-to-end against a real
running backend in this worktree (D020's original Docker-unreachable gap
is closed, and D023 re-confirms it for the new endpoints): real
login → admin create/update user/role → create/revoke broker grant →
agent-trade `NOT_CONFIGURED:` → non-admin 403, all through the app's own
route handlers, never the backend directly.

Phase 28 (D031/D032) is frontend v3, closing three of those four
candidates without adding a single new dependency. The historical
performance chart is a hand-rolled inline SVG polyline of `total_equity`
over `captured_at` reading Phase 25/D027's
`GET /brokers/{id}/portfolio/history` - every vertex is one real
persisted snapshot, nothing interpolated or extended past the last real
capture, and an empty history renders as "nothing captured yet" rather
than a zeroed curve. The users/roles/grants listing UI required closing
D013's no-listing scope cut first: `GET /admin/users`, `GET /admin/roles`
and `GET /admin/broker-grants` (D031) are read-only, carry no new
permission (they inherit the router's existing `admin:manage`
dependency), paginate on the same `limit`/`offset` convention as D027,
and reuse the existing response DTOs so no password or hash can leak
into a listing. That closes the real usability gap D023/D026 both
flagged - `PATCH /admin/users/{id}` and
`DELETE /admin/broker-grants/{grant_id}` previously needed an id no part
of the product could show you. Session expiry UX (D032) adds
`GET /auth/session`, which returns expiry metadata only - never the
token, never a renewed one, and 401 for an expired token or a
deactivated user. It exists because the JWT is in an httpOnly cookie
(D020) that client JS cannot read, so any "expiring soon" warning would
otherwise have been a fabricated local countdown; `SessionStatus` shows
the server's own `expires_in_seconds` and re-polls rather than ticking
down locally. A shared `handleExpiredSession()` now turns any route
handler's 401 into a redirect to `/login?reason=session-expired` with a
real explanation instead of a bare "HTTP 401". There is still no
refresh-token or sliding-expiry mechanism - D010's fixed-lifetime access
token is unchanged, and the new endpoint reports on a session, it cannot
extend one. 226 backend tests and 61 frontend tests pass; ruff/mypy
clean; `npm run build` has zero type errors. Verified live against a
real stack and a real browser (see D031/D032). Playwright/e2e was
deliberately skipped - it needs a new tool dependency.

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
  optional context into `TraderAgent.propose()`. Real historical prices
  for it: done (D021) — `HistoryProvider`/`LongbridgeHistoryProvider` +
  deterministic `sma()`/`rsi()`, verified live against real Longbridge
  data. Still open: fundamental/news/sentiment analysts (each blocked on
  a real wired data source), research debate, and Portfolio Manager from
  the governing spec's target architecture are not started; re-verifying
  D018/D019's LLM-completion path against a real OmniRoute completion
  (only fake-provider-verified so far, OmniRoute unreachable at
  implementation time — D021 confirmed the Longbridge/history side works
  with real data, this gap is specifically about the LLM call itself).
- Risk-engine rule set is decided and implemented (D004). Duplicate-order
  detection: done (D024) — same broker+symbol+side+quantity+estimated_price
  within a 5s window, compared only against FILLED orders (a REJECTED
  order's identical retry is deliberately not itself flagged), the
  recent-orders query supplied to the still-pure `evaluate_trade()` as
  plain data by the caller. Still open: an explicit emergency-stop
  *source* (currently just a boolean parameter on
  `evaluate_trade`/`submit_trade` — where it reads from in production is
  undecided).
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
