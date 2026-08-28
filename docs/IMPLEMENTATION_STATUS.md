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
  such endpoint exists), session refresh. `npm run build` succeeds with
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

## In Progress

Nothing currently mid-implementation.

## Blocked

Nothing currently blocked.

## Planned

Phase 19+ (order not finalized): the rest of the parallel analyst layer
(fundamental/news/sentiment — each blocked on a real, wired data source),
research debate, and Portfolio Manager per the governing spec's "Target"
architecture. If a second analyst is ever added, revisit whether
parallel-execution infrastructure across analysts is now warranted
(deliberately not built in Phase 16 — one analyst has nothing to
parallelize against). Frontend v2 candidates remaining after Phase 20/D023
(`/admin/*` UI and agent-trades UI are now built): broker discovery (no
such endpoint exists yet), session refresh/expiry UX, Playwright if a
protectable flow emerges, and a users/roles/grants listing UI (blocked on
D013's deliberate no-listing-endpoints scope cut). Re-verify D018/D019's LLM-completion path
once OmniRoute (or another Anthropic-Messages-API-compatible endpoint) is
reachable — D021 confirmed the Longbridge/history side works with real
data, but no real LLM completion has been exercised yet, only fakes.

## Technical Debt

None yet — codebase is small enough that debt hasn't accumulated.

## Optimization Tasks

Deferred until an agent/LLM layer exists — see [AI_OPTIMIZATION.md](AI_OPTIMIZATION.md).
Building a token router/cache with nothing to route would be premature
optimization.

## Tests

145 tests, all passing: fail-closed live-mode gate (4, incl. missing JWT
secret), log redaction (1), health endpoint (1), risk engine (14), paper
broker (5), OMS (3), execution context (5), order/fill persistence (3,
DB-backed), market data router + snapshot model (9), Longbridge provider
(8, against a fake client), trades HTTP endpoint (18, DB-backed, all
require auth+permission+broker grant — 14 pre-D017 + 4 covering the
omitted-price/live-quote path), auth security unit tests (8),
authorization unit tests (3), login route (4, DB-backed), admin routes
(16, DB-backed — 9 create/grant + 7 update/deactivate), broker-state
persistence (3, DB-backed), TraderAgent unit tests (8, against a fake
`LLMProvider`), agent-trades HTTP endpoint (5, DB-backed), TechnicalAnalyst
unit tests (7, against a fake `LLMProvider`), technical-analyst wiring on
agent-trades (3, DB-backed), indicator unit tests (9, pure math),
Longbridge history provider unit tests (5, against a fake candlestick
client), history-provider wiring on agent-trades (4, DB-backed).

## Known Issues

None open.
