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
  a feed, forbidden per spec §57). **Superseded in part by Phase 44/D059**,
  which found that the same already-credentialed Longbridge SDK also
  exposes company fundamentals and news, and built those two analysts on
  it; sentiment remains blocked. `TechnicalAnalyst.analyze()` reads the
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
  FIFO/LIFO cost basis (see D022's alternatives; subsequently revisited
  and built in Phase 34/D041, which derives lots from the `orders`/`fills`
  history rather than adding per-lot state).
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

- **Phase 32 — Portfolio Manager verdict in the web UI (2026-08-29, D038).**
  Closes D029's own "Consequences" gap: `apps/web/`'s trade forms showed
  the Risk Engine's verdict only, never the Portfolio Manager's, even
  though `approved: true` + `status: "rejected"` (a portfolio rejection)
  is a real, easily-misread response combination. New
  `components/PortfolioVerdict.tsx` renders alongside — never replacing —
  the existing risk verdict in both `TradeForm.tsx` and
  `AgentTradeForm.tsx`: a `modify` shows the binding constraint, the
  backend's detail string, and both quantities (requested vs. actually
  filled); a `reject` renders visually and textually distinct from a risk
  rejection (different color/label/icon, so the two rejection sources are
  never conflated); `approve` or a `null` action (Portfolio Manager never
  ran, e.g. risk-rejected first) render no extra panel at all — a null
  action is never shown as if it approved anything. Also fixed a
  pre-existing bug found in passing: the risk block's color was keyed on
  `status`, so a portfolio rejection was painted the Risk Engine's amber;
  it is now keyed on `approved`, so amber always means the Risk Engine
  said no. **No backend change.** **79/79 frontend tests passing** (69
  pre-existing + 10 new, 5 per form: approve/modify/reject/null-action/
  older-shaped-response), `npm run build` clean with zero TypeScript
  errors. **Verified live** against this worktree's own Docker stack
  (ports remapped to 5438/6388/8008; torn down afterwards; the user's own
  dev stack and sibling phase31/33 worktrees confirmed untouched): four
  real trade submissions on one real broker (approve, a real modify via a
  tightened `PORTFOLIO_MAX_SYMBOL_PCT_OF_EQUITY`, a real portfolio reject,
  and a real risk-engine reject) fed verbatim into the real components,
  confirming each renders the correct panel/color. **Not verified live:**
  a full click-through of the running dev server itself — the browser
  tool in this environment could not load its JS chunks, so React never
  hydrated; the real-payload-into-real-component check above substitutes
  for it. No market-data vendor or LLM was wired, so `AgentTradeForm` was
  exercised only through its own tests. See D038.

- **Phase 31 — backtest frontend (2026-08-29, D037).** Closes the gap D035
  recorded verbatim in its own "Not verified live" note: `apps/web/` did not
  render the backtest response at all, let alone the new Portfolio Manager
  counters. Adds `app/api/backtests/route.ts` (the same route-handler-proxy
  + httpOnly-cookie pattern every other authenticated feature uses, minus a
  `brokerId` segment because `POST /backtests` is deliberately not
  broker-scoped per D025), `components/BacktestPanel.tsx`, and one line
  wiring the panel into `/dashboard`. Renders the real `BacktestResult`:
  a hand-rolled inline-SVG equity curve in the same style as
  `PortfolioHistoryChart.tsx` (no new npm dependency, one vertex per real
  trading day, index x-axis, flat curves drawn flat), the five headline
  metrics, and all three D035 counters — `portfolio_modified_trades`,
  `portfolio_modify_risk_blocked_trades`, `portfolio_rejected_trades` —
  never collapsed into one number, always shown even at zero, with a caption
  stating that Risk Engine rejections are a separate gate counted by none of
  them and that all-zero does not by itself mean everything was approved.
  Every real error sentinel is surfaced verbatim and no error path ever
  draws a curve or counters. **No backend change was needed or made.**
  **89/89 frontend tests passing** (69 pre-existing + 20 new in
  `test/BacktestPanel.test.tsx`), `npm run build` clean with zero TypeScript
  errors. **Verified live** against this worktree's own Docker stack (ports
  remapped to 5451/6391/8031; torn down afterwards) with `npm run dev` on
  3031: real Alembic `0001`->`0009`, a seeded user, a real browser login,
  and real submissions through the real UI rendering
  `HTTP 400: NOT_CONFIGURED: no history provider.` and
  `HTTP 422: body: Value error, end_date must be after start_date`, plus a
  real 401 from the route handler with no cookie.
  **Not verified live:** the success path — no market-data vendor
  credentials were readable in this session (the same limitation D025/D035
  recorded), so no real equity curve, real metrics, or real non-zero
  counters could be produced. Success-path rendering, and the
  `UNSUPPORTED_DATE_RANGE:`/`DATA_UNAVAILABLE:` sentinels (both unreachable
  live behind the `NOT_CONFIGURED` guard), are covered by component tests
  against mocked responses only. See D037.

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
- **Phase 34 — selectable FIFO/LIFO cost basis alongside average-cost
  (2026-08-30, D041).** Closes IMPLEMENTATION_STATUS's own "FIFO/LIFO
  cost-basis reporting as a Portfolio module alternative" planned item,
  which D022 had deferred. D022's stated blocker — "this schema has no
  per-lot data" — was true of stored *state* but not of history:
  `orders`/`fills` already record every individual buy with its own
  quantity, price and `filled_at`, which is exactly a lot ledger, so the
  lots are **derived, not invented, and no migration is needed**.
  `apps/api/app/portfolio/models.py` adds `CostBasisMethod`
  (`average`/`fifo`/`lifo`); `snapshot.py` splits the replay into
  `replay_symbol_fills_average()` (verbatim D022) and
  `replay_symbol_fills_lots()` (a signed open-lot tracker; one
  `newest_first` flag is the only difference between FIFO and LIFO, so
  they cannot drift apart), behind an unchanged `replay_symbol_fills()`
  that still defaults to AVERAGE. Both are pure — no DB, no I/O, no LLM.
  `GET /brokers/{broker_id}/portfolio` gains an optional
  `?cost_basis_method=` query param; **omitting it is byte-identical to
  the D022 response** (a test asserts full response equality, not just
  matching numbers), and an unrecognised value is a 422, never a silent
  fallback. **Deliberate non-scope at the time:** the persisted-snapshot
  POST (D027) and the scheduler (D030) remained average-cost only —
  `portfolio_snapshots` recorded no method column, so a stored FIFO row
  would have been indistinguishable from an average one in
  `GET .../history`. **Superseded by Phase 36/D044**, which added that
  column and opened the write path. **317/317 pytest tests passing** (304 D040-confirmed
  merged baseline + 13 new: 10 pure lot-math unit tests in
  `tests/portfolio/test_snapshot.py`, 3 DB-backed HTTP tests in
  `tests/api/test_portfolio.py`), `ruff check .` and `mypy apps` clean (74
  source files). **Verified live** against this worktree's own Docker
  Postgres/Redis and a rebuilt API container (ports remapped to
  5442/6389/8010 to avoid the user's dev stack and the sibling Phase 35
  worktree; torn down afterwards): a real seeded multi-lot history — buy
  10 @ 100, buy 10 @ 110, sell 15 @ 120, marked at 130 — returned over
  real HTTP `average` basis 105 / realized 225 / unrealized 125, `fifo`
  110 / 250 / 100, and `lifo` 100 / 200 / 150, each matching the
  hand-computed figure, with `cash` 99700 and `total_equity` 100350
  identical across all three and `?cost_basis_method=hifo` rejected 422.
  See D041.

- **Phase 36 — `cost_basis_method` recorded on every persisted snapshot
  (2026-08-30, D044).** Closes the single tracked follow-on D041 left
  open. Migration `0011_portfolio_snapshots_cost_basis_method.py` adds
  `cost_basis_method VARCHAR(16) NOT NULL DEFAULT 'average'` to
  `portfolio_snapshots` — the column whose absence was D041's stated
  reason for keeping the write path average-only.
  `POST /brokers/{broker_id}/portfolio/snapshots` now takes an optional
  `cost_basis_method` **in its request body** (default `average`, same
  `average`/`fifo`/`lifo` enum, 422 on anything else with nothing
  written), threads one variable into both `compute_portfolio_snapshot()`
  and `persist_portfolio_snapshot()` so the recorded method cannot
  disagree with the numbers, and echoes it back.
  `GET /brokers/{broker_id}/portfolio/history` surfaces it on every entry,
  so a FIFO row's realized P&L can no longer be misread as average-cost.
  The scheduler (D030) gains the same capability via a new
  `portfolio_snapshot_cost_basis_method` setting that **defaults to
  `average` — its behaviour is unchanged unless explicitly configured**,
  and is typed as the real enum so a typo fails at app startup.
  **Backward compatibility is the load-bearing constraint:** every row
  written before the migration reads `average` from the column's
  `server_default`, never null — that is a recorded fact (the write path
  was average-only until now), not a stand-in for "unknown" — and a test
  INSERTs a row omitting the column entirely, asserting both the raw
  Postgres value and the HTTP response read `average`.
  **359/359 pytest tests passing** (349 D043-confirmed merged baseline +
  10 net new: 6 in `tests/api/test_portfolio.py`, which also replaced
  D041's now-obsolete `test_persisted_snapshots_stay_average_cost_...`
  guard against the very behaviour this phase adds; 3 DB-backed scheduler
  tests in `tests/api/test_snapshot_scheduler.py`; 2 settings tests in
  `tests/test_config.py`). `ruff check .` and `mypy apps` clean (75 source
  files). **Verified live** against this worktree's own Docker
  Postgres/Redis and a rebuilt API container (ports remapped to
  5452/6399/8020 via an untracked compose override to avoid the user's dev
  stack and the sibling Phase 37 worktree; torn down afterwards): all
  eleven migrations clean from empty with `\d portfolio_snapshots`
  confirming the NOT NULL default, then over real HTTP on a real seeded
  multi-lot history (buy 10 @ 100, buy 10 @ 110, sell 15 @ 120, marked at
  130) the default POST stored and read back `average` 105 / 225 / 125,
  `fifo` 110 / 250 / 100 and `lifo` 100 / 200 / 150 — each matching
  D041's already-verified hand-computed figures — with `cash` 99700 and
  `total_equity` 100350 identical across all three, history naming each
  row's method, `hifo` rejected 422, and a column-less INSERT reading back
  `average`. See D044.

- Phase 44: FundamentalAnalyst + NewsAnalyst (2026-09-01). Closes the
  "no real data source" block that kept these two unbuilt since Phase 16.
  No new vendor: direct introspection of the already-installed, already-
  credentialed `longport` 4.3.7 SDK found company fundamentals
  (`AsyncFundamentalContext.company()`/`.valuation()`) and news
  (`AsyncContentContext.news()`) alongside the quotes and candlesticks
  D015/D021 already use — same three `LONGPORT_*` variables, no new
  service, no new credentials. Adds two ports
  (`marketdata/fundamentals_provider.py`, `marketdata/news_provider.py`),
  their Longbridge implementations, one pure-deterministic metrics module
  (`marketdata/fundamental_metrics.py`: exact `100/PE` earnings yield and
  latest-point-by-timestamp selection — the LLM computes nothing), and the
  two analysts. Both return the same `stance`/`summary`/`confidence`
  contract as `TechnicalRead` with no price/side/quantity field. Wired as
  optional, additive context into `POST .../agent-trades` exactly like
  D019 — no new endpoint, no new request/response field, no new error
  response; all three analysts now run concurrently and are independently
  failure-isolated. A missing vendor metric renders as "not reported by
  the vendor", never a zero or an estimate; the news prompt's headline
  count and date range are computed in code so the model cannot overstate
  its coverage. `SentimentAnalyst` deliberately NOT built (see D059).
  481 tests pass (57 new). **Not verified against a real vendor response:**
  no `LONGPORT_*` credentials exist in this environment, so the provider
  layer is verified only against fakes at the vendor boundary plus SDK
  introspection — a first live run remains outstanding. See D059.

## In Progress

Nothing currently mid-implementation.

## Blocked

Nothing currently blocked.

## Planned

Phase 20+ (order not finalized): the rest of the parallel analyst layer —
fundamental and news are now DONE (Phase 44/D059, built on the existing
Longbridge SDK's fundamentals and news endpoints); **sentiment remains
blocked** on a real sentiment data source, since the SDK exposes no
sentiment score and having the LLM invent one from headlines is exactly
what spec §57 forbids (see D059's scope cut). Research debate is still
planned. FIFO/LIFO cost-basis reporting is now DONE (Phase
34/D041), and its one tracked follow-on - offering the choice on the
write path, which needed a migration recording which method produced each
row - is now DONE too (Phase 36/D044). No cost-basis follow-on remains.
Automatic/scheduled
portfolio snapshotting is now DONE (Phase 27/D030) - remaining follow-ons
there: market-hours awareness is now PARTIALLY addressed (Phase 35/D042
gates whole cycles on a UTC weekend; intraday after-hours and exchange
holidays are still NOT checked, and closing that needs a real
trading-calendar port over the Longbridge SDK's `trading_session()` /
`trading_days()`, plus a market-timezone source this repo does not have -
see D042's Alternatives for the three concrete blockers and the
fail-open requirement). Multi-worker safety is now DONE (Phase 38/D047:
a non-blocking Postgres advisory lock per cycle, so >1 uvicorn/gunicorn
worker yields one row per interval instead of one per worker); no
scheduler follow-on remains apart from the intraday/holiday half of
market-hours awareness above. If a second analyst is
ever added, revisit whether
parallel-execution infrastructure across analysts is now warranted
(deliberately not built in Phase 16 — one analyst has nothing to
parallelize against). Frontend candidates remaining after Phase 20/D023
and Phase 24/D026, now further reduced by Phase 28/D031/D032 (the
historical performance chart, the users/roles/grants listing UI, and
session expiry UX are all built) and by Phase 29/D034 (broker discovery,
backend and UI): Playwright e2e coverage — deliberately skipped in
Phase 28 because it needed a new tool dependency — is now DONE too
(Phase 37/D045, 11/11 specs passing against a real backend). No frontend
candidates remain. Re-verify D018/D019's LLM-completion path
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

- Phase 35: market-hours gating for the snapshot scheduler (2026-08-30).
  `apps/api/app/portfolio/market_hours.py` — a frozen, I/O-free
  `MarketHoursGate` whose `evaluate(as_of)` returns a typed
  `MarketHoursDecision` (`RUN` / `RUN_GATE_DISABLED` / `SKIP_WEEKEND`).
  `run_snapshot_cycle()` (D030) evaluates it **before** the eligible-broker
  query, so a gated cycle costs zero SQL and zero market-data vendor
  calls; `SnapshotCycleResult` gained a `market_hours` field and a `gated`
  property, and `PortfolioSnapshotScheduler` gained a `market_hours_gate`
  and an injectable `clock` read once per cycle. New setting
  `PORTFOLIO_SNAPSHOT_MARKET_HOURS_GATE_ENABLED`, **default true** (the
  safe side here is the side that does less work), wired in `main.py` and
  surfaced in the startup log line. No new dependency, table, migration,
  or HTTP surface.
  **Scope is weekends only, and deliberately so.** This partially closes
  D030's recorded gap ("the interval is wall-clock, not market-hours-aware
  ... will keep recording unchanged after-hours rows"). Saturday/Sunday is
  a property of the calendar, not of any exchange's policy, so it is
  computable without a vendor; intraday session times and exchange
  holidays are NOT checked, because doing that honestly needs a real
  trading-calendar source. The installed `longport` v4.3.7 SDK *does*
  expose `trading_session()` and `trading_days()` on `AsyncQuoteContext`
  (confirmed by direct introspection, not assumed), but wiring them needs
  a market→timezone map the SDK does not supply, a symbol→`Market`
  mapping, and — for `market_status()` — a second SDK context this
  codebase has never constructed. That is a phase, not a footnote; see
  docs/DECISIONS.md D042, which also records that a market-calendar pip
  dependency was deliberately NOT added and that a hardcoded holiday/hours
  table was rejected as fabrication under docs/TRADING_SAFETY.md / spec
  §57.
  32 new tests (20 pure unit in `tests/portfolio/test_market_hours.py`,
  12 DB-backed in `tests/api/test_snapshot_scheduler_market_hours.py`);
  **336 backend tests passing** in this worktree over a measured 304
  baseline, `ruff check .` and `mypy apps` clean (75 source files).
  Live-verified against a real uvicorn process and real Postgres on a real
  UTC **Sunday**: gate on → 12 consecutive `portfolio_snapshot_cycle_gated`
  events, zero captures, zero rows for a genuinely eligible seeded broker;
  same process and broker with the gate off → 6 real captures at
  `total_equity = 100100`. Weekday behavior in tests uses an **injected**
  Monday instant, not a real one.

- Phase 41: production readiness — a real readiness probe and a
  multi-stage, non-root API image (2026-08-31, D054). **(1) `GET /health`
  was fake.** It reported `"status": "ok"` from in-process `Settings`
  alone and never checked whether the app could reach Postgres, so an
  instance with an unreachable database advertised itself as healthy and
  an orchestrator would have kept routing traffic to it. `/health` is now
  explicitly the **liveness** probe (unchanged body, no I/O — deliberately
  stays 200 during a database outage, because restarting a process does
  not fix a down database and a dependency-checking liveness probe would
  crash-loop every replica). The new **`GET /health/ready`** runs a real
  `SELECT 1` through the app's own engine, bounded by
  `HEALTH_READINESS_TIMEOUT_SECONDS` (default 3.0), and returns 503 with a
  fixed-vocabulary `reason` (`connection_failed` / `timeout`) and the same
  body shape as its 200. Exception *type names* only, never messages — a
  DSN error carries the password and this endpoint is unauthenticated.
  **No Redis check**: Redis is provisioned but still unwired in any Python
  code (re-confirmed this phase), so checking it would be fabricated
  signal. **(2) `apps/api/Dockerfile` was single-stage and ran as root.**
  Now `builder` (deps → `/opt/venv`) + `runtime` (venv, app code,
  `packages/`, `migrations/`, `alembic.ini`), `USER appuser` before `CMD`,
  pip/setuptools/wheel removed from both the venv and `/usr/local`. Port,
  CMD, and env-var contract unchanged; no new dependency; no migration.
  Image **373MB → 354MB** (an intermediate version was briefly *larger*
  at 387MB — see D054). **404 backend tests passing** over the measured
  400 baseline, `ruff check .` clean, `mypy apps` clean (78 source files,
  77 before). Live-verified by actually running the rebuilt image: `whoami`
  → `appuser`, `alembic current` → `0012 (head)` from inside it, and the
  full probe cycle against a real isolated Postgres — healthy 200/200,
  Postgres stopped → `/health` still 200 while `/health/ready` → 503
  `reason: "timeout"`, Postgres back → `/health/ready` 200 again with the
  API container never restarted.

- Phase 40: CI hardening — a frontend job, two long-standing CI-breaking
  bugs fixed, migration round-trip confirmed sound (2026-08-31, D052).
  **(1) `apps/web` now has CI at all.** A second `web` job in
  `.github/workflows/ci.yml` runs `npm ci` → `npm run build` → `npm test`
  (Vitest) on Node 22, `working-directory: apps/web`, with npm caching
  keyed on `apps/web/package-lock.json`. It declares no services and no
  env because none of those commands need Postgres, Redis, or the API, and
  it does not `needs:` the backend job so neither can mask the other's
  failure. Twenty-plus phases of frontend work had no automated coverage
  on push or PR before this. **(2) The secret-scan step had been failing
  every build since Phase 1/7.** It inlined its own regex into the
  workflow and then `git grep`-ed the repo for it, so it matched
  `ci.yml` itself plus the deliberate `sk-live-`-shaped fixture in
  `tests/test_logging.py`, and exited 1 before `ruff` ever ran. The scan
  moved to `scripts/secret_scan.sh` (new), which excludes only itself by
  path and skips lines marked `pragma: allowlist secret` — per line, not
  per file. **(3)
  `tests/test_config.py::test_missing_jwt_secret_key_fails_closed` was
  failing under CI's own env.** `Settings(_env_file=None)` does not
  suppress the process environment, and the CI job exports
  `JWT_SECRET_KEY` for every step, so the fail-closed assertion ran with
  the key present; the test now `monkeypatch.delenv`s it. **(4) The
  migration downgrade round-trip is genuinely fine** — verified against a
  real TimescaleDB/pg16 instance, twice, with `psql` confirming
  `downgrade base` leaves only `alembic_version` and zero enum types. No
  migration was changed. Deliberately out of scope and recorded as such in
  D052: the Playwright e2e suite is **not** wired into CI (needs a live
  backend, seeded Postgres, and a browser install — a bigger job that
  could not be verified CI-equivalent locally), and `npm run lint` is
  **not** in the web job (it is currently red: two
  `react-hooks/set-state-in-effect` errors in
  `apps/web/components/SessionStatus.tsx`, one Next warning in
  `apps/web/lib/session.ts`). No new CI service, SaaS, or secret was
  introduced. Verified live, every CI step run locally with its exact
  command against a private compose stack on remapped ports: ruff clean,
  mypy 77 files clean, migrations both directions, **400 pytest tests
  passing** (399/1 before fix 3), **102 Vitest tests across 13 files
  passing**, `npm run build` green with all 22 routes.

- Phase 39: security-audit remediation — login lockout, CSRF posture on
  record, vitest CVE (2026-08-30). Fixes three findings from a completed
  read-only security review of the backend and frontend.
  **(1) Failed-login lockout on `POST /auth/login`** (D049), the app's one
  previously unthrottled credential endpoint: `apps/api/app/auth/lockout.py`
  (new, pure arithmetic with a single `utcnow()` clock seam) + two new
  `users` columns (`failed_login_count`, `locked_until`; migration `0012`)
  + two new settings (`AUTH_MAX_FAILED_LOGIN_ATTEMPTS` default 5, `0`
  disables; `AUTH_LOCKOUT_DURATION_MINUTES` default 15, both validated at
  startup). N consecutive failures lock the account; a correct password
  during the lock gets **423** with a clear message, while a wrong password
  still gets the generic **401** — so 423 is unreachable to anyone who
  doesn't already know the password and adds no email-enumeration oracle on
  top of the existing `_DUMMY_HASH` timing parity. No new pip dependency and
  no first-ever Redis client: Postgres was already a hard dependency of this
  route, and unlike an in-process counter it survives restarts and is
  correct under `uvicorn --workers N`.
  **(2) CSRF posture recorded** (D050): documentation only — `SameSite=Lax`
  on D020's httpOnly cookie remains the sole defence, which is sound
  precisely because no route in this app changes state on a GET. Written up
  in `docs/API.md` so the invariant it rests on is visible to whoever adds
  the next route.
  **(3) vitest 2 → 4** in `apps/web` (D050), clearing a critical
  `vitest --ui` advisory (never used here, dev-only, never shipped) plus
  transitive `vite`/`esbuild` ones: `npm audit` **5 → 0 vulnerabilities**;
  `@vitejs/plugin-react` to v6 and `vitest.config.ts` → `.mts` with
  `import.meta.dirname` were the real config migrations required, not
  suppressed warnings.
  **400 backend tests passing** over the measured 379 baseline (+21: 9
  pure-arithmetic, 8 real-Postgres integration, 4 config-validation),
  `ruff check .` and `mypy apps` clean (77 source files); **102 Vitest
  tests passing** over the 99 baseline (+3 `LoginForm` tests pinning that
  the 423 message reaches the user verbatim), `npm run build` succeeds.
  Live-verified against a real uvicorn + real Postgres: real HTTP POSTs
  drove a real lockout (401 ×3 → 423 on the correct password → 401 on a
  wrong one), the `users` row showed the real counter and timestamp, and
  the lock cleared on the **real wall clock** with a deliberately short
  1-minute configured window. The 15-minute default's expiry is covered by
  the injected-clock tests, not walked in real time.

- Phase 38: multi-worker safety for the portfolio snapshot scheduler
  (2026-08-30). `apps/api/app/portfolio/cycle_lock.py` (new) +
  `PORTFOLIO_SNAPSHOT_CYCLE_LOCK_ENABLED` (new setting, **default true**)
  + `cycle_lock=` on `run_snapshot_cycle()` and
  `PortfolioSnapshotScheduler`, wired in `main.py`. Closes the last open
  consequence D030 recorded: the scheduler's lifespan-owned loop runs once
  per worker process, so `uvicorn --workers N` used to append N
  near-simultaneous rows per interval to an **append-only** table. A cycle
  now takes a non-blocking session-level
  `pg_try_advisory_lock(1953653092, 1936613744)` — two fixed int4 keys
  (ASCII `trad`/`snap`), not `hashtext()`, so the pair is stable across
  Postgres major versions and greppable in `pg_locks` — before enumerating
  any broker, and releases it with `pg_advisory_unlock` in a `finally` on
  every path including an exception. A worker that cannot get it records
  the new typed `SnapshotCycleLockDecision.SKIPPED_LOCK_HELD` (an ordinary
  outcome, logged at info, **not** an error) and skips without one broker
  query or one vendor call. The lock is taken *after* D042's weekend gate,
  so a gated weekend cycle still costs zero DB round-trips.
  **No new dependency**: Redis is provisioned in `docker-compose.yml` and
  named by `Settings.redis_url` but is still unwired in Python as of this
  phase, and Postgres is the one service the cycle cannot run without
  anyway — so the lock adds nothing that can fail independently of the
  work it guards. No new table and no migration either: a session-level
  advisory lock dies with its connection, so a SIGKILLed worker cannot
  wedge the schedule. Single-worker behaviour is unchanged (the lock is
  always acquired) and `=false` restores D030's exact unguarded path.
  **379/379 pytest tests passing** (359 D046-confirmed baseline + 20 new:
  8 unit in `tests/portfolio/test_cycle_lock.py`, 2 settings tests in
  `tests/test_config.py`, 10 DB-backed in
  `tests/api/test_snapshot_scheduler_multiworker.py` where the contending
  "other worker" is a genuinely separate real session really holding the
  real lock). `ruff check .` and `mypy apps` clean (76 source files).
  **Verified live** against this worktree's own Docker Postgres (isolated
  compose project `tos38`, ports remapped to 55432/56379 so the user's dev
  stack on 5432/6379 was untouched; torn down afterwards) with
  `scripts/seed_e2e.py` fixtures: **two separate OS processes** each
  running one real cycle, released at the same instant against the same
  database — with the lock on, one process captured and the other logged
  `portfolio_snapshot_cycle_lock_not_acquired` and returned
  `skipped_lock_held`, leaving **1 row**; the identical race with the lock
  off produced **2 rows**, reproducing the pre-D047 bug live as a control.
  See D047.

- Phase 37: Playwright e2e coverage for `apps/web/` (2026-08-30).
  `apps/web/playwright.config.ts` + `apps/web/e2e/` (`fixtures.ts`,
  `helpers.ts`, `global-setup.ts`, `auth.spec.ts`, `trade.spec.ts`,
  `broker-discovery.spec.ts`, `admin.spec.ts`) and `scripts/seed_e2e.py`
  — the last remaining frontend candidate, unblocked by the user's
  explicit permission for the one new dependency (`@playwright/test`
  plus Chromium only, not all three engines). Run by a new
  `npm run test:e2e`; deliberately NOT part of `npm test`, and
  `vitest.config.ts` now excludes `e2e/**` so Vitest never collects specs
  that cannot run without a database. Nothing is mocked: real Chromium →
  real `next dev` → real route handlers → real FastAPI → real Postgres
  with real seeded rows, and real `orders`/`fills` written by the real
  Risk Engine (D004) and Portfolio Manager (D029). The backend URL is
  configurable (`E2E_API_BASE_URL`, default `http://localhost:8000`), as
  is the dev-server port (`E2E_WEB_PORT`, default 3100). Covered: valid
  login → `/dashboard` + a real httpOnly cookie invisible to
  `document.cookie`; invalid login → the backend's real
  `Incorrect email or password.`, no redirect, no cookie; no cookie →
  proxy bounce to `/login`; an invalid session on a protected page →
  `/login?reason=session-expired` (D032); a real approved trade with a
  real fill and, per D038, no Portfolio Manager panel; a real
  `missing_stop_price` risk rejection; a real Portfolio Manager MODIFY
  (requested 100 / filled 50, binding constraint `symbol_concentration`)
  beside a non-amber risk verdict; a real 403 on an ungranted broker;
  `BrokerDiscovery` listing real granted brokers and pushing one into the
  trade and agent-trade forms; and `/admin` listing real users/roles/
  grants for an admin while a non-admin gets three real 403s. **11/11
  specs passing, verified three times consecutively** against a real stack
  (compose project `tradingos-e2e37` on ports 55437/56437, all eleven
  migrations applied, real uvicorn on 8037, real Chromium) — the user's
  own dev stack on 5432/6379/8000/3005 was left untouched. Not covered,
  deliberately: every flow needing a real LLM provider or market-data
  vendor (agent trades, quotes, portfolio views, backtests) since neither
  is configured here, and the Portfolio Manager's REJECT /
  `max_open_positions` paths, which `TradeForm` cannot reach because it
  has no "marks" input. See docs/DECISIONS.md D045 (D044 was claimed by
  the parallel phase-36 worktree).

## Tests

Phases 43, 44, and 45 (live-trading path, FundamentalAnalyst/NewsAnalyst,
dashboard redesign) were built in three parallel worktrees off the same
D057 `main` and independently confirmed **487** (Phase 43, 424 baseline +
63 new) and **481** (Phase 44, same 424 baseline + 57 new) — disjoint new
test files from two branches that both touched
`apps/api/app/api/dependencies.py`. After merging all three (43 → 44 → 45,
resolving an additive conflict in that same file plus `docs/DECISIONS.md`)
a full from-scratch `pytest tests/ -q` against a freshly migrated, isolated
Postgres/Redis stack (compose project `tosverifymain`, ports 55499/56499,
never touching the user's own running dev stack on 5432/6379/8000/3005)
collected the real merged total: **544 passed, 0 failed, 3 pre-existing
warnings** (424 + 63 + 57 = 544 exactly — confirms the two branches' new
tests were genuinely disjoint, not that any silently dropped or collided).
`ruff check .` and `mypy apps` both clean (86 source files); `bash
scripts/secret_scan.sh` clean; `alembic upgrade head` applied cleanly with
no new migration from any of the three phases. The live-trading safety
invariant was explicitly re-checked post-merge: `git grep` for
`live_trading_enabled\s*=\s*True|LIVE_TRADING_ENABLED=true` across `apps/`
and `tests/` returns only docstrings/error-message text and two
pre-existing unit tests that construct a `Settings` object to test the
fail-closed validator itself (`tests/core/test_execution_context.py`,
`tests/test_config.py`) — neither starts an app or attempts a broker call.
No code path anywhere sets this flag true and runs. See D061 for the full
verification record.

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

That re-derivation has since been done. A full from-scratch backend
verification run 2026-08-29 against `main` at `05e6cc2` (Phases 31
backtest-frontend, 32 portfolio-manager-UI, and 33 emergency-stop-admin
all merged) confirmed the true post-merge total is **304 tests, all
passing** — exactly Phase 33's own worktree count, because Phases 31 and
32 were frontend-only and added no backend tests on top of the 288
baseline. Following the same D028/D033/D036 precedent: fresh Python 3.13
venv, `pip install -e ".[dev]"`, `ruff check .` (clean, "All checks
passed!"), `mypy apps` (clean, "Success: no issues found in 74 source
files" — 74 vs. D036's 71 reflects Phase 33's new `safety/` module),
a separately-named throwaway Postgres/Redis pair on remapped host ports
(the user's own default-port 5432/6379 dev stack, plus their uvicorn on
8000 and Next.js dev server on 3005, was left completely untouched and
confirmed still running before and after), `alembic upgrade head` — all
ten migrations (0001 through Phase 33's new 0010
`emergency_stop_events`) applied cleanly in sequence against a real
Postgres 16 (timescaledb image) database with no manual intervention —
and `pytest tests/ -q` against that same real database: 304 passed, 3
pre-existing warnings (the same two `InsecureKeyLengthWarning`s and one
`StarletteDeprecationWarning` seen in every prior verification pass),
zero failures, zero errors. One transient failure surfaced on the first
attempt (`test_missing_jwt_secret_key_fails_closed`) — the same shell-
environment-leakage artifact documented in D036, this time from this
verification's own shell exporting `JWT_SECRET_KEY` ahead of the run;
re-running with only `DATABASE_URL` and `REDIS_URL` set reproduced the
clean 304-pass result with no code changes required. No real cross-phase
integration bug was found between Phase 33's DB-read emergency-stop
wiring (D039) and Phases 26-30's earlier trade-path work. Frontend
(`apps/web`) test file count was independently confirmed unchanged at
the 89-test / 12-file total reported after Phase 31 (D037): neither
Phase 32 nor Phase 33 added a new `apps/web/test/*.test.tsx` file. See
docs/DECISIONS.md D040 for the full verification record.

A full from-scratch backend verification run 2026-08-30 against `main` at
`ba941cb` (Phase 34 FIFO/LIFO cost-basis and Phase 35 market-hours
scheduler gate both merged) confirmed the true post-merge total is
**349 tests, all passing** — exactly the 304 D040-confirmed baseline plus
Phase 34's 13 new tests plus Phase 35's 32 new tests, since the two
phases' new test files are disjoint and neither phase's tests exercise
the other's code path. Following the same D028/D033/D036/D040 precedent:
fresh Python venv, `pip install -e ".[dev]"`, `ruff check .` (clean, "All
checks passed!"), `mypy apps` (clean, "Success: no issues found in 75
source files"), a separately-named throwaway Postgres/Redis pair on
remapped host ports 55432/56379 under its own compose project name
`tradingos-verify34-35` (the user's own default-port 5432/6379 dev stack,
plus their uvicorn on 8000 and Next.js dev server on 3005, was left
completely untouched and confirmed still running before and after),
`alembic upgrade head` — all eleven migrations (0001 through Phase 33's
`0010_emergency_stop_events`) applied cleanly in sequence against a real
Postgres 16 (timescaledb image) database with no manual intervention and
no new migration from either phase, as expected — and `pytest tests/ -q`
against that same real database: 349 passed, 3 pre-existing warnings (the
same two `InsecureKeyLengthWarning`s and one `StarletteDeprecationWarning`
seen in every prior verification pass), zero failures, zero errors. One
transient failure surfaced on the first attempt
(`test_missing_jwt_secret_key_fails_closed`) — the same shell-
environment-leakage artifact documented in D036 and D040, this time from
this verification's own shell exporting `JWT_SECRET_KEY` via its
throwaway `.env` file; re-running with that variable unset reproduced the
clean 349-pass result with no code changes required. No real cross-phase
integration bug was found between Phase 34's cost-basis method (D041) and
Phase 35's scheduler gate (D042). See docs/DECISIONS.md D043 for the full
verification record.

A full from-scratch backend verification run 2026-08-30 against `main` at
`034f4c3` (Phase 36 FIFO/LIFO-aware portfolio-snapshot cost-basis
persistence and Phase 37 frontend-only work both merged) confirmed the
true post-merge total is unchanged at **359 tests, all passing** — Phase
36's own report of 359, independently reconfirmed, since Phase 37 shipped
no backend changes and no new `tests/*.py` files. Following the same
D028/D033/D036/D040/D043 precedent: fresh Python venv, `pip install -e
".[dev]"`, `ruff check .` (clean, "All checks passed!"), `mypy apps`
(clean, "Success: no issues found in 75 source files"), a separately-named
throwaway Postgres/Redis pair on remapped host ports 15432/16379 under its
own compose project name `tos-verify` (the user's own default-port
5432/6379 dev stack, plus their uvicorn on 8000 and Next.js dev server on
3005, was left completely untouched and confirmed still running before
and after), `alembic upgrade head` — all eleven migrations (0001 through
Phase 36's `0011_portfolio_snapshots_cost_basis_method`) applied cleanly
in sequence against a real Postgres 16 (timescaledb image) database with
no manual intervention and no new migration beyond 0011, as expected —
and `pytest tests/ -q` against that same real database: 359 passed, 3
pre-existing warnings (the same two `InsecureKeyLengthWarning`s and one
`StarletteDeprecationWarning` seen in every prior verification pass), zero
failures, zero errors. One transient failure surfaced on the first
attempt (`test_missing_jwt_secret_key_fails_closed`) — the same shell-
environment-leakage artifact documented in D036, D040, and D043, this
time from this verification's own shell exporting `JWT_SECRET_KEY` via its
throwaway `.env` file; re-running with that variable unset reproduced the
clean 359-pass result with no code changes required. No real cross-phase
integration bug was found. A separate frontend check was also run:
`cd apps/web && npm install && npm run build && npm test` (Vitest unit
tests only, per this pass's scope — the new Playwright e2e suite added in
Phase 37 needs its own real backend/DB setup and was skipped here, already
verified live by Phase 37's own agent) — build succeeded, and **99 Vitest
tests, all passing**, exactly matching Phase 37's own report, unchanged.
See docs/DECISIONS.md D046 for the full verification record.

A full from-scratch backend verification run 2026-08-30 against `main` at
`b0040ea` (Phase 38's multi-worker snapshot-scheduler advisory lock
merged) confirmed the true post-merge total is **379 tests, all
passing** — exactly Phase 38's own independently-reported 379, since
Phase 38 added its new coverage on top of the 359 D046-confirmed
baseline and no other phase landed in between. Following the same
D028/D033/D036/D040/D043/D046 precedent: fresh Python venv, `pip install
-e ".[dev]"`, `ruff check .` (clean, "All checks passed!"), `mypy apps`
(clean, "Success: no issues found in 76 source files" — 76 vs. D046's 75
reflects Phase 38's new `apps/api/app/portfolio/cycle_lock.py`), a
separately-named throwaway Postgres/Redis pair on remapped host ports
15532/16479 under its own compose project name `tosverify38` (the user's
own default-port 5432/6379 dev stack, plus their uvicorn on 8000 and
Next.js dev server on 3005, was left completely untouched and confirmed
still running via `docker ps` and live HTTP checks against 8000/3005
before and after), `alembic upgrade head` — all eleven migrations (0001
through Phase 36's `0011_portfolio_snapshots_cost_basis_method`, still
the current head) applied cleanly in sequence against a real Postgres 16
(timescaledb image) database with no manual intervention and no new
migration from Phase 38, exactly as expected for an advisory-lock feature
that adds no schema — and `pytest tests/ -q` against that same real
database: 379 passed, 3 pre-existing warnings (the same two
`InsecureKeyLengthWarning`s and one `StarletteDeprecationWarning` seen in
every prior verification pass — neither new nor actionable), zero
failures, zero errors, in the clean run. One transient failure surfaced
on the first attempt (`test_missing_jwt_secret_key_fails_closed`) — the
same recurring D036/D040/D043/D046 shell-environment-leakage artifact:
this verification's own shell had exported `JWT_SECRET_KEY` ahead of
`alembic upgrade head` and the first `pytest` run, which leaked into that
one test's `Settings(_env_file=None)` construction; re-running with only
`DATABASE_URL`/`REDIS_URL` set (and `JWT_SECRET_KEY` unset) reproduced
the clean 379-pass result with no code changes required. No real
cross-phase integration bug was found between Phase 38's advisory-lock
cycle guard (D047) and any earlier phase's work. See docs/DECISIONS.md
D048 for the full verification record.


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

Frontend (`apps/web`, Vitest + React Testing Library): **89 tests**, all
passing. 61 through Phase 28 — 34 pre-existing plus 27 added in Phase 28
across `test/PortfolioHistoryChart.test.tsx`, `test/AdminListings.test.tsx`,
and `test/SessionStatus.test.tsx`; 8 more from Phase 29's broker discovery
(`test/BrokerDiscovery.test.tsx`), bringing the pre-Phase-31 total to 69;
and 20 added in Phase 31 (`test/BacktestPanel.test.tsx`, D037). Between
them they cover success, empty results, real error sentinels, 403, 404,
422, the 401→login redirect, and network failure for every component.

Frontend e2e (`apps/web/e2e`, Playwright + Chromium): **11 specs, all
passing** — a separate suite from the Vitest one above, run by
`npm run test:e2e` and never by `npm test`. It cannot run without a real
backend, a real migrated database and the fixtures `scripts/seed_e2e.py`
writes; see docs/DEVELOPMENT_WORKFLOW.md for the bring-up and
docs/DECISIONS.md D045 for what it does and does not cover. The Vitest
count above is unchanged by Phase 37 (99 as run on this branch): the
phase adds no component test, only the `e2e/**` exclude that keeps the
two suites apart.

A full from-scratch integration verification run 2026-08-31 against
`main` at `c2e0ec4` (Phase 39's login-lockout persistence and the
vitest 2→4 major bump merged) confirmed the true post-merge backend
total is **400 tests, all passing** — exactly Phase 39's own
independently-reported 400. Following the same
D028/D033/D036/D040/D043/D046/D048 precedent: fresh Python 3.13 venv
(3.11/3.12 were unavailable on the host; 3.13 is within the project's
`>=3.11` requirement and all dependencies built clean wheels for it),
`pip install -e ".[dev]"`, `ruff check .` (clean, "All checks passed!"),
`mypy apps` (clean, "Success: no issues found in 77 source files" — 77
vs. D048's 76 reflects Phase 39's new login-lockout code), a
separately-named throwaway Postgres/Redis pair on remapped host ports
55432/56379 under its own compose project name `tradingos-verify39`
(the user's own default-port 5432/6379 dev stack, plus their uvicorn on
8000 and Next.js dev server on 3005, was left completely untouched — no
`docker compose down` was ever run, and the throwaway config lived in
a temporary `.env`-free setup: `DATABASE_URL`/`REDIS_URL`/`JWT_SECRET`
were exported as shell variables for this session only, since
`pydantic-settings` gives shell env vars precedence over the repo's real
`.env` file, so the user's own `.env` was never read from or written to),
`alembic upgrade head` — all twelve migrations (0001 through Phase 39's
`0012_users_login_lockout`) applied cleanly in sequence against a real
Postgres 16 (timescaledb image) database with no manual intervention —
and `pytest tests/ -q` against that same real database: 400 passed, 3
pre-existing warnings (the same two `InsecureKeyLengthWarning`s and one
`StarletteDeprecationWarning` seen in every prior verification pass),
zero failures, zero errors. No cross-phase integration bug was found. A
separate frontend check was also run: `cd apps/web && npm install &&
npm run build && npm test` (Vitest unit tests only, per this pass's
scope — Playwright e2e needs its own real backend and was skipped) —
`npm install` reported 0 vulnerabilities, `npm run build` succeeded with
zero TypeScript errors under the bumped vitest/vite toolchain, Vitest
reported **102 tests across 13 files, all passing**, exactly matching
Phase 39's own report, and a standalone `npm audit` confirmed 0
vulnerabilities, verifying Phase 39's CVE-clearing claim. See
docs/DECISIONS.md D051 for the full verification record.

A second, fully independent full from-scratch integration verification
run 2026-08-31 against `main` at `cd847ea` (Phase 40's CI hardening —
secret-scan fix, JWT test fix, new `apps/web` CI job, D052 — already
merged) re-confirmed the true post-merge total unchanged at **400 tests,
all passing**, and specifically set out to independently verify D052's
two headline self-reported fixes rather than take them on trust.
Following the same D028/D033/D036/D040/D043/D046/D048/D051 precedent:
fresh Python 3.13 venv, `pip install -e ".[dev]"`, `bash
scripts/secret_scan.sh` (clean, exit 0 — the exact newly-fixed CI step,
confirmed working independent of D052's own run), `ruff check .` (clean,
"All checks passed!"), `mypy apps` (clean, "Success: no issues found in
77 source files", unchanged from D051/D052 since Phase 40 touched no
`apps/**` source), a separately-named throwaway Postgres/Redis pair on
remapped host ports 15432/16380 under its own compose project name
`trading-os-verify40` (the user's own default-port 5432/6379 dev stack,
plus their uvicorn on 8000 and Next.js dev server on 3005, was confirmed
running via `docker ps`/`netstat` before and after, and left completely
untouched throughout), `alembic upgrade head` → `alembic downgrade base`
→ `alembic upgrade head` (all twelve migrations, still headed at Phase
39's `0012_users_login_lockout` since Phase 40 added none, cleanly in
both directions against a real Postgres 16 database — independently
reconfirming D052's finding that the round-trip was never actually
broken), and `pytest tests/ -q` with `JWT_SECRET_KEY` exported in-shell
exactly as CI does: 400 passed, 3 pre-existing warnings, zero failures,
zero errors, on the first attempt — no transient
`test_missing_jwt_secret_key_fails_closed` failure this time, which is
exactly what D052's `monkeypatch.delenv`-based fix predicts under the
CI-realistic condition (the key exported for the whole run) that broke
that test before the fix; `tests/test_config.py` was also re-run alone
under the same exported key as an extra check (12 passed). A separate
frontend check was run in an isolated copy of `apps/web` (source only,
`node_modules`/`.next` excluded) rather than in place, because the real
`apps/web/node_modules` is shared with the user's live Next.js dev server
and an in-place `npm ci` hit a live file lock on the first attempt
(`EPERM` unlinking a lightningcss binary) — confirming that server was
still genuinely running, and reason enough to isolate the whole frontend
check: `npm ci` (462 packages, 0 vulnerabilities), `npm run build`
(Next.js 16.3.3 Turbopack, TypeScript clean, all 16 routes generated),
`npm test` (**102 tests across 13 files, all passing**, exactly matching
D051/D052, unchanged). No real cross-phase integration bug was found.
See docs/DECISIONS.md D053 for the full verification record.

A third, fully independent full from-scratch integration verification run
2026-08-31 against `main` at `db36ba8` (Phase 41's readiness-probe split
and hardened Dockerfile, D054, already merged) re-confirmed the true
post-merge total unchanged at **404 tests, all passing**, and specifically
set out to independently rebuild D054's own Docker image and re-verify its
two headline claims — the real readiness probe and the non-root
multi-stage image — rather than take them on trust. Following the same
D028/D033/D036/D040/D043/D046/D048/D051/D053 precedent: fresh Python 3.13
venv, `pip install -e ".[dev]"`, `bash scripts/secret_scan.sh` (clean, exit
0), `ruff check .` (clean, "All checks passed!"), `mypy apps` (clean,
"Success: no issues found in 78 source files", unchanged from D054), a
separately-named throwaway Postgres/Redis pair on remapped host ports
55432/56379 under its own compose project name `verify41` (the user's own
default-port 5432/6379 dev stack, plus their uvicorn on 8000 and Next.js
dev server on 3005, was confirmed running via `docker ps`/`netstat` before
and after, and left completely untouched throughout), `alembic upgrade
head` (all twelve migrations, headed at `0012` since Phase 41 added none,
cleanly against a real Postgres 16 database), and `pytest tests/ -q` with
the throwaway env exported in-shell: 404 passed, the same 3 pre-existing
warnings, zero failures, zero errors. The Dockerfile itself was then
independently rebuilt from scratch — `docker build -f apps/api/Dockerfile
-t tradingos-verify41 .` — succeeding cleanly through both the `builder`
and `runtime` stages. `docker run --rm tradingos-verify41 whoami` →
`appuser`, confirming the non-root claim from a genuinely fresh build, not
D054's own image. `docker run --rm tradingos-verify41 alembic current`
against the isolated Postgres over a shared Docker network → `0012
(head)`, confirming the shipped `migrations/`/`alembic.ini` make the image
independently migration-capable. The image was then run as a real
container against the isolated Postgres: `GET /health` →
`{"status":"ok","trading_mode":"research","live_trading_enabled":false}`
(200), `GET /health/ready` →
`{"status":"ready","checks":{"database":{"status":"ok"}}}` (200); Postgres
made unreachable (bad host port, no restart of the API container) →
`GET /health` unchanged at 200 while `GET /health/ready` →
`{"status":"not_ready","checks":{"database":{"status":"error","reason":"connection_failed","error_type":"ConnectionRefusedError"}}}`
(503) — independently reconfirming D054's liveness-vs-readiness split
behaves exactly as claimed. No real cross-phase integration bug was found.
Cleanup was verified complete: the isolated compose stack, its named
volume, the built `tradingos-verify41` image, the throwaway `.env`, and the
verification venv were all removed, and the user's own
`trading-os-postgres-1`/`trading-os-redis-1` containers plus their
uvicorn (8000) and Next.js (3005) dev servers were confirmed still running
and untouched. See docs/DECISIONS.md D055 for the full verification
record.

- Phase 42: observability and lifecycle — per-request correlation IDs and
  an explicit ordered shutdown (2026-08-31, D056). **(1) Structured logs
  carried no correlation key.** A single trade submission emits its risk
  decision, portfolio decision, and fill on three separate JSON lines, and
  in a production stream interleaved across concurrent requests nothing
  grouped them back into one request — making "what happened to the order
  the user says was rejected at 14:03?" unanswerable from the logs alone.
  The new `RequestIDMiddleware` (`apps/api/app/core/request_id.py`,
  registered outermost in `main.py`) assigns every HTTP request a UUID4,
  binds it under the `request_id` key with
  `structlog.contextvars.bind_contextvars`, and returns it as an
  `X-Request-ID` response header on every response including 401s, 404s,
  and validation errors. **No existing `logger.info(...)` call site
  changed** — `merge_contextvars` was already the first processor in
  `configure_logging()`, so the key is merged into every event dict
  automatically. An inbound `X-Request-ID` is honored verbatim when it
  matches `^[A-Za-z0-9._-]{8,128}$` (so a load balancer or the frontend can
  propagate one ID across a hop); a malformed one is **replaced with a
  fresh UUID4 rather than rejected**, because the value is a correlation
  label — never an auth input — and a misconfigured proxy must not be able
  to fail a real trading request. The `request_id` key was explicitly
  asserted not to collide with the secret-redaction patterns in
  `core/logging.py`, and a credential logged alongside a bound ID is still
  redacted. It is pure-ASGI middleware, not `BaseHTTPMiddleware`, so bind
  and unbind stay in one context; cleanup unbinds rather than leaving the
  key set, since uvicorn reuses a task context across requests.
  **(2) The async SQLAlchemy engine was never explicitly disposed.** The
  lifespan stopped only the snapshot scheduler; the process-wide asyncpg
  pool was left for process exit — the OS cleaning up after us rather than
  the service shutting down. Shutdown is now explicit and ordered:
  background work is stopped first (`PortfolioSnapshotScheduler.stop()`
  cancels **and awaits** its task, traced and separately tested, so no
  cycle can still hold a pooled session), then Phase 41/D054's
  `get_engine()` is disposed — never the reverse, which would tear the pool
  out from under a running cycle — followed by a
  `trading_os_shutdown_complete` line. **No new external dependency**:
  structlog's contextvars (installed 26.1.0, API verified against the
  installed package), Starlette's own ASGI protocol, and stdlib `uuid`.
  **424 backend tests passing** over a measured 404 baseline (20 new),
  `ruff check .` and `mypy apps` clean (79 source files). Live-verified
  against an isolated docker compose stack on non-default ports (Postgres
  55432, Redis 56379, the real multi-stage API image on 58000; the user's
  own 5432/6379/8000/3005 dev stack untouched): two `GET /health` calls
  returned two different `x-request-id` headers
  (`683e9914-…` then `220c42b6-…`); a supplied
  `X-Request-ID: lb-edge-phase42-abc123` came back unchanged; a malformed
  `X-Request-ID: bad` was replaced with `23d09c56-…` and still returned
  200. Real container logs for one `GET /health/ready` showed two lines
  from two different modules (`request_id_header_rejected` from the
  middleware and `readiness_check_failed` from the health route) both
  carrying `"request_id": "6d1f0a5e-596a-4d49-8662-dcfc493705a8"`, matching
  that response's header. A real SIGTERM (`docker stop`) produced
  `Waiting for application shutdown.` → `trading_os_shutdown_complete` →
  `Application shutdown complete.` with a clean exit inside the grace
  period. Cleanup was verified complete: the compose stack was torn down
  with `-v`, the built image removed, and the throwaway compose file and
  verification venv deleted; no `.env` was created at any point. See
  docs/DECISIONS.md D056.

- Phase 43: the live-trading execution path — built, gated, and left INERT
  (2026-09-01, D058). **No real order was ever placed, no real broker was
  ever contacted, and `LIVE_TRADING_ENABLED` was never set to `true` — not
  in a default, not in a test fixture, not transiently, not in the smoke
  test. This phase delivers the CODE PATH only.** On a default checkout the
  system is exactly as inert as before it: a request against a live-kind
  broker is refused and no trade context is ever constructed.
  **(1) `LiveBrokerAdapter`** (`apps/api/app/execution/live_broker.py`) —
  the first concrete `BrokerAdapter` that can move real money, implementing
  the same Protocol as `PaperBrokerAdapter` so the OMS and every gate above
  it cannot tell the two apart. Uses the SDK's SYNCHRONOUS
  `longport.openapi.TradeContext` (verified by direct introspection of the
  installed package, not from docs: `TradeContext` has no `.create()`, and
  `submit_order` returns only an `order_id`, never fill information). It
  therefore reads the order back once via `order_detail()` and builds its
  `Fill` from the broker's own `executed_quantity`/`executed_price`; an
  accepted-but-unexecuted order raises `LiveOrderNotFilledError` and the
  route answers `502 LIVE_ORDER_UNCONFIRMED` naming the real order id
  rather than inventing a fill at the caller's estimated price. The
  account snapshot is fetched once per adapter instance (one adapter per
  request), so every gate in a trade reasons about the same book.
  `build_live_broker_adapter()` returns `None` unless `TRADING_MODE=live`
  AND `LIVE_TRADING_ENABLED=true` AND all three `LONGPORT_LIVE_*`
  credentials are set together — separate settings from the read-only
  `LONGPORT_*` quote credentials, so a quote key can never become a
  trading key.
  **(2) Separate, tighter live risk limits** — `LIVE_RISK_*` at 5% max
  position / 20% max exposure / 1% risk per trade, against paper's
  10%/50%/1%, built by the new pure `build_risk_limits(settings, live=)`
  in `apps/api/app/risk/limits.py`. The live branch reads only
  `live_risk_*` and the paper branch only `risk_*`, so paper behaviour is
  structurally unaffected rather than unaffected by convention.
  **(3) A mandatory per-trade `confirm: true`** on `POST
  /brokers/{id}/trades` for a live broker (400 `LIVE_CONFIRMATION_REQUIRED`
  otherwise), plus `trade:submit:live` required IN ADDITION to
  `trade:submit:paper`. A confirmation that lives only in a frontend
  dialog is not one the server can enforce — this is a payload field, and a
  structural safeguard rather than a UX nicety. The agent route stays
  paper-only.
  **(4) The shared gates verified, not assumed** — the emergency stop
  (D039), the duplicate-order check (D024), the Risk Engine, and the
  Portfolio Manager (D029) gate a live order via literally the same
  `_execute_trade()` a paper order runs, and each has its own explicit
  live test against a fake broker double.
  **(5) The per-broker paper/live toggle** — `POST /admin/brokers` and
  `PATCH /admin/brokers/{id}/mode` (both `admin:manage`). A broker row's
  `kind` IS the trading-mode switch: one trade endpoint, and the broker
  row — never a request-body field — decides paper vs live routing.
  Designating a broker live needs `confirm_live: true`; a broker that has
  recorded orders or holds a simulated book can no longer be flipped (409),
  because `orders`/`fills` are append-only with no per-order kind and
  flipping would make simulated and real history indistinguishable.
  No frontend was built (sibling phase 45 owns frontend; a live-trading UI
  deserves its own reviewed phase). Also not built: live limit orders,
  reconciliation of an unfilled live order, fractional shares (refused, not
  rounded), multi-currency live accounts.
  **487 tests passing** (424 pre-existing + 63 new: 23 live-broker unit, 6
  risk-limits unit, 17 live-trade integration, 17 broker-mode-toggle
  integration), ruff + mypy clean across 81 source files. Verified against
  a real Postgres on a remapped port and a real `uvicorn` in
  `TRADING_MODE=paper`; the paper path returned results identical to before
  the phase, and a CONFIRMED live trade still returned `400 NOT_CONFIGURED`
  because `LIVE_TRADING_ENABLED` is false.


A fourth, fully independent full from-scratch integration verification run
2026-08-31 against `main` (Phase 42's request-ID middleware and ordered
shutdown, D056, already merged) re-confirmed the true post-merge total
unchanged at **424 tests, all passing**, and specifically set out to
independently exercise both of D056's headline behaviors live rather than
take them on trust. Following the same
D028/D033/D036/D040/D043/D046/D048/D051/D053/D055 precedent: fresh Python
3.13 venv, `pip install -e ".[dev]"`, `bash scripts/secret_scan.sh` (failed
once — see the real bug below — then clean, exit 0), `ruff check .` (clean,
"All checks passed!"), `mypy apps` (clean, "Success: no issues found in 79
source files", unchanged from D056), a separately-named throwaway
Postgres/Redis pair on remapped host ports 55432/56379 under its own
compose project name `trading-os-verify` (the user's own default-port
5432/6379 dev stack, plus their uvicorn on 8000 and Next.js dev server on
3005, was confirmed running via `docker ps`/`netstat` before and after, and
left completely untouched throughout), `alembic upgrade head` (all twelve
migrations, headed at `0012` since Phase 42 added none, cleanly against a
real Postgres 16 database), and `pytest tests/ -q` with the throwaway env
exported in-shell: 424 passed, the same 3 pre-existing warnings, zero
failures, zero errors. **One real cross-phase bug was found and fixed**:
Phase 42's own `tests/core/test_request_id.py` added a deliberate
secret-look-alike fixture (`api_key="sk-live-should-not-appear"`) without
the `pragma: allowlist secret` marker the D052 secret-scan convention
requires, breaking `scripts/secret_scan.sh`; fixed by naming the literal and
appending the marker on the same matched line. Live request-ID behavior was
then verified against the real multi-stage API image run as a container on
port 58000 (a bare `uvicorn` process was tried first, but Windows has no way
to deliver a true, handler-invoking SIGTERM cross-process, so the container
path was used for a genuine signal test): two plain `GET /health` calls
returned two different `x-request-id` headers; a supplied
`X-Request-ID: my-test-id-12345` came back unchanged; a malformed
`X-Request-ID: a` was replaced with a fresh UUID4 and the request still
returned 200, exactly matching the documented behavior in `docs/API.md`; the
container's stdout showed the matching `request_id_header_rejected` warning
carrying the same `request_id` as the response header, with only
`supplied_length` logged, never the offending value. A real SIGTERM
(`docker stop`) on the running container produced, in order, `Shutting
down` → `Waiting for application shutdown.` → `trading_os_shutdown_complete`
→ `Application shutdown complete.` → `Finished server process [1]`, exit
code 0, no unhandled exception. Cleanup was verified complete: the isolated
compose stack, its volume, the built image, the throwaway `.env`, and the
verification venv were all removed, and the user's own
`trading-os-postgres-1`/`trading-os-redis-1` containers plus their uvicorn
(8000) and Next.js (3005) dev servers were confirmed still running and
untouched. See docs/DECISIONS.md D057 for the full verification record.

- Phase 45: dashboard redesign — a real layout shell and a design-token
  system (2026-09-01, D060). A **visual and layout pass only**: no
  endpoint, no fetch, and no condition governing whether something
  renders was changed. `/dashboard` was a single `max-w-2xl` column of
  eight identically-weighted bordered boxes with ad-hoc
  `border-neutral-*` classes repeated at every call site; it is now a
  12-column grid inside a persistent app shell (left rail carrying brand,
  navigation, live `SessionStatus` and sign-out; sticky header carrying
  the page title and the real `/health` status strip). Panel order stays
  semantically load-bearing: broker discovery ahead of everything
  broker-scoped because it is the source of the `broker_id` those panels
  need (D034), portfolio state and the equity curve leading in the wide
  column, the two order-entry panels sharing a row so neither reads as
  the default, and the backtest last and separate because it alone is not
  broker-scoped (D025). `app/globals.css` gained a semantic token set
  (four-step surface scale, two line weights, three text weights, accent,
  `--pos`/`--neg`/`--grid`) wired through Tailwind v4's `@theme inline`;
  dark is primary (`#060b16`, not `#000000`) and light is a separately
  contrast-checked palette, not an inversion — every text token clears
  4.5:1 against `--surface` in both. `/login` and `/admin` were given the
  same tokens and shell. The Risk Engine's amber and the Portfolio
  Manager's violet deliberately stay explicit palette classes rather than
  tokens, because those hues carry meaning fixed by D029.
  `components/ui/ChartFrame.tsx` adds gridlines, an area fill and axis
  labels to both equity charts but draws no data — `buildPoints` and
  `buildBacktestPoints` are untouched and still hand-rolled, and both
  charts keep their full data table as the accessible fallback. **No new
  dependency**: `package.json` is byte-identical, fonts come from
  `next/font/google`, and the `ui-ux-pro-max` skill's component-library
  suggestion was translated into local Tailwind components instead of
  installed. **102 frontend tests over 13 files pass**, the same 102 as
  before the redesign, with none deleted, skipped or weakened; the two
  that initially failed were fixed by rewording new chrome that had
  named the Portfolio Manager when the backend had not, not by relaxing
  the assertion. Live-verified in a real browser against an isolated
  stack (Postgres 5445, Redis 6445, uvicorn 8045, Next 3045) seeded with
  real users, brokers, grants, a real trade and five real snapshots:
  three distinct `NOT_CONFIGURED:` sentinels (market data, LLM provider,
  history provider), two real 403s (ungranted broker, and
  `admin:manage`), the backend's own login 401, and the full D029 case —
  `approved: true` with `status: "rejected"` and
  `portfolio_action: "reject"` rendering as a neutral Risk Engine block
  beside a violet Portfolio Manager panel. Both colour schemes and a
  1024px viewport were checked. Stack, venv and throwaway compose file
  were removed afterwards; no `.env` was ever created, and the user's own
  5432/6379/8000/3005 services were left untouched.

- Phase 47: broker paper/live mode UI — the frontend D058 deferred
  (2026-09-02, D062). **Frontend-only**: no backend route, schema or
  business rule was changed. D058 built and tested `POST /admin/brokers`
  and `PATCH /admin/brokers/{broker_id}/mode` with no UI at all, on the
  stated grounds that "a live-trading UI deserves its own reviewed
  phase"; until now the one row-level switch deciding which adapter a real
  order reaches could only be flipped by a hand-rolled HTTP call. New:
  `components/admin/BrokerModeAdmin.tsx` (a create-broker form and a
  change-mode form), two pass-through proxies under
  `app/api/admin/brokers/`, a `BrokersList` added to the existing
  `AdminListings.tsx`, and a "Brokers" section on `/admin`. The
  confirmation gate is the point of the phase: `confirm_live` is sent
  **only** when a dedicated, separately-labelled checkbox is ticked **and**
  the target kind is `live` — never pre-checked, omitted entirely (not
  sent as `false`) for a paper target, never injected by either proxy, and
  cleared whenever the target kind changes so a stale tick cannot survive
  a live → paper → live round trip. Submitting unticked is deliberately
  **not** blocked client-side: the backend's own 400
  `LIVE_KIND_CONFIRMATION_REQUIRED` is rendered verbatim, because a
  confirmation living only in the browser is not one the server can
  enforce and a disabled button would have made that real refusal
  unreachable through the product. The 409 for a broker with recorded
  orders is rendered with its full text including its remedy sentence.
  The broker list reads D034's `GET /brokers`, which is **grant-scoped to
  the caller, not platform-wide** — stated in the panel, since an admin
  without a grant will not see a broker there.
  **Two gaps found and deliberately NOT papered over.** (1)
  `AgentTradeResponse` exposes no per-analyst field whatsoever — D059's
  Technical/Fundamental/News analysts run server-side and feed the
  TraderAgent's prompt, but nothing about them is returned, so the
  response cannot even report whether any ran. `AgentTradeForm` now says
  so next to the real result rather than rendering three panels built from
  nothing; a real breakdown needs a backend response-shape change.
  (2) **There is no order/fill list endpoint** — `orders`/`fills` have
  been persisted since Phase 4, but no `GET /brokers/{id}/orders` or
  `/trades` exists in `apps/api/app/api/routes/` or `docs/API.md`, so the
  intended trade-history panel was not built. The platform keeps an
  append-only audit trail no user can read back; this is the largest
  remaining product gap and needs a backend phase.
  **116 frontend tests over 14 files pass**, up from the 102/13 baseline
  (re-confirmed by running the suite before any edit); 14 added, none
  deleted, skipped or weakened. `npm run build` exits 0 with both new
  handlers registered; `npm run lint` ends at the identical pre-existing
  3 problems (2 errors, 1 warning), all in untouched files. Live-verified
  in a real browser against an isolated stack (Postgres 55447, Redis
  63447, uvicorn 8047, Next 3047) seeded via `scripts/seed_e2e.py` plus
  one real paper trade, so a broker genuinely held a recorded order: seven
  scenarios covering both real refusals (400 and 409), a real successful
  paper → live flip on a clean broker, and the stale-tick guard. Both
  colour schemes checked. `LIVE_TRADING_ENABLED` was never touched, no
  live credential was ever set, `/health` reported
  `live_trading_enabled: false` throughout, and the only broker ever
  designated `live` was a throwaway row in a throwaway database. Stack,
  venv and containers removed afterwards; no `.env` created; the sibling
  phase's `tos-p46-pg` and the user's own containers left untouched.
- Phase 50: watchlists — the research half of the research-to-trade
  dashboard (2026-09-03, D067). Before this phase the trade side of that
  loop was complete (D058/D062) while the research side was a single
  symbol input box, so following a handful of names and looking at them
  together had no representation in the product.
  **(1) Schema** — migration `0015` adds `watchlists` (`id`, `user_id`
  FK ON DELETE CASCADE, `name`, `created_at`, plus
  `ix_watchlists_user_created`) and `watchlist_items` (`id`,
  `watchlist_id` FK ON DELETE CASCADE, `symbol`, `added_at`, plus
  `uq_watchlist_item_watchlist_symbol`). Two tables rather than a
  `symbols text[]` column so the one-symbol-per-list invariant is a
  database constraint two concurrent adds cannot both pass, not an
  application check.
  **(2) Five endpoints** — `POST /watchlists`, `GET /watchlists`,
  `DELETE /watchlists/{id}`, `POST /watchlists/{id}/items`,
  `DELETE /watchlists/{id}/items/{symbol}`, plus the payoff,
  `GET /watchlists/{id}/quotes`. These are the **first user-scoped**
  id-addressed resources in the codebase: authentication only, no
  `Permission`, and deliberately **no `BrokerGrant`** — a watchlist
  authorizes nothing and a user with no broker access must still be able
  to research. Ownership lives in one helper all four id-addressed routes
  call; "not there" is 404 and "not yours" is 403, the same order and
  codes `require_broker_access` uses. Watchlists are created explicitly —
  there is no lazily-created default, because a GET must not write a row.
  Symbols are trimmed and upper-cased on the way in and on the DELETE path
  segment, so `"aapl.us"` and `"AAPL.US"` are one entry and a duplicate is
  a real 409.
  **(3) One shared quote-resolution path** — the NOT_CONFIGURED /
  NO_DATA_AVAILABLE decision moved out of `routes/marketdata.py` into
  `apps/api/app/marketdata/resolution.py`, which returns a `ResolvedQuote`
  value instead of raising. `GET /market-data/{symbol}/quote` maps it to
  its unchanged 503/404, and the watchlist route maps it to a per-row
  sentinel — one function, two callers, so the two surfaces cannot drift
  apart about what "no price" means.
  **(4) No fabrication, structurally** — a `WatchlistQuote` carries either
  a price block copied off a real `MarketSnapshot` or a
  `DATA_UNAVAILABLE:`-prefixed string, never neither and never both, and
  the response is always exactly as long as the watchlist. An unpriceable
  symbol keeps its row; with no vendor wired at all the endpoint still
  returns 200, `market_data_configured: false`, and one honest sentinel
  per row (a 503 there would hide the user's own symbols to report
  something the payload already says). Resolution is sequential and a list
  caps at 200 symbols, so one page refresh cannot become a burst of
  rate-limited vendor calls.
  **(5) Frontend** — `apps/web/components/Watchlist.tsx` on the Phase 45 /
  D060 design system, five proxy route handlers under
  `apps/web/app/api/watchlists/`, and a new "Research" section on the
  dashboard holding `Watchlist` and `QuoteLookup` together. Per D034's
  ordering convention neither is broker-scoped, so both sit outside the
  broker-scoped block — `QuoteLookup` moved out of the "Position &
  performance" grid, where it had only ever lived for width.
  Verified on an isolated throwaway stack (Postgres `55432`, Redis
  `56379`, API `18050`/`18051` — never the dev stack's ports): migration
  `downgrade base` → `upgrade head` round-trip clean; backend **615 → 635**
  passing (20 new), frontend **140 → 152** passing (12 new, 17/17 files,
  nothing weakened or deleted); `ruff check .`, `mypy apps` (96 files),
  `npm run build` and `bash scripts/secret_scan.sh` all clean. Live-checked
  over real HTTP: create / add / duplicate-409 / list / quotes /
  cross-user-403 / remove-204 / 404s / delete-204 / 401, with quotes
  exercised both on the committed default (no vendor credentials — every
  row `DATA_UNAVAILABLE: NOT_CONFIGURED: …`, not one invented price) and
  against a stub vendor (two real prices and one
  `DATA_UNAVAILABLE: NO_DATA_AVAILABLE: …` row in the same response).
  `LIVE_TRADING_ENABLED`, the Risk Engine, the Portfolio Manager and the
  emergency stop were untouched; the branch diff contains nothing under
  `apps/api/app/execution/` and does not touch
  `apps/api/app/api/routes/trades.py`, both of which the concurrent
  Phase 48/49 worktrees were editing.
- Phase 46: self-service password reset, with email as an optional vendor
  (2026-09-02, D063). Closes the gap D049 opened: before this phase the
  only recovery path for a forgotten password was a direct SQL update, and
  a user who mistyped five times was locked out for fifteen minutes with
  "no unlock endpoint and no email flow".
  **(1) `password_reset_tokens`** (migration `0013`, the first new table
  since `0012`) — `id`, `user_id` (FK, ON DELETE CASCADE), `token_hash`
  (UNIQUE), `created_at`, `expires_at`, `used_at`. Only a **SHA-256 hex
  digest** of the token is stored, never the token, so a database dump is
  not a set of working reset links. SHA-256 rather than bcrypt is
  deliberate and argued in D063: the input is 32 bytes from
  `secrets.token_urlsafe`, so bcrypt's cost factor buys nothing against a
  256-bit space while making redemption unindexable.
  **(2) `POST /auth/password-reset/request`** (public) — **always 200 with
  one byte-identical body**, across all five branches (address unknown,
  account inactive, per-account throttle tripped, delivery NOT_CONFIGURED,
  send failed). No row is written for an unknown or inactive address, so
  storage cannot be an oracle either. The acknowledgement says a link "has
  been *issued*", never "sent": that is the only wording true in all five
  cases, since three of them send nothing.
  **(3) `POST /auth/password-reset/confirm`** (public) — one 400 sentinel,
  `INVALID_OR_EXPIRED_TOKEN`, for unknown/expired/used/deactivated alike;
  never a stack trace and never a hint at which check failed. On success it
  rehashes with the same bcrypt scheme Phase 7 uses, **clears any active
  D049 login lockout**, and marks every other outstanding unused token for
  that user consumed — all in one transaction. Issuing a second token does
  NOT kill the first (a user who clicked twice cannot tell which email they
  are looking at); invalidation happens at redemption, when the account is
  provably back under someone's control.
  **(4) Email as a NOT_CONFIGURED vendor** — `EMAIL_PROVIDER_BASE_URL` /
  `_API_KEY` / `_FROM_ADDRESS`, all-or-nothing, exactly like `LONGPORT_*`
  (D008/D015) and `LLM_PROVIDER_*` (D018). `notifications/provider.py` is
  the Protocol; `notifications/transactional_email.py` is one
  vendor-agnostic adapter for a generic `POST {base}/emails` bearer-auth
  JSON API (Resend's shape). **Unset is a working state, not a broken
  one**: tokens are still issued, and an admin reads the real link out of
  the new **`POST /admin/users/{id}/password-reset`** (`admin:manage`),
  which answers `"delivery": "NOT_CONFIGURED_returned_directly"` with the
  live `reset_link`. Configured, the same endpoint sends the mail and
  returns `"delivery": "SENT"` with `reset_link: null`. A refused send is a
  real **502** that first invalidates the token it just issued — there is
  no third `delivery` value meaning "we tried and failed", and nothing
  anywhere claims delivery (only that the vendor *accepted* the message).
  **(5) Two throttle layers, honestly scoped** — per-account (default 5/h,
  counted from the table itself so it holds across workers, and never
  visible in the response) and per-IP (default 20/h, real 429, but an
  in-process counter that resets on restart and ignores `X-Forwarded-For`;
  its own docstring says a real rate limit belongs at the reverse proxy).
  **(6) Frontend** — `/forgot-password` and `/reset-password?token=...`,
  plus a "Forgot password?" link always present on `/login` and a
  full-width "Issue password reset link" panel on `/admin`. Both new pages
  use `components/auth/AuthCard.tsx`, which lifts `/login`'s own Phase 45
  layout (D060) verbatim rather than `AppShell` — that shell polls
  `GET /auth/session` and would bounce a signed-out user to `/login` from
  the one page that exists to break that loop. The pages render the
  backend's words verbatim and are tested for it: no "check your inbox",
  and no invented reason behind `INVALID_OR_EXPIRED_TOKEN`.
  Deliberately NOT built: an admin endpoint that sets a password directly
  (an admin who could type it would know it), a session issued on
  successful reset, password rules beyond the existing 8-character floor,
  email verification, self-service registration, HTML email, delivery
  receipts, retries, and a per-row reset button on the admin users listing.
  **615 backend tests passing** (544 baseline re-measured on this branch +
  71 new: 11 token-arithmetic, 8 throttle, 16 email-adapter, 5 config, 22
  public-flow integration, 9 admin-endpoint integration), ruff + mypy
  clean across 93 source files, secret scan clean. **126 frontend tests
  over 15 files** (102/13 baseline + 24 new, none deleted or weakened) and
  `npm run build` clean with all four new routes in the manifest. Verified
  against a real Postgres 16 on remapped port 55446, `alembic upgrade head`
  through `0013` plus the `downgrade base` round-trip. Also **live-verified
  over real HTTP** across three uvicorn instances (8046 NOT_CONFIGURED,
  8047 against a local stub vendor, 8048 against a dead port): registered
  and unknown addresses returned byte-identical 200s and the unknown flood
  wrote zero rows; the admin endpoint gave a real link to an admin and a
  real 403 to a non-admin; the link redeemed once, the old password stopped
  working, and a replay and a fabricated token returned identical
  `INVALID_OR_EXPIRED_TOKEN` bodies; redeeming a newer link killed an older
  one; the per-IP 429 fired for a *registered* address too; the stub vendor
  received exactly the documented `POST /emails` contract and the emailed
  link redeemed; an unreachable vendor produced a real 502 on the admin
  path and an unchanged generic 200 on the public one. Postgres showed only
  64-character hashes, and grepping every server's stdout for the raw
  tokens returned zero matches. The container and its volume were removed
  afterwards and no `.env` was ever created. **No real email vendor was
  contacted and no email credential exists in this branch.**
  `LIVE_TRADING_ENABLED` was never touched and nothing under
  `apps/api/app/execution/` was modified.

## Known Issues

None open.

Open, known-and-scoped limitations (not defects): the snapshot scheduler's
market-hours gate is weekend-only — intraday after-hours and exchange
holidays are not checked (Phase 35/D042) — and each API worker process
still runs its own independent scheduler loop, so >1 worker would multiply
snapshot rows (D030).

Two backend gaps identified by Phase 47 (D062) while building the frontend
for them, both needing a backend phase — neither was worked around in the
UI, and neither is a defect in existing code:

1. **No order/fill list endpoint.** `orders` and `fills` have been
   persisted since Phase 4, but nothing exposes them: there is no
   `GET /brokers/{broker_id}/orders` or `/trades` in
   `apps/api/app/api/routes/` and none in `docs/API.md`. The platform
   keeps an append-only audit trail that no user can read back through the
   product, and no trade-history UI can exist until a listing endpoint
   (plus its response schema and pagination, following D027/D031's
   `{items, limit, offset}` convention) does.
2. **`AgentTradeResponse` returns no per-analyst output.** D059's
   Technical, Fundamental and News analysts run server-side and feed the
   TraderAgent's prompt, but the response carries only the final decision
   (`side`, `quantity`, `rationale`) plus the risk/portfolio verdicts — no
   analyst field at all, so a client cannot even tell which analysts
   contributed. Surfacing their real reasoning requires widening the
   response shape; note each analyst is failure-isolated and may
   legitimately have produced nothing, so any such field must be
   nullable per analyst rather than implying all three always ran.
