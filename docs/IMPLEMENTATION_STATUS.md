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
- Phase 5: market data service (2026-08-23).
  `apps/api/app/marketdata/{models,provider,router}.py` —
  `MarketDataProvider` Protocol, `MarketDataRouter` (ordered fallback,
  typed `DataUnavailableError`/`VendorError` vs. any other exception,
  which is never caught), `MarketSnapshot` (normalized quote type).
  **No concrete vendor is wired** — this is deliberately a routing/
  no-fabrication skeleton only; see D008 for why and what's still open.
  9 new tests (all against in-process fakes, no network calls), 44/44
  total passing (41 non-DB + 3 DB-backed, run separately since they need
  a live Postgres), ruff+mypy clean (24 source files).
- Phase 6: HTTP endpoint for trade submission (2026-08-23).
  `POST /brokers/{broker_id}/trades` (`apps/api/app/api/routes/trades.py`)
  — the first externally-reachable path to `submit_trade_and_record()`.
  Looks up the broker (404 if missing, 400 `NOT_CONFIGURED` if not a paper
  broker — no live execution path exists), resolves a per-broker
  `PaperBrokerAdapter` from a new in-process `PaperBrokerRegistry`, builds
  `RiskLimits`/emergency-stop from new `Settings` fields, and returns the
  risk decision plus fill (if any). A missing mark for an existing
  position is a 400 `DATA_UNAVAILABLE`, never a guessed price. Verified
  live against a real running server + Postgres (manual curl, not just
  tests): a fill and a rejection both persisted correctly, readable
  directly via SQL. Found and fixed a second cross-event-loop issue (see
  D009) — FastAPI's `TestClient` runs in its own thread/loop, incompatible
  with the shared DB engine the same way D007's bug was; fixed by using
  `httpx.AsyncClient` + `ASGITransport` instead, which was the correct
  test client for an async app all along. 6 new integration tests, 50/50
  total passing, ruff+mypy clean (30 source files).
- Phase 7: authentication (2026-08-23). `apps/api/app/auth/` — bcrypt
  password hashing, JWT access tokens (`PyJWT`), `POST /auth/login`
  (OAuth2 password flow), and `get_current_user` — now required by
  `POST /brokers/{broker_id}/trades`. `Settings.jwt_secret_key` has no
  default; the app refuses to start without one configured (see D010).
  Migration `0003` adds `orders.submitted_by_user_id`, set on every
  submission. No registration endpoint exists (users are created by
  direct DB insert — deliberate, see D010) and no authorization/role
  checks exist yet (only *authenticate*, not *authorize*). Verified live
  against a running server: unauthenticated request → 401, authenticated
  request with a real token from `/auth/login` → 200 filled, with
  `submitted_by_user_id` confirmed via SQL to match the authenticated
  user. 12 new tests, 67/67 total passing, ruff+mypy clean (36 source
  files).
- Phase 8: role-based authorization (2026-08-23). Migration `0004` adds
  `roles.permissions` (a flat list of permission strings — data-only to
  extend, no migration needed to grant an existing permission to a role).
  `apps/api/app/auth/permissions.py`'s `Permission` enum defines the known
  values (`SUBMIT_PAPER_TRADE`; `SUBMIT_LIVE_TRADE` defined but enforced
  nowhere — no live path exists to gate). New
  `require_permission(Permission)` dependency returns 403 (distinct from
  401) for an authenticated user whose role doesn't grant the permission;
  `POST /brokers/{broker_id}/trades` now requires
  `Permission.SUBMIT_PAPER_TRADE`. `get_current_user` now eager-loads
  `User.role`. 5 new tests (3 pure unit tests against the checker logic,
  2 integration), 72/72 total passing, ruff+mypy clean (37 source files).
  Verified live: a no-role user got 403 with the missing-permission
  detail; a role-holding user got 200 filled — both users created by
  direct SQL insert, confirmed via curl.
- Phase 9: per-broker access grants (2026-08-23). Migration `0005` adds
  `broker_grants` (a `user_id`/`broker_id` join table with a unique
  constraint). New `require_broker_access(Permission)`
  (`apps/api/app/api/dependencies.py`) composes on top of
  `require_permission`: identity → global permission → broker existence
  (404) → specific grant (403). Returns an `AuthorizedBroker(user, broker)`
  so `trades.py` no longer does its own broker lookup. Closes the gap
  D011 explicitly called out — a user with `trade:submit:paper` can no
  longer trade on every `broker_id` they know, only ones they've been
  granted. 2 new tests, 74/74 total passing, ruff+mypy clean (37 source
  files). Verified live against a running server with two real broker
  rows: the same trader got 200 on a granted broker and 403 on an
  ungranted one.
- Phase 10: minimal admin API (2026-08-23). `apps/api/app/api/routes/admin.py`
  — `POST /admin/users`, `POST /admin/roles`, `POST /admin/broker-grants`,
  `DELETE /admin/broker-grants/{id}`, all gated by one new coarse
  `Permission.ADMIN` ("admin:manage"). Closes the D010/D011/D012
  "raw SQL only" gap for day-to-day operation — the very first admin
  user/role still needs one direct DB insert (documented bootstrap step,
  D013), but everything after that goes through the API. Deliberately no
  update/deactivate for users or roles, no listing endpoints — scoped to
  what's actually needed routinely (grants especially, since revoking
  broker access is the realistic day-to-day lever). 9 new tests, 83/83
  total passing, ruff+mypy clean (39 source files). Verified live against
  a running server: bootstrapped one admin via SQL, then created a role,
  a user, and a broker grant entirely through the API, confirming the new
  user could trade only after the grant existed — the same
  grant/trade/revoke/trade-again round-trip is also covered as an
  integration test, not just a manual check.
- Phase 11: persisted paper-broker state (2026-08-23). Migration `0006`
  adds `broker_accounts` (cash) and `broker_positions` (nonzero
  quantities only). `apps/api/app/execution/persistence.py`'s
  `load_paper_broker()`/`save_paper_broker()` reconstruct/save a
  `PaperBrokerAdapter` around each trade request, with
  `load_paper_broker()` taking a `SELECT ... FOR UPDATE` lock on the
  account row held for the entire request to prevent a double-spend race
  on concurrent trades against the same broker.
  `submit_trade_and_record()` no longer commits internally (flushes only)
  so that lock survives until the route's single final commit. The old
  in-process `PaperBrokerRegistry` (D009) is deleted entirely, closing
  the D005/D009 gap for real — a restart no longer resets any account. 3
  new tests, 86/86 total passing, ruff+mypy clean (39 source files).
  Verified live in the way that actually matters here: submitted a trade,
  killed the running server process, started a fresh one, submitted a
  second trade on the same broker — the resulting cash balance
  (99,000 − 250 = 98,750) and both positions (one from before the
  restart, one from after) were only explainable if state genuinely
  survived the restart.
- Phase 12: Longbridge market data provider (2026-08-23). Closes D008.
  `apps/api/app/marketdata/providers/longbridge.py`'s
  `LongbridgeMarketDataProvider` wraps the `longport` SDK (verified
  against the real installed package via direct introspection, not
  documentation, after two web sources gave conflicting method names).
  `build_longbridge_provider()` returns `None` unless all three
  `LONGPORT_APP_KEY`/`LONGPORT_APP_SECRET`/`LONGPORT_ACCESS_TOKEN`
  settings are configured — the app boots fine either way. New
  `GET /market-data/{symbol}/quote` (auth required) returns 503
  `NOT_CONFIGURED:` with no vendor wired, 404 `NO_DATA_AVAILABLE:` if the
  vendor has no data, 200 with a real quote otherwise. **Not** wired into
  trade-proposal construction — `POST /brokers/{broker_id}/trades` still
  takes price/timestamp from the caller, unchanged; whether/how to
  connect the two is left as an explicit open decision (D015). 8 new
  tests (all against a fake client — no real credentials exist in this
  environment to test the "configured" path end to end), 94/94 total
  passing, ruff+mypy clean (43 source files). Verified live: server boots
  and logs `market_data_vendor=NOT_CONFIGURED`, and the endpoint returns
  503 rather than any fabricated quote.
- Phase 13: user/role update and deactivate endpoints (2026-08-23).
  Closes D013's deliberately-cut scope. `PATCH /admin/users/{id}`
  (`is_active`, `role_id`) and `PATCH /admin/roles/{id}` (`description`,
  `permissions`) added to `apps/api/app/api/routes/admin.py`, both gated
  by the same `Permission.ADMIN`. Both use Pydantic's `model_fields_set`
  to tell "key omitted" (leave untouched) apart from "key explicitly
  null" (unassign `role_id`) — a plain `is not None` check can't make
  that distinction. Still no delete endpoints for either resource
  (deleting a row would orphan `orders.submitted_by_user_id`/
  `users.role_id`); `is_active=false` remains the only way to deactivate
  a user, and clearing `permissions` is how a role gets neutralized. 7
  new tests, 101/101 total passing, ruff+mypy clean (43 source files).
  Verified live against a running server, real Postgres: deactivated an
  already-logged-in user via PATCH and confirmed their existing token got
  401 on its very next request (not just after re-login), then separately
  PATCHed a role to grant `trade:submit:paper` and confirmed a different
  already-issued token for that role immediately cleared the permission
  check — both prove `get_current_user`'s live re-check (never a cached
  JWT claim) is what makes authorization changes take effect
  immediately.
- Phase 14: market data wired into trade submission (2026-08-24). Closes
  D015's explicitly-left-open question. `TradeSubmissionRequest.estimated_price`
  is now optional — supplied, it's authoritative exactly as every prior
  phase behaved and the vendor is never consulted; omitted,
  `POST /brokers/{broker_id}/trades` fetches one live `MarketSnapshot`
  from the same `MarketDataRouter` the read-only quote endpoint uses, and
  takes both price and `market_data_as_of` from it together (never a
  vendor price paired with a caller-chosen timestamp). No vendor wired is
  400 `NOT_CONFIGURED:`; no data for the symbol is 400
  `NO_DATA_AVAILABLE:` — the same no-fabrication sentinels used
  everywhere else in the codebase. 4 new integration tests (one proves
  the vendor is never called at all when a price is supplied, using a
  provider that raises if invoked), 105/105 total tests passing (104 in
  one run plus the fail-closed JWT-secret test re-verified in true
  isolation, since it needs the env var genuinely unset), ruff+mypy clean
  (43 source files). Verified live against a running server, real
  Postgres, no Longbridge credentials configured: omitting
  `estimated_price` returned 400 `NOT_CONFIGURED`; supplying one still
  returned 200 filled exactly as before this phase. (Update 2026-08-24:
  real paper-trading Longbridge credentials were supplied and both this
  phase's and D015's live-quote paths were re-verified against genuine
  live data — see D015/D017's updates in `docs/DECISIONS.md`.)
- Phase 15: first agent, TraderAgent (2026-08-24). `apps/api/app/agents/`
  adds the first LLM-backed code in the codebase — a single
  single-responsibility agent (`docs/AGENT_POLICY.md`), not the full
  parallel-analyst/research-debate/portfolio-manager stack the governing
  spec eventually wants. `LLMProvider` is a narrow Protocol mirroring
  `MarketDataProvider`'s shape; `AnthropicCompatibleProvider` talks to any
  endpoint speaking the Anthropic Messages API (OmniRoute is the intended
  one) and is deliberately provider-name-agnostic
  (`LLM_PROVIDER_BASE_URL`/`_API_KEY`/`_MODEL`, not `OMNIROUTE_*` —
  `docs/MODEL_ROUTING.md`). `TraderAgent.propose()` takes a symbol and a
  free-text directive and returns a validated `TradeIdea`
  (side/quantity/stop_distance_pct/rationale) — **no price field at
  all**. New `POST /brokers/{broker_id}/agent-trades`
  (`apps/api/app/api/routes/trades.py`) combines that idea with the same
  live-quote path D017 uses for price, converts the stop distance into a
  real stop price deterministically, and submits through the identical
  `submit_trade_and_record()` → Risk Engine path a human-submitted trade
  uses — no separate or weaker validation for agent-originated trades.
  Malformed/unparseable LLM output is 502 `AGENT_OUTPUT_INVALID:`, never
  a fabricated trade; no provider configured is 400 `NOT_CONFIGURED:`,
  same convention as every other optional vendor in this codebase. 13 new
  tests (8 unit against a fake `LLMProvider`, 5 integration against real
  Postgres — including one proving an oversized agent-proposed quantity
  is rejected by the Risk Engine and not the agent, and one proving a
  missing broker grant returns 403 before the agent is ever called, using
  a provider that raises if invoked), 118/118 total tests passing,
  ruff+mypy clean (47 source files). OmniRoute was unreachable in this
  environment at implementation time (connection refused on
  `127.0.0.1:20128`), so only the NOT_CONFIGURED path was verified live —
  the "provider actually returns a usable completion" path is verified
  only against a fake provider in tests, the same limitation D015
  originally had for Longbridge before real credentials existed.
- Phase 16: first analyst, TechnicalAnalyst (2026-08-27). Closes the
  first slice of the parallel analyst layer per the governing spec's
  "Target" architecture — one analyst, not the full technical/
  fundamental/news/sentiment team, since only one has a real data source
  wired (Longbridge live quotes; `packages/data_providers/` remains an
  empty placeholder, so fundamental/news/sentiment would mean fabricating
  a feed, forbidden per spec §57). `TechnicalAnalyst.analyze()` reads the
  same live quote `agent-trades` already resolves and returns a
  `TechnicalRead` (`stance`/`summary`/`confidence`) — no price/side/
  quantity field, so nothing here can be mistaken for a trade proposal.
  It computes no indicator itself (real indicators must stay
  deterministic per `docs/TOKEN_POLICY.md` once real historical price
  data exists — it doesn't yet). Wired as optional context into
  `TraderAgent.propose()`'s prompt on `POST /brokers/{broker_id}/agent-trades`
  — absent or failed, the trade proceeds exactly as before this phase, no
  context appended, never blocked. `agent-trades` now resolves the live
  quote before calling the trader agent (was: agent first) so the
  analyst and trader share one quote; this real behavior change broke an
  existing D018 test that hadn't stubbed a market-data router, fixed by
  stubbing one to match its siblings. 10 new tests (7 unit against a fake
  `LLMProvider`, 3 integration against real Postgres — including proof
  via a `CapturingTraderAgent` subclass that the analyst's read actually
  reaches the trader's prompt, and proof that a failing analyst never
  blocks the trade), 128/128 total tests passing, ruff+mypy clean (49
  source files). Verified live against a running server, real Postgres,
  no `LLM_PROVIDER_*` configured: app booted logging
  `technical_analyst=NOT_CONFIGURED`, and `POST /brokers/{id}/agent-trades`
  returned 400 `NOT_CONFIGURED` rather than any fabricated read or trade.
  OmniRoute was unreachable at implementation time, same limitation as
  D018 — the "analyst returns a usable read" path is verified only
  against a fake provider.

- **Phase 17 — frontend, first slice (D020).** `apps/web/` — Next.js 16
  (App Router), TypeScript, Tailwind v4. `/login` posts to a Next.js route
  handler (`/api/auth/login`) that forwards OAuth2 form-encoded credentials
  to the backend and, on success, stores the JWT in an httpOnly cookie —
  the browser's own JS never touches the token. `/dashboard` (gated by
  `middleware.ts` on cookie presence) renders three real backend
  round-trips, each proxied through its own route handler that attaches
  `Authorization: Bearer <token>` server-side: `GET /health` status,
  a quote-lookup form against `GET /market-data/{symbol}/quote` (renders
  the actual 503 `NOT_CONFIGURED:`/404 `NO_DATA_AVAILABLE:` detail string
  on failure, never a generic message), and a paper-trade submission form
  against `POST /brokers/{broker_id}/trades` (renders the full
  `TradeSubmissionResponse` — `status`/`approved`/`block_reason`/
  `fill_price`/`fill_quantity` — including a rejected trade's
  `block_reason` legibly). No fabricated data anywhere: every fetch
  failure renders a real error state. Deliberately out of scope for v1:
  `/admin/*` UI, `/brokers/{id}/agent-trades` UI, broker discovery (no
  such endpoint existed then — built in Phase 29/D034), session refresh. `npm run build` succeeds with
  zero TypeScript errors. Test strategy: Vitest + React Testing Library
  (7 component tests covering quote success/503/404/network-failure and
  trade rejected/filled/403 rendering) — chosen over Playwright for this
  phase's scope (no real browser E2E flow to protect yet, and the app has
  no complex client-side state machine that unit/component tests can't
  already cover); Playwright is a reasonable v2 addition once there's a
  login → dashboard → trade flow worth protecting end-to-end. Verified:
  `npm run build` (zero type errors), `npm run dev` + curl against the
  Next.js app's own pages/routes. (Update 2026-08-28: re-verified
  end-to-end against a real running backend — a real login set the
  httpOnly cookie, a quote lookup returned the backend's genuine 503
  `NOT_CONFIGURED:`, and a trade submission returned a genuine `filled`
  response with a real fill price, all through the frontend's own proxy
  routes. See D020's update for full detail.)
- Phase 18: real historical prices for TechnicalAnalyst (2026-08-28).
  Closes D019's "future work" gap. `apps/api/app/marketdata/history_provider.py`
  adds a `HistoryProvider` Protocol (a price series, distinct from
  `MarketDataProvider`'s single quote); `LongbridgeHistoryProvider`
  implements it via the same Longbridge SDK connection (D015) using
  `candlesticks()`, verified against the real installed package by
  introspection. `apps/api/app/marketdata/indicators.py` adds pure,
  deterministic `sma()`/`rsi()` — no LLM, per `docs/TOKEN_POLICY.md`'s
  mandatory-deterministic list. `TechnicalAnalyst` (D019) can now narrate
  real computed indicator values when history is available — its prompt
  still explicitly forbids calculating or inventing one itself. Wired
  into `POST /brokers/{broker_id}/agent-trades`: optional, never blocking
  — a short history, vendor failure, or unconfigured provider all just
  mean no indicator context that call. 18 new tests (9 indicator unit
  tests, 5 unit tests against a fake candlestick client, 4 integration
  tests against real Postgres), 145/145 total tests passing, ruff+mypy
  clean (51 source files). One real bug caught during this phase's own
  test run and fixed: `rsi()`'s `zip(..., strict=True)` was wrong for its
  naturally-unequal-length consecutive-pair slices, raising on every real
  call. Verified live against the real Longbridge API directly (real
  paper-trading credentials): fetched 30 real daily closes for `AAPL.US`
  and computed genuine `SMA(20)=309.3215`/`RSI(14)=51.57...` — a level of
  verification D019 couldn't reach at the time. Also verified against a
  running server with no credentials configured: startup logged
  `history_provider=NOT_CONFIGURED`, `agent-trades` correctly 400s on
  D018's LLM-provider gate first.
- Phase 19: first Portfolio module (2026-08-28, D022). Closes
  `docs/MODULE_MAP.md`'s Portfolio gap. `apps/api/app/portfolio/` adds
  `compute_portfolio_snapshot()` — deterministic, no LLM, no writes —
  reading `broker_accounts`/`broker_positions` for current cash/quantity
  and replaying `orders`/`fills` per symbol for average-cost-basis
  `avg_cost`/realized P&L (`replay_symbol_fills()`, a pure function).
  `GET /brokers/{broker_id}/portfolio` exposes it, gated by a new
  `Permission.VIEW_PORTFOLIO` (deliberately separate from
  `SUBMIT_PAPER_TRADE` — read-only reporting shouldn't require trade
  rights) plus the usual `require_broker_access` grant check; current
  marks travel as a JSON body (`{"marks": {...}}`) on the GET, same shape
  as `TradeSubmissionRequest.marks`. A missing mark for a held position is
  400 `DATA_UNAVAILABLE:`, never a guessed price. 16 new tests (6 pure
  unit tests hand-verifying the average-cost replay across a
  buy/buy/sell/sell sequence, 10 integration tests against real Postgres
  reusing `tests/api/test_trades.py`'s fixtures), 169/169 total tests
  passing, `ruff check .`/`mypy apps` clean (one pre-existing, unrelated
  `B905` finding in `indicators.py` predated this phase — fixed
  independently in the concurrent Phase 22 below). Verified live
  against a real running server (`docker compose -p trading-os-phase19`,
  remapped host ports to avoid a sibling worktree's port clash): a
  no-trade broker reports starting cash and empty positions; a real
  `AAPL` buy followed by a marked portfolio call returned the
  hand-computed quantity/avg_cost/current_value/unrealized_pnl exactly;
  omitting the mark for that held position correctly 400'd
  `DATA_UNAVAILABLE:`. Explicit non-scope, not built: backtesting,
  alerts, performance-attribution-over-time (all need persisted
  historical snapshots — a bigger decision for a future phase), and
  FIFO/LIFO cost basis (this schema has no per-lot data to support it —
  see D022's alternatives).
- Phase 22: deterministic duplicate-order detection in the Risk Engine
  (2026-08-28, D024). Closes the `docs/PROJECT_CONTEXT.md` "Open
  Decisions" gap D004 flagged as not built. A duplicate is defined as a
  proposal matching a FILLED order on the same broker (symbol, side,
  quantity, estimated_price all equal) within a 5s window — REJECTED
  orders are deliberately excluded (see D024). `apps/api/app/risk/models.py`
  adds `RecentOrder` and `RiskLimits.duplicate_order_window_seconds`;
  `evaluate_trade()` gains an optional `recent_orders` parameter and stays
  fully I/O-free — the caller (`trades.py`'s `_execute_trade()`, shared by
  both the human and agent-trades routes) queries
  `oms/persistence.py`'s new `get_recent_filled_orders()` and hands the
  result in as plain data. A new composite index
  `ix_orders_broker_symbol_submitted` (migration 0007) backs that query.
  New `BlockReason.DUPLICATE_ORDER`. 14 new tests (9 pure unit tests in
  `tests/risk/test_engine.py`, 1 in `tests/oms/test_service.py`, 1
  DB-backed in `tests/db/test_order_persistence.py`, 2 DB-backed HTTP
  integration tests in `tests/api/test_trades.py`, 1 DB-backed HTTP
  integration test in `tests/api/test_agent_trades.py`), 159/159 total
  tests passing, ruff+mypy clean (also fixed one pre-existing, unrelated
  ruff finding in `marketdata/indicators.py`). Verified live against a
  running server with a directly-inserted user/role/broker/grant: an
  identical `AAPL buy 10 @ 100` paper trade submitted twice ~0.5s apart —
  first filled, second rejected with `block_reason=duplicate_order`; a
  third submission with `quantity=5` right after filled normally,
  confirming no over-firing. All verification rows and infra (venv,
  `.env`, Docker containers) removed afterward.
- **Phase 20 — frontend v2: admin UI and agent-trades UI (D023).** Builds
  the two "v2 candidates" Phase 17/D020 explicitly deferred, in
  `apps/web/`, on the same route-handler-proxy pattern (no new auth
  mechanism). Admin UI (`/admin`, gated by `proxy.ts` on cookie presence
  only, same as `/dashboard`): six forms covering every `/admin/*`
  endpoint — create/update user, create/update role, create/revoke broker
  grant — each behind its own route handler
  (`app/api/admin/users(/[userId])`, `app/api/admin/roles(/[roleId])`,
  `app/api/admin/broker-grants(/[grantId])`). The dashboard's "Admin" nav
  link is always shown regardless of the viewer's actual permissions —
  deliberate: the real `admin:manage` gate is the backend's 403 on
  submit, and a client-side guess about permissions the client can't
  verify would be its own kind of fabrication. A non-admin submitting any
  admin form sees the backend's real 403 detail string. Agent-trades UI:
  `AgentTradeForm` on the dashboard, posting to
  `POST /brokers/{broker_id}/agent-trades` via
  `app/api/agent-trades/[brokerId]/route.ts`; renders the full
  `AgentTradeResponse` (`side`/`quantity`/`rationale` plus the existing
  trade-response fields) and the endpoint's two real error sentinels —
  400 `NOT_CONFIGURED:` and 502 `AGENT_OUTPUT_INVALID:` — never a
  fabricated trade. Deliberately out of scope, unchanged from D020:
  broker-discovery UI (no such endpoint), session refresh/expiry UX,
  Playwright e2e. `npm run build` succeeds with zero TypeScript errors.
  `npm test`: 28/28 passing (7 pre-existing + 21 new component tests
  across 4 new test files, covering success, a real 403, a real 404/409
  where applicable, and network failure for every new form). Verified
  live against a real running backend in this worktree (`docker compose
  up -d --build`, host ports for Postgres/Redis temporarily remapped only
  to avoid colliding with sibling Phase 19/21 worktrees, restored
  afterward): ran real Alembic migrations, bootstrapped one admin
  user/role via direct SQL (bcrypt hash, same pattern as D013/D016/D021),
  then through `npm run dev` + curl against the frontend's own routes —
  real 201s creating a role and a user, a real 200 assigning the role via
  PATCH, a real 200 updating the role's description via PATCH, a real 201
  granting broker access against a SQL-inserted paper broker, a real 204
  revoking it followed by a real 404 on repeating the delete, a real 403
  `Missing required permission: admin:manage` from the new non-admin user
  attempting an admin action, and a real 400
  `NOT_CONFIGURED: no LLM provider is wired` from that same non-admin
  user's agent-trade attempt (no `LLM_PROVIDER_*` configured in this
  pass). `AGENT_OUTPUT_INVALID:` (502) was exercised only via a mocked
  component test, not a real misbehaving provider. See D023 for full
  detail.
- **Phase 24 — frontend portfolio view (D026).** Builds the "portfolio
  view" v2/v3 candidate D020/D023 both deferred, against Phase 19/D022's
  `GET /brokers/{broker_id}/portfolio`. `PortfolioView`
  (`components/PortfolioView.tsx`) on `/dashboard` takes a broker UUID
  plus a `SYMBOL=price` marks string and renders the real
  `PortfolioSnapshot` — `cash`, a table of every position's
  `symbol`/`quantity`/`avg_cost`/`current_value`/`unrealized_pnl`/
  `realized_pnl`, and the three totals — a single real-time snapshot,
  deliberately no historical chart (needs Phase 25's persisted
  snapshots). Backed by `app/api/portfolio/[brokerId]/route.ts`, exposed
  to the browser as `POST` but internally issuing the real backend call
  as a `GET` carrying `marks` as a JSON body, per D022's contract. Node's
  `fetch` (undici) cannot send a body on GET at all — `lib/backend.ts`
  gained `backendGetWithBody()`, which uses Node's core `http`/`https`
  module directly for this one call instead. `npm run build` succeeds
  with zero TypeScript errors. `npm test`: 34/34 passing (28 pre-existing
  + 6 new in `test/PortfolioView.test.tsx`: a real rendered snapshot, an
  empty-positions render, the 400 `DATA_UNAVAILABLE:` sentinel for a
  missing mark, a 403, a 404, and network failure). Verified live against
  a real running backend in this worktree (`docker compose up -d
  --build`, host ports temporarily remapped to avoid colliding with
  sibling Phase 23/25 worktrees, restored afterward): ran real Alembic
  migrations against a fresh database, bootstrapped two users via direct
  SQL (one with `portfolio:view` + a broker grant, one with the grant but
  no `portfolio:view`), submitted a real trade that filled
  (`fill_price: "100"`, `fill_quantity: "10"`), then through `npm run dev`
  + curl against the frontend's own `/api/portfolio/[brokerId]` route —
  a real 200 rendering that exact filled position and its computed
  totals from a supplied mark of `120`, a real 400
  `DATA_UNAVAILABLE: No mark supplied for open position 'AAPL.US'; cannot
  value account.` when the mark was omitted, a real 404 for an unknown
  broker, a real 401 with no session cookie, and a real 403
  `Missing required permission: portfolio:view` from the second seeded
  user. See D026 for full detail.

---
- **Phase 25 — persisted portfolio snapshots (2026-08-28, D027).** Closes
  the "persisted historical portfolio snapshots" item Phase 19/D022
  explicitly deferred. Two new append-only tables (migration
  `0008_portfolio_snapshots.py`; models `PortfolioSnapshotRow`/
  `PortfolioSnapshotPositionRow` in `apps/api/app/db/models.py`) - a
  parent `portfolio_snapshots` row (cash/total_equity/total_unrealized_pnl/
  total_realized_pnl/captured_at) plus a child `portfolio_snapshot_positions`
  row per open position (chosen over a JSON column for queryability - see
  D027). `POST /brokers/{broker_id}/portfolio/snapshots` computes a
  snapshot via the existing `compute_portfolio_snapshot()` (same
  `{"marks": {...}}` body, same `DATA_UNAVAILABLE:` missing-mark
  discipline) and persists it; `GET /brokers/{broker_id}/portfolio/history`
  returns persisted snapshots for a broker ordered oldest-to-newest by
  `captured_at`, paginated (`limit` default 50/max 500, `offset` default
  0). Both gated by the existing `Permission.VIEW_PORTFOLIO` (see D027 for
  why no new permission was needed) plus `require_broker_access`.
  Deliberately manual-only - no cron/scheduler was built; a snapshot only
  ever exists because a caller explicitly POSTed it. 8 new integration
  tests against real Postgres in `tests/api/test_portfolio.py` (23 total
  in that file), plus `tests/api/test_trades.py`'s shared
  `paper_broker_row` fixture extended to clean up the two new tables on
  teardown. 188/188 total tests passing, `ruff check .`/`mypy apps` both
  clean (58 source files). Verified live against a running Docker Compose
  stack in this worktree: seeded a role/user/broker/grant via direct SQL
  (bcrypt hash), submitted a real `AAPL` buy, POSTed a real snapshot with
  a real mark (response showed the real position and P&L), then GET
  `.../history` and confirmed both the pre-trade and post-trade snapshots
  came back in the correct order with values matching exactly. All seeded
  rows, the Docker stack, the local venv, and `.env` were removed
  afterward.

- **Phase 26 — trade-path Portfolio Manager (2026-08-29, D029).** Builds the
  "Portfolio Manager" box in docs/ARCHITECTURE.md's Target diagram / the
  "Portfolio Decision" step in docs/TRADING_SAFETY.md's pipeline — the
  component D022 and D027 both explicitly flagged as *not* what
  `apps/api/app/portfolio/` is. New package
  `apps/api/app/portfolio_manager/` (`models.py`, `manager.py`): a pure
  `decide(proposal, portfolio, limits) -> PortfolioDecision` with no LLM,
  no network call and no I/O, mirroring the Risk Engine's own zero-I/O
  discipline. Wired into `submit_trade()`
  (`apps/api/app/oms/service.py`) AFTER `evaluate_trade()` approves a
  proposal and BEFORE the broker call — `apps/api/app/risk/engine.py`
  itself is unmodified. Two structural properties, each with a dedicated
  test: a risk-rejected proposal never reaches the Portfolio Manager
  (it is a second gate, not a bypass), and a MODIFY-resized proposal is
  re-run through `evaluate_trade()` before any broker call, so no
  quantity this system produces reaches a broker ungated. Three
  constraints, all computable from data the repo actually stores:
  `symbol_concentration` (post-trade value of one symbol vs equity,
  default 25% — the aggregate-position gap no per-trade check sees),
  `cash_reserve` (post-trade cash vs equity, default 5%), and
  `max_open_positions` (distinct held symbols, default 20, labelled as a
  count, not as diversification). Spec §18's correlation, sector
  concentration, portfolio volatility, expected return and drawdown are
  NOT implemented — no sector column on `assets` and no persisted
  per-symbol return series exist, and approximating them would be
  fabrication (see D029). `REQUEST_MORE_RESEARCH` exists in the enum for
  spec fidelity but is never emitted; a test pins that. A failing check
  only blocks when the trade *worsens* that measure, so a de-risking sell
  is never refused because of the breach it relieves. Audit record per
  spec §18: migration `0009_orders_portfolio_decision.py` adds four
  nullable `orders` columns (`portfolio_action`,
  `portfolio_binding_constraint`, `portfolio_detail`,
  `portfolio_requested_quantity`); `orders.quantity` is now the quantity
  actually acted on, so a resize shows as `quantity !=
  portfolio_requested_quantity` rather than a rewritten proposal, and a
  null `portfolio_action` means "never ran", never "approved".
  `TradeSubmissionResponse` surfaces the same four fields, which makes
  `approved: true` + `status: "rejected"` a real combination (risk passed,
  portfolio didn't). 26 new tests — 13 pure unit
  (`tests/portfolio_manager/test_manager.py`), 5 OMS-wiring
  (`tests/oms/test_service_portfolio_manager.py`), 4 real-Postgres
  persistence (`tests/db/test_portfolio_decision_persistence.py`), 4
  real-Postgres HTTP (`tests/api/test_trades_portfolio_manager.py`) —
  for 234/234 total passing, `ruff check .`/`mypy apps` clean (66 source
  files). Verified live against a real Docker Compose stack in this
  worktree (host ports remapped to 5435/6382/8002 via a throwaway
  override so it could run alongside sibling Phase 27/28 worktrees; the
  tracked `docker-compose.yml` was never edited), with real Alembic
  migrations `0001`→`0009` against a fresh database: real curl requests
  to the containerized API produced, on one broker, two `approve` fills,
  a real `modify` cutting a 98-share buy to 51 with
  `binding_constraint: symbol_concentration`, a real `reject` at the
  exact 25% cap (`approved: true`, `block_reason: null`), and a
  de-risking sell correctly approved anyway — with Postgres confirming
  the resized row (`quantity 51`, `portfolio_requested_quantity 98`), 250
  shares held and 75,000 cash, i.e. the resized trade's arithmetic and
  not the requested one. Not verified live: no market-data vendor or LLM
  provider was configured, so all prices were caller-supplied and the
  agent-trades route (which shares the same `_execute_trade()` path) was
  exercised only against a fake `LLMProvider`; `apps/web/` was not run or
  updated and still renders only the risk verdict; `apps/api/app/
  backtesting/` calls `evaluate_trade()` directly, so backtests do not
  model portfolio constraints. Docker containers/volumes, the compose
  override, the test-only `.env` and the venv were removed afterward.

- **Phase 27 — automatic/scheduled portfolio snapshots (2026-08-29, D030).**
  Closes the "automatic/scheduled portfolio snapshotting" item Phase
  25/D027 explicitly deferred, and adds the first background component in
  the system. `apps/api/app/portfolio/scheduler.py`: a
  `PortfolioSnapshotScheduler` (plain `asyncio` task + `asyncio.sleep`, no
  new dependency) started from the FastAPI lifespan, running one cycle
  immediately and then every `PORTFOLIO_SNAPSHOT_INTERVAL_SECONDS`
  (new setting, default 3600). Gated by
  `PORTFOLIO_SNAPSHOT_SCHEDULER_ENABLED` (new setting, **default false** -
  see D030 for the fail-closed reasoning); with the defaults, nothing is
  constructed and runtime behavior is unchanged from Phase 25. A cycle
  reads the brokers that have a `broker_accounts` row, and for each one
  fetches a real quote for every nonzero-position symbol through the
  *existing* `MarketDataRouter` (D015/D017) - the scheduler has no caller
  to supply `marks`, and it never invents, defaults, or carries one
  forward. If any held symbol has no real quote, that broker is skipped
  for that cycle: nothing is written, and a typed
  `ScheduledSnapshotOutcome` (`CAPTURED` /
  `SKIPPED_MARKET_DATA_UNAVAILABLE` /
  `SKIPPED_MARKET_DATA_NOT_CONFIGURED` / `SKIPPED_NO_BROKER_ACCOUNT` /
  `SKIPPED_INCOMPLETE_VALUATION`) is returned and logged at warning level
  with the unpriced symbols and the vendor's own sentinel text. A
  cash-only broker (no open positions) needs no marks and is captured
  normally even with market data entirely NOT_CONFIGURED. When it does
  capture, it runs the identical `compute_portfolio_snapshot()` and the
  identical write path the manual `POST .../portfolio/snapshots` uses -
  that write was extracted this phase into
  `apps/api/app/portfolio/persistence.py`
  (`persist_portfolio_snapshot()`), with the route's response-shaping
  pulled into a `_to_history_entry()` helper, so the two paths cannot
  drift; a scheduler-written row reads back through
  `GET .../portfolio/history` indistinguishably from a manual one. No new
  table and no migration were needed. 20 new tests (8 unit in
  `tests/portfolio/test_scheduler.py` - mark resolution through a real
  `MarketDataRouter` with a fake only at the vendor boundary, plus the
  interval and default-off guards; 12 DB-backed integration in
  `tests/api/test_snapshot_scheduler.py`, reusing
  `tests/api/test_trades.py`'s fixtures, covering capture, each skip
  reason, "one unpriceable symbol blocks the whole snapshot", a full
  mixed cycle, and the running task actually writing a row on its timer).
  228/228 total tests passing, `ruff check .`/`mypy apps` both clean
  (65 source files). Verified live against a real uvicorn process and a
  real Postgres in this worktree with the scheduler enabled at a 10s
  interval: two seeded brokers, both given real positions through the
  real trade endpoint. The flat (bought-then-sold) broker was
  snapshotted automatically every cycle, and `.../portfolio/history`
  returned four genuine scheduler-written rows (cash `100100.00000000`,
  realized P&L `100.00000000`, `captured_at`s ~10s apart) computed from
  the real fills; the broker still holding `AAPL.US` was skipped on every
  single cycle with `status: "skipped_market_data_not_configured"` and
  `unpriced_symbols: ["AAPL.US"]`, its history staying empty - the
  no-fabrication guarantee observed live rather than only asserted in
  tests. **Not verified live:** the router-supplied-mark *capture* path
  against a real vendor - no Longbridge credentials exist in this
  environment, so the market-data layer genuinely reported
  NOT_CONFIGURED and the live run exercised the refusal path, not the
  success path; that path is covered only by the integration tests
  (real router, fake at the vendor boundary). Graceful lifespan shutdown
  (`scheduler.stop()`) was covered by tests but not in the live run,
  which ended in a forced kill. Docker containers/volumes, a temporary
  port-remapping compose override (needed to run alongside concurrent
  phase-26/phase-28 stacks), the test-only `.env`, and the verification
  venv were all removed afterward.

- **Phase 28 — frontend v3: historical performance chart, admin listing UI
  + its backend endpoints, session expiry UX (2026-08-29, D031/D032).**
  Closes three of the four frontend candidates Phase 24/D026 left open.
  (1) **Historical performance chart.** `PortfolioHistoryChart`
  (`apps/web/components/PortfolioHistoryChart.tsx`) on `/dashboard`
  charts `total_equity` over `captured_at` from Phase 25/D027's
  `GET /brokers/{id}/portfolio/history`, backed by a new
  `app/api/portfolio/[brokerId]/history/route.ts` proxy. The chart is a
  hand-rolled inline SVG polyline — **no new npm dependency was added**;
  every plotted vertex is one real returned snapshot, nothing is
  interpolated, smoothed, back-filled, or extended past the last real
  capture, and the accompanying table prints each snapshot's raw values
  verbatim. The x axis is by snapshot index, not elapsed time, because
  D027 capture is manual and a time-scaled axis would imply a sampling
  cadence that does not exist. Zero snapshots renders "no snapshots have
  been captured", never an empty or zeroed curve.
  (2) **Users/roles/grants listing UI + the backend endpoints it needed
  (D031).** `GET /admin/users`, `GET /admin/roles`, and
  `GET /admin/broker-grants` were added to
  `apps/api/app/api/routes/admin.py` — read-only, no new permission
  (they inherit the router's existing `admin:manage` dependency),
  paginated with the same `limit`(50/500)/`offset` convention as D027's
  history endpoint, each with a fixed total sort. Rows reuse the existing
  `CreateUserResponse`/`CreateRoleResponse`/`BrokerGrantResponse` DTOs,
  so no password or hash can appear. `UsersList`/`RolesList`/
  `BrokerGrantsList` (`apps/web/components/admin/AdminListings.tsx`) on
  `/admin` render them; the grants table surfaces the `id` the revoke
  form needs, which previously required a direct DB query.
  (3) **Session refresh/expiry UX (D032).** New `GET /auth/session`
  returns `user_id`/`email`/`issued_at`/`expires_at`/
  `expires_in_seconds` — never the token, never a renewed one, and 401
  for an expired token or a deactivated user. It exists because the JWT
  is in an httpOnly cookie (D020) that client JS cannot read, so a
  "session expiring soon" warning was otherwise impossible without
  fabricating a local countdown. `SessionStatus` polls it once a minute
  and warns below 5 minutes; it never ticks a local timer down between
  polls. A shared `apps/web/lib/session.ts` `handleExpiredSession(status)`
  is now called at the `!res.ok` branch of every authenticated component,
  so any route handler's 401 redirects to `/login?reason=session-expired`
  where a real explanation renders instead of a bare "HTTP 401".
  **Backend:** `ruff check .` and `mypy apps` clean (64 source files);
  **226/226 pytest tests passing** (208 pre-existing + 18 new: 12 in
  `tests/api/test_admin_listings.py`, 6 in `tests/api/test_session.py`),
  all against real Postgres. **Frontend:** `npm run build` with zero
  TypeScript errors; **61/61 Vitest tests passing** (34 pre-existing + 27
  new across `test/PortfolioHistoryChart.test.tsx`,
  `test/AdminListings.test.tsx`, `test/SessionStatus.test.tsx` — success,
  empty-result, real error sentinels, 403, 404, 401-redirect, and
  network-failure paths for every new component, plus pure-function tests
  for the chart's coordinate mapping and the remaining-time formatter).
  **Verified live** against a real Docker Compose stack in this worktree
  (host ports remapped to 5436/6383/8003 to avoid the sibling phase26
  worktree's stack, removed afterward): real Alembic migrations, a seeded
  admin user/role/broker/grant, a real filled `AAPL.US` trade, and four
  real POSTed snapshots producing a genuinely varying equity curve
  (100000 → 100050 → 99980 → 100120). Exercised every new endpoint by
  curl both directly against the API and through the frontend's own route
  handlers with a real login cookie: all three listings, the history
  endpoint, `/auth/session`, plus `limit=501` → 422, `offset=-1` → 422,
  no token → 401, and a non-admin's real 403 on all three listings. In a
  real browser: logged in through the real UI, loaded the history chart
  (4 real plotted vertices with correct geometry), saw all three admin
  listings render the real seeded rows, saw `SessionStatus` show the
  backend's real "29m left", and — after deactivating that user directly
  in Postgres — watched the next `/admin` load redirect to
  `/login?reason=session-expired` with the real message.
  **Not done:** Playwright/e2e coverage, deliberately skipped because it
  requires a new tool/vendor dependency that was outside this phase's
  no-new-dependency scope; it remains open pending explicit permission.
  See D031/D032 for full detail.

- **Phase 30 — backtests gated by the Portfolio Manager too (2026-08-29,
  D035).** Closes the honest gap D029 itself recorded: the backtest engine
  called `evaluate_trade()` directly, so backtests modelled per-trade risk
  but no portfolio-level constraint. `apps/api/app/backtesting/engine.py`
  now replicates `oms/service.py::submit_trade()`'s RISK ENGINE ->
  PORTFOLIO MANAGER -> BROKER sequence inline, on every simulated trade
  decision: a risk-approved proposal goes to the real
  `portfolio_manager.manager.decide()`, and a MODIFY's resized quantity is
  passed back through `evaluate_trade()` before the simulated fill — the
  same load-bearing re-gate invariant D029 pinned for the live path. The
  `PortfolioState` is built at each step from the run's own disposable
  in-memory `PaperBrokerAdapter` (D025), never from a real broker's rows,
  and `submit_trade()` itself is deliberately NOT called because it is
  DB/audit-coupled while a backtest writes nothing. The loop stays pure and
  deterministic — no LLM, no I/O added. `POST /backtests` supplies the same
  `Settings.portfolio_*` limits the live trade path uses (no
  backtest-specific override), and `BacktestResult` gains
  `portfolio_modified_trades`, `portfolio_modify_risk_blocked_trades` and
  `portfolio_rejected_trades` so the audit trail exists in backtests too.
  **277/277 pytest tests passing** (272 pre-existing + 5 new in
  `tests/backtesting/test_engine_portfolio_manager.py`), `ruff check .` and
  `mypy apps` clean (69 source files). **Verified live** over real HTTP
  against this worktree's own Docker stack (ports remapped to 5443/6390/8030
  to avoid the running sibling phase29 stack; torn down afterwards): real
  Alembic `0001`->`0009`, a seeded user, a real login, and three real
  `POST /backtests` runs showing approve-only, a real MODIFY, and a real
  portfolio REJECT. **Not verified with real vendor data:** no Longbridge
  credentials were readable in this session, so the containerized API
  honestly returned `NOT_CONFIGURED` for a real-symbol run and the live
  runs above used an injected fake `HistoryProvider`'s synthetic closes —
  unlike D025, which was verified against real Longbridge history. See D035.

- **Phase 33 — persisted, audited, live-flippable emergency stop
  (2026-08-29, D039).** Closes PROJECT_CONTEXT.md's long-standing
  "emergency-stop *source* is undecided" open item. Spec §46's kill switch
  was an `.env` value that required a restart to flip — the exact thing its
  own docstring said it must not require. It is now an append-only
  `emergency_stop_events` table (migration `0010`): one row per flip with
  `active`, a required `reason`, the acting `actor_user_id`, and
  `created_at`; the current state is the highest-`id` row, so state and
  audit trail are the same object. Three endpoints in
  `apps/api/app/api/routes/emergency_stop.py`:
  `POST /admin/emergency-stop` and `POST /admin/emergency-stop/deactivate`
  (both `admin:manage`, both requiring a non-blank reason) and
  `GET /admin/emergency-stop` (authentication only — knowing you are halted
  is not privileged, same scoping argument as D034). **The Risk Engine is
  unchanged and still zero-I/O:** `evaluate_trade()` still takes
  `emergency_stop_active: bool` as a plain parameter, and the DB read
  happens one layer up in `_execute_trade()` (shared by the human and agent
  trade routes, a single read site), with the new persistence deliberately
  in a new `apps/api/app/safety/` package rather than inside `risk/` — a
  test asserts that boundary structurally.
  `Settings.emergency_stop_active` survives only as the bootstrap default
  used while the table is empty; once a row exists it is never consulted.
  **304/304 pytest tests passing** (288 post-Phase-29/30-merge baseline +
  16 new in `tests/api/test_emergency_stop.py`), `ruff check .` and
  `mypy apps` clean (74 source files). **Verified live** against this
  worktree's own Docker Postgres/Redis (ports remapped to 55433/56380 to
  avoid the user's own dev stack; torn down afterwards): real
  `alembic upgrade head` `0001`→`0010`, then against a real `uvicorn`
  process — a real trade filled, the stop activated over HTTP with a real
  reason, the next real trade rejected with `emergency_stop_active` in the
  same process with no restart, then the **process killed and restarted**
  with a byte-identical `.env` (md5-checked) and the state confirmed still
  active and still blocking real trades, then deactivated and a real trade
  filled again. See D039.

## In Progress

Nothing currently mid-implementation.

## Blocked

Nothing currently blocked.

## Planned

Phase 20+ (order not finalized): the rest of the parallel analyst layer
(fundamental/news/sentiment — each blocked on a real, wired data source),
research debate, and FIFO/LIFO cost-basis reporting as a Portfolio module
alternative to the current average-cost method. Automatic/scheduled
portfolio snapshotting is now DONE (Phase 27/D030) - remaining follow-ons
there: market-hours awareness (the interval is plain wall-clock, so an
enabled scheduler records unchanged after-hours rows or logs repeated
skips), and multi-worker safety (each API worker process runs its own
independent loop, so >1 worker would multiply rows - see D030's
Consequences). If a second analyst is
ever added, revisit whether
parallel-execution infrastructure across analysts is now warranted
(deliberately not built in Phase 16 — one analyst has nothing to
parallelize against). Frontend candidates remaining after Phase 20/D023
and Phase 24/D026, now further reduced by Phase 28/D031/D032 (the
historical performance chart, the users/roles/grants listing UI, and
session expiry UX are all built) and by Phase 29/D034 (broker discovery,
backend and UI): what remains is Playwright e2e coverage — the latter
deliberately skipped in Phase 28 because it needs a new tool dependency,
open pending explicit permission to add one. Re-verify D018/D019's LLM-completion path
once OmniRoute (or another Anthropic-Messages-API-compatible endpoint) is
reachable — D021 confirmed the Longbridge/history side works with real
data, but no real LLM completion has been exercised yet, only fakes.

## Technical Debt

None yet — codebase is small enough that debt hasn't accumulated.

## Optimization Tasks

Deferred until an agent/LLM layer exists — see [AI_OPTIMIZATION.md](AI_OPTIMIZATION.md).
Building a token router/cache with nothing to route would be premature
optimization.

## Completed (continued)

- Phase 23: backtesting engine (2026-08-28). `apps/api/app/backtesting/`
  (`strategy.py`, `metrics.py`, `models.py`, `errors.py`, `engine.py`) — a
  single hard-coded SMA(20)-crossover strategy replayed against real
  historical closes (`HistoryProvider`, D021) through the real Risk
  Engine (`evaluate_trade`, D004) and a fresh in-memory
  `PaperBrokerAdapter` per run (D014) — never a real broker's persisted
  state. `POST /backtests` (`api/routes/backtests.py`, gated by
  `get_current_user` alone, no `broker_id`/`Permission`) returns a
  `BacktestResult` (equity curve, total return, trade count, win rate,
  max drawdown — every number hand-verified in tests). See
  docs/DECISIONS.md D025 for the strategy/scope/permission reasoning and
  the `HistoryProvider`-imposed `end_date == today` limitation. 33 new
  tests (21 pure unit — metrics/strategy/engine — + 7 HTTP integration +
  5 already counted via reused fixtures), 202/202 total tests passing,
  ruff+mypy clean (67 source files). Live-verified against real
  Longbridge paper-trading credentials: a real `AAPL.US` backtest over
  the most recent ~45 days produced a genuine, non-trivial equity curve
  with one real trade filled and risk-gated exactly as the unit tests
  predict.

- Phase 29: broker discovery (2026-08-29). `GET /brokers` and
  `GET /brokers/{broker_id}` (`apps/api/app/api/routes/brokers.py`,
  `api/schemas_brokers.py`) — authentication-only, scoped to the calling
  user's own `BrokerGrant` rows, so a non-admin trader can finally learn
  which broker ids they may trade on instead of being handed a UUID
  out-of-band. Paginated `limit`(50/500)/`offset` exactly as D027/D031;
  detail route returns 404 for a nonexistent broker and 403 for one the
  caller holds no grant on, matching `require_broker_access`; response is
  a five-field allow-list (`id`, `name`, `kind`, `provider`,
  `is_active`), never a credential-shaped field. Frontend:
  `components/BrokerDiscovery.tsx` + `app/api/brokers/route.ts` (same
  httpOnly-cookie route-handler proxy as every other feature), with a
  per-row "Use" button that pre-fills the real broker id into the trade,
  agent-trade, portfolio and portfolio-history forms via
  `lib/brokerSelection.ts` — a convenience only; the backend still
  re-checks the grant on every request. See docs/DECISIONS.md D034 for
  the grants-define-discoverability scoping decision and the rejected
  alternatives (reusing the `admin:manage` grant listing, a global broker
  listing with an `accessible` flag, permission-gating, 403-on-no-grants).
  11 new backend integration tests (`tests/api/test_brokers.py`) against
  real Postgres + 8 new Vitest tests
  (`apps/web/test/BrokerDiscovery.test.tsx`); **283 backend tests
  passing**, **69 web tests passing**, `ruff check .` and `mypy apps`
  clean (71 source files), `npm run build` clean. Live-verified against a
  running stack: two seeded users with disjoint grants each saw only
  their own broker; cross-user detail 403, unknown id 404, no token 401.

## Tests

Each of Phase 26, Phase 27, and Phase 28 was built in its own parallel
worktree against the same 208-test baseline and independently confirmed a
real `pytest tests/ -q` collected count within its own worktree: **234**
for Phase 26 (208 baseline + 26 new — 13 pure unit tests for the
trade-path Portfolio Manager in `tests/portfolio_manager/test_manager.py`,
5 for its OMS wiring in `tests/oms/test_service_portfolio_manager.py`
including one proving a Portfolio-Manager-resized quantity is itself
re-gated by the Risk Engine, 4 real-Postgres persistence tests for the
`orders.portfolio_*` audit columns in
`tests/db/test_portfolio_decision_persistence.py`, and 4 real-Postgres
HTTP integration tests in `tests/api/test_trades_portfolio_manager.py`),
**228** for Phase 27 (208 baseline + 20 new — 8 unit in
`tests/portfolio/test_scheduler.py`, 12 DB-backed integration in
`tests/api/test_snapshot_scheduler.py` covering scheduled-snapshot
capture, each typed skip reason, and the running asyncio task writing a
real row on its timer), and **226** for Phase 28 (frontend-heavy;
`GET /auth/session` and the admin listing endpoints added their own
backend test files independently of the other two worktrees). None of
those three numbers was the real post-merge total, since each worktree
only saw its own disjoint additions layered on the shared 208 baseline,
not the other two phases' new tests combined.

That real merged total has since been confirmed: **272 tests, all
passing** (now **283** with Phase 29's broker-discovery tests), by a full from-scratch backend verification of the fully-merged
main branch (fresh venv, `ruff check .`, `mypy apps`, real Postgres/Redis
via `docker compose`, `alembic upgrade head` through migration 0009 —
`orders.portfolio_*` from Phase 26 — and `pytest tests/ -q`) run
2026-08-29 after Phases 26/27/28 were merged. `ruff` and `mypy` were both
clean and no cross-phase integration bug was found this time — see
docs/DECISIONS.md D033 for the full verification record and D028 for the
precedent this pass follows.

That 272/283 figure has itself since been superseded by a further
from-scratch verification confirming the true post-Phase-29/30-merge
total: **288 tests, all passing**, run 2026-08-29 against `main` at
`d7069e0` (Phases 29 broker-discovery and 30 backtest-Portfolio-Manager-
gating both merged), following the same D028/D033 precedent — fresh venv,
`ruff check .` (clean), `mypy apps` (clean, 71 source files), a
separately-project-named `docker compose` Postgres/Redis pair,
`alembic upgrade head` through migration 0009 (still the current head —
neither phase added a migration), and `pytest tests/ -q` against that real
database. This does not simply add Phase 29's independently-reported 283
to Phase 30's independently-reported 277 (off its own 272 baseline plus 5
new disjoint tests for the backtest-engine Portfolio Manager gating,
D035): 283 already **is** the correct merged Phase-26-through-29 total per
the paragraph above, and Phase 30 layered its own 5 new tests on top of
that same 283, giving 288 as the real collected count — confirmed live,
not computed. Zero failures, zero errors, 3 pre-existing warnings (same
two `InsecureKeyLengthWarning`s and one `StarletteDeprecationWarning` as
every prior run — neither new nor actionable). See docs/DECISIONS.md D036
for the full verification record.

Phase 33 (D039, this worktree) adds 16 new tests in
`tests/api/test_emergency_stop.py` on top of that confirmed 288 baseline,
for **304 collected and passing** in this worktree against a real Postgres.
As with every parallel-worktree count before it, 304 is this worktree's own
number: sibling phase-31/32 worktrees are adding their own disjoint tests
concurrently, so the true post-merge total must be re-derived from a
clean-room run against merged `main`, per the D028/D033/D036 precedent.


208 tests, all passing - confirmed 2026-08-29 by a full from-scratch
backend verification (fresh venv, `ruff check .`, `mypy apps`, real
Postgres/Redis via docker-compose, `alembic upgrade head` through 0008,
`pytest tests/ -q`) after the Phase 23/24/25 merge, correcting this
section's earlier 210-test placeholder estimate. That run also caught
one real cross-phase bug: `backtesting/engine.py`'s default "today" used
the raw calendar date instead of the most recent trading day, so any run
landing on a Saturday/Sunday rejected every request with
`UNSUPPORTED_DATE_RANGE` - fixed by rolling the default back to the prior
weekday (a caller-supplied `today`, as every existing test uses, is left
untouched). The count below is this verification's actual collected
total, not a per-phase arithmetic sum - see docs/DECISIONS.md D025/D027
for the per-phase new-test breakdowns that make up most of it.
Baseline, 174 tests: fail-closed live-mode gate (4, incl. missing JWT
secret), log redaction (1), health endpoint (1), risk engine (23, incl. 9
duplicate-order-detection tests — D024), paper broker (5), OMS (4, incl. 1
proving `recent_orders` blocks a duplicate before the broker is touched),
execution context (5), order/fill persistence (4, DB-backed, incl. 1
covering `get_recent_filled_orders()`), market data router + snapshot
model (9), Longbridge provider (8, against a fake client), trades HTTP
endpoint (20, DB-backed, all require auth+permission+broker grant — 14
pre-D017 + 4 covering the omitted-price/live-quote path + 2 covering
duplicate-order detection — D024), auth security unit tests (8),
authorization unit tests (3), login route (4, DB-backed), admin routes
(16, DB-backed — 9 create/grant + 7 update/deactivate), broker-state
persistence (3, DB-backed), TraderAgent unit tests (8, against a fake
`LLMProvider`), agent-trades HTTP endpoint (6, DB-backed, incl. 1 covering
duplicate-order detection — D024), TechnicalAnalyst unit tests (7, against
a fake `LLMProvider`), technical-analyst wiring on agent-trades (3,
DB-backed), indicator unit tests (9, pure math), Longbridge history
provider unit tests (5, against a fake candlestick client), history-provider
wiring on agent-trades (4, DB-backed), Portfolio module unit tests (6,
pure average-cost-basis replay math, hand-verified), portfolio HTTP
endpoint (23, DB-backed — 15 pre-D027 covering `GET .../portfolio` + 8
new covering `POST .../portfolio/snapshots` and
`GET .../portfolio/history` — D027).

Phase 28 (D031/D032) adds 18, bringing the backend total to **226**: admin
listing endpoints (12, DB-backed — real rows listed with no password/hash
field, deterministic `offset` paging, `limit=501`/`offset=-1` both 422,
and 403/401 on all three routes) and `GET /auth/session` (6, DB-backed —
real expiry/issued_at bounds, no token material in the response, and 401
for a missing, malformed, expired, or deactivated-user token).

Frontend (`apps/web`, Vitest + React Testing Library): **61 tests**, all
passing — 34 pre-existing plus 27 added in Phase 28 across
`test/PortfolioHistoryChart.test.tsx`, `test/AdminListings.test.tsx`, and
`test/SessionStatus.test.tsx`, covering success, empty results, real
error sentinels, 403, 404, the 401→login redirect, and network failure
for every new component.

## Known Issues

None open.
