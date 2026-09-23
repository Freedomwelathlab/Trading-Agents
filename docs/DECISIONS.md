# Decision Log

Only architecturally significant decisions. Trivial choices don't belong
here.

---

**D001 — Build fresh rather than fork TradingAgents**
Date: 2026-08-22
Decision: New modular-monolith repo (`trading-os/`), vendoring select
reusable TradingAgents packages later (Apache-2.0, see root `NOTICE`)
rather than forking and extending.
Reason: TradingAgents is a research-only pipeline (no OMS/risk/broker code);
its `risk_mgmt/` package is LLM debate personas with zero deterministic
math — the opposite of what this project's risk engine must be. Extending
it would mean fighting its shape more than reusing it.
Alternatives: fork-and-extend TradingAgents directly.
Consequences: more upfront scaffolding (this Phase 1), but a data model and
risk boundary that match the spec from the start.
Status: Approved by user 2026-08-22 ("The architecture is approved.").

---

**D002 — Typed, fail-closed execution-mode gate**
Date: 2026-08-22
Decision: `TradingMode` enum (`research`/`paper`/`live`) in `Settings`,
which raises at construction if `live` is set without
`LIVE_TRADING_ENABLED=true`.
Reason: Spec §46 requires fail-closed behavior; encoding the check in the
settings object itself means it's impossible to boot the app into an
inconsistent live-but-disabled state, rather than relying on every call site
to remember to check.
Alternatives: runtime check scattered at each order-submission call site.
Consequences: any future LIVE-mode confirmation flow (spec §56) must build
on top of this flag, not bypass it.
Status: Implemented, tested (`tests/test_config.py`).

---

**D003 — Documentation-as-memory bootstrap**
Date: 2026-08-23
Decision: Adopted the `docs/` hierarchy + root `CLAUDE.md` structure so
future sessions read documentation instead of reconstructing context from
chat history.
Reason: User-requested project optimization; matches the project's own
scale better than inventing token-router/agent-policy infrastructure before
any agent exists.
Alternatives: full token-routing/caching infra now, ahead of any LLM call
existing in the codebase — rejected as premature (nothing to route yet).
Consequences: `docs/AI_OPTIMIZATION.md`, `TOKEN_POLICY.md`,
`MODEL_ROUTING.md`, `AGENT_POLICY.md` are written as policy-to-build-against,
not descriptions of running systems — revisit their content once the first
agent ships.
Status: Implemented.

---

**D004 — Risk engine as a pure function, block-not-resize**
Date: 2026-08-23
Decision: `evaluate_trade()` is a pure function (no I/O, no LLM, no DB) that
returns a typed `RiskDecision` for every input, never raises for a business
rule violation, and never silently resizes a rejected order — it surfaces
`max_quantity_allowed` as information only.
Reason: purity is what makes the MVP acceptance test possible (blocks a bad
trade with every LLM stubbed to raise — see `tests/risk/test_engine.py`'s
last test). Not resizing preserves the audit trail: what got approved is
always exactly what was proposed, never a value the engine invented.
Alternatives: engine clips an oversized proposal down to the max allowed
quantity and approves the clipped version. Rejected — that would make the
engine an implicit second proposer, blurring the "LLM proposes, engine
decides" boundary spec Sec3 requires.
Consequences: whatever calls this later (Phase 3 OMS) must handle a
rejection by generating a genuinely new proposal, not by assuming the engine
already picked a safe fallback size.
Status: Implemented, tested (`tests/risk/test_engine.py`, 14 tests).

---

**D005 — Paper broker adapter is in-memory, not persisted**
Date: 2026-08-23
Decision: `PaperBrokerAdapter` holds cash/positions/fills in process memory
only; no `orders`/`fills` DB tables were added this phase.
Reason: designing a real Order/Fill persistence schema (append-only,
audit-ready, matching spec §17's decision-chain requirement) is a bigger
decision than Phase 3's scope of "prove the OMS→risk→broker path works
end-to-end." Building persistence now, ahead of knowing what the OMS's
eventual async/DB-session shape looks like, risks a schema that has to be
redone.
Alternatives: add `orders` table + wire the OMS to a DB session this phase.
Consequences: `PaperBrokerAdapter` state doesn't survive a process restart,
and there's no audit trail yet — acceptable for proving the risk-gate
boundary, not acceptable to keep once real (even paper) trading matters
across sessions. Must be revisited before Phase 4 broker work goes further.
Status: Implemented (deliberately partial), tested
(`tests/execution/test_paper_broker.py`, `tests/oms/test_service.py`).

---

**D006 — Order/Fill persistence: append-only, denormalized symbol, wraps not modifies submit_trade**
Date: 2026-08-23
Decision: Added `orders`/`fills` tables (migration `0002`) and
`submit_trade_and_record()` (`apps/api/app/oms/persistence.py`), which calls
the existing pure `submit_trade()` and then writes exactly one `Order` row
per call (plus a `Fill` row if filled) — never an update to a prior row.
Reason: closes D005's gap without touching `submit_trade()` itself, so the
DB-free unit tests and the "risk engine works with everything else down"
property both stay intact; persistence is additive, not load-bearing for
correctness.
Alternatives: build persistence into `submit_trade()` directly. Rejected —
would force every unit test to spin up a DB, and would make the OMS's core
logic depend on a session it doesn't need to decide anything.
Consequences: two call sites now exist (`submit_trade` for tests/pure logic,
`submit_trade_and_record` for anything that needs an audit trail) — any
future HTTP endpoint must use the `_and_record` variant, not the pure one,
or trades won't be recorded.
Status: Implemented, tested (`tests/db/test_order_persistence.py`, 3 tests
against a real Postgres/TimescaleDB instance).

---

**D007 — Two real bugs found and fixed during Phase 4 verification**
Date: 2026-08-23
Decision: (a) All SQLAlchemy `Enum()` columns now go through a
`_pg_enum()` helper (`apps/api/app/db/models.py`) that passes
`values_callable` so the Python enum's `.value` is sent to Postgres, not
its member `.name`. (b) `pyproject.toml`'s pytest config pins
`asyncio_default_fixture_loop_scope`/`asyncio_default_test_loop_scope` to
`"session"`.
Reason: (a) `Enum(BrokerKind)` without `values_callable` sent the literal
string `"PAPER"` instead of `"paper"`, which the DB's lowercase enum type
rejected — this existed since Phase 1's `Broker` model but was invisible
until Phase 4 actually inserted a row via the ORM for the first time. (b)
`apps/api/app/db/base.py`'s engine is created once at import time; asyncpg
connections are bound to the event loop that created them, and
pytest-asyncio's default per-test loop handed a second test a connection
tied to an already-closed loop from the first, producing a nondeterministic
teardown crash.
Alternatives: (a) construct every `Broker`/`Order` row with the enum's
name in uppercase to match — rejected, that's fixing the symptom at every
call site instead of the one place the mismatch originates. (b) restructure
`db/base.py` to create the engine lazily per-request — bigger change than
this phase needed; revisit if the app ever runs multiple event loops for a
real reason (it doesn't today).
Consequences: both fixes are structural, not per-call-site patches — a new
enum column or a new async test automatically avoids both bugs.
Status: Implemented, verified (`tests/db/test_order_persistence.py` failed
before both fixes, passes after).

---

**D008 — Market data service ships as a routing skeleton, no vendor wired**
Date: 2026-08-23
Decision: `apps/api/app/marketdata/` provides `MarketDataProvider` (a
Protocol), `MarketDataRouter` (ordered fallback across providers, typed
`DataUnavailableError`/`VendorError` distinguished from any other
exception), and `MarketSnapshot` (the normalized quote type). No concrete
vendor adapter (Longbridge, IBKR, a public REST API, anything) is
implemented or wired to a default router this phase.
Reason: no vendor credentials are configured in this project, and inventing
a "default" free-data integration without the user choosing one would mean
either (a) silently depending on an unauthenticated public endpoint the
user never agreed to, or (b) building against fixture data that risks being
mistaken for real market data later — both cut against spec Sec57's
no-fabrication rule in spirit. The router's no-fabrication guarantee (raise
`NoDataAvailableError` with a `NO_DATA_AVAILABLE:`-prefixed message rather
than ever inventing a price) is real and tested regardless of which vendor
eventually plugs in.
Alternatives: wire a specific free vendor now (e.g. a public quote API) as
a default. Rejected pending the user's choice - this is an open decision,
not a technical blocker.
Consequences: the risk engine's `market_data_as_of` freshness check still
has no real data source feeding it in production - every test that builds
a `TradeProposal` still supplies its own timestamp by hand. That's the
concrete gap this decision leaves open.
Status: Implemented (routing/normalization contract only), tested
(`tests/marketdata/`, 9 tests, all against in-process fakes - never a real
network call).

---

**D009 — Trades endpoint uses an in-process PaperBrokerRegistry; tests use httpx.AsyncClient, not TestClient**
Date: 2026-08-23
Decision: `POST /brokers/{broker_id}/trades` (`apps/api/app/api/routes/trades.py`)
looks up a `PaperBrokerAdapter` from a new `PaperBrokerRegistry`
(`apps/api/app/execution/registry.py`) keyed by `broker_id`, held on
`app.state` and created once at startup. Separately, `tests/api/test_trades.py`
uses `httpx.AsyncClient` + `ASGITransport` instead of FastAPI's `TestClient`.
Reason: (a) the registry is the smallest thing that lets the HTTP layer
call a real (paper) broker without inventing per-broker persistence this
phase didn't scope - see the Consequences below for what it doesn't solve.
(b) `TestClient` runs the ASGI app in a separate thread with its own event
loop; combined with the module-level, loop-bound DB engine (D007), any test
that both drives `TestClient` and touches the DB directly hits the same
cross-event-loop crash D007 fixed for `tests/db/`. `AsyncClient` with
`ASGITransport` runs entirely in-process on the test's own event loop, so
there's only ever one loop involved.
Alternatives: (a) require the caller to pass starting cash/positions on
every request instead of a server-side registry - rejected, that's not
what a broker account is. (b) keep using `TestClient` and dodge the crash
by never touching the DB directly in the same test - rejected, that would
make the tests unable to set up a `Broker` row or verify persisted `Order`
rows, i.e. unable to test the thing that matters.
Consequences: broker state (cash/positions, via `PaperBrokerRegistry`) is
still in-memory and per-process (same limitation as D005, now reachable
over HTTP) - a restart loses it even though the `Order`/`Fill` audit trail
in Postgres survives. Any future async endpoint test must use
`AsyncClient`/`ASGITransport`, not `TestClient`, or it will hit this same
crash.
Status: Implemented, tested (`tests/api/test_trades.py`, 6 integration
tests against a real Postgres instance).

---

**D010 — JWT auth, no registration endpoint, fail-closed secret**
Date: 2026-08-23
Decision: Bearer-token auth (`apps/api/app/auth/`) using bcrypt password
hashes and a JWT access token (`PyJWT`, HS256). `Settings.jwt_secret_key`
has no default - the app refuses to start without one configured, same
posture as `live_trading_enabled`. No public registration endpoint exists;
users are created by inserting a row directly (tests do this; production
would need an admin path, not built yet). `POST /brokers/{broker_id}/trades`
now requires a valid, active user via `get_current_user`, and every
persisted `Order` records `submitted_by_user_id` (migration `0003`).
Reason: a financial-transaction endpoint with no auth (D009's state) was
explicitly called out as unacceptable beyond local development. JWT
over sessions/cookies matches a stateless API with no browser session
concept yet. No default secret follows the same fail-closed reasoning as
the live-trading gate: an app that silently boots with a built-in secret
is worse than one that refuses to start. No registration endpoint is
scope discipline, not an oversight - open account creation on a trading
platform is a decision that deserves its own review, not a side effect of
wiring auth.
Alternatives: session cookies (rejected - no browser/CSRF surface exists
to justify it yet); OAuth2/SSO via a third party (rejected - premature
for a single-tenant dev-stage app); a hardcoded/default JWT secret for
convenience (rejected - directly the kind of insecure default this
project has avoided everywhere else, e.g. D002).
Consequences: nobody can self-register - every user must be created
directly in the database until an admin/registration flow exists. No
role-based authorization yet either (`Role`/`role_id` exist on `User` but
nothing checks them) - authentication (who you are) is done,
authorization (what you're allowed to do) is not, and the two shouldn't be
conflated when reading `docs/IMPLEMENTATION_STATUS.md`.
Status: Implemented, tested (`tests/auth/test_security.py` 8 unit tests,
`tests/api/test_auth.py` 4 integration tests, `tests/api/test_trades.py`
updated to require auth on every request, +3 new auth-specific cases).
Verified live: a real running server correctly returned 401 with no
token, then 200 with one obtained from a real `/auth/login` call, with
the resulting order's `submitted_by_user_id` matching the authenticated
user - confirmed directly via SQL, not just the HTTP response.

---

**D012 — Per-broker access grants: a join table, checked after the global permission**
Date: 2026-08-23
Decision: `broker_grants` (migration `0005`) is a `(user_id, broker_id)`
join table with a unique constraint. New `require_broker_access(Permission)`
(`apps/api/app/api/dependencies.py`) composes on top of `require_permission`:
identity (401) -> global permission (403) -> broker existence (404) ->
specific grant (403). It returns an `AuthorizedBroker(user, broker)` so
the route no longer does its own broker lookup - the dependency is now the
single place that decides "can this request touch this broker at all."
Reason: D011 explicitly scoped role-based authorization to a *global*
permission and called out per-broker access as a separate, real gap - this
closes it. A join table (not a column on `Broker` or `User`) is the only
structure that supports many-to-many cleanly, e.g. two traders sharing one
broker, or one trader with grants on several.
Alternatives: (a) a single `owner_user_id` column on `Broker` - rejected,
forces one owner per broker, can't express shared access without adding
the join table anyway. (b) checking the grant before the global permission
- rejected, permission is a property of the user regardless of resource
and should fail first/cheaper; also keeps the 403 error messages
distinguishable ("missing permission" vs "no access to this broker").
(c) returning 404 instead of 403 for "no grant" (to avoid confirming a
broker exists to an unauthorized caller) - rejected for this phase, since
broker IDs are already unguessable UUIDs and a clear 403 is more useful
for legitimate debugging than the marginal enumeration resistance would be
worth; revisit if this app ever has attacker-guessable broker IDs.
Consequences: creating a grant is still direct DB insert - no
grant-management endpoint exists (same pattern as D010's users/D011's
roles). Every existing "happy path" test needed a `broker_grant()` fixture
added or it would now fail with 403 - a reminder that this is a real,
enforced restriction, not documentation-only.
Status: Implemented, tested (`tests/api/test_trades.py`, 2 new tests:
permission-but-no-grant gets 403, a grant for one broker doesn't authorize
a different one). Verified live against a running server: the same
trader, holding the same permission, got 200 on a broker they were
explicitly granted and 403 on one they weren't - confirmed by curl against
two real broker rows, not simulated.

---

**D013 — Minimal admin API: one coarse permission, create-only for users/roles, create+revoke for grants**
Date: 2026-08-23
Decision: `POST /admin/users`, `POST /admin/roles`, `POST /admin/broker-grants`,
`DELETE /admin/broker-grants/{id}` (`apps/api/app/api/routes/admin.py`),
all gated by one new coarse permission, `Permission.ADMIN` ("admin:manage")
- not per-resource admin permissions (`admin:create_user`,
`admin:create_role`, etc.). No update/deactivate for users, no update for
roles, no listing endpoints anywhere.
Reason: this closes the "raw SQL only" gap D010/D011/D012 each left open,
without building a general admin panel the user didn't ask for. Grants
got both create *and* revoke because access control is the lever an
operator will realistically need to flip routinely (onboarding/offboarding
a trader from a specific broker); users and roles are comparatively
rarely-changing setup, so create-only is enough to remove the SQL
dependency without over-building. One coarse permission (not several
granular ones) matches "minimal" - a real per-action admin permission
model is a straightforward follow-up if ever needed, not a foundational
decision to get right on the first pass.
Alternatives: (a) per-action admin permissions - rejected as premature
granularity with only one admin operator persona so far. (b) update/delete
for users and roles too - rejected as scope creep past "minimal"; a user
can already be effectively disabled by revoking every broker grant and
role permission, even without a dedicated deactivate endpoint. (c) a
public self-registration endpoint - never considered; this is explicitly
an *admin* path, not open account creation (would contradict D010's
original reasoning).
Consequences: **the very first admin user and role still require one
direct DB insert** - there is no user holding `admin:manage` to call
these routes with the first time. Documented as the one unavoidable
bootstrap step; everything after it can go through the API. If a role or
user needs to change after creation (e.g. revoke `admin:manage` from a
compromised account), that still requires SQL - a real, acknowledged gap.
Status: Implemented, tested (`tests/api/test_admin.py`, 9 integration
tests against real Postgres, including one that grants and then revokes
broker access via the API and confirms trade authorization actually
flips both ways - not just that the grant/revoke calls return the right
status code). Verified live against a running server: bootstrapped one
admin via SQL, then created a role, a user, and a broker grant entirely
through the API, and confirmed the new user could trade only after the
grant existed.

---

**D011 — Authorization: permission-list-on-Role, not a fixed enum or per-broker grants**
Date: 2026-08-23
Decision: `roles.permissions` (migration `0004`) is a flat Postgres
`text[]` of permission strings (`apps/api/app/auth/permissions.py`'s
`Permission` enum defines the known values). A new
`require_permission(Permission)` dependency
(`apps/api/app/auth/dependencies.py`) checks the current user's role for a
specific permission and returns 403 if absent; `POST /brokers/{broker_id}/trades`
now depends on `require_permission(Permission.SUBMIT_PAPER_TRADE)` instead
of the bare `get_current_user` from D010.
Reason: the existing `Role`/`User.role_id` model (from Phase 1) was
explicitly designed for this - its docstring already said "gate the LIVE
execution path... by role." A permission list per role means granting an
existing permission to a role is a data change (insert/update a `Role`
row), not a code change; only adding a *new* permission requires code.
Alternatives considered: (a) a fixed set of hardcoded role names checked
by string comparison (`if role.name == "trader"`) - rejected, conflates a
role's *label* with what it's *allowed to do*, and can't grant one
permission to two differently-named roles without duplicating logic.
(b) Per-broker access grants (a `user_id`/`broker_id` join table) -
rejected for this phase: the user asked for *role-based* authorization,
which naturally maps to the existing `Role` table's global permissions,
not a new per-resource ACL; per-broker grants remain a real, separate gap
(anyone with `trade:submit:paper` can trade on any `broker_id` they know)
and should be its own decision if wanted.
Consequences: `SUBMIT_LIVE_TRADE` is defined but deliberately enforced
nowhere - there's no live execution path for it to gate yet (see the
permission's own docstring warning against wiring it prematurely).
`get_current_user` now eager-loads `User.role` via `selectinload` -
lazy-loading it later in an async context without that would raise, not
silently work.
Status: Implemented, tested (`tests/auth/test_authorization.py` 3 unit
tests against the checker logic directly, `tests/api/test_trades.py` +2
integration tests for the 403 cases). Verified live against a running
server: a user with no role got 403 with the missing-permission detail
message; a user with a role granting `trade:submit:paper` got 200 filled
- both created via direct SQL insert (no registration endpoint, per D010),
confirmed via curl, not just the test suite.

---

**D014 — Persisted paper-broker state: new tables, row-locked load, PaperBrokerRegistry removed entirely**
Date: 2026-08-23
Decision: `broker_accounts` (cash, one row per broker) and
`broker_positions` (nonzero quantities only, one row per broker+symbol) -
migration `0006`. `apps/api/app/execution/persistence.py`'s
`load_paper_broker()`/`save_paper_broker()` reconstruct a `PaperBrokerAdapter`
from these tables at the start of a request and write it back at the end,
mirroring the pure/persisted split D006 already established for
`submit_trade`/`submit_trade_and_record`. `load_paper_broker()` takes the
`broker_accounts` row with `SELECT ... FOR UPDATE`, and
`submit_trade_and_record()` no longer commits internally (it now only
`flush()`es) so that lock is held for the entire trade - risk evaluation,
order/fill persistence, and the account/position save - and released only
by one commit at the very end, in `trades.py`. The old in-process
`PaperBrokerRegistry` (D009) is deleted, not deprecated - it's fully
superseded by DB-backed state.
Reason: D005/D009 explicitly flagged that a restart silently resets every
paper account despite the `Order`/`Fill` audit trail surviving - this
closes that gap for real, not just for one process's lifetime. The
`FOR UPDATE` lock exists because loading cash, mutating it, then writing
it back without holding a lock across that whole span is exactly the kind
of check-then-act race that lets two concurrent trades on the same broker
both read the same starting cash and both succeed when only one should -
a double-spend bug, which this project's fail-closed posture (already
applied to the risk engine and to auth) should extend to as well.
Alternatives: (a) an application-level in-memory lock/mutex per
`broker_id` - rejected, doesn't survive a restart or work across multiple
worker processes, the exact class of problem this phase exists to solve.
(b) optimistic concurrency (a version column, retry on conflict) -
rejected as more complexity than a single-process paper-trading MVP
needs; `SELECT ... FOR UPDATE` is the simpler correct tool given trades
are already short, single-row-locking transactions. (c) keeping
`submit_trade_and_record()`'s internal commit and accepting the small
window where the lock releases early - rejected, that reopens exactly
the race the lock exists to close.
Consequences: `submit_trade_and_record()`'s commit contract changed - any
future direct caller must commit the session itself (or accept
rollback-on-close, which the direct-test callers in
`tests/db/test_order_persistence.py` already do implicitly, since they
verify state within the same still-open transaction). Every test fixture
that creates a `Broker` row now also needs to clean up `broker_accounts`/
`broker_positions` before deleting the `Broker` itself, or hit the new FK
constraint - the same append-only-FK cleanup-ordering lesson D011's tests
already had to learn.
Status: Implemented, tested (`tests/execution/test_persistence.py`, 3
new tests: default-seed on first load, a load→mutate→save→reload
round-trip, and confirming a position closed back to zero is deleted, not
kept as a zero row). Verified live end to end in the way that actually
matters for this decision: submitted a trade, killed the running server
process, started a fresh one, and submitted a second trade on the same
broker - the resulting cash balance (99,000 − 250 = 98,750) and both
positions (AAPL from before the restart, MSFT from after) were only
explainable if state genuinely survived the restart, not coincidental.

---

**D015 — Longbridge wired as the first concrete MarketDataProvider (closes D008)**
Date: 2026-08-23
Decision: `apps/api/app/marketdata/providers/longbridge.py`'s
`LongbridgeMarketDataProvider` wraps the `longport` SDK's
`AsyncQuoteContext.quote()`. Credentials
(`LONGPORT_APP_KEY`/`LONGPORT_APP_SECRET`/`LONGPORT_ACCESS_TOKEN`) are
optional `Settings` fields with no default; `build_longbridge_provider()`
returns `None` unless all three are set, and `main.py`'s lifespan only
constructs a `MarketDataRouter` when a provider actually builds - the app
boots fine either way, logging `market_data_vendor=NOT_CONFIGURED` when
it doesn't. New `GET /market-data/{symbol}/quote` (auth required, no
special permission) exposes it: 503 `NOT_CONFIGURED:` with no vendor
wired, 404 `NO_DATA_AVAILABLE:` (from the router) if the vendor has no
data, 200 with a real `MarketSnapshot` otherwise.
Reason: the user chose Longbridge (already using it via other MCP
tooling) over IBKR or a free public API. Before writing any integration
code, I installed the real `longport` package into a throwaway venv and
introspected its actual classes (`Config`, `AsyncQuoteContext`,
`SecurityQuote`) directly with `dir()`/`inspect.signature()`/docstrings,
because two different web sources gave conflicting method names
(`quote` vs `realtime_quote`) and calling signatures - trusting either
blindly would have meant writing code against an API that doesn't
actually exist, the same anti-fabrication principle this project applies
to data and phase reports, applied here to an external SDK's surface.
`LongbridgeQuoteClient` is a narrow Protocol (not a direct dependency on
`longport.openapi.AsyncQuoteContext`) so unit tests inject a fake client
- the real SDK is only ever imported inside `build_longbridge_provider()`,
never touched by the provider class or by any test, and no test
constructs a real `AsyncQuoteContext` (even with fake credentials, since
that call could still attempt a real connection).
Alternatives: IBKR (rejected - user chose Longbridge); wiring the vendor
directly into trade-proposal construction so `estimated_price` auto-fills
from a live quote (deliberately not done - that changes trade submission
semantics load-bearingly, e.g. whether the *caller's* submitted price or
the *vendor's* quote is authoritative for a fill, and deserves its own
decision rather than being a side effect of "wire the vendor").
Consequences: the market-data router is reachable and real, but nothing
in the trading path calls it yet - `POST /brokers/{broker_id}/trades`
still takes `estimated_price`/`market_data_as_of` directly from the
caller, unchanged since Phase 6. Whether/how to connect the two is an
explicit open decision, not an oversight (see `docs/PROJECT_CONTEXT.md`).
At the time this decision was made, no real Longbridge credentials
existed in this environment, so only the routing/fallback logic
(unit-tested against a fake client) and the "not configured" path
(verified live: app boots, logs `NOT_CONFIGURED`, and the endpoint
returns 503 rather than any invented price) had been verified directly.
**Update 2026-08-24:** real paper-trading credentials were supplied and
verified live - `GET /market-data/AAPL.US/quote` returned an actual
Longbridge quote (`price: 311.550`, real `as_of` timestamp,
`source: longbridge`), closing this gap. Credentials live in a local,
gitignored `.env` (never committed) — see D017's update for the trade-path
verification this unblocked.
Status: Implemented, tested (`tests/marketdata/providers/test_longbridge.py`,
8 unit tests covering snapshot construction, both timestamp shapes the
SDK could plausibly return, empty results, non-positive price, a typed
`VendorError` wrapping an arbitrary SDK exception, and both
"credentials missing/partial" branches of `build_longbridge_provider`).
94/94 total tests passing, ruff+mypy clean (43 source files). Verified
live: server boots and logs `market_data_vendor=NOT_CONFIGURED` with no
credentials set; `GET /market-data/{symbol}/quote` returns 503 with the
`NOT_CONFIGURED:` detail rather than any fabricated quote.

---

**D016 — User/role update and deactivate endpoints close the D013 gap**
Date: 2026-08-23
Decision: `PATCH /admin/users/{id}` (`is_active`, `role_id`) and
`PATCH /admin/roles/{id}` (`description`, `permissions`) added to
`apps/api/app/api/routes/admin.py`, gated by the same `Permission.ADMIN`
as every other admin route. Both use Pydantic's `model_fields_set` to
distinguish "key omitted from the JSON body" (leave untouched) from "key
explicitly set to `null`" (unassign `role_id`) - a plain `is not None`
check on the parsed model can't tell these apart. `is_active` cannot be
set to `null` (422) since the field is a `bool`, not nullable in
practice - it can only be omitted or given a real boolean. `name` is
deliberately not updatable on roles (roles are looked up/referenced by
name elsewhere; renaming is out of scope here). Still no delete endpoint
for either resource - deleting a `User` or `Role` row outright would
orphan `orders.submitted_by_user_id` / `users.role_id` foreign keys;
`is_active=false` is the only supported way to deactivate a user, and a
role is neutralized by clearing its `permissions` rather than removed.
Reason: D013 shipped create-only for users/roles and flagged "if a role
or user needs to change after creation, that still requires SQL" as an
acknowledged gap. This closes exactly that gap - deactivating a
compromised user or revoking a role's permissions no longer needs raw
SQL - without adding delete endpoints that would need FK-orphan handling
this phase didn't ask for.
Alternatives: (a) a separate `DELETE`/`POST .../deactivate` endpoint
instead of `is_active` via PATCH - rejected, `is_active` is already the
field that gates login (D010) and trading (via `get_current_user`'s live
check), so folding deactivation into the same PATCH avoids a second
code path for the same state. (b) allowing `role_id: null` to mean "no
change" - rejected, that would make it impossible to ever unassign a
role via the API; `model_fields_set` gives an unambiguous way to express
both "leave alone" and "clear" instead.
Consequences: authorization changes take effect immediately, not on next
login or token refresh - `get_current_user` re-loads `is_active` and the
user's role/permissions fresh from the database on every request rather
than trusting anything cached in the JWT, so a revoked user or a
stripped-down role is enforced starting with that user's very next
request. No listing endpoints still exist for either resource (unchanged
from D013) - an operator applying these PATCHes needs the id from
elsewhere (e.g. the `POST` response when the row was created, or direct
SQL to look one up).
Status: Implemented, tested (`tests/api/test_admin.py`, 7 new integration
tests added to the D013 suite: deactivation blocks a live token
immediately, role reassignment via PATCH, reassigning to an unknown role
is 404, patching an unknown user is 404, omitting `role_id` leaves it
untouched, a role's `permissions` update changes authorization for every
holder of that role, patching an unknown role is 404). 101/101 total
tests passing, ruff+mypy clean (43 source files). Verified live against
a running server, real Postgres: issued a JWT for an active user,
confirmed it worked against `GET /market-data/{symbol}/quote` (503
`NOT_CONFIGURED`, not 401), had a separate admin PATCH that same user to
`is_active=false`, then reused the *same already-issued* token against
the same endpoint and got 401 - proving deactivation is enforced without
waiting for token expiry. Separately verified a role's `permissions`
PATCH the same way: a user's already-issued token got 403 (missing
`trade:submit:paper`) against `POST /brokers/{id}/trades`, an admin then
PATCHed that user's role to add the permission, and the same token
immediately got past the permission check (404 for the nonexistent
broker id used in the test, rather than another 403) - confirming role
permission changes also propagate without re-login.

---

**D017 — Market data connected to trade submission: caller-supplied price stays authoritative, omission fetches a live quote**
Date: 2026-08-24
Decision: `TradeSubmissionRequest.estimated_price` (`apps/api/app/api/schemas.py`)
becomes optional. If the caller supplies it, behavior is unchanged from
every phase before this one - the vendor is never consulted, and
`market_data_as_of` defaults to the server's current time exactly as
before. If the caller omits it, `POST /brokers/{broker_id}/trades`
(`apps/api/app/api/routes/trades.py`) fetches a live quote via the same
`MarketDataRouter` `GET /market-data/{symbol}/quote` uses, and takes both
`estimated_price` and `market_data_as_of` from that one `MarketSnapshot` -
never mixing a live price with a caller-chosen timestamp, and never
partially filling in only one of the two from a quote. No router
configured is 400 `NOT_CONFIGURED:`; a router configured but with no data
for the symbol is 400 `NO_DATA_AVAILABLE:` (the router's own message,
already prefixed). Neither case invents a price.
Reason: D015 explicitly left "whether/how to connect the two" open rather
than deciding it as a side effect of wiring the vendor. Now that both
sides exist and are independently tested, the two credible answers were
(a) caller-supplied price always required, quote endpoint stays read-only
forever, or (b) an omitted price falls back to a live quote. (b) was
chosen because `docs/API.md`'s original description of the quote endpoint
already said "not connected to trade submission... see D015 for why
that's an open decision, not an oversight" - i.e. the omission was always
provisional, not a settled design. Keeping the caller-supplied path fully
authoritative (never overridden even when a vendor is wired) preserves
every existing test and every prior phase's behavior unchanged - this is
purely additive.
Alternatives: (a) always require a caller-supplied price - rejected,
leaves the vendor wired but genuinely useless for anything but manual
`GET` inspection. (c) let a caller-supplied `market_data_as_of` survive
even when the price comes from a live quote - rejected as a lie: pairing
a fresh price with a stale/arbitrary caller timestamp would misrepresent
when that price was actually observed, undermining the freshness check
the risk engine runs on `market_data_as_of`.
Consequences: submitting without a price now depends on the market data
vendor being configured and having data for the symbol - a new way for a
trade submission to legitimately 400 that didn't exist before, but it's
the same "fail closed, never fabricate" posture as every other gap in
this codebase, not a weakening of it. At the time this decision was made,
no real Longbridge credentials existed in this environment, so the
"vendor returns a real quote" path was verified only against a fake
provider in tests, not Longbridge itself - same limitation D015 already
had for the read endpoint.
Status: Implemented, tested (`tests/api/test_trades.py`, 4 new
integration tests against real Postgres: omitting the price with no
vendor wired is 400 `NOT_CONFIGURED`; omitting it with a fake vendor
wired via `dependency_overrides` fills using that vendor's price;
omitting it when the vendor has no data is 400 `NO_DATA_AVAILABLE`; and
supplying a price with a vendor wired proves the vendor is never called
at all, using a provider that raises `AssertionError` if invoked).
105/105 total tests passing (104 via the disposable venv's DB-backed run
plus one fail-closed JWT-secret test re-verified in isolation, since it
requires the env var truly unset), ruff+mypy clean (43 source files).
Verified live against a running server, real Postgres, no Longbridge
credentials configured: omitting `estimated_price` returned 400
`NOT_CONFIGURED` as expected; supplying one still returned 200 filled
exactly as every prior phase's trades did.
**Update 2026-08-24:** re-verified live with real paper-trading Longbridge
credentials configured — an omitted-price trade on a granted broker
returned 200 `filled` with a real market price (`311.680`) sourced from
`GET /market-data/{symbol}/quote`'s live response, closing the one
remaining gap this entry originally flagged. Credentials in a local,
gitignored `.env`, never committed.

---

**D018 — First agent: a single-responsibility TraderAgent, provider-agnostic Anthropic-Messages-API client, never authoritative for price or risk**
Date: 2026-08-24
Decision: `apps/api/app/agents/` adds the first LLM-backed code in this
codebase. `LLMProvider` (`provider.py`) is a narrow Protocol
(`complete(system, user, max_tokens) -> str`) mirroring
`marketdata/provider.py`'s shape exactly — same optional-vendor,
NOT_CONFIGURED-if-unset, typed-error posture, just for text completions.
`AnthropicCompatibleProvider` (`anthropic_compatible.py`) is the one
concrete implementation: any endpoint speaking the Anthropic Messages API
(`POST {base_url}/v1/messages`, bearer auth) works, OmniRoute included —
deliberately not named or hard-coded to OmniRoute specifically
(`LLM_PROVIDER_BASE_URL`/`_API_KEY`/`_MODEL`, not `OMNIROUTE_*`), per
`docs/MODEL_ROUTING.md`'s "model names must be configurable, never
hard-coded" rule. `TraderAgent` (`trader.py`) is the first (and, this
phase, only) agent: given a symbol and a free-text directive, it asks the
provider for a JSON `{side, quantity, stop_distance_pct, rationale}` and
validates it into a `TradeIdea` — single responsibility per
`docs/AGENT_POLICY.md` (drafts a proposal shape only; does not analyze,
research, or size against portfolio risk). New
`POST /brokers/{broker_id}/agent-trades` (`apps/api/app/api/routes/trades.py`)
wires it up: the agent proposes side/quantity/stop distance, the same
deterministic live-quote path from D017 supplies the price, a new
deterministic `stop_price_from_distance()` converts the agent's percentage
into an actual stop price against that real price, and the resulting
`TradeProposal` goes through the exact same `submit_trade_and_record()`
→ Risk Engine path a human-submitted trade uses — no separate, weaker
validation path for agent-originated trades. Extracted `_resolve_live_quote`/
`_execute_trade`/`_require_paper_broker` helpers in `trades.py` so both
routes share one implementation of "fetch a live quote" and "run the risk
path," rather than the agent route re-implementing D017's logic.
Reason: `docs/AGENT_POLICY.md` and `docs/TRADING_SAFETY.md` (spec §62)
both require that no agent output is ever authoritative for price,
balance, position, P&L, or risk, and that the deterministic Risk Engine
validates everything an LLM proposes. The design here makes that
structurally true rather than a convention to remember: the agent's
`TradeIdea` type has no price field at all, so there's no field to
accidentally trust; the endpoint fetches price the same way D017 already
does, unconditionally, regardless of what the agent said. A single narrow
agent (not a full analyst/debate/portfolio-manager stack) is a deliberate
scope cut — `docs/AGENT_POLICY.md`: "each agent is a specialist with one
responsibility... don't build a do-everything agent," and there is no
existing consumer yet for the parallel-analyst/research-debate layers the
governing spec eventually wants.
Alternatives: (a) let the agent propose a price too, cross-checked against
a live quote — rejected as unnecessary complexity; the live quote is
already authoritative, so an agent-proposed price would only ever be
overridden or used as a sanity-check nobody asked for. (b) a generic
"LLM client" without a typed `TradeIdea` schema, parsed ad hoc at each
call site — rejected, `docs/AGENT_POLICY.md` requires structured output,
not free text a downstream consumer parses; the whole point of `TradeIdea`
is that `trades.py` never touches raw LLM text. (c) hard-code the client
against OmniRoute's specific base URL/auth scheme — rejected per
`docs/MODEL_ROUTING.md`; the settings are provider-name-agnostic on
purpose, matching the Longbridge precedent's request-a-vendor-not-assume-
a-vendor posture.
Consequences: a malformed or unparseable LLM response is a 502
`AGENT_OUTPUT_INVALID:` — a new external failure mode this route can hit
that the human-submitted route can't, but consistent with "fail closed,
never fabricate" rather than a weakening; the caller gets a clear signal
to retry the directive or fall back to the human-submitted endpoint,
never a guessed trade. No LLM provider configured is 400
`NOT_CONFIGURED:`, identical convention to D008/D015/D017. OmniRoute was
unreachable in this environment at implementation time (connection
refused on `127.0.0.1:20128`), so only the NOT_CONFIGURED path and the
fake-provider-backed agent/routing logic were verified directly — the
"provider actually configured and returns a usable completion" path is
untested against a real vendor, the same limitation D015 had for
Longbridge before real credentials existed (see D015/D017's 2026-08-24
updates for how that gap was later closed — the analogous step here is
pointing `LLM_PROVIDER_*` at a reachable OmniRoute instance and re-running
this verification).
Status: Implemented, tested: `tests/agents/test_trader.py` (8 unit tests
against a fake `LLMProvider` — valid JSON, JSON wrapped in prose, non-JSON
response, a response missing a required field, a zero quantity failing
validation, a provider error wrapped as `AgentOutputError`, and both stop-
price-from-distance directions); `tests/api/test_agent_trades.py` (5
integration tests against real Postgres — no provider configured is 400
NOT_CONFIGURED; a valid agent idea is submitted through the normal risk
path and fills; an oversized agent-proposed quantity is rejected by the
Risk Engine, not the agent, proving no bypass; malformed agent output is
502 not a fabricated trade; a missing broker grant is 403 before the
agent is ever called, proven with a provider that raises if invoked).
118/118 total tests passing, ruff+mypy clean (47 source files). Verified
live against a running server, real Postgres: with no `LLM_PROVIDER_*`
configured, the app booted logging `llm_provider=NOT_CONFIGURED`, and
`POST /brokers/{id}/agent-trades` returned 400 `NOT_CONFIGURED` rather
than any fabricated trade idea.

---

**D019 — First analyst: a single read-only TechnicalAnalyst, no fabricated data sources for the analysts that would need them**
Date: 2026-08-27
Decision: `apps/api/app/agents/technical_analyst.py` adds the first
member of the parallel analyst layer per the governing spec's "Target"
architecture — a single `TechnicalAnalyst`, not the full technical/
fundamental/news/sentiment team. `TechnicalAnalyst.analyze(symbol, price,
as_of)` calls the same `LLMProvider`/`AnthropicCompatibleProvider`
connection `TraderAgent` uses and returns a validated `TechnicalRead`
(`stance: bullish|bearish|neutral`, `summary`, `confidence`) — **no
price, side, or quantity field at all**, so there is no field a caller
could mistake for a trade proposal. It is deliberately read-only and
deliberately not a technical-indicator calculator: it receives one live
quote (price + timestamp, from the same D017 live-quote path
`agent-trades` already resolves), never a price series, and its system
prompt explicitly forbids claiming to compute RSI/MACD/moving averages/
etc — `docs/TOKEN_POLICY.md`'s mandatory-deterministic list still applies
to any *real* indicator; this codebase just doesn't have the historical
price data to compute one yet (`packages/data_providers/` remains an
empty placeholder). Extracted `apps/api/app/agents/parsing.py`'s
`extract_json_object()` out of `trader.py` so both agents share one
JSON-extraction helper instead of duplicating it. `POST
/brokers/{broker_id}/agent-trades` (`trades.py`) now resolves the live
quote once, up front, then optionally runs the `TechnicalAnalyst` against
that same quote and appends its read to `TraderAgent.propose()`'s prompt
as informational context (a new `technical_context: str | None` param) —
never a separate trusted channel, never required, and a missing or
failing analyst silently omits the context rather than blocking or
degrading the trade in any way.
Reason: `docs/AGENT_POLICY.md` explicitly warns against "run[ning] agents
whose output the current task can't use," and this codebase's
no-fabrication rule (`docs/TRADING_SAFETY.md` spec §57) forbids inventing
a data source. There is no real fundamental/news/sentiment feed wired
into this codebase — only a live price quote via Longbridge (D015/D017).
Building a `FundamentalAnalyst`/`NewsAnalyst`/`SentimentAnalyst` today
would mean either fabricating a data feed (forbidden outright) or naming
an agent after data it doesn't actually have access to (dishonest, and a
predictable source of confusion for whoever wires a real feed in later
and has to figure out what the agent was actually doing before). Building
only the one analyst type with genuine data behind it is the same
conservative, "build only what has real data behind it" call this
project has made at every prior phase (D008 shipping a routing skeleton
with no vendor, D015 introducing exactly one vendor rather than several
speculative ones). A single analyst also means there is nothing to
parallelize yet — `docs/AI_OPTIMIZATION.md`'s "parallelize independent
agents" rule presumes more than one agent exists; building fan-out/
concurrency infrastructure for one agent would be unused complexity, not
optimization, so it isn't built this phase (see Consequences).
Alternatives: (a) build all four analyst types now, with fundamental/
news/sentiment analysts operating on whatever data happens to be
reachable (e.g. asking the LLM to reason about a company "from general
knowledge") - rejected, that's not analysis of real data, it's the model
inventing a plausible-sounding opinion with nothing underneath it, which
is exactly the fabrication spec §57 prohibits in spirit even though no
literal fake price is involved. (b) have the `TechnicalAnalyst` compute a
real indicator from Longbridge's candlestick history endpoints (not yet
wired into `apps/api/app/marketdata/`) - rejected as scope creep past
"first analyst"; wiring historical OHLC data is its own decision with its
own testing/verification burden, better done deliberately (see Planned
Work) than as a side effect of shipping the first analyst. (c) give
`TechnicalAnalyst` its own separately-configured `LLM_PROVIDER_*`
connection - rejected as unnecessary; nothing yet distinguishes what
model tier the analyst vs. the trader should use, and a shared connection
is simpler to operate and matches "don't build unused infrastructure."
Consequences: `agent-trades` now resolves the live quote *before* calling
`TraderAgent.propose()` (previously the agent was called first, since it
never needed a price) - purely an internal reordering so the
`TechnicalAnalyst` and `TraderAgent` can share one quote rather than each
fetching separately, but it meant an existing D018 test
(`test_malformed_agent_output_is_a_502_not_a_fabricated_trade`) that
never stubbed a market-data router started failing on the NOT_CONFIGURED
quote path before ever reaching the agent - fixed by stubbing a fake
router in that test, matching its siblings; this is a real behavior
change worth noting for anyone reading `git blame` on that line, not a
flaky test. No fan-out/parallel-execution scaffolding was built - if/when
a second analyst is added, revisit `docs/AI_OPTIMIZATION.md`'s
parallelize-independent-agents rule then, not speculatively now. Real
historical price data (for an actual computed indicator, not qualitative
commentary) remains unwired - a genuine "future work" gap, not a
completed item being understated.
Status: Implemented, tested: `tests/agents/test_technical_analyst.py` (7
unit tests against a fake `LLMProvider` - valid JSON, malformed JSON, a
response missing a required field, a provider error wrapped as
`AnalystOutputError`, and boundary values for `confidence`);
`tests/api/test_technical_analyst_wiring.py` (3 integration tests against
real Postgres - no analyst configured still succeeds with no context
appended; a configured analyst's read demonstrably reaches
`TraderAgent`'s prompt, proven with a `CapturingTraderAgent` subclass that
records what it was called with rather than relying on an LLM to echo it
back; a failing analyst never blocks the trade, context silently omitted).
128/128 total tests passing (127 in one run plus the fail-closed
JWT-secret test re-verified in true isolation, the same known
shell-env-false-failure pattern documented in every prior phase),
ruff+mypy clean (49 source files). Verified live against a running
server, real Postgres, no `LLM_PROVIDER_*` configured: the app booted
logging `technical_analyst=NOT_CONFIGURED` alongside `llm_provider=NOT_CONFIGURED`,
and `POST /brokers/{id}/agent-trades` returned 400 `NOT_CONFIGURED` (the
same D018 gate - a missing `TraderAgent` is checked first) rather than
any fabricated trade idea or analyst read. OmniRoute was unreachable in
this environment at implementation time (connection refused on
`127.0.0.1:20128`), so - same limitation as D018 - the "analyst actually
returns a usable read" path is verified only against a fake provider in
tests, not a real completion.

---

**D020 — First frontend: Next.js/TypeScript, JWT in an httpOnly cookie via a route-handler proxy, Vitest over Playwright for v1**

Date: 2026-08-27
Decision: Built `apps/web/` — Next.js 16 (App Router), TypeScript,
Tailwind v4 — as the first UI on top of the API this repo has had since
Phase 9. It covers exactly three screens' worth of function: `/login`
(posts to `POST /auth/login`), and `/dashboard` (backend health status,
a quote-lookup form against `GET /market-data/{symbol}/quote`, and a
paper-trade submission form against `POST /brokers/{broker_id}/trades`).
The browser never calls the backend directly — every backend call goes
through a same-origin Next.js route handler
(`app/api/auth/login`, `app/api/health`, `app/api/quote/[symbol]`,
`app/api/trades/[brokerId]`) that does the actual `fetch` to
`API_BASE_URL` server-side. The JWT from `/auth/login` is stored as an
httpOnly cookie set by the login route handler, never in `localStorage`
or any value client JS can read; the other route handlers read that
cookie server-side and attach `Authorization: Bearer <token>` before
calling the backend. `proxy.ts` (Next.js 16's replacement for the
deprecated `middleware.ts` convention) redirects `/dashboard/*` to
`/login` when the cookie is absent — presence only, not validity; an
expired or otherwise-invalid token still surfaces as the backend's real
401 through the route handlers, never guessed at the proxy layer. Test
strategy: Vitest + React Testing Library (jsdom), 7 component tests
covering the quote-lookup and trade-submission components' real
rendering of success, the 503 `NOT_CONFIGURED:` and 404
`NO_DATA_AVAILABLE:` sentinel details, a 403 detail, a rejected trade's
`block_reason`, a filled trade's fill price/quantity, and a network
failure — never a generic "error occurred" swallowing which case fired.
Reason: Next.js/TypeScript is mandated by
`docs/ARCHITECTURE.md`/`docs/PROJECT_CONTEXT.md`'s "Technology Stack"
entry (`Frontend (Next.js/TypeScript, per spec) not started`), so that
part wasn't a choice to make, just to execute. The httpOnly-cookie
choice follows directly from `docs/TRADING_SAFETY.md`'s "never fabricate
... use NOT_CONFIGURED / DATA_UNAVAILABLE sentinels" posture extended to
the frontend: a token sitting in `localStorage` is readable by any
script that runs on the page (XSS), which for a trading UI that can
submit real (paper, today; live, eventually) orders is a materially
worse blast radius than the added plumbing of routing every authenticated
call through a proxy handler. Vitest/RTL over Playwright: this phase
ships three screens with no complex client-side state machine and no
existing browser-driven flow worth protecting end-to-end yet — the
value Playwright adds (real browser, real navigation, real cookie
handling) is exactly the surface `npm run build`'s type check plus curl
verification (see Consequences) already covered by hand for this pass,
while Vitest component tests catch the thing most likely to actually
regress here: a response-shape rendering path silently swallowing a
sentinel string.
Alternatives: (a) JWT in `localStorage`, read directly by client
components calling the backend's CORS-enabled endpoints — rejected as
the weaker security posture above; kept as the explicit v2 reconsideration
point below since it is simpler (no route-handler duplication, no cookie
plumbing) and some teams accept the XSS tradeoff deliberately. (b) a
single generic `/api/proxy/[...path]` route handler forwarding path/method/
body to the backend generically — rejected for now because it would
also blindly forward whatever headers/methods a compromised client sent,
whereas one route handler per endpoint keeps the attack surface and the
request/response shape explicit and typed; worth reconsidering only if
the number of proxied endpoints grows enough that the duplication cost
exceeds this benefit. (c) Playwright from the start — rejected per
Reason above; noted as the natural v2 addition once there's a
login → dashboard → trade flow with real navigation/session state worth
protecting, not before.
Consequences: every new authenticated backend endpoint this frontend
wants to call needs its own route handler repeating the
cookie-read-and-forward pattern — a real but small and consistent tax,
not a design flaw. No CSRF token exists yet; `sameSite: "lax"` on the
cookie is the only mitigation today, and should be revisited if
state-changing routes grow beyond the current login/trade-submission
pair. `npm run build` passes with zero TypeScript errors (confirmed via
the actual build output, not assumed). Verified via `npm run dev` +
curl against the Next.js app's own routes: `/login` returns 200 with the
correct page title, `/dashboard` 307-redirects to `/login` with no
cookie, `/api/health` returns a real 503
`DATA_UNAVAILABLE: could not reach the trading API` (backend not running
in this pass, not faked as healthy), and `/api/quote/[symbol]` /
`/api/trades/[brokerId]` both correctly 401 `Not authenticated` with no
cookie present. NOT verified: a live login/quote/trade round-trip
against the real backend — Docker Desktop's daemon was unreachable in
this environment (`npipe:////./pipe/dockerDesktopLinuxEngine` connection
refused, and no working launcher path was found either), so
`docker compose up` could not be run here at all; this is a genuine gap,
not a "didn't bother" — the next session with a working Docker daemon
should run `docker compose up -d` (remapping ports if a sibling
Phase 16 worktree's containers are still up on 8000/5432/6379) and
confirm the login → quote → trade path against a real, seeded user.
v2 candidates, deliberately cut from this pass: `/admin/*` UI,
`POST /brokers/{id}/agent-trades` UI (both explicitly out of scope per
the Phase 17 task), a broker-discovery UI (no such endpoint exists —
broker IDs are entered by hand today), session refresh/expiry UX (an
expired cookie today just means the next authenticated call 401s and the
user has to log in again manually), and Playwright e2e coverage once a
protectable flow exists.
Status: Implemented. `npm run build` clean (zero TypeScript errors),
`npm test` (Vitest) 7/7 passing.
**Update 2026-08-28:** re-verified end-to-end against a real running
backend (Docker was down during Phase 17's original build, up for this
pass). `npm run build` reconfirmed clean. Via curl against the running
`npm run dev` app: `POST /api/auth/login` with a real seeded user returned
`200 {"ok":true}` and set the httpOnly `trading_os_token` cookie exactly
as designed; `GET /api/quote/[symbol]` with that cookie reached the real
backend and returned its genuine 503 `NOT_CONFIGURED:` (no market data
vendor wired in this environment) rather than any fabricated quote;
`POST /api/trades/[brokerId]` with that cookie submitted a real trade
against a real granted broker and returned `200` with a genuine fill
(`status: "filled"`, `fill_price: "100"`) — closing the one remaining gap
this entry originally flagged.

---

**D021 — Real historical prices for TechnicalAnalyst: a HistoryProvider port, deterministic SMA/RSI, LLM narrates but never calculates**
Date: 2026-08-28
Decision: Closes D019's explicit "future work" gap. `apps/api/app/marketdata/history_provider.py`
adds a `HistoryProvider` Protocol (`get_daily_closes(symbol, count) ->
list[Decimal]`, oldest-first) — a distinct capability from
`MarketDataProvider` (a series vs. one quote), deliberately not
overloaded onto `get_snapshot()`. `LongbridgeHistoryProvider`
(`marketdata/providers/longbridge.py`) implements it via
`AsyncQuoteContext.candlesticks(symbol, Period.Day, count,
AdjustType.NoAdjust)` — verified against the installed `longport`
package (v4.3.7) by direct introspection, same discipline as D015: the
real method returns `Candlestick` objects with `.close`/`.timestamp`,
and the provider sorts them by timestamp itself rather than trusting the
SDK's return order. `apps/api/app/marketdata/indicators.py` adds `sma()`
and `rsi()` as pure functions - no LLM, no I/O, per
`docs/TOKEN_POLICY.md`'s mandatory-deterministic list, which explicitly
names both. `TechnicalAnalyst.analyze()` (D019) gained an optional
`indicator_context` parameter and its system prompt now explicitly
permits narrating *given* indicator values while still forbidding it
from calculating or inventing one itself - the LLM's role stays strictly
narration of numbers it was handed, never computation.
`POST /brokers/{broker_id}/agent-trades` (`trades.py`) wires it: when a
`HistoryProvider` is configured, it fetches 30 daily closes and computes
SMA(20)/RSI(14) if enough history exists, passing whichever succeeded to
the (also optional) `TechnicalAnalyst`; a short history, a vendor
failure, or no configured provider all just mean no indicator context
this call, never a blocked trade or an invented value - same
never-blocking posture as D019's original TechnicalAnalyst wiring.
Reason: D019 shipped honestly scoped to "no historical data exists yet,"
explicitly framing the analyst's commentary as qualitative rather than
technical-indicator analysis, and named wiring real OHLC data as the
natural next step rather than doing it as a rushed side effect of
shipping the first analyst. That data now exists via the same Longbridge
SDK already integrated (D015) - no new vendor decision needed, just
using more of the connection already trusted and tested. RSI's classic
(simple-average, Wilder's original) definition was chosen over a
smoothed/exponential variant deliberately - it's the textbook definition,
not tuned to match any specific charting platform's convention, and
matching a specific platform wasn't asked for.
Alternatives: (a) let the LLM estimate an indicator from a description
of recent price action in the prompt - rejected outright, this is
exactly what `docs/TOKEN_POLICY.md` forbids: an LLM "computing" a number
that must be deterministic. (b) overload `MarketDataProvider.get_snapshot()`
to optionally return a series - rejected, conflates two different
capabilities (one price now vs. many prices over time) into one method
signature, and every existing caller of `get_snapshot()` would need to
handle a shape it never asked for. (c) share one `AsyncQuoteContext`
between the quote and history providers instead of each building their
own - deferred as a reasonable future optimization, not built this
phase; restructuring D015's already-tested construction path carries
more risk than the connection-count savings justify right now.
Consequences: a new optional `HISTORY_PROVIDER`-shaped credential gate
exists alongside the market-data and LLM-provider gates already in
`main.py`'s startup log (`history_provider`) - one more thing to notice
is `NOT_CONFIGURED` when diagnosing why an agent trade has no indicator
context, not a bug. `agent-trades` now makes up to one additional
network call (the candlestick fetch) beyond what D019 required, still
strictly optional and still never blocking the trade if it fails or is
unconfigured.
Status: Implemented, tested: `tests/marketdata/test_indicators.py` (9
unit tests - SMA/RSI correctness including a strictly increasing series
giving RSI 100, a strictly decreasing series giving RSI 0, a flat series
giving the neutral 50, and both raising `InsufficientDataError` rather
than padding when too few closes exist);
`tests/marketdata/providers/test_longbridge_history.py` (5 unit tests
against a fake candlestick client - out-of-order candles sorted
correctly, empty results, an SDK exception wrapped as `VendorError`,
both "no credentials"/"partial credentials" branches);
`tests/api/test_history_provider_wiring.py` (4 integration tests against
real Postgres - no provider configured succeeds with no indicator
context; a configured provider with enough history produces real
SMA(20)/RSI(14) values that demonstrably reach the analyst, proven via a
`CapturingTechnicalAnalyst` subclass; too-short a history omits the
context without failing; a failing provider never blocks the trade).
145/145 total tests passing, ruff+mypy clean (51 source files). One real
bug caught during this phase's own test run and fixed before completion:
`rsi()`'s consecutive-pair `zip(window, window[1:], strict=True)` was
wrong - the two slices are naturally different lengths by one (that's
the intended pairing, not a data-integrity problem `strict=True` should
guard against) and raised `ValueError` on every real call; fixed by
removing `strict=True`.
Verified live: (a) against the real Longbridge API directly (not just a
fake client) using real paper-trading credentials - fetched 30 real
daily closes for `AAPL.US` and computed genuine `SMA(20)=309.3215` /
`RSI(14)=51.57...` from them, proving the whole real-data path works
end to end, a level of verification D019 couldn't reach at the time
(OmniRoute/no history data then); (b) against a running server with no
`LLM_PROVIDER_*`/Longbridge credentials configured: startup logged
`history_provider=NOT_CONFIGURED`, and `POST /brokers/{id}/agent-trades`
returned 400 `NOT_CONFIGURED` for the LLM provider (D018's gate, checked
first) rather than any fabricated trade or indicator.

---

**D022 — First Portfolio module: deterministic snapshot from existing tables, average-cost-basis P&L, no new mutable state**
Date: 2026-08-28
Decision: `apps/api/app/portfolio/` (`models.py`, `errors.py`, `snapshot.py`)
adds `PortfolioSnapshot`/`PortfolioPosition` (Pydantic, all money/quantity
fields `Decimal`) and `compute_portfolio_snapshot(broker_id, session, *,
marks, default_starting_cash=None)`. It reads only existing rows -
`broker_accounts` (current cash), `broker_positions` (current
quantities), and `orders`/`fills` joined and replayed per symbol for
average cost and realized P&L - and writes nothing; this module has no
migration, because it needs none. `GET /brokers/{broker_id}/portfolio`
(`apps/api/app/api/routes/portfolio.py`) exposes it, current marks
supplied as a JSON body (`{"marks": {symbol: price}}`, same shape as
`TradeSubmissionRequest.marks`) since GET has no query-string-friendly
way to carry an arbitrary symbol->price map. Realized/unrealized P&L use
average-cost basis: `replay_symbol_fills()` (a pure function, no DB, no
I/O) walks one symbol's fills in `filled_at` order, updating a single
running `avg_cost` on every BUY (weighted average of existing basis and
the new fill) and accumulating `realized_pnl += (fill_price - avg_cost) *
quantity` on every SELL, `avg_cost` itself untouched by sells. Current
open quantity is taken from `BrokerPosition` (the execution layer's
already-authoritative current state, D014), not from the fills replay
total, so a snapshot's position sizes always agree with what the
execution layer itself believes it holds; `avg_cost` and `realized_pnl`
have no other source of truth in this schema so they come from the
replay. A missing mark for a currently-held symbol raises
`MissingMarkError` -> 400 `DATA_UNAVAILABLE:`, identical discipline to
`PaperBrokerAdapter.get_account_state()` (spec Sec57) - never a stale or
guessed price. A new `Permission.VIEW_PORTFOLIO` ("portfolio:view") gates
the route via `require_broker_access`, deliberately separate from
`Permission.SUBMIT_PAPER_TRADE` - viewing a broker's positions/P&L is
strictly weaker than moving money in it, and a read-only reporting role
should not have to also hold trade rights.
Reason: `docs/TOKEN_POLICY.md`'s mandatory-deterministic list explicitly
names P&L, exposure, and position sizing - this had to be plain Python
over existing rows, never an LLM read of the account state, and
`docs/MODULE_MAP.md`'s Portfolio row already flagged this gap ("paper
broker tracks cash/positions itself for now, no separate portfolio
module"). Average-cost basis was chosen over FIFO/LIFO lot tracking
because it's the only method this schema can compute without inventing
data that was never recorded: `BrokerPosition`/`BrokerAccount` already
hold one running quantity and one running cash balance per symbol/broker,
no per-lot records exist anywhere (D006/D014), and FIFO/LIFO both require
knowing which specific historical buy a given sell closes against -
retrofitting that would mean either a schema change (out of this phase's
scope) or silently assuming an ordering the data doesn't actually
establish. A new `Permission` (rather than reusing `SUBMIT_PAPER_TRADE`)
follows the coarse-but-intentional model D013/D016 already established
for `ADMIN` - one permission per genuinely distinct capability, not
per-resource, but capabilities that are actually different (view vs.
trade) still get different permissions.
Alternatives: (a) reuse `Permission.SUBMIT_PAPER_TRADE` to gate the
portfolio route - rejected, it would force every read-only viewer to also
be grantable trade rights, the opposite of least privilege for what
should be the least-privileged operation in this API. (b) query params
for `marks` (`?marks=AAPL:120,MSFT:50`) instead of a JSON body on GET -
rejected, an ad-hoc delimiter-packed string is worse to validate and
worse to extend than reusing the exact dict shape `TradeSubmissionRequest`
already uses, and FastAPI's `Body()` on a GET works fine here (this
endpoint has no other use for a request body to conflict with). (c) FIFO
lot tracking via a new `fill_lots`-style table - deferred, real added
value (matches how most brokers actually report cost basis) but a
genuine schema change or migration, not something to slip into a report-only
phase; flagged as explicit future work below. (d) treat a missing
`broker_accounts` row as zero cash - rejected as a silent default that
could misrepresent a real balance; `default_starting_cash` exists so a
caller (the route, using the same `Settings.paper_broker_starting_cash`
`load_paper_broker` uses) can supply the one value that's actually
configured rather than this module inventing zero.
Consequences: this is a read-only, real-time-only report - no
backtesting, no alerts, no performance-attribution-over-time exist or are
implied by this phase, and none should be assumed present. Those all need
persisted historical snapshots (a table, a write path, a retention
policy), which is a bigger decision deliberately left for a future phase
rather than being built speculatively here. A caller that wants FIFO/LIFO
cost basis, not average-cost, has no way to get it from this endpoint
today - also explicit future work, tracked here rather than silently
decided by omission.
Status: Implemented, tested. `tests/portfolio/test_snapshot.py` (6 pure
unit tests, no DB - hand-verified average-cost-basis scenarios including
a multi-buy average, a partial sell that leaves `avg_cost` unchanged, a
losing sell, and a mixed buy/sell/buy/sell sequence verified by hand:
buy 10@100 then 10@110 -> avg 105; sell 5@120 -> realized +75; sell
15@90 -> realized -225; total realized -150, final avg_cost 105).
`tests/api/test_portfolio.py` (10 integration tests against real
Postgres, reusing `tests/api/test_trades.py`'s fixtures by import per
this project's established pattern rather than duplicating them): empty
portfolio reports starting cash and zero P&L; a real position reports
correct avg_cost/current_value/unrealized_pnl from a supplied mark; a
missing mark for a held position is 400 `DATA_UNAVAILABLE`; a fully
closed position leaves zero open positions but correctly retains its
realized P&L; `VIEW_PORTFOLIO` alone cannot submit a trade (proves the
permission split is real, not accidental); missing permission, missing
grant, unknown broker (404), and no token (401) all behave exactly like
the equivalent trades-endpoint cases. 169/169 total tests passing (159
pre-existing/unrelated-work plus these 10), `ruff check .` clean on every
file this phase touched (one pre-existing `B905` finding in
`apps/api/app/marketdata/indicators.py` predates this phase - see D021's
own comment there explaining why `strict=` is deliberately omitted - and
is untouched by this change), `mypy apps` clean (56 source files).
Verified live: brought up this worktree's own Postgres/Redis/API stack
via `docker compose -p trading-os-phase19` with host ports remapped in a
local, uncommitted `docker-compose.override.yml` (`!override` merge key)
to avoid a port clash with a sibling worktree's already-running
containers on 5432 - `docker compose up -d --build` to pick up this
phase's code. Inserted a real user/role/broker/grant via direct SQL
(bcrypt hash generated locally, same pattern as prior phases' live
verification), logged in for a real JWT, and confirmed against the
running server: a broker with no trades reports starting cash
(`100000`) and empty positions; submitting a real `AAPL` buy through
`POST /brokers/{id}/trades` then calling `GET
.../portfolio` with `{"marks":{"AAPL":"120"}}` returned quantity `10`,
avg_cost `100`, current_value `1200`, unrealized_pnl `200`, matching the
hand-computed expectation exactly; calling the same endpoint with no
marks while still holding that position returned 400
`DATA_UNAVAILABLE: No mark supplied for open position 'AAPL'`; a request
with no token returned 401; an unknown broker id returned 404. All
inserted SQL rows were deleted afterward, `docker compose down -v`
removed every container/volume/network this phase created, and the
disposable `.venv19`/`.env`/`docker-compose.override.yml` were all
removed - nothing from this phase's local verification setup was left on
disk or in git status, and the sibling `trading-os-phase21` Postgres
container on port 5432 was never touched.

---

**D024 — Deterministic duplicate-order detection: same broker+symbol+side+quantity+price within a 5s window, FILLED orders only**
Date: 2026-08-28
Decision: A "duplicate" is defined precisely and deterministically: a new
proposal whose (symbol, side, quantity, estimated_price) exactly matches
a **FILLED** order on the same broker submitted within the preceding **5
seconds**. `apps/api/app/risk/models.py` adds `RecentOrder` (the plain-data
shape of one prior order) and `RiskLimits.duplicate_order_window_seconds`
(no default, same "unconfigured is a bug" posture as every other limit on
that model — the app-level default lives in
`Settings.risk_duplicate_order_window_seconds = 5`, `core/config.py`).
`evaluate_trade()` (`risk/engine.py`) gains an optional `recent_orders:
list[RecentOrder] | None` parameter and checks it right after the
market-data-freshness gate, before the stop-price/sizing rules — a
duplicate-execution risk is checked as a fundamental precondition, not as
one more sizing constraint. The engine never queries anything itself:
`apps/api/app/oms/persistence.py` adds `get_recent_filled_orders()`, a
single indexed SELECT the caller (`trades.py`'s `_execute_trade()`, shared
by both the human and agent-trades routes) runs before calling
`submit_trade_and_record()`, which now threads `recent_orders` straight
through to `submit_trade()` -> `evaluate_trade()`. A new composite index
`ix_orders_broker_symbol_submitted` (`broker_id`, `symbol`, `submitted_at`
— migration 0007) backs that query; the existing single-column
`ix_orders_symbol` doesn't serve a broker-scoped, time-bounded lookup
well. A new `BlockReason.DUPLICATE_ORDER` is added — never an existing
reason reused, per that enum's own docstring.

Reason, on the four judgment calls this phase asked for:
- Which fields define "identical": symbol+side+quantity+estimated_price
  together, not a subset. Symbol+side alone would block a trader from
  legitimately scaling into or out of a position seconds apart (a very
  normal thing to do). Quantity and price make the match precise enough
  that it's very unlikely two *different* real trading decisions would
  produce the exact same four values by coincidence within a few seconds
  — but a double-click, a client-side retry after a slow response, or a
  frontend resubmitting the same form on a spinner would.
- Why 5 seconds, not shorter or longer: a double-click is usually under
  1s; a client retrying after an HTTP timeout is commonly configured in
  the 2-5s range (typical `fetch`/`axios` defaults land there). 5s
  comfortably covers both without needing to guess a specific client's
  retry policy. Going shorter (e.g. 1s) would miss a slow-network retry
  storm — the more damaging case, since it's the one likeliest to
  double-execute a real order. Going longer (e.g. 60s) would block a
  trader legitimately re-entering the exact same setup — same symbol,
  side, size, and price — on a second signal or a scale-in a minute
  later; that is normal rapid trading, not an accidental duplicate, and
  the risk engine blocking it would be a false positive with real cost.
  5s sits in the gap between "clearly a resubmission artifact" and
  "clearly a new decision".
- FILLED only, not REJECTED: a REJECTED order never reached the broker —
  no market exposure was created, so there is no double-execution risk to
  guard against. Flagging a REJECTED order's identical retry as a
  *duplicate* would in fact be actively harmful: it would replace one
  useful rejection reason (e.g. `exceeds_max_position_size`, telling the
  caller exactly what to fix) with `duplicate_order`, which explains
  nothing about why the original attempt failed. A resubmission of
  identical parameters after a rejection is far more likely to be "I
  fixed the underlying condition and I'm trying again" or "the condition
  was transient" than "I am accidentally duplicating a position" — and if
  the underlying condition still holds, the retry will simply be rejected
  again on its own merits, which is the correct outcome. There is no
  PENDING status in this schema today (the paper broker always fills
  synchronously — `OrderStatus` only has `FILLED`/`REJECTED`) so this
  decision didn't need to additionally resolve a PENDING case; if an
  asynchronous/PENDING broker adapter is ever added, it should almost
  certainly be included alongside FILLED (an in-flight order not yet
  filled is exactly the double-execution risk this check exists for) —
  noted for whoever builds that.
- Where the check runs / what stays pure: per `docs/TRADING_SAFETY.md` and
  `risk/engine.py`'s own docstring, the engine must stay I/O-free. Option
  (a) from the phase brief was chosen over (b): the caller (the HTTP
  layer, which already owns the DB session) queries recent orders and
  hands them to the engine as plain `RecentOrder` data, exactly the same
  shape `HistoryProvider`/D021 established for optional external data
  reaching a pure component. This was preferred over teaching the
  *engine* a DB-aware helper method because the module's entire reason
  for existing (this docstring, `docs/TRADING_SAFETY.md`'s "must run even
  if every LLM [or, by the same logic, every downstream service] is
  down") is exactly this kind of boundary — adding even a well-isolated
  DB call inside `risk/` would be the first crack in a property this
  codebase has kept perfectly clean since D004.
Alternatives: (a) hash-based idempotency key supplied by the client
(standard for payment APIs) — rejected for now: it requires client
cooperation (a header/field no current caller sends) and solves a
different problem (exact request replay) than this task asked for
(detecting *distinct* requests that happen to describe the same trade);
worth revisiting once a frontend/agent client can be trusted to generate
one. (b) compare against a broader set of statuses or no status filter at
all — rejected per the FILLED-only reasoning above. (c) a longer window
tied to `max_market_data_age_seconds` (300s) — rejected, conflates two
unrelated concepts (how stale is the *price* vs. how recently was this
*exact order* filled) and would block obviously-legitimate re-entries
minutes apart. (d) resize/merge the duplicate into the existing position
instead of rejecting — rejected, breaks D004's block-not-resize precedent
and this module's audit-trail guarantee (what got approved is always
exactly what was proposed).
Consequences: every trade submission now does one additional indexed
SELECT before evaluation — bounded and cheap (index-only, narrow window,
single symbol) but real; if this ever needs to scale past a single-row
lookup pattern, the index is already the right shape to extend. A
resubmission of identical parameters within 5s of a fill now surfaces
`duplicate_order` instead of whatever the *next* rule would have said
(e.g. it would no longer separately report `insufficient_buying_power` on
a genuine double-submit that also happens to exceed cash) — acceptable,
since the duplicate check fires first specifically because it's the more
fundamental problem. Closes the `docs/PROJECT_CONTEXT.md` "Open
Decisions" gap D004 explicitly flagged as not built.
Status: Implemented, tested: `tests/risk/test_engine.py` (9 new unit
tests — exact duplicate within window blocked; same order just outside
the window allowed; different quantity/price/side/symbol each allowed;
an empty `recent_orders` list, standing in for "the only prior identical
order was REJECTED", allowed; no `recent_orders` argument at all allowed;
an unrelated recent order doesn't suppress a real duplicate elsewhere in
the list); `tests/oms/test_service.py` (1 new test — `recent_orders`
threaded through `submit_trade()` blocks before the broker is ever
touched); `tests/db/test_order_persistence.py` (1 new integration test
against real Postgres — `get_recent_filled_orders()` returns only the
matching symbol's FILLED order within the window, excluding a REJECTED
order, a different symbol, and anything outside the window);
`tests/api/test_trades.py` + `tests/api/test_agent_trades.py` (3 new
integration tests against real Postgres and the real HTTP routes — an
identical trade submitted twice via `POST .../trades` is blocked on the
second call with `block_reason=duplicate_order`; a different quantity
right after is not blocked; the same protection is proven end-to-end on
`POST .../agent-trades`, closing the "LLM-originated duplicate" half of
the requirement). 159/159 total tests passing (145 at D021 + 14 new),
ruff+mypy clean (fixed one pre-existing ruff finding encountered along
the way in `marketdata/indicators.py` - a `zip()` without an explicit
`strict=` - unrelated to this phase's own code but blocking a clean
`ruff check .`).
Verified live: against a running server (real Postgres, no Docker port
conflict with the parallel phase-19/20 worktrees since Postgres/Redis run
under this worktree's own `docker compose` project and the API ran
directly via `uvicorn` on a non-default port to avoid the sibling
worktrees' already-bound 8000) with a directly-inserted user/role/broker/
grant (bcrypt hash generated via `apps.api.app.auth.security.hash_password`,
same pattern as D021): submitted the identical AAPL buy 10 @ 100 paper
trade twice via curl roughly 0.5s apart — the first filled normally, the
second came back rejected with `block_reason=duplicate_order` and a
detail naming the 0.5s gap and the 5s window; a third submission for the
same symbol/side/price but quantity=5 immediately after filled normally,
confirming the check doesn't over-fire. All inserted rows (fills, orders,
broker_grants, broker_accounts/positions, broker, user, role) were
deleted afterward; the API process, `.venv21`, and `.env` created for
this verification were removed; `docker compose down -v` tore down this
worktree's Postgres/Redis containers and volume.

---

**D023 — Frontend v2: admin UI and agent-trades UI, one route handler per endpoint, same proxy pattern as D020**
Date: 2026-08-28
Decision: Built the two "v2 candidates" D020 explicitly deferred, in
`apps/web/`, on top of D020's existing pattern unchanged (Next.js route
handlers proxying to the backend with the httpOnly-cookie JWT attached
server-side; no new auth mechanism). Admin UI (`/admin`,
`app/admin/page.tsx`): six forms covering every `/admin/*` endpoint in
docs/API.md — create/update user (`components/admin/UsersAdmin.tsx`),
create/update role (`components/admin/RolesAdmin.tsx`), create/revoke
broker grant (`components/admin/BrokerGrantsAdmin.tsx`) — each backed by
its own route handler
(`app/api/admin/users/route.ts`, `app/api/admin/users/[userId]/route.ts`,
`app/api/admin/roles/route.ts`, `app/api/admin/roles/[roleId]/route.ts`,
`app/api/admin/broker-grants/route.ts`,
`app/api/admin/broker-grants/[grantId]/route.ts`) repeating D020's
cookie-read-and-forward shape exactly, including the DELETE handler's one
new wrinkle: a 204 backend response must be re-returned as
`new NextResponse(null, { status: 204 })`, not `NextResponse.json(null, {
status: 204 })`, since a 204 must carry no body. The `/admin` page is
reachable by any authenticated user — `proxy.ts`'s matcher gates it on
cookie presence only, the same as `/dashboard`, and the dashboard's nav
always shows an "Admin" link regardless of the viewer's actual
permissions. This was a deliberate choice, not an oversight: the real
`admin:manage` gate is the backend's 403 on submit, and hiding the nav
link based on a client-side guess about permissions the client cannot
actually know (no "am I admin" endpoint exists, and the JWT's claims
aren't decoded client-side) would imply a security boundary at the UI
layer that doesn't exist there — see docs/TRADING_SAFETY.md's "no
fabrication" posture extended to permissions, not just market data. A
non-admin submitting any admin form sees the backend's real 403 detail
string rendered as-is. Agent-trades UI: a new `AgentTradeForm` on the
dashboard (`components/AgentTradeForm.tsx`) posting to
`POST /brokers/{broker_id}/agent-trades` via
`app/api/agent-trades/[brokerId]/route.ts`; renders the full
`AgentTradeResponse` shape (`side`/`quantity`/`rationale` alongside the
existing `TradeSubmissionResponse` fields) and the two error sentinels
specific to this endpoint — 400 `NOT_CONFIGURED:` (no LLM provider or
market data vendor wired) and 502 `AGENT_OUTPUT_INVALID:` (the agent's
response didn't parse) — exactly as the backend returns them, never a
fabricated trade for either case. `marks` is entered as a
`SYMBOL=price, SYMBOL2=price2` string and parsed client-side into the
object the API expects, matching the existing pattern of keeping
malformed-input handling in the browser rather than adding a JSON
textarea.
Reason: both pieces are direct executions of what D020 already scoped
and deferred — no new architectural choice was available or needed;
following the identical route-handler-per-endpoint shape keeps the
proxy layer's attack surface exactly as explicit as D020 argued it
should be (alternative (b) in D020 — one generic passthrough proxy —
remains rejected for the same reason, now across nine proxied endpoints
instead of four). The "show the nav, surface the real 403" choice
follows directly from this repo's fail-closed, never-fabricate posture:
a client-side permission guess is itself a kind of fabrication (a claim
about access the client isn't actually in a position to verify), and a
hidden nav item is strictly worse than a visible one that 403s, because
a hidden item can read as "you don't have this" when the true state is
simply "unchecked."
Alternatives: (a) decode the JWT client-side to conditionally hide the
Admin link when the role's permission list doesn't include
`admin:manage` — rejected: the JWT's claims are an implementation detail
of `apps/api/app/auth/security.py`'s `create_access_token`, not a
documented, stable contract this frontend should couple to, and doing so
would be exactly the "client decides what it can't verify" pattern this
entry's Reason argues against; the real check happens once, correctly,
on the backend, on every request — duplicating it client-side buys
nothing but a second place for it to drift out of sync. (b) a single
combined create-or-update form per resource (toggle between modes)
instead of two separate forms — rejected as a marginal UI simplification
not worth the added state-management complexity for a UI this minimal;
two forms per resource matches the two distinct backend endpoints
one-to-one, which is easier to reason about and to test. (c) building
broker-discovery UI, session refresh/expiry UX, or Playwright e2e in
this pass — rejected per the Phase 20 task's explicit scope cut; these
remain open, see Consequences.
Consequences: nine new route handlers now repeat D020's
cookie-read-and-forward tax (Consequences already flagged this scaling
cost). No listing endpoints exist for users/roles/grants (D013), so an
operator must already know a resource's UUID to update or delete it —
the UI can't offer a picker; this is a real usability gap inherited from
the API, not something the frontend can paper over without fabricating
a list the backend doesn't provide. `npm run build` passes with zero
TypeScript errors (confirmed via the actual build output). `npm test`
(Vitest): 28/28 passing — the original 7 plus 21 new component tests
across `test/UsersAdmin.test.tsx`, `test/RolesAdmin.test.tsx`,
`test/BrokerGrantsAdmin.test.tsx`, and `test/AgentTradeForm.test.tsx`,
covering success, a real 403 (non-admin), a real 404/409 where
applicable, and network failure for every new form — the same
sentinel-vs-generic-message discipline D020 established. Verified live
against a real running backend (`docker compose up -d --build` in this
worktree, with `docker-compose.override.yml`-style host-port remapping
used only transiently during verification to avoid colliding with
sibling Phase 19/21 worktrees' Postgres/Redis containers on
5432/6379 — the port numbers in `docker-compose.yml` itself were
restored to their originals afterward, since sibling worktrees are
independent checkouts and this remapping was never meant to be a
tracked change): ran real Alembic migrations, bootstrapped one admin
user/role via direct SQL insert (bcrypt hash via
`apps/api/app/auth/security.py:hash_password`, same pattern as
D013/D016/D021), then through `npm run dev` + curl against the app's own
route handlers — never calling the backend directly — logged in as that
admin and got a real `POST /api/admin/roles` 201, a real
`POST /api/admin/users` 201, a real `PATCH /api/admin/users/{id}` 200
assigning the new role, a real `PATCH /api/admin/roles/{id}` 200, a real
`POST /api/admin/broker-grants` 201 against a SQL-inserted paper broker,
a real `DELETE /api/admin/broker-grants/{id}` 204 followed by a real 404
on repeating the same delete, and a real 403
`Missing required permission: admin:manage` when the newly-created
non-admin user attempted `POST /api/admin/roles`. Also logged in as that
non-admin user and got a real 400
`NOT_CONFIGURED: no LLM provider is wired (see docs/DECISIONS.md D018).`
from `POST /api/agent-trades/{brokerId}` — genuine, not fabricated, since
no `LLM_PROVIDER_*` credentials were configured in this verification
pass; `AGENT_OUTPUT_INVALID:` (502) was exercised only via the component
test's mocked response, not against a real misbehaving provider, since
reaching that path for real would require a configured-but-malfunctioning
LLM provider, which this pass had no reason to set up.
Status: Implemented and verified as above.
v2 candidates still open, not built in this pass (unchanged from D020
except broker-discovery, which remains blocked on the same missing
endpoint): broker-discovery UI (still no such endpoint), session
refresh/expiry UX (an expired cookie still just 401s the next
authenticated call), Playwright e2e coverage (still no protectable
navigation flow complex enough to justify it over the current Vitest
component coverage), a users/roles/grants listing UI (blocked on D013's
deliberate no-listing-endpoints scope cut, not a frontend gap), and
decoding the JWT client-side to pre-filter the nav (rejected above, not
merely deferred).

---

**D025 — Backtesting engine: one hard-coded SMA(20)-crossover strategy, replayed through the real Risk Engine and a fresh in-memory PaperBrokerAdapter, never a real broker's persisted state**
Date: 2026-08-28
Decision: `apps/api/app/backtesting/` adds `strategy.py` (a pure
`generate_signals(closes, period=20)` function - BUY when yesterday's
close was at/below SMA(20) and today's is above it, SELL on the mirror
crossover down, HOLD otherwise, reusing `marketdata/indicators.sma()`
rather than recomputing a moving average a second way), `metrics.py`
(pure `compute_total_return_pct`/`compute_max_drawdown_pct`/
`compute_win_rate_pct`, hand-verifiable in isolation from the engine),
`models.py` (`BacktestRequest`/`EquityPoint`/`BacktestResult`, all
money/quantity fields `Decimal`), `errors.py`
(`InsufficientHistoryError`/`UnsupportedDateRangeError`), and
`engine.py`'s `run_backtest()`, which orchestrates all of the above: it
fetches real closes via `HistoryProvider.get_daily_closes()` (D021),
generates signals, and for every BUY/SELL signal builds a real
`TradeProposal` and calls the real `evaluate_trade()` (D004) against a
real `AccountState` read from a real `PaperBrokerAdapter.get_account_state()`
(D014) - a fresh `PaperBrokerAdapter(starting_cash=...)` constructed once
per run, in memory, and discarded at the end of the call. This module
never imports `execution/persistence.py`'s `load_paper_broker`/
`save_paper_broker` - the only two functions that ever touch the
`broker_accounts`/`broker_positions` tables - so it is structurally
impossible, not just policy, for a backtest run to read or write a real
broker's persisted state. `POST /backtests` (`api/routes/backtests.py`,
no `broker_id` in the path) wires it up, gated by `get_current_user`
alone - no new `Permission`, no `require_broker_access` - and registered
in `main.py`.

Reason, on the four judgment calls this phase asked for:
- Why SMA(20) crossover, and why hard-coded not pluggable: it is the
  simplest strategy that still exercises every part of the pipeline this
  phase exists to prove (a real signal, a real risk-gated proposal, a
  real fill) without inventing a second, competing definition of "the
  strategy" the way a config-driven or LLM-authored strategy would. A
  pluggable strategy interface is real, useful future work - explicitly
  not built here: half-building one (e.g. an abstract `Strategy` Protocol
  with exactly one implementation) would add indirection with nothing yet
  to justify it, the same "don't build unused parallel infra" judgment
  D019/D021 already made about a second LLM provider. `strategy.py`'s
  `generate_signals()` takes a `period` parameter mainly so its own tests
  can exercise the crossover math on a short, hand-computable series
  without needing 20+ closes - `engine.py` itself only ever calls it with
  the hard-coded `SMA_PERIOD = 20`, never a caller-supplied value.
- Why the strategy runs through the real Risk Engine instead of just
  computing raw returns: that is this phase's entire point per the brief
  - a backtest that only multiplied share counts by price deltas would
  prove nothing about whether the Risk Engine behaves sanely over a real
  historical sequence, which is the actual open question after D004
  through D024 built the engine itself. Concretely, every BUY proposal
  in the live-verified AAPL.US run below was sized as "all available
  cash" and then genuinely blocked by `EXCEEDS_MAX_POSITION_SIZE` -
  `engine._attempt_trade()` retries exactly once at the engine's own
  `max_quantity_allowed` (a caller-side policy choice, not the engine
  resizing itself - `RiskDecision.max_quantity_allowed`'s own docstring is
  explicit that the engine never does that) and if that retry is also
  rejected, the signal is simply skipped for that day. A SELL proposes
  the entire held quantity and is not capped by
  `max_position_pct_of_equity` (that limit governs how large a *new*
  position may become, not how much of an *existing* one may be closed);
  it still passes through every other check unchanged.
- Why `require_stop_price=False` for this backtest's `RiskLimits`, unlike
  every other trading path in this codebase: the SMA-crossover strategy's
  exit rule *is* the SELL signal itself, not a stop price - it has no
  independent stop-loss concept to supply a real value for. Setting
  `require_stop_price=True` here would force `engine.py` to invent a stop
  distance with no grounding in the strategy, which is exactly the kind
  of fabrication `docs/TRADING_SAFETY.md` forbids in spirit even though
  the letter of the rule is about prices, not risk parameters. Every
  other limit (`max_position_pct_of_equity`,
  `max_portfolio_exposure_pct_of_equity`, `max_risk_pct_of_equity_per_trade`,
  `max_market_data_age_seconds`, `duplicate_order_window_seconds`) is read
  from `Settings` unchanged, same values a real paper trade would be
  gated by.
- Why `POST /backtests` needs no `broker_id` and no specific `Permission`:
  a backtest never reads or writes any broker's row - there is no
  resource to scope a grant to, unlike `VIEW_PORTFOLIO`/
  `SUBMIT_PAPER_TRADE` which both name a specific broker's money or
  positions. It also never touches real capital by construction (see the
  in-memory-only `PaperBrokerAdapter` point above), so unlike those two
  permissions a coarse "any authenticated, active user" gate
  (`get_current_user` alone) is proportionate - adding a dedicated
  `Permission.RUN_BACKTEST` would be a permission that gates nothing a
  plain authenticated check doesn't already gate equally safely, the same
  "don't add a permission that does nothing" judgment `Permission.ADMIN`'s
  own docstring makes about coarse-vs-fine-grained permissions (D013).
Alternatives: (a) a pluggable/configurable strategy (parametrized SMA
periods, multiple strategies, a strategy registry) - rejected as
explicit future work per the brief's own instruction not to half-build
this; noted above. (b) let the caller supply an explicit stop_price per
backtest request - rejected, there is nowhere in an SMA-crossover
strategy for a user-supplied stop to come from without turning "one
hard-coded strategy" into "one hard-coded strategy plus one user-supplied
parameter that changes its risk behavior," which is scope creep toward
(a). (c) gate `POST /backtests` behind `SUBMIT_PAPER_TRADE` (reusing the
existing trading permission) - rejected, a backtest is strictly weaker
than submitting a paper trade (no broker state changes at all) and
requiring a trading permission for a read-only simulation would be
backwards, forcing a report-only user to hold trade rights they don't
need, the same reasoning `VIEW_PORTFOLIO`'s docstring gives for being
separate from `SUBMIT_PAPER_TRADE`. (d) let `HistoryProvider` gain a
`get_daily_closes_between(symbol, start, end)` method so an arbitrary
historical window could be served directly - rejected for this phase:
`HistoryProvider` is a `marketdata/` capability shared by D021's
indicator wiring, and this worktree's assigned scope is
`apps/api/app/backtesting/` plus directly-related tests/docs; extending a
shared Protocol other worktrees might also be touching is exactly the
kind of change that scope boundary exists to prevent. Noted below as the
natural next step for whoever picks this up.
Consequences: `end_date` in `BacktestRequest` must equal today (UTC) - a
request for an arbitrary past window (e.g. "backtest Q1 2024") gets a
clear 400 `UNSUPPORTED_DATE_RANGE`, not a silently mislabeled result,
because `HistoryProvider.get_daily_closes(symbol, count)` (D021) only
ever returns "the most recent `count` closes as of now" with no
timestamps attached - there is no way to honestly serve a historical
window that doesn't end at the present without either fabricating dates
on real prices or extending a Protocol outside this phase's scope (see
alternative (d) above). Given that constraint, `start_date`/`end_date`
are used only to size the `HistoryProvider` request (weekday count
between them, plus a fixed 20-day SMA warmup) and to label the returned
closes with best-effort trading-day dates (`engine._label_trading_days`,
skipping weekends only - it does not know about market holidays, so a
date label can be off by a day or two around one; the *prices* themselves
are always exactly what the vendor returned, in order, never interpolated
or padded). `InsufficientHistoryError` fires when the vendor has fewer
closes than the requested window plus warmup requires - never a shorter,
silently-truncated backtest. This is the single most significant scope
note for whoever extends this phase: a real date-range backtest (e.g.
"AAPL.US from 2023-01-01 to 2023-06-30") needs `HistoryProvider` itself
to grow a timestamped, arbitrary-window query capability first; that is
`marketdata/` work, explicitly out of scope here, not solved by this
phase.
Status: Implemented, tested: `tests/backtesting/test_metrics.py` (10
unit tests - total return, max drawdown, and win rate every hand-computed
in the test's own comments, including empty/single-point/all-flat edge
cases); `tests/backtesting/test_strategy.py` (6 unit tests - a fully
hand-derived SMA(3) crossover sequence showing HOLD/BUY/SELL/HOLD/SELL
across a 7-close series, a too-short series, a warmup period, and a
perfectly flat series that never signals); `tests/backtesting/test_engine.py`
(5 unit tests against a fake `HistoryProvider` - a fully hand-computed
single round trip through the real Risk Engine and real
`PaperBrokerAdapter` showing the exact equity at every day including the
`EXCEEDS_MAX_POSITION_SIZE`-then-retry sizing math, a flat series
producing zero trades and zero return, `InsufficientHistoryError` on too
little history, `UnsupportedDateRangeError` on a non-today `end_date`,
and equity-curve dates spanning only the requested window, not the
warmup); `tests/api/test_backtests.py` (7 integration tests against real
Postgres and a fake `HistoryProvider` - 401 with no token, 400
`NOT_CONFIGURED` with no history provider configured, a successful
backtest run by a user whose role grants *no* permissions at all
(proving the deliberate no-`Permission` gate), 400 `DATA_UNAVAILABLE` on
insufficient history, 400 `UNSUPPORTED_DATE_RANGE` on a non-today
`end_date`, 502 `DATA_UNAVAILABLE` on a failing vendor, and 422 on
`end_date <= start_date`). 202/202 total tests passing (the 14 tests
between D024's 159 and this phase's own 21 backtesting-unit + 7
backtests-API tests belong to the sibling phase-24/25 worktrees' work,
not this one), ruff+mypy clean (67 source files).
Verified live: against a running server (real Postgres via this
worktree's own `docker compose`, remapped to host ports 5433/6380 to
avoid a port clash with the already-running phase-25 worktree's
containers, `uvicorn` run directly on port 8023 to avoid the sibling
worktrees' already-bound 8000) with real Longbridge paper-trading
credentials (`Longbridge-MCP-Server/API Keys.env`) and a directly-inserted
test user (bcrypt hash, same pattern as D021/D024): startup logged
`history_provider=longbridge`; `POST /backtests` for `AAPL.US` over the
most recent ~45-day window against real historical daily closes returned
a genuine, non-trivial result - a 34-point equity curve that starts flat
through the SMA(20) warmup-adjacent early days, one real trade filled on
2026-08-20 (sized down from an all-cash proposal to a Risk-Engine-capped
quantity, exactly the `EXCEEDS_MAX_POSITION_SIZE`-then-retry path the
unit tests exercise with fake data), a small drawdown, and equity
recovering by 2026-08-28 - `num_trades=1`, real dollar figures
throughout, nothing fabricated. All inserted rows (the one test user)
were deleted afterward; the `uvicorn` process, `.venv23`, and `.env`
(including the real credentials) created for this verification were
removed; `docker compose down -v` tore down this worktree's Postgres/
Redis containers and volume; `docker-compose.yml`'s port mappings were
restored to their committed values (the 5433/6380 remap was a
local-only, never-committed change for this verification pass).

---

**D027 — Persisted portfolio snapshots: a child positions table, manual capture only, same VIEW_PORTFOLIO gate**
Date: 2026-08-28
Decision: Two new append-only tables (migration `0008_portfolio_snapshots.py`,
models `PortfolioSnapshotRow`/`PortfolioSnapshotPositionRow` in
`apps/api/app/db/models.py`) close the gap D022 explicitly flagged -
"future backtesting/alerts/performance-attribution work" needs persisted
history, not just a point-in-time `compute_portfolio_snapshot()` read.
`portfolio_snapshots` holds one row per capture (`broker_id`,
`captured_at`, `cash`, `total_equity`, `total_unrealized_pnl`,
`total_realized_pnl`); `portfolio_snapshot_positions` holds one row per
open position in that capture (`snapshot_id` FK, `symbol`, `quantity`,
`avg_cost`, `current_value`, `unrealized_pnl`, `realized_pnl`). Both are
append-only like `orders`/`fills` - a historical record is never updated
or overwritten, unlike the mutable `broker_accounts`/`broker_positions`
current-state rows D022 explicitly declined to add more of.
`POST /brokers/{broker_id}/portfolio/snapshots`
(`apps/api/app/api/routes/portfolio.py`) takes the same `{"marks": {...}}`
body as the existing GET, calls `compute_portfolio_snapshot()` exactly as
the read endpoint does, and persists the result as one new
`PortfolioSnapshotRow` plus its `PortfolioSnapshotPositionRow` children in
a single commit. `GET /brokers/{broker_id}/portfolio/history` returns
persisted snapshots for a broker ordered oldest-to-newest by
`captured_at`, paginated with `limit` (default 50, max 500) and `offset`
(default 0) query params. Both routes are gated by the existing
`Permission.VIEW_PORTFOLIO` plus `require_broker_access`, same as the
read endpoint - no new permission was added.
Reason: a child table (`portfolio_snapshot_positions`), not a JSON column
on `portfolio_snapshots`, was chosen for the per-position detail. This is
a genuine judgment call the phase's task explicitly asked not to
default-away, so the actual reasoning: a JSON column would be simpler to
write today (one row, one nested value, no join), but this schema already
treats "per-symbol history over time" as something worth its own indexed
table everywhere else it appears - `orders`/`fills` are exactly that
pattern for the trade side, and `docs/DECISIONS.md`'s own framing of this
phase ("show me AAPL's position history") is precisely the query a JSON
column makes hard: it would require either scanning every snapshot row
and JSON-parsing its blob in application code, or a Postgres JSON-path
index that duplicates most of a real table's value while still being
harder to reason about and to extend (e.g. adding a new per-position
column later is a plain `ALTER TABLE` on a child table, versus an
application-level migration of every stored JSON blob). The extra join
this costs on `GET .../history` is one `selectinload()` on a table that,
by design (D027 below - manual capture only, no scheduler), grows at a
caller-triggered, human-timescale rate, not a high-frequency one - the
join cost is not a real concern at this table's expected size. `VIEW_PORTFOLIO`
(not a new, stronger permission) was chosen to gate `POST .../snapshots`
because creating a persisted record of the portfolio's current state
doesn't move money, change any mutable state (`broker_accounts`/
`broker_positions` are untouched), or place any order - it's exactly the
same category of action `GET /brokers/{broker_id}/portfolio` already
performs (D022's reasoning: viewing is strictly weaker than trading), it
just additionally writes a timestamped copy of what was viewed. Requiring
a stronger permission here would be inconsistent with D022's own
least-privilege argument for why `VIEW_PORTFOLIO` exists as its own
permission in the first place - the read/write distinction that matters
in this codebase is "does this touch capital or orders," not "does this
write any row at all." This phase is deliberately *not* an automatic or
scheduled snapshotting system: no cron, no background job, no
`APScheduler`/Celery-beat-style component was added. A snapshot is
created only when a caller explicitly `POST`s to `.../snapshots` - see
Consequences for why scheduled capture is left as explicit future work
rather than half-built here.
Alternatives: (a) a JSON column on `portfolio_snapshots` for position
detail - rejected per the reasoning above; noted as the "simpler to build
now" option the task explicitly warned against defaulting to. (b) a
stronger new permission (e.g. `CREATE_PORTFOLIO_SNAPSHOT`) distinct from
`VIEW_PORTFOLIO` for the POST route - rejected, this phase found no
capability difference between "compute and return a snapshot" and
"compute, return, and also persist a snapshot" that rises to D013/D016's
bar for a genuinely distinct permission (unlike view-vs-trade, which
D022 correctly treated as distinct); adding one anyway would just be
permission-per-endpoint rather than permission-per-capability. (c) build
a scheduled/automatic snapshot job in this same phase since the future
need was already visible - rejected, out of scope as stated by the task,
and a half-built scheduler (no retry/backoff policy, no decision on
snapshot frequency, no interaction with `TRADING_MODE`) would be worse
than clearly deferring it; see Consequences.
Consequences: automatic/scheduled snapshotting (e.g. hourly or
daily-close capture per broker) is explicit future work, not started
here - it would need its own design pass (a scheduler component, a
frequency policy, backoff/retry on a failed `MissingMarkError` capture,
and a decision on whether a scheduled capture skips a broker with no
fresh marks available or fails loudly) that this phase's scope
deliberately excludes. Consumers named in D022's original gap
(backtesting, alerting, performance attribution) can now read a real
time series via `GET .../history` instead of having no persisted source
at all, but each of those remains its own future phase - this phase only
ever writes rows, it does not yet read them for any of those purposes.
`portfolio_snapshot_positions` rows are never fabricated or interpolated:
a snapshot POST that hits `MissingMarkError`/`BrokerAccountNotFoundError`
persists nothing at all (the DB write happens only after
`compute_portfolio_snapshot()` returns successfully), so a broker with
incomplete marks has a gap in its history rather than a guessed data
point - the same fail-closed posture `docs/TRADING_SAFETY.md` requires
everywhere else.
Status: Implemented, tested: `tests/api/test_portfolio.py` gained 8 new
integration tests against real Postgres (23 total in that file) - POST
persists a snapshot and it appears correctly in GET .../history; three
POSTs return in oldest-to-newest order and `limit`/`offset` paginate
correctly; a missing mark on POST is 400 `DATA_UNAVAILABLE:` and persists
nothing (confirmed via a follow-up empty history read); POST and GET
.../history each 403 without `VIEW_PORTFOLIO`; POST 403s without a
broker grant even with the permission (mirroring D022's existing grant
tests). `tests/api/test_trades.py`'s shared `paper_broker_row` fixture
was extended to also clean up `portfolio_snapshot_positions`/
`portfolio_snapshots` rows on teardown (a foreign-key violation surfaced
this gap immediately - the fixture's existing broker-teardown delete list
did not yet know about the new tables). 188 total tests passing
(180 before this phase + 8 new), ruff and mypy both clean (58 source
files). Verified live against a running Docker Compose stack (real
Postgres + the built API image): seeded a role/user/broker/grant via
direct SQL insert (D021/D022/D024 pattern, bcrypt-hashed password),
logged in for a real JWT, submitted a real paper trade (`AAPL` x10 @
100), `POST`ed a real snapshot with a real mark (`AAPL`: 120) - response
showed the real position (`avg_cost=100`, `current_value=1200`,
`unrealized_pnl=200`, `total_equity=100200`) - then `GET .../history`
and confirmed both the empty pre-trade snapshot and the post-trade
snapshot came back in the correct order with the persisted values
matching exactly what the POST had returned. All seeded rows, the Docker
stack (`docker compose down -v`), the local `.venv25`, and the local
`.env` were removed after verification.

---

**D026 — Frontend v3: portfolio view, GET-with-body proxied via Node's `http`/`https` module instead of `fetch`**
Date: 2026-08-28
Decision: Built the "portfolio view" v2/v3 candidate D020/D023 both
deliberately deferred, in `apps/web/`, against Phase 19/D022's
`GET /brokers/{broker_id}/portfolio`. `PortfolioView`
(`components/PortfolioView.tsx`) on `/dashboard` takes a broker UUID and
a `SYMBOL=price, SYMBOL2=price2` marks string (same parsing pattern as
`AgentTradeForm`'s marks input, D023), and renders the real
`PortfolioSnapshot`: `cash`, a table of every position's
`symbol`/`quantity`/`avg_cost`/`current_value`/`unrealized_pnl`/
`realized_pnl`, and the three totals
(`total_equity`/`total_unrealized_pnl`/`total_realized_pnl`) — a
single real-time snapshot, deliberately not a historical chart (that
needs Phase 25's persisted snapshots, out of scope here regardless of
whether that work has landed by the time this is read). Backed by
`app/api/portfolio/[brokerId]/route.ts`, exposed to the browser as a
same-origin `POST` (matching every other authenticated route handler's
shape: cookie read server-side, `Authorization: Bearer <token>`
attached, real backend status/body passed through unchanged) that
internally issues the real backend call as a `GET` carrying `marks` as a
JSON body, exactly as D022 specifies. That internal call could not use
this repo's established `fetch`-based proxy pattern: Node's built-in
`fetch` (undici) throws `Request with GET/HEAD method cannot have body`
for any GET carrying a body, unconditionally, per the Fetch spec — no
override flag exists. `lib/backend.ts` gained `backendGetWithBody()`,
which bypasses `fetch` for this one call and issues the request with
Node's core `http`/`https` module directly (verified against the
installed Node runtime by direct reproduction — see Consequences); the
FastAPI/Starlette backend itself has no such restriction and accepts a
GET-with-body exactly as documented.
Reason: the httpOnly-cookie proxy shape itself is not a new choice — it's
D020's established pattern, executed. The GET-with-body plumbing was the
only real decision here. Alternatives considered below explain why a
raw Node request, not a different HTTP method or a bundled HTTP client,
was the right fix.
Alternatives: (a) have the route handler call the backend with `POST`
or put `marks` in the query string instead of a body-carrying GET —
rejected: this frontend proxies the backend's actual contract, and D022
explicitly chose a GET-with-body over a query string because
`dict[symbol, price]` doesn't serialize cleanly as query params; the
frontend re-deciding that tradeoff on its own would silently diverge
from the one real contract this endpoint has. (b) pull in a third-party
HTTP client (axios, node-fetch's older non-spec-compliant behavior,
undici's lower-level `request()` API) — rejected in favor of Node's
built-in `http`/`https` module: zero new dependencies, and the need is
narrow enough (one call site) that a bespoke ~30-line Promise wrapper is
easier to audit than a new package's full surface area. (c) special-case
this one form to send the browser's own body as query params or a
different shape than every other proxied form — rejected as inconsistent
with the client-side pattern every other component here follows (POST a
JSON body to a same-origin route handler; let the route handler carry
the shape mismatch, if any, entirely server-side, invisible to the
browser).
Consequences: `npm run build` passes with zero TypeScript errors
(confirmed via the actual build output). `npm test` (Vitest): 34/34
passing — the existing 28 plus 6 new tests in
`test/PortfolioView.test.tsx`, covering a real rendered snapshot (cash,
one position's full field set, and all three totals), an empty-positions
render, the real 400 `DATA_UNAVAILABLE:` sentinel for a missing mark,
a real 403, a real 404, and network failure — same
sentinel-vs-fabrication discipline as every prior frontend phase.
Verified against a real running backend: `docker compose up -d --build`
in this worktree, with the same transient host-port remapping D023 used
to avoid colliding with sibling Phase 23/25 worktrees' Postgres/Redis
containers (5433/6380 and 5432/6379 respectively were already taken;
this pass ran Postgres on 5434, Redis on 6381, the API on 8001 — restored
to `docker-compose.yml`'s original 5432/6379/8000 afterward, since this
remapping was never meant to be a tracked change). Ran real Alembic
migrations (`alembic upgrade head`, 0001 through 0007) against a fresh
database — no tables existed before this pass ran them. Bootstrapped two
users via direct SQL insert (bcrypt hash via
`apps/api/app/auth/security.py:hash_password`, same pattern as D021/D023):
one with a role holding both `trade:submit:paper` and `portfolio:view`
plus a `BrokerGrant` on a SQL-inserted paper broker, one with
`trade:submit:paper` only (no `portfolio:view`) and the same grant, to
exercise the 403 path deliberately. Submitted a real trade via
`POST /brokers/{id}/trades` directly against the backend
(`estimated_price`/`stop_price` supplied by hand since no market data
vendor is wired in this environment) that filled for real
(`fill_price: "100"`, `fill_quantity: "10"`), so the portfolio actually
held a position. Then, through `npm run dev` + curl against the app's
own `/api/auth/login` and `/api/portfolio/[brokerId]` route handlers —
never calling the backend directly for this half of the verification —
confirmed: a real 200 rendering the exact filled position
(`avg_cost: "100.00000000"`, `current_value: "1200.00000000"`,
`unrealized_pnl: "200.0000000000000000"`) and totals
(`total_equity: "100200.00000000"`) computed by the backend from a mark
of `120`; a real 400
`DATA_UNAVAILABLE: No mark supplied for open position 'AAPL.US'; cannot
value account.` when the mark was omitted for that held position; a real
404 `No broker with id ...` for a broker UUID that doesn't exist; a real
401 `Not authenticated` with no session cookie; and, with the
second seeded user's cookie, a real 403
`Missing required permission: portfolio:view`. Every one of these five
response bodies came from the real backend through the real proxy route,
not a mock — the most complete live round-trip this repo's frontend
phases have run to date (D020's original pass could not reach Docker at
all; D023's re-verification covered success/403/404/409 for admin
resources but not this endpoint's distinct 400 sentinel).
Status: Implemented and verified as above.
v3/v4 candidates still open, not built in this pass: a historical
performance chart (blocked on Phase 25's persisted snapshots landing and
being documented in docs/API.md — explicitly out of scope here per the
Phase 24 task even if that work has landed by the time this is read), a
positions/broker-holdings listing UI to discover broker IDs without
knowing them in advance (same D013-rooted gap D023 already flagged for
users/roles/grants — no listing endpoint exists), session refresh/expiry
UX, and Playwright e2e coverage (unchanged reasoning from D020/D023).

---

**D028 — Full post-merge backend verification (Phases 23/24/25): one real bug found and fixed, test-count placeholder corrected**
Date: 2026-08-29
Decision: Ran a from-scratch backend verification of the fully-merged
main branch (fresh venv, `ruff check .`, `mypy apps`, real Postgres/Redis
via `docker compose`, `alembic upgrade head`, `pytest tests/ -q`) rather
than trusting docs/IMPLEMENTATION_STATUS.md's Phase 25 self-reported
"210 tests" placeholder. Fixed `apps/api/app/backtesting/engine.py`:
`run_backtest`'s default `today` (used when no `today` is explicitly
passed) is now rolled back to the most recent Mon-Fri date via a new
`_most_recent_trading_day()` helper, instead of using the raw calendar
date from `datetime.now(UTC).date()`.
Reason: D025's `end_date == today` check (`UnsupportedDateRangeError`)
compared the request's `end_date` against the literal current calendar
date. On any Saturday or Sunday, `today` is not a trading day, so no
`end_date` a caller could legitimately supply (all of which are meant to
be trading days) could ever match it — `POST /backtests` rejected every
request with `UNSUPPORTED_DATE_RANGE` on weekends, a real cross-phase
bug invisible in Phase 23's own verification (which happened not to run
on a weekend) and exposed by running this pass on a Saturday
(2026-08-29). All five call sites in `tests/backtesting/test_engine.py`
already pin an explicit weekday `today=date(2026, 8, 26)`, so they were
unaffected by both the bug and the fix; only the real-clock default path
was wrong.
Alternatives: relaxing the equality check to accept any `end_date` up to
today — rejected, that would silently let a caller ask for a stale window
past the actual most-recent trading day, which is exactly the mislabeled-
data risk `UnsupportedDateRangeError`'s docstring (docs/DECISIONS.md
D025) exists to prevent. Hardcoding a weekday check into the route layer
instead of the engine — rejected, `run_backtest` already owns "what is
today" for this module and is the one place both the route and every
test call through.
Consequences: `POST /backtests` now behaves identically on every day of
the week, not just Monday-Friday plus a lucky non-weekend `pytest` run.
`ruff check .` and `mypy apps` both stayed clean; the four seemingly
unrelated failures this fix corrected (`test_backtests.py`'s three D025
integration tests) were this bug, not flakiness — a fourth failure in
`tests/test_config.py::test_missing_jwt_secret_key_fails_closed` was
independently traced to this pass's own shell having sourced a local
`.env` into `os.environ` (a verification-harness artifact, not a code
bug) and reproduced as a pass once that leak was removed. Final count:
208 tests, all passing, ruff+mypy clean — corrects
docs/IMPLEMENTATION_STATUS.md's prior 210-test placeholder estimate for
the Phase 23/24/25 merge to this run's actual collected total. Docker
containers/volumes, the test-only `.env`, and the verification venv were
all removed afterward; nothing from this pass was left running.
Status: Implemented and verified as above.

---

**D029 — Trade-path Portfolio Manager: a deterministic, no-LLM allocation gate between the Risk Engine and the broker**
Date: 2026-08-29
Decision: Built `apps/api/app/portfolio_manager/` — the "Portfolio Manager"
box in docs/ARCHITECTURE.md's Target diagram and the "Portfolio Decision"
step in docs/TRADING_SAFETY.md's pipeline. It is a pure function,
`decide(proposal, portfolio, limits) -> PortfolioDecision`, with no LLM, no
network call and no I/O, mirroring `apps/api/app/risk/engine.py`'s own
zero-I/O discipline. It is deliberately NOT the read-only reporting module
`apps/api/app/portfolio/` (D022/D027), which never touches the trade path;
the two are separate packages with separate purposes and no shared code.

Placement: `submit_trade()` (`apps/api/app/oms/service.py`) now calls it
AFTER `evaluate_trade()` has approved a proposal and BEFORE the broker
call. Two properties are load-bearing and both have dedicated tests:
(1) a risk-rejected proposal short-circuits and the Portfolio Manager never
sees it — it is a second, narrower gate, never a way around the first;
(2) when it returns MODIFY, the resized proposal is passed back through
`evaluate_trade()` before any broker call, so no quantity this system
produces — from an LLM, a human, or the Portfolio Manager itself — reaches
a broker without the deterministic Risk Engine having approved that exact
quantity. `apps/api/app/risk/engine.py` was not modified at all.

Scope: spec §18 lists ten considerations (positions, cash, exposure,
correlation, sector concentration, portfolio volatility, expected risk,
expected return, drawdown, diversification) and four outcomes (APPROVE,
REJECT, MODIFY, REQUEST_MORE_RESEARCH). This implementation covers exactly
the three constraints computable from data this repo actually stores, and
names each honestly:

- `SYMBOL_CONCENTRATION` — post-trade market value of one symbol as a
  share of equity (default 25%).
- `CASH_RESERVE` — post-trade cash as a share of equity (default 5%).
- `MAX_OPEN_POSITIONS` — count of distinct held symbols (default 20),
  labelled as a position count, not as a diversification measure.

Correlation, sector concentration, portfolio volatility, expected return
and drawdown are NOT implemented: `assets` carries no sector or
classification column, and no persisted price series exists that a
covariance or a volatility could be computed from (D027's snapshots are
portfolio-level, not per-symbol return series, and are written only on an
explicit API call — there is no scheduler). Approximating any of them from
data that does not exist would be fabrication (docs/TRADING_SAFETY.md), so
they are deferred rather than guessed. `REQUEST_MORE_RESEARCH` exists in
the `PortfolioAction` enum for spec fidelity but is never emitted —
"this needs more research" is inherently a discretionary judgement, and
this component is deliberately deterministic; a test pins that promise.
Reason: the concrete gap this closes is real and no existing component
covers it. The Risk Engine's `max_position_pct_of_equity` measures ONE
order's notional; a series of individually compliant orders can therefore
accumulate an aggregate position no per-trade check ever sees. Likewise
`INSUFFICIENT_BUYING_POWER` only asks whether cash covers one notional at
all, never whether a reserve survives across all of them. Those are
portfolio-construction questions, structurally outside a per-trade gate —
which is exactly why the spec has a separate component for them and why
this logic must not be pushed into `evaluate_trade()`.
A second, subtler rule: a failing check only BLOCKS when the trade
worsens that measure (`PortfolioCheck.worsened_by_trade`). A portfolio
that is already over-concentrated must not have the de-risking sell that
relieves it refused because of the very breach it relieves. Failing-but-
not-binding checks are still recorded in the audit trail, so the state is
visible rather than silently ignored.
Alternatives: (a) put the portfolio checks inside `evaluate_trade()` —
rejected outright: docs/TRADING_SAFETY.md makes the Risk Engine the
load-bearing boundary, and growing it into a portfolio-construction engine
would blur the one component whose job is to be small, pure, and
independently auditable. (b) Run the Portfolio Manager BEFORE the Risk
Engine — rejected: spec §18 says it receives the Trade Proposal *plus the
Risk Report*, and both the Target diagram and the TRADING_SAFETY pipeline
put it below the Risk Engine. Running it first would also mean a proposal
the Risk Engine would have killed still consumed portfolio reasoning.
(c) Have the Portfolio Manager write the resized order straight to the
broker — rejected as the whole point of (2) above. (d) Make it an LLM
(the upstream TradingAgents project's Portfolio Manager is one, per
../ARCHITECTURE-DISCOVERY-REPORT.md) — rejected: spec §62 and
docs/TRADING_SAFETY.md both say position sizing is never an LLM's output.
(e) Approximate correlation with a naive same-prefix symbol heuristic, or
volatility from the handful of `portfolio_snapshots` rows — rejected as
fabricated risk numbers dressed as real ones. (f) Add a market-data
dependency to fetch return series inside the component — rejected: it
would break the zero-I/O property that makes this testable and makes it
work when every vendor is down. (g) Fail closed when no portfolio state is
supplied — rejected: skipping this component cannot make a trade *less*
safe (the Risk Engine already gated it, and this component can only shrink
or stop a trade), so `portfolio=None` simply restores pre-D029 behaviour
for the pure unit-test callers. The HTTP trade path always supplies it.
Audit record: spec §18 requires a complete one. `PortfolioDecision` carries
every constraint evaluated (pass or fail, with projected value, limit, and
whether the trade worsened it), and migration `0009` adds four nullable
columns to `orders`: `portfolio_action`, `portfolio_binding_constraint`,
`portfolio_detail`, `portfolio_requested_quantity`. `orders.quantity` now
records the quantity actually acted on, so a resize is visible as
`quantity != portfolio_requested_quantity` rather than as a silently
rewritten proposal. A null `portfolio_action` means "no Portfolio Manager
ran" (risk-rejected first, or no state supplied) and must never be read as
approval — documented on the column and pinned by a test.
Consequences: `TradeSubmissionResponse` gains four fields, so
`approved: true` with `status: "rejected"` is now a real and meaningful
combination (the Risk Engine passed it, the Portfolio Manager did not) —
documented on the `approved` field. `apps/web/` was not updated to render
the new fields; the frontend still shows the risk verdict only, which is
an honest gap, not a claim. `apps/api/app/backtesting/` calls
`evaluate_trade()` directly rather than `submit_trade()`, so backtests are
unchanged by this phase and do NOT model portfolio-level constraints —
also an honest gap worth closing later.
26 new tests, 234/234 total passing, `ruff check .` and `mypy apps` both
clean (66 source files). Breakdown: 13 pure unit tests
(`tests/portfolio_manager/test_manager.py`), 5 OMS-wiring tests
(`tests/oms/test_service_portfolio_manager.py`, including one that proves
the MODIFY re-gate actually catches something — the resized quantity trips
the D024 duplicate-order check that the original quantity did not, which
is the one risk check that is genuinely not monotone in quantity), 4
real-Postgres persistence tests
(`tests/db/test_portfolio_decision_persistence.py`), and 4 real-Postgres
HTTP integration tests (`tests/api/test_trades_portfolio_manager.py`).
Verified live, not only in tests: brought up this worktree's own docker
stack (`docker compose -p trading-os-phase26` with host ports remapped to
5435/6382/8002 via a throwaway override file, to avoid colliding with
sibling Phase 27/28 worktrees; the tracked `docker-compose.yml` was never
edited), ran real Alembic migrations `0001`→`0009` against a fresh
database, seeded a role/user/paper-broker/grant, and drove the real
containerized API over HTTP with curl. Observed, in order, on one real
broker: two approved buys (100 and 99 shares at 100, `portfolio_action:
"approve"`); a third 98-share buy returning `portfolio_action: "modify"`,
`portfolio_binding_constraint: "symbol_concentration"`,
`portfolio_requested_quantity: "98"`, `fill_quantity: "51"` with the
detail `Quantity reduced from 98 to 51 to satisfy symbol_concentration.`;
then a 1-share buy at the exact 25% cap returning `status: "rejected"`,
`approved: true`, `block_reason: null`, `portfolio_action: "reject"`; then
a 50-share de-risking SELL correctly approved despite the position sitting
at the cap. Confirmed in Postgres directly that the modify row persisted as
`quantity 51.00000000` / `portfolio_requested_quantity 98.00000000`, that
`broker_positions` held 250 shares and `broker_accounts` 75,000 cash
(exactly the resized trade's arithmetic, not the requested one), and that
the risk-rejected rows carry a null `portfolio_action`.
Not verified live: no real market-data vendor or LLM provider was wired in
this environment, so every live price above was caller-supplied; the
agent-trades route shares `_execute_trade()` and therefore the identical
Portfolio Manager path, but was exercised only through its existing
fake-`LLMProvider` tests, not against a real LLM. `apps/web/` was not run
at all this phase. Docker containers, volumes, the throwaway compose
override, the test-only `.env` and the verification venv were all removed
afterward.
Status: Implemented and verified as above.

---

**D031 — Admin listing endpoints: close D013's no-listing scope cut, read-only and paginated on the same limit/offset convention as D027**
Date: 2026-08-29
Decision: Added `GET /admin/users`, `GET /admin/roles`, and
`GET /admin/broker-grants` to `apps/api/app/api/routes/admin.py`. All
three are read-only, carry no new permission (they inherit the router's
existing `require_permission(Permission.ADMIN)` dependency, so they are
gated exactly as every write route in that file already is), and
paginate with `limit` (default 50, max 500) / `offset` (default 0) —
byte-for-byte the same convention and the same default/ceiling values as
Phase 25/D027's `GET /brokers/{id}/portfolio/history`, so this codebase
has one pagination story rather than two. Each listing has a fixed,
total sort so `offset` paging is deterministic: users by `email`, roles
by `name`, grants by `(user_id, broker_id)`. Rows reuse the existing
`CreateUserResponse`/`CreateRoleResponse`/`BrokerGrantResponse` DTOs
rather than defining parallel "list row" models.
Reason: D013 deliberately shipped no listing endpoints, and D023/D026
both re-flagged the consequence: the admin UI could create a user, a
role, or a grant, but `PATCH /admin/users/{id}` and
`DELETE /admin/broker-grants/{grant_id}` need an id the operator had no
way to discover through the product — the only route to one was a direct
DB query. That made the D016 update/deactivate surface effectively
unusable for anyone not holding a psql prompt. Reusing the existing
response DTOs is the security-relevant half of the decision: a separate
list-row model is where a `hashed_password` field eventually gets added
by accident, whereas `CreateUserResponse` is already the model that
deliberately has no such field (see its docstring).
Alternatives considered: a single `GET /admin` aggregate returning all
three collections — rejected, it would force an unbounded response or a
three-way pagination scheme, and the frontend refreshes the three lists
independently anyway. Filter/search params (`?email=`, `?user_id=`) —
rejected for this pass as scope creep: a caller pages rather than
queries, and adding filters later is additive and non-breaking, whereas
shipping a half-designed query grammar now is not. Cursor pagination —
rejected, it would diverge from D027's offset convention for a dataset
whose realistic size is dozens of rows.
Consequences: `admin:manage` now reads as well as writes; a compromised
admin token can enumerate every email in the system, which it could
already do indirectly via the 409-on-duplicate-email path, so this
widens convenience rather than the trust boundary. Still deliberately
absent: listing endpoints for brokers, orders, or fills (nothing in this
phase needs them), and any way to delete a user or role (unchanged from
D016). Developed in a worktree in parallel with sibling phases 26 and
27; originally numbered D030 in that worktree, renumbered to **D031** at
merge time because sibling Phase 27 independently also claimed D030 for
its own decision (docs/DECISIONS.md's automatic-snapshotting entry) —
both worktrees branched from the same D028 base and picked their own
headroom number, so one had to yield at merge.
Verified: 12 new integration tests in `tests/api/test_admin_listings.py`
against real Postgres (real seeded rows appear; no password/hash field
on any row; two single-row pages reassemble into the two-row page in
order; `limit=501` and `offset=-1` are both 422; 403 for a non-admin and
401 for no token, on all three routes), plus real curl calls against the
running stack.
Status: Implemented and verified as above.

---

**D030 — Automatic/scheduled portfolio snapshots: an opt-in in-process asyncio task that sources real marks from MarketDataRouter and skips rather than fabricating**
Date: 2026-08-29
Decision: Added `apps/api/app/portfolio/scheduler.py` — a
`PortfolioSnapshotScheduler` started from the FastAPI lifespan
(`apps/api/app/main.py`) that runs `run_snapshot_cycle()` immediately on
start and then every `PORTFOLIO_SNAPSHOT_INTERVAL_SECONDS` (new setting,
default 3600). It is gated behind
`PORTFOLIO_SNAPSHOT_SCHEDULER_ENABLED` (new setting, **default false**).
Each cycle selects the brokers that have a `broker_accounts` row, reads
each one's nonzero `broker_positions` symbols, fetches a real quote for
every one of those symbols through the *existing* `MarketDataRouter`
(D015/D017), and — only if every symbol resolved — calls the same
`compute_portfolio_snapshot()` and the same persistence code the manual
`POST /brokers/{id}/portfolio/snapshots` route uses. That shared write
path was extracted this phase into
`apps/api/app/portfolio/persistence.py` (`persist_portfolio_snapshot()`),
and the route's response-shaping into a `_to_history_entry()` helper, so
the manual and scheduled paths cannot drift apart. No new pip dependency
was added and no new table or migration was needed.
Reason: `docs/PROJECT_CONTEXT.md`'s "Planned Work" listed automatic
snapshotting as the follow-on to Phase 25/D027's deliberately manual-only
capture. The hard problem is that D027's design leans on the *caller* to
supply `marks`, and a scheduler has no caller — so the obvious
implementation would have to invent a price, which
`docs/TRADING_SAFETY.md` and spec Sec57 forbid outright. Routing the
scheduler through `MarketDataRouter` resolves this without weakening
anything: the marks are real vendor quotes from the identical path
`GET /market-data/{symbol}/quote` and the omitted-`estimated_price` trade
path already use. When a real quote is unavailable, the broker is skipped
for that cycle and a typed `ScheduledSnapshotOutcome`
(`ScheduledSnapshotStatus.SKIPPED_MARKET_DATA_UNAVAILABLE` /
`SKIPPED_MARKET_DATA_NOT_CONFIGURED` / `SKIPPED_NO_BROKER_ACCOUNT` /
`SKIPPED_INCOMPLETE_VALUATION`) is returned and logged at **warning**
level with the exact unpriced symbols and the vendor's own
`NO_DATA_AVAILABLE:`/`NOT_CONFIGURED:` sentinel text. Skipping is
specifically right for an append-only table: a row silently missing one
holding's value would be indistinguishable, permanently, from a row where
that holding was genuinely closed. A gap is honest; a wrong row is not.
The default-off posture matches every other consequential switch in this
codebase (`LIVE_TRADING_ENABLED`, `EMERGENCY_STOP_ACTIVE`, the
Longbridge and LLM credential trios): this is the first unattended
behavior in the system that reads real broker state and writes real rows
on a timer with no human in the loop, so it should exist only where
someone asked for it.
Alternatives: **APScheduler / Celery / a cron sidecar** — rejected, and
deliberately *not* installed. The requirement is "await this coroutine
every N seconds while the process is up", which `asyncio.sleep` in a
lifespan-owned task expresses completely. Those libraries would buy cron
expressions, cross-restart persistence, and multi-worker leader election,
none of which this phase needs, and each would need its own configuration
surface, failure modes, and safety review before being allowed near real
broker state. Project policy is to ask before adding a new tool, and
since no such approval could be obtained mid-task, the correct default
was to not add one — which turned out to cost nothing, since the stdlib
covers the actual requirement.
**Falling back to `Settings.paper_broker_starting_cash` when a broker has
no `broker_accounts` row** (as `GET .../portfolio` does) — rejected for
the scheduler. A human asking for a live read of a never-traded broker
gets a clearly-labelled hypothetical; a scheduler writing that same
number into append-only history would be manufacturing a permanent record
of a cash balance no row ever asserted. Hence
`default_starting_cash=None` and `SKIPPED_NO_BROKER_ACCOUNT`.
**Persisting a partial snapshot with the priced positions only, or with a
`marks_incomplete` flag** — rejected: it invites exactly the silent
misreading described above, and every downstream consumer (performance
attribution, charts) would have to remember to honor the flag forever.
**Carrying the previous snapshot's mark forward for an unpriceable
symbol** — rejected outright; that is fabrication with extra steps.
**Snapshotting every row in `brokers`** — rejected in favor of "brokers
with a `broker_accounts` row", since that row is created lazily by the
execution layer (D014) and its presence is the existing signal that a
broker has real persisted state.
**Fanning out across brokers concurrently** — rejected at this scale;
quotes are already fetched concurrently *within* one broker
(`resolve_marks` uses `asyncio.gather`), and parallelizing across brokers
too would multiply DB connections and vendor rate-limit pressure for no
practical gain.
Consequences: The system now has its first background component. Two new
settings are documented above; with both at their defaults the runtime
behavior is byte-identical to Phase 25. Each API **worker process** runs
its own independent loop, so running more than one uvicorn/gunicorn
worker would multiply snapshot rows — the single clearest trigger for
revisiting the "no scheduler library" choice (leader election or an
external trigger would then be warranted). Snapshots are not backfilled
across a restart, and the interval is wall-clock, not market-hours-aware:
a scheduler left enabled overnight will keep recording unchanged
after-hours rows, or keep logging skips if the vendor returns nothing —
both deliberate, both future work rather than something to solve by
guessing a market calendar this repo doesn't have.
**Update (Phase 35, D042): the market-hours half of that is now
PARTIALLY closed — an enabled scheduler skips whole cycles on a UTC
Saturday or Sunday, before any DB query or vendor call, via
`apps/api/app/portfolio/market_hours.py`
(`PORTFOLIO_SNAPSHOT_MARKET_HOURS_GATE_ENABLED`, default true). It is a
weekend gate only: intraday after-hours and exchange holidays are still
NOT checked, deliberately, because doing so honestly needs a real
trading-calendar source rather than a hardcoded table. Read D042 before
assuming this is "market-hours aware".**
**Update (Phase 38, D047): the multi-worker consequence above is now
CLOSED. Each worker process still runs its own loop — the lifespan runs
once per worker — but before a cycle does any work it takes a
non-blocking Postgres session-level advisory lock
(`apps/api/app/portfolio/cycle_lock.py`,
`PORTFOLIO_SNAPSHOT_CYCLE_LOCK_ENABLED`, default true), so exactly one
worker per interval captures and the rest record the typed
`SnapshotCycleLockDecision.SKIPPED_LOCK_HELD` and skip. `uvicorn
--workers N` therefore produces one snapshot row per interval rather
than N. No leader-election or scheduler library was needed after all, so
D030's "no scheduler library" choice stands rather than being revisited;
see D047. With a single worker the lock is always acquired and the cycle
is unchanged.** `captured_at` is left
to the DB's `server_default=func.now()` for both paths so the app clock
can't reorder the series.
Numbering note: developed in a `phase-27-scheduled-snapshots` worktree in
parallel with sibling `phase-26` and `phase-28` worktrees, all three
branched from `main` at D028. This entry claims **D030** rather than the
next free D029 specifically to leave headroom for those siblings and
avoid a merge-time collision on the same number; the gap is intentional
and does not indicate a missing decision.
Verification: `ruff check .` clean, `mypy apps` clean (65 source files),
228/228 tests passing against real Postgres (208 pre-existing + 20 new:
8 unit in `tests/portfolio/test_scheduler.py`, 12 DB-backed integration
in `tests/api/test_snapshot_scheduler.py`). Live-verified end to end
against a real uvicorn process and a real Postgres in this worktree with
`PORTFOLIO_SNAPSHOT_SCHEDULER_ENABLED=true`,
`PORTFOLIO_SNAPSHOT_INTERVAL_SECONDS=10` and two seeded brokers, both
given real positions through the real `POST /brokers/{id}/trades` path:
the flat (bought-then-sold) broker was snapshotted automatically on every
cycle, and `GET .../portfolio/history` returned four genuine
scheduler-written rows (`cash: "100100.00000000"`,
`total_realized_pnl: "100.00000000"`, distinct `captured_at`s ~10s
apart), computed from the real fills; the broker still holding 10
`AAPL.US` was skipped on **every** cycle with
`portfolio_snapshot_skipped` /
`status: "skipped_market_data_not_configured"` /
`unpriced_symbols: ["AAPL.US"]`, and its history endpoint returned
`snapshots: []` — the no-fabrication guarantee observed live, not just in
tests. **Not verified live:** the router-supplied-mark capture path
against a *real vendor*, because no Longbridge credentials exist in this
environment (the market-data layer genuinely reported NOT_CONFIGURED, so
the live run exercised the refusal path rather than the success path);
that path is covered by the DB-backed integration tests, which drive the
real `MarketDataRouter` with a fake only at the vendor boundary, exactly
as `tests/marketdata/test_router.py` does. Graceful lifespan shutdown
(`scheduler.stop()`) was exercised by tests but not in the live run,
which ended with a forced kill. Docker containers/volumes, the temporary
port-remapping compose override, the test-only `.env`, and the
verification venv were all removed afterward.
Status: Implemented and verified as above.

---

**D032 — Session expiry UX: a read-only GET /auth/session for expiry metadata, and a shared 401 -> login redirect, rather than a client-side countdown or a refresh-token mechanism**
Date: 2026-08-29
Decision: Two parts. (1) Backend: a new `GET /auth/session`
(`apps/api/app/auth/routes/session.py`) returning `user_id`, `email`,
`issued_at`, `expires_at`, and a server-computed `expires_in_seconds` —
never the token, never a renewed token, and 401 (not a described-but-
dead session) for an expired token or a deactivated user.
`decode_access_token` was refactored onto a new
`decode_access_token_claims` so the full verified claim set is available
without duplicating the JWT verification. (2) Frontend: a shared
`apps/web/lib/session.ts` whose `handleExpiredSession(status)` is called
at the single `if (!res.ok)` branch of every authenticated component, so
a 401 from any route handler navigates to `/login?reason=session-expired`
where a real explanation renders, instead of leaving a bare
"HTTP 401" beside a form that can never work again. A `SessionStatus`
component polls the new endpoint once a minute and warns below five
minutes remaining.
Reason: the JWT lives in an httpOnly cookie by design (D020), so client
JS cannot read its expiry — there was no truthful way to warn a user
before their session died. The only alternative available to the UI was
a local countdown started at login, which is a fabricated number the
moment the tab is backgrounded, the machine suspends, the clock skews,
or the cookie was set in another tab. Spec §57's no-fabrication rule
applies to a session clock as much as to a price. `SessionStatus`
therefore re-fetches rather than ticking a local timer down between
polls, and shows the server's own arithmetic.
Alternatives considered: a refresh-token / sliding-expiry mechanism —
rejected, that is a real auth-model change (token rotation, revocation
list, replay handling) that D010's fixed-lifetime single access token
does not have, and it is not what "expiry UX" was asked for; the
endpoint deliberately reports on a session and cannot extend one.
Reading `exp` from the cookie client-side — impossible, it is httpOnly,
which is the whole reason this endpoint exists. Setting a second,
non-httpOnly "expires_at" cookie at login — rejected, it duplicates
authoritative state into a place any script can rewrite, and it goes
stale the moment the backend rejects the token early (a deactivated
user, D016). Middleware-only handling — rejected, `proxy.ts` catches a
*missing* cookie on navigation, but a present-but-dead cookie only fails
at the route handler, mid-page, which is exactly the case that produced
the raw 401.
Consequences: every authenticated component now has one extra line at
its error branch and one shared import; the redirect is a full document
navigation, deliberately, so all client state tied to a dead session is
discarded. The session route handler also clears the dead cookie on a
401 so the next page load is a clean unauthenticated one.
`GET /auth/session` adds one lightweight authenticated request per
minute per open tab.
Verified: 6 new backend integration tests (`tests/api/test_session.py` —
real expiry/issued_at bounds against the configured token lifetime, no
token material anywhere in the response, 401 for missing/malformed/
expired/deactivated-user) and 6 new Vitest tests in
`apps/web/test/SessionStatus.test.tsx`, plus a 401-redirect test in
each of the two other new frontend test files, plus a live
end-to-end run: signed in through the real UI, deactivated that user
directly in Postgres, and the next admin-page load redirected to
`/login?reason=session-expired` showing the real message.
Numbering note: originally D031 in the `phase-28-frontend-v3` worktree;
renumbered to **D032** at merge time since D031 was reassigned to this
same worktree's admin-listings decision above, which itself moved from
D030 to D031 because sibling Phase 27 also claimed D030.
Status: Implemented and verified as above.

---

**D033 — Full post-merge backend verification (Phases 26/27/28): clean run, real merged test count established**
Date: 2026-08-29
Decision: Ran a from-scratch backend verification of the fully-merged
main branch at `c1b06fa` (Phases 26 portfolio-manager, 27
scheduled-snapshots, and 28 frontend-v3 all merged), following the exact
D028 precedent: fresh venv, `pip install -e ".[dev]"`, `ruff check .`,
`mypy apps`, real Postgres/Redis via `docker compose up -d postgres
redis`, `alembic upgrade head`, then `pytest tests/ -q` — rather than
trusting any of the three phases' own independent worktree counts (234
for Phase 26, 228 for Phase 27, 226 for Phase 28), none of which reflects
the other two phases' additions layered on top.
Verified live: `ruff check .` reported "All checks passed!" with zero
findings. `mypy apps` reported "Success: no issues found in 69 source
files" with zero findings. `alembic upgrade head` applied all nine
migrations in sequence against a real freshly-created Postgres 16
(timescaledb image) database with no manual intervention, including
migration 0009 (`orders.portfolio_*` — the Phase 26 trade-path Portfolio
Manager audit columns, D029) landing cleanly on top of 0001-0008.
`pytest tests/ -q` collected and ran the entire suite against that same
real Postgres/Redis pair: **272 tests, all passing**, 3 warnings (two
pre-existing `InsecureKeyLengthWarning`s from a short test-only JWT
secret in `tests/auth/test_security.py`, one `StarletteDeprecationWarning`
from `fastapi.testclient`'s `httpx` usage — neither new nor actionable),
zero failures, zero errors, in a single run.
Reason: unlike D028, this pass found no real cross-phase integration bug
— ruff, mypy, migrations, and the full test suite were all clean on the
first run with no code changes required. This is itself the useful
result to record: it confirms Phases 26/27/28 compose correctly on `main`
with no interaction defects between the trade-path Portfolio Manager's
`orders.portfolio_*` audit columns (D029), the scheduled-snapshot
background task (D030), and the new admin-listing/session-expiry surface
(D031/D032), and it replaces the three phases' self-reported per-worktree
placeholder counts in docs/IMPLEMENTATION_STATUS.md with one real number
collected from the actual merged codebase.
Alternatives: none considered — this is a verification pass, not a
design decision; the only question was whether to trust the per-worktree
counts (rejected, per D028's own reasoning: a worktree's own count can
never reflect what changes once it's combined with sibling phases) versus
re-deriving the true count from a clean-room run against real
infrastructure, which is what was done.
Consequences: docs/IMPLEMENTATION_STATUS.md's Tests section now states
272 as the confirmed post-Phase-26/27/28-merge total, with the prior
234/228/226 per-worktree figures kept alongside it for provenance but
explicitly marked as not the merged total. The verification venv
(`.venv_verify`), the test-only `.env` copied from `.env.example`, and
the `postgres`/`redis` docker containers and their volume were all
removed after the run; nothing from this pass was left running.
Status: Implemented and verified as above.

---

**D034 — Broker discovery: `GET /brokers` scoped by the caller's own broker grants, not by an admin permission**
Date: 2026-08-29
Decision: Added `GET /brokers` (paginated) and `GET /brokers/{broker_id}`
in a new `apps/api/app/api/routes/brokers.py`. Both are gated on
authentication only (`get_current_user`), and both are scoped by the
calling user's `BrokerGrant` rows: the listing returns exactly the
brokers that user holds a grant for, and the detail route returns 404 for
a broker that does not exist and 403 for one that exists but the caller
has no grant on — the same two codes, in the same order,
`require_broker_access` already uses. Pagination is `limit` (default 50,
max 500) / `offset` (default 0), the identical convention and values as
D027's portfolio history and D031's admin listings. The response is an
explicit five-field allow-list (`id`, `name`, `kind`, `provider`,
`is_active`) in `apps/api/app/api/schemas_brokers.py`, never the ORM row.
Frontend: `apps/web/components/BrokerDiscovery.tsx` renders that listing
through the standard route-handler proxy
(`apps/web/app/api/brokers/route.ts`, httpOnly cookie read server-side),
and a per-row "Use" button pushes the real broker id into the existing
trade, agent-trade, portfolio and portfolio-history forms via a tiny
`window` CustomEvent channel (`apps/web/lib/brokerSelection.ts`).
Reason: every broker-scoped route in this codebase takes a `broker_id`
path param, and until now nothing told a user what those ids were. A
non-admin trader had to be handed a UUID out-of-band or ask an admin —
the frontend forms all shipped with a bare "broker UUID" text box. Grants
are the right scoping key because they are already the authorization
model: the read side of D012's grant now mirrors its enforcement side, so
the discovery list can never advertise a broker whose trade/portfolio
routes would immediately 403.
Alternatives considered: reusing the `admin:manage`-gated
`GET /admin/broker-grants` (D031) — rejected, it is precisely the wrong
audience (a trader is not an admin), it returns every user's grants
rather than the caller's, and it returns grant rows, not broker rows, so
a client would still have to resolve each `broker_id` to a name with a
lookup that did not exist. A global `GET /brokers` listing every
configured broker with a separate `accessible` flag — rejected: it leaks
the existence, names and providers of brokers the caller has no
relationship with, for no gain, and it invites a client to render a row
it cannot use. Gating on `VIEW_PORTFOLIO` or `SUBMIT_PAPER_TRADE` —
rejected, wrong in both directions: it would hide brokers from a user who
genuinely holds a grant but only the other permission, while not
narrowing the result for anybody (the grant scope already does that
work). Returning 403 for a user with zero grants — rejected: "you may
reach nothing" is a correct, truthful answer to this question, and an
empty list is how the frontend can say so without inventing a broker.
Depending on `require_broker_access` for the detail route — rejected,
that dependency additionally demands a `Permission`, and there is no
single permission that means "may look up a broker I already hold a grant
for"; the check is re-implemented in ~10 lines with the same semantics
instead of widening a shared dependency's contract.
Consequences: an authenticated user can now enumerate the id, name, kind,
provider and active flag of every broker they hold a grant for — nothing
they were not already authorized to trade on or view. The trust boundary
does not move: pre-filling a broker id in a form authorizes nothing, and
every subsequent call is still re-checked by `require_broker_access` on
the server. `is_active` is deliberately reported rather than filtered on:
hiding an inactive broker the user has a grant for would make this
listing disagree with what the trade routes say about the same id. Still
deliberately absent: any listing of brokers a caller has no grant for
(there is no admin broker listing — D031's "no listing endpoint for
brokers" gap is closed only for the grant-scoped case), any filter/search
params (a caller pages, it does not query), and a frontend proxy for the
detail route — the discovery component only needs the listing, so
`app/api/brokers/` exposes exactly what the UI calls.
Verified: 11 new integration tests in `tests/api/test_brokers.py` against
real Postgres (two users with disjoint grants each see only their own and
neither sees an ungranted broker; exact five-field row shape; empty list
rather than 403 for a user with no grants; limit/offset paging
reassembles in order; 422 on limit=501 and offset=-1; 401 without a
token; detail 200/403/404/401). Full backend suite: **283 passed** (272
before this phase), `ruff check .` clean, `mypy apps` clean (71 source
files). Frontend: 8 new Vitest tests in
`apps/web/test/BrokerDiscovery.test.tsx` (real rows, documented query
params, empty state, 401→login redirect, network failure sentinel,
backend error detail, selection broadcast, and end-to-end pre-fill of
TradeForm's broker id); full web suite **69 passed**, `npm run build`
clean. Live-verified against the running stack: two seeded users with
different grants, each `GET /brokers` returned only that user's broker;
cross-user detail returned 403, a nonexistent id 404, no token 401.
Status: Implemented and verified as above.

---

**D035 — Backtests run through the Portfolio Manager too: the RISK -> PORTFOLIO -> BROKER sequence replicated inline in the replay loop, not by calling submit_trade()**
Date: 2026-08-29
Decision: `apps/api/app/backtesting/engine.py` now gates every simulated
trade by BOTH the Risk Engine and the trade-path Portfolio Manager,
closing the gap D029's own "Consequences" section recorded honestly
("`apps/api/app/backtesting/` calls `evaluate_trade()` directly ... so
backtests are unchanged by this phase and do NOT model portfolio-level
constraints"). Concretely, `_attempt_trade()` keeps its existing risk
path (including the single EXCEEDS_MAX_POSITION_SIZE retry at the
engine-reported `max_quantity_allowed`) and hands every risk-APPROVED
proposal to a new `_apply_portfolio_gate_and_fill()`, which calls the
real `portfolio_manager.manager.decide()` and then the real
`PaperBrokerAdapter`. `POST /backtests` builds a `PortfolioLimits` from
`Settings.portfolio_max_symbol_pct_of_equity` /
`portfolio_min_cash_reserve_pct_of_equity` / `portfolio_max_open_positions`
- the exact same values the live trade path reads - and passes it to
`run_backtest(..., portfolio_limits=...)`. `BacktestResult` gains three
counters (`portfolio_modified_trades`,
`portfolio_modify_risk_blocked_trades`, `portfolio_rejected_trades`).
Neither `apps/api/app/risk/engine.py` nor `apps/api/app/portfolio_manager/`
was modified at all; this phase is wiring plus a result-shape addition.

Reason, on the judgment calls this phase asked for:
- Why replicate the sequence inline rather than call `submit_trade()`:
  `submit_trade()` is the sanctioned live path precisely because it is
  DB/audit-coupled at its edges (`submit_trade_and_record()` writes the
  `orders`/`fills` rows, including D029's `orders.portfolio_*` audit
  columns), and D025's whole structural guarantee is that a backtest
  writes nothing and cannot reach a real broker's persisted state. Making
  `engine.py` call into `oms/` would either drag that coupling into the
  backtest path or force `submit_trade()` to grow a "backtest mode"
  parameter - both worse than ~25 lines that mirror it. The mirroring is
  not left to trust: `_apply_portfolio_gate_and_fill()` reproduces
  `submit_trade()`'s branch-for-branch order (reject -> return, modify ->
  re-gate -> return-or-fill, else fill), its docstrings name the file they
  mirror, and `tests/backtesting/test_engine_portfolio_manager.py`
  deliberately mirrors `tests/oms/test_service_portfolio_manager.py`'s
  structure so the two stay comparable when either changes.
- Why the MODIFY re-gate is unconditional here as well: it is D029's
  correctness-critical invariant ("no quantity this system produces - from
  an LLM, a human, or the Portfolio Manager itself - reaches a broker
  without the deterministic Risk Engine having approved that exact
  quantity"), and a backtest whose fills were gated more loosely than
  production's would answer a different question than the one asked of it.
  Two dedicated tests pin it - see the honest caveat under Status.
- Why the `PortfolioState` is rebuilt at each simulated decision point from
  the run's own broker: D025's fresh, disposable `PaperBrokerAdapter` IS
  the simulated portfolio at that point in the timeline.
  `portfolio_manager.manager.portfolio_state_from_positions()` already
  existed for exactly this shape of input (raw positions + caller marks +
  cash) and is itself pure, so no new I/O and no new dependency enters the
  replay loop; the only mark supplied is that day's real close for the
  traded symbol, the same price the proposal carries, which is the
  mark-consistency precondition `decide()`'s docstring requires.
- Why no backtest-specific limit override: the HTTP caller cannot override
  them, deliberately. A backtest exists to tell you how the *deployed*
  configuration would have behaved; letting a request supply looser
  portfolio limits would produce a number that describes no system that
  exists, and it would be the same scope creep D025's alternative (b)
  rejected for stop prices. `run_backtest()`'s `portfolio_limits` argument
  is optional only so pure unit tests can exercise the pre-D035 path,
  mirroring D029's identical decision for `submit_trade()`'s own optional
  `portfolio`/`portfolio_limits` - and for the same reason: skipping this
  component cannot make a simulated trade less safe, because the Risk
  Engine still gated it and this component can only shrink or stop a
  trade. `POST /backtests` always supplies them.
- Why three counters rather than one "portfolio_blocked" number: a MODIFY
  that filled at a smaller size and a MODIFY whose resized quantity the
  risk re-gate then rejected are different outcomes, and collapsing them
  would hide the more interesting one. Risk Engine rejections are
  deliberately not counted by any of the three: D029's reasoning for
  keeping `BlockReason` and `PortfolioConstraint` separate applies
  identically here - an audit reader must be able to tell which gate
  stopped a trade.
Alternatives: (a) call `submit_trade()` directly - rejected above.
(b) Run the Portfolio Manager once per backtest against a starting
portfolio instead of per-step - rejected: concentration and cash-reserve
limits are functions of the portfolio as it evolves, so a single up-front
evaluation would be a different, weaker check wearing the same name.
(c) Retry a portfolio-REJECTED trade at a smaller size - rejected:
`decide()` already returns MODIFY when a compliant size exists, so a
REJECT means no size >= 1 complies; retrying would be the caller
second-guessing the component, and `submit_trade()` does not do it either.
(d) Persist per-simulated-trade portfolio decisions the way
`orders.portfolio_*` does for real trades - rejected outright: that is a
DB write in the backtest path, which is exactly what D025 made
structurally impossible; the aggregate counters on `BacktestResult` are
the audit surface a backtest can honestly offer. (e) Fabricate marks for
symbols other than the traded one so a multi-symbol portfolio could be
simulated - not applicable and not attempted: the v1 strategy trades
exactly one symbol (D025), so the only open position ever held is the one
being marked at its real close.
Consequences: an existing backtest whose portfolio limits now bind will
report a DIFFERENT (smaller or absent) trade than it did pre-D035. That is
the intended correction, not a regression - the previous number described
fills a real `submit_trade()` would never have made. Runs where nothing
binds are byte-identical to before, which a baseline test asserts by
comparing a loose-limits run against a no-limits run field by field.
`MAX_OPEN_POSITIONS` can in practice never bind in a v1 backtest (one
symbol, so the projected count is at most 1) - it is still evaluated
rather than skipped, because the component decides that, not the caller.
Numbering note: this worktree (`phase-30-backtest-portfolio-manager`)
branched from `main` at D033. It claims **D035** rather than the next free
D034 to leave headroom for the concurrently-running sibling phase-29
worktree, the same convention D030/D031/D032 used when parallel worktrees
collided on a number at merge time. If D034 turns out to be free at merge,
this entry stays D035 - renumbering a decision after the fact is what those
three notes exist to avoid repeating.
Status: Implemented, tested: 5 new tests in
`tests/backtesting/test_engine_portfolio_manager.py` - (1) a loose-limits
run proved field-for-field identical to a no-limits run (the baseline that
pins "an unbinding gate is invisible"), (2) a 5% per-symbol cap that
resizes the risk-approved 9 shares to 4 and fills exactly 4, with the whole
equity curve (10,000 / 10,040 / 9,920) hand-derived in the test's own
comment, (3) a 99% cash-reserve limit that produces a real portfolio
REJECT, no fill, and a run that completes normally on a flat curve, (4) the
re-gate observed directly, and (5) a re-gate rejection proved to prevent
the fill entirely. 277/277 total tests passing (272 pre-existing + 5),
`ruff check .` and `mypy apps` clean (69 source files).
Honest caveat on tests (4) and (5): unlike the live path's equivalent test,
they monkeypatch `engine.evaluate_trade` - (4) to a pass-through spy that
records every quantity the real engine was asked about (observing
`[90, 9, 4, 4]`: the blocked all-cash proposal, the risk-approved 9, the
Portfolio Manager's own 4 re-gated, then the sell), and (5) to inject a
rejection for the resized quantity only. That is not a stylistic choice:
the one risk check that is genuinely non-monotone in quantity is D024's
duplicate-order rule, and the backtest engine passes no `recent_orders`
because nothing is persisted (D025), so no real input exists that makes the
real engine reject a smaller quantity it just approved a larger version of.
The alternative was to leave the invariant unpinned in this context, which
for a correctness-critical property is worse than an explicitly-labelled
seam.
Verified live: this worktree's own `docker compose -p tradingos-phase30`
stack with host ports remapped to 5443/6390/8030 via a throwaway override
file (the tracked `docker-compose.yml` was never edited) to avoid the
concurrently-running sibling phase29 stack on 5442/6389/8029; real Alembic
`0001`->`0009` against a fresh database, a directly-inserted test user, a
real form login, and the real containerized API driven by curl:
`POST /backtests` with no token -> 401, and with a real token -> 400
`NOT_CONFIGURED: no history provider.` Then three real HTTP runs against
the real app (real Postgres, real auth, real Risk Engine, real Portfolio
Manager, real paper broker) under a real uvicorn server, one per outcome:
default `PORTFOLIO_*` settings -> final equity 9,820 with all three
counters 0; `PORTFOLIO_MAX_SYMBOL_PCT_OF_EQUITY=0.05` ->
`portfolio_modified_trades: 1` and the equity curve 10,000 / 10,040 /
9,920, matching the hand-derived unit test exactly; and
`PORTFOLIO_MIN_CASH_RESERVE_PCT_OF_EQUITY=0.99` ->
`portfolio_rejected_trades: 1`, `num_trades: 0`, flat 10,000 curve.
Not verified live: unlike D025, this was NOT verified against real
Longbridge historical data. No usable vendor credentials were readable in
this session, so the containerized API reported
`history_provider=NOT_CONFIGURED` and honestly refused a real-symbol run;
the three runs above used an injected fake `HistoryProvider` whose closes
are synthetic (the same hand-derived series the unit tests use). Every
other component in those runs was the real one. No frontend work was done
(`apps/web/` still does not render the backtest response at all, let alone
the new counters). The docker containers, their volume, the throwaway
compose override, the throwaway seed/live-check scripts, the test-only
`.env` and the verification venv were all removed afterward.
Status: Implemented and verified as above.

---

**D036 — Full post-merge backend verification (Phases 29/30): clean run, real merged test count established at 288**

Date: 2026-08-29
Decision: Ran a from-scratch backend verification of the fully-merged
main branch at `d7069e0` (Phase 29 broker-discovery and Phase 30
backtest-Portfolio-Manager-gating both merged), following the exact
D028/D033 precedent: fresh venv, `pip install -e ".[dev]"`, `ruff check .`,
`mypy apps`, real Postgres/Redis via a separately-project-named
`docker compose -p tradingos-verify29-30` stack on remapped host ports
(5555/6390, chosen to avoid the user's own default-port dev stack on
5432/6379 that was left running throughout for their manual testing),
`alembic upgrade head` against that database, then `pytest tests/ -q` —
rather than trusting either phase's own independent worktree count (283
for Phase 29, 277 for Phase 30 off a 272 baseline plus 5 new tests), since
neither reflects the other phase's additions layered on top.
Verified live: `ruff check .` reported "All checks passed!" with zero
findings. `mypy apps` reported "Success: no issues found in 71 source
files" with zero findings. `alembic upgrade head` applied all nine
migrations in sequence against a real freshly-created Postgres 16
(timescaledb image) database with no manual intervention — migration
0009 is still the current head, since neither Phase 29 nor Phase 30 added
a new migration. `pytest tests/ -q` collected and ran the entire suite
against that same real Postgres/Redis pair: **288 tests, all passing**, 3
warnings (the same two pre-existing `InsecureKeyLengthWarning`s and one
`StarletteDeprecationWarning` seen in every prior verification pass —
neither new nor actionable), zero failures, zero errors, in a clean run.
One transient failure did surface on the first attempt
(`test_missing_jwt_secret_key_fails_closed`), but it was an artifact of
this verification's own shell exporting `JWT_SECRET_KEY` and other
config values as OS environment variables ahead of the run, which leaked
into that one test's `Settings(_env_file=None)` construction (env vars
are read regardless of `_env_file`); re-running with only `DATABASE_URL`
and `REDIS_URL` set — letting every other test manage its own settings
via `monkeypatch`, as the suite is designed to — reproduced the clean
288-pass result with no code changes required. This is not a
cross-phase integration bug and nothing in `apps/` was touched.
Reason: this pass found no real cross-phase integration bug between
Phase 29's caller-scoped `GET /brokers` discovery endpoint and Phase 30's
inline RISK -> PORTFOLIO -> BROKER replication in the backtest replay
loop — ruff, mypy, migrations, and the full test suite were all clean
once the verification's own environment leakage was corrected. This
confirms the two phases compose correctly on `main` and replaces both
phases' self-reported per-worktree placeholder counts (283 and 277) in
docs/IMPLEMENTATION_STATUS.md with the one real number collected from
the actual merged codebase: 288.
Alternatives: none considered — this is a verification pass, not a
design decision; the only question was whether to trust either
worktree's own count (rejected, per D028/D033's own reasoning: a
worktree's own count can never reflect what changes once combined with a
sibling phase) versus re-deriving the true count from a clean-room run
against real infrastructure, which is what was done.
Consequences: docs/IMPLEMENTATION_STATUS.md's Tests section now states
288 as the confirmed post-Phase-29/30-merge total, with the prior
272/283/277 figures kept alongside it for provenance but explicitly
marked as not the merged total. The verification venv
(`.venv_verify2930`), the test-only `.env.verify2930`, the throwaway
`docker-compose.verify2930.yml`, and the `tradingos-verify29-30`-project
docker containers and their (unnamed, ephemeral) volumes were all removed
after the run. The user's own separately-running dev stack — the
default-project-name `trading-os-postgres-1` and `trading-os-redis-1`
containers on ports 5432/6379, the uvicorn process on port 8000, and the
Next.js dev server on port 3005 — was confirmed still up and untouched
after cleanup; nothing from this pass was left running.
Status: Implemented and verified as above.

---

**D037 - Backtest frontend: a dashboard panel that renders the real BacktestResult, including the D035 Portfolio Manager counters, and refuses to draw a curve the backend did not produce**
Date: 2026-08-29
Decision: `apps/web/` now renders `POST /backtests`, closing the gap D035's
own "Not verified live" section recorded verbatim ("No frontend work was
done (`apps/web/` still does not render the backtest response at all, let
alone the new counters)"). Three additions, no backend change of any kind:
`apps/web/app/api/backtests/route.ts` (a route-handler proxy),
`apps/web/components/BacktestPanel.tsx` (form + result rendering), and one
line wiring the panel into `app/dashboard/page.tsx`.

Reason, on the judgment calls this phase asked for:
- Why a dashboard panel rather than a new `/backtest` route: every other
  authenticated feature in this app is a section on `/dashboard`, and the
  panel needs nothing a dedicated route would give it - no path params, no
  server-side data fetch, no separate `proxy.ts` matcher entry (the
  existing `/dashboard/:path*` gate already covers it). A new route would
  have been a second place to keep the auth gate correct for no gain.
- Why the route handler takes no `brokerId` segment, unlike its siblings:
  the backend endpoint is deliberately not broker-scoped (D025) because a
  backtest builds its own disposable in-memory paper broker. Inventing a
  broker id in the URL to look consistent with `/api/trades/[brokerId]`
  would have implied a broker association the backend does not have and
  the run does not use.
- Why the equity chart is hand-rolled inline SVG: the same reason
  `PortfolioHistoryChart.tsx` is (D031) - a single-series line does not
  justify a new npm dependency, and this phase's scope was explicitly
  no-new-dependency. `buildBacktestPoints()` mirrors that component's
  `buildPoints()` deliberately, including its flat-series guard (a span of
  0 is drawn as a centred horizontal line rather than dividing by zero),
  so the two stay comparable when either changes. A backtest that never
  traded really does produce a flat curve; that is a result, not an error.
- Why the x axis is by index rather than by calendar date: the curve
  carries one point per *trading* day (D025), so a calendar-scaled axis
  would draw gaps for weekends and holidays and imply the simulation
  covered days it did not. Same reasoning as D031's index axis, different
  cause.
- Why all three D035 counters are always rendered, including at zero: the
  counters are the only audit surface a backtest can honestly offer
  (D035's rejected alternative (d) - no per-trade portfolio decision is
  persisted, by construction), and hiding a zero would make "the Portfolio
  Manager changed nothing" indistinguishable from "the panel decided this
  was not worth showing". A caption states plainly that all-zero counters
  do NOT by themselves mean every trade was approved - the exact caveat
  D035's own docstring makes on `portfolio_modified_trades`.
- Why the three counters are never summed into one "blocked" number: D035
  rejected that collapse for the response model and the same reasoning
  applies to its display; a MODIFY that filled smaller and a MODIFY whose
  re-gate then blocked it are different outcomes. The panel also states
  that Risk Engine rejections are counted by none of the three, so a reader
  cannot mistake a quiet Portfolio Manager for a quiet Risk Engine (D029).
- Why `win_rate_pct` is shown as "n/a (no completed round trips)" when
  `num_trades` is 0: the backend returns `Decimal(0)` there and documents
  it as deliberately-not-fabricated, but a bare "0" in a UI reads as "0% of
  your trades won", which is a claim about trades that do not exist.
  Rendering the backend's own meaning is not the same as rendering its
  literal digit.
- Why `formatDetail()` exists: FastAPI returns `detail` as a string for the
  hand-raised sentinels but as a list of objects for a 422. Rendering the
  latter directly produces "[object Object]", which would hide a real
  validation error behind a rendering bug. It is flattened to `loc: msg`
  pairs and never replaced with a friendlier invented message.
- Why the form defaults `end_date` to today (UTC): it is the only value the
  endpoint accepts (D025), so defaulting to anything else would ship a form
  whose out-of-the-box submission is guaranteed to fail. The field stays
  editable and a wrong value still surfaces the backend's real
  `UNSUPPORTED_DATE_RANGE:` - the default is a convenience, not a
  client-side validation that second-guesses the backend.
Alternatives: (a) validate `end_date == today` client-side and block
submission - rejected: it would duplicate a backend rule in a second place
that can drift, and would hide the real sentinel from the user. (b) Add a
charting library - rejected, out of scope and unnecessary for one line.
(c) Render a zeroed or placeholder result when the backend returns
`NOT_CONFIGURED:` so the layout "looks complete" - rejected outright; that
is precisely the fabrication TRADING_SAFETY forbids, and a test pins that
neither the curve nor the counters appear on any error. (d) Collapse the
counters into a single "Portfolio Manager intervened: yes/no" badge -
rejected, see above.
Consequences: the D035 gap is closed - a user can now see, in the UI, when
the Portfolio Manager (not just the Risk Engine) intervened during a run.
No backend file was touched, so no backend test count changes. The frontend
suite grows from 69 to 89 tests. `docs/API.md` needed no edit: the endpoint
contract is unchanged and was already documented there in full.
Numbering note: this worktree (`phase-31-backtest-frontend`) branched from
`main` at D036 and claims **D037**, developed in parallel with sibling
phases 32 and 33. It follows the same convention D030/D031/D032 and D035
established for concurrent worktrees colliding on a D-number: the number
claimed at authoring time stands, and a decision is never renumbered after
the fact even if a lower number turns out to be free at merge.
Status: Implemented, tested: 20 new tests in
`apps/web/test/BacktestPanel.test.tsx` - the posted request shape (exactly
the four documented fields, with `end_date` defaulted to today UTC), a real
equity curve rendering one vertex per trading day with all five headline
metrics, the D035 counters rendering non-zero values without collapsing
them, the counters rendering at zero alongside the "zero is not approval"
caveat, `win_rate_pct` shown as n/a for zero round trips, a flat curve
drawn flat rather than dividing by zero, and every real error sentinel on
its own: 400 `NOT_CONFIGURED:`, 400 `UNSUPPORTED_DATE_RANGE:`, 400
`DATA_UNAVAILABLE:`, 502 `DATA_UNAVAILABLE:`, a 422 list-shaped detail
rendered legibly instead of "[object Object]", the 401 -> login redirect,
and a network failure - each asserting that no curve and no counters are
drawn. Plus unit tests for `buildBacktestPoints()`, `formatDetail()` and
the UTC date helpers. **89/89 frontend tests passing** (69 pre-existing +
20 new), `npm run build` clean with zero TypeScript errors and
`/api/backtests` registered as a real route. `npx eslint .` reports 2
errors and 1 warning, all pre-existing in `components/SessionStatus.tsx`
and `lib/session.ts` - files this phase did not touch; the three files it
added are clean.
Verified live: this worktree's own `docker compose -p tradingos-phase31`
stack with host ports remapped to 5451/6391/8031 via a throwaway override
file (the tracked `docker-compose.yml` was never edited; the `!override`
YAML tag was needed because Compose appends `ports` sequences rather than
replacing them), chosen to avoid both the sibling phase worktrees and the
user's own default-port dev stack on 5432/6379/8000, which was left running
and untouched throughout. Real Alembic `0001`->`0009` against a fresh
database, a directly-inserted test user with a real bcrypt hash, and
`npm run dev` on port 3031 pointed at the containerized API via
`API_BASE_URL`. In a real browser: logged in through the real login form
(real `POST /auth/login`, real httpOnly cookie, real redirect to
`/dashboard`), saw the Backtest panel render with `end_date` correctly
defaulted to today UTC, and submitted a real backtest - the full chain of
real React handler -> real Next route handler reading the real httpOnly
cookie -> real containerized FastAPI returned
`HTTP 400: NOT_CONFIGURED: no history provider.`, which the panel rendered
verbatim with no equity curve and no counters drawn. A second real
submission with the dates inverted rendered
`HTTP 422: body: Value error, end_date must be after start_date` - proving
`formatDetail()`'s list handling against the real FastAPI response shape,
not a fixture. `POST /api/backtests` with no cookie returned a real 401
`Not authenticated` from the route handler without ever reaching the
backend. The same three outcomes were independently confirmed by curl
directly against the containerized API on port 8031 (401 without a token,
400 `NOT_CONFIGURED:` with a real token, 422 for the inverted range).
Honest note on how the button was clicked: the Browser pane in this
environment could not composite frames, so `computer{action:"screenshot"}`
timed out and coordinate clicking was unavailable. The two form submissions
above were therefore triggered by dispatching a real click on the real
`Run backtest` button (and real `input` events on the real date fields)
from the page console, which runs the component's genuine React submit
handler and its genuine `fetch` - every layer below the synthetic click was
the real one. This is a limitation of the automation harness, not a stub in
the app.
Not verified live: the success path. Exactly as D025 and D035 recorded, no
usable market-data vendor credentials were readable in this session, so the
containerized API honestly reported `NOT_CONFIGURED` and no real equity
curve, real metrics, or real non-zero Portfolio Manager counters could be
produced to render. The success-path rendering - the curve, the five
headline metrics, and all three D035 counters - is covered by component
tests against a mocked backend response whose values are the ones
documented in `docs/API.md` and hand-derived in D035's own unit tests, NOT
by a live run. The `UNSUPPORTED_DATE_RANGE:` and `DATA_UNAVAILABLE:`
sentinels are likewise test-covered only: both are raised downstream of the
`NOT_CONFIGURED` guard in `backtests.py`, so with no history provider
configured they are unreachable live by construction.
Cleanup: the `tradingos-phase31` containers, their volume, the throwaway
`docker-compose.phase31.yml`, the test-only `.env`, and the `next dev`
process on port 3031 were all removed after the run. The user's own
default-port stack was confirmed still running and untouched afterward.
Status: Implemented and verified as above.

---

**D038 — Portfolio Manager verdict in the web UI: a shared, additive panel next to the Risk Engine's, and an absent Portfolio Manager rendered as absent**
Date: 2026-08-29
Numbering note: developed in parallel with sibling Phases 31 and 33 in
separate worktrees. The last number merged to `main` was D036; Phase 31 is
claiming D037, so this phase takes D038. Nothing here depends on either
sibling.
Decision: Closed the frontend gap D029 recorded against itself
("`apps/web/` was not updated to render the new fields; the frontend still
shows the risk verdict only, which is an honest gap, not a claim"). Added
`apps/web/components/PortfolioVerdict.tsx` — one shared presentational
component plus a `PortfolioVerdictFields` type — and rendered it from BOTH
`TradeForm.tsx` and `AgentTradeForm.tsx`. The existing risk-verdict block
was extended, never replaced: every field it already showed still shows,
in the same place, and the Portfolio Manager's verdict appears as a
separate panel BELOW it.

One shared component rather than two copies, because the two forms consume
the same four `portfolio_*` fields of the same DTO
(`AgentTradeResponse` extends `TradeSubmissionResponse`), and a divergence
between them would mean the same backend verdict reading differently
depending on which form submitted the trade.

Rendering rules, one per `portfolio_action` value:
- `"modify"` — a blue "Resized by the Portfolio Manager" panel showing the
  binding constraint, the backend's `portfolio_detail` verbatim, and BOTH
  quantities (`portfolio_requested_quantity` vs the actual `fill_quantity`)
  side by side. This is the case D029 called out as easily misread, and the
  panel says in words that the Risk Engine approved the trade and then the
  Portfolio Manager shrank it, and that the reduced quantity was re-gated.
- `"reject"` — a violet panel, deliberately NOT the amber a risk rejection
  uses, labelled "Rejected by the Portfolio Manager (not the Risk Engine)".
  Different colour, different label, different icon: conflating the two
  rejection sources is exactly the failure mode this phase exists to fix.
- `"approve"` — nothing. The existing block already conveys the outcome and
  a second "approved" badge would be noise.
- `null`, or the fields absent entirely (an older-shaped response) —
  nothing at all. A null action means the Portfolio Manager NEVER RAN
  (risk-rejected first, or no portfolio state supplied); it is rendered as
  absent, never as a success. The component fabricates no verdict the
  backend did not send, and displays no sub-field the backend did not
  return.

A second, smaller change makes the colour honest: the result block's tone
is now keyed on the RISK verdict (`approved`) rather than on `status`.
Before, a `status: "rejected"` painted the block amber regardless of who
rejected it — so a Portfolio-Manager rejection was rendered in the Risk
Engine's own colour, attributing it to the wrong gate. Now amber always
means "the Risk Engine said no"; a portfolio rejection gets a neutral
block plus its violet panel. The `approved` row is also relabelled
"approved (Risk Engine)", and `AgentTradeForm`'s `quantity` row
"quantity (agent proposed)", since D029 made both ambiguous.
Reason: the backend has carried a complete portfolio audit record since
D029 and the product simply did not show it. The specific harm was not a
missing feature but a misleading one: `approved: true` with `status:
"rejected"` rendered as a bare amber "rejected / approved true" told a user
the Risk Engine had both passed and stopped the same trade, with no
indication that a second gate existed. Showing a resize was the same
problem in a quieter form — `fill_quantity` differed from what was typed
and nothing said why.
No backend change was needed or made: all four fields already existed on
`TradeSubmissionResponse` and are already documented in docs/API.md.
Consequences: 10 new frontend tests (5 per form), 79/79 `apps/web` tests
passing (69 before), `npm run build` compiles with zero TypeScript errors.
Per form the five cover: approve (no panel), modify (both quantities plus
the constraint and detail), reject (violet panel, "not the Risk Engine"
wording, and an assertion that the risk block is NOT amber), null action
(panel absent AND no "Portfolio Manager" text anywhere, so nothing implies
approval), and an older-shaped response carrying no `portfolio_*` fields at
all. Two pre-existing `eslint` findings in `SessionStatus.tsx`/`session.ts`
remain and were not touched — they are unrelated to this change.
Verified live, not only in tests: brought up this worktree's own docker
stack (`docker compose -p trading-os-phase32` with host ports remapped to
5438/6388/8008 through a throwaway override file using `!override`, to
avoid colliding with sibling Phase 31/33 worktrees and with the user's own
stack on the default 5432/6379/8000; the tracked `docker-compose.yml` was
never edited), ran real Alembic migrations `0001`→`0009` against a fresh
database, seeded a role/user/paper-broker/grant, and drove the real
containerized API over HTTP. Obtained four REAL responses from it, in
order, on one real broker: `AAPL` buy 5 @100 →
`portfolio_action: "approve"`; then with
`PORTFOLIO_MAX_SYMBOL_PCT_OF_EQUITY=0.001`, `MSFT` buy 5 @100 →
`"modify"` / `symbol_concentration` / `portfolio_requested_quantity: "5"`
/ `fill_quantity: "1"`; `TSLA` buy 5 @200 → `status: "rejected"` with
`approved: true`, `block_reason: null`, `portfolio_action: "reject"`; and
`NVDA` buy 5000 @100 → `approved: false`,
`block_reason: "exceeds_max_position_size"`, `portfolio_action: null`.
Each of those four verbatim JSON bodies was then fed to the real
`TradeForm` component and the resulting DOM was dumped and read: approve
rendered a green risk block and NO portfolio panel; modify rendered the
blue panel with `5` and `1` in the two quantity cells and the backend's
own detail string; reject rendered the violet panel with "not the Risk
Engine" while the risk block came out neutral, not amber; the risk-
rejected response rendered an amber risk block with the portfolio panel
entirely absent.
Not verified live: the trades were driven against the running API over
HTTP rather than by clicking the running Next.js dev server's own form —
the browser tool available in this environment returned HTTP 403 for the
dev server's JS chunks, so the page never hydrated and no React handler
could fire. The dev server itself did start and serve the real dashboard
(both forms present in the DOM), and the component rendering above used
the real component against real backend payloads, but the full
click-through path was not exercised end to end this phase. No market-data
vendor or LLM provider was wired, so all prices were caller-supplied and
`AgentTradeForm` was exercised only through its own tests, not against a
real agent — its rendering path is the identical shared component.
Docker containers, volumes, the throwaway compose override, the test-only
`.env`, `apps/web/.env.local` and the throwaway render harness were all
removed afterward.
Status: Implemented and verified as above.

---

**D039 — Emergency stop becomes a persisted, audited, live-flippable control: an append-only `emergency_stop_events` table read one layer above the (still pure) Risk Engine**

Date: 2026-08-29
Decision: Closed PROJECT_CONTEXT.md's long-standing "an explicit
emergency-stop *source* ... is undecided" open item. Until this phase the
kill switch was `Settings.emergency_stop_active`, an `.env` value — so
halting the platform meant editing a file and restarting the app, which
is exactly what the setting's own docstring said it should not require.
Spec §46's emergency stop is now:

1. A new table, `emergency_stop_events` (migration `0010`), **append-only**:
   one row per flip, carrying `active`, a required `reason`, the
   `actor_user_id` who flipped it, and `created_at`. The CURRENT state is
   the `active` value of the highest-`id` row. Nothing is ever updated in
   place, so the state and its audit trail are the same object and cannot
   drift apart — the same append-only discipline as Order/Fill (D006) and
   the portfolio snapshots (D027). Chosen over a single-row config table
   plus a separate audit log: two tables can disagree about what actually
   happened, one cannot.
   `id` is a monotonic `BigInteger` identity rather than this schema's
   usual random `uuid4` PK, deliberately — "latest row wins" has to be a
   total order, and two flips inside one clock tick would make `created_at`
   alone ambiguous. This is the only table whose ordering is load-bearing
   for a safety control, so it is the only one that gets a sequence.
2. Three endpoints: `POST /admin/emergency-stop` (activate) and
   `POST /admin/emergency-stop/deactivate`, both gated by `admin:manage`
   like every other admin write (no new finer-grained permission — D013's
   one-coarse-admin-permission reasoning still holds); and
   `GET /admin/emergency-stop` (status), which requires **authentication
   only**. Separate routers are used because they carry different
   dependencies. The status scoping follows D034's broker-discovery
   argument directly: a trader whose orders are all being rejected is
   entitled to see *that the system is halted, by whom, and why* without
   holding `admin:manage` — knowing you are blocked is not privileged
   information, only flipping the switch is. Deactivate is a separate URL
   rather than a boolean field on one route, so "turn the safety control
   OFF" can never be the accidental result of a defaulted or malformed
   body. `reason` is required, non-blank after stripping, on BOTH
   directions — a kill switch that can be turned back off with no recorded
   justification is not an audited control.
3. The Risk Engine is UNCHANGED. `evaluate_trade()` still takes
   `emergency_stop_active: bool` as a plain parameter and still returns
   `BlockReason.EMERGENCY_STOP_ACTIVE`; `apps/api/app/risk/` performs no
   I/O and imports nothing from the new module. The database read happens
   one layer up, in `_execute_trade()` (shared by the human and agent trade
   routes, so both get identical treatment from a single read site), and
   is handed down as data — the same discipline D024 used for the
   recent-orders query and D029 for the Portfolio Manager. The new
   persistence code deliberately lives in a new `apps/api/app/safety/`
   package, not inside `risk/`, so that boundary is structural rather than
   a convention someone can quietly erode. A test asserts it
   (`test_the_risk_engine_stays_free_of_the_persistence_layer`).
4. `Settings.emergency_stop_active` survives as a documented **bootstrap
   default only**: it is consulted if and only if the table is still
   empty, i.e. the switch has never been flipped on this database. Once
   any row exists, `Settings` is never read again on the trade path — which
   is the whole point, since otherwise a redeploy could silently reassert
   a stale `.env` value over a live operational halt. The status response
   reports `source` as `"settings_default"` or `"database"` verbatim, so
   which one is in force is always visible rather than inferred, and the
   provenance fields are `null` in the fallback case rather than invented.
   The startup log line was renamed to `emergency_stop_settings_default`
   for the same reason — the old `emergency_stop_active` key would now
   read as a claim about current state that it cannot make.
5. The state is read from the database on EVERY trade submission, never
   cached in the process. That is one indexed single-row read (served by
   the PK index) on a path that already runs several queries, and it buys
   the property that matters: a flip takes effect on the next trade in
   every worker, with no restart and no cache-invalidation channel to get
   wrong. Caching was rejected outright — a stale-cache emergency stop is
   a failure mode that fails OPEN.

Verified live: `ruff check .` — "All checks passed!". `mypy apps` —
"Success: no issues found in 74 source files". Full suite against real
Postgres (own `docker compose -p tradingos-phase33` stack on remapped host
ports 55433/56380, chosen so it could not touch the user's own dev stack
on 5432/6379/8000/3005, which was left running and untouched throughout):
**304 passed, 0 failed** — the 288 established by D036 plus 16 new tests
in `tests/api/test_emergency_stop.py`. `alembic upgrade head` applied
`0001`→`0010` cleanly against that database.
Then an end-to-end run against a real `uvicorn` process on port 58001,
with `Settings.emergency_stop_active` left FALSE the entire time: a real
trade filled; `POST /admin/emergency-stop` with reason "Phase 33 live
verification halt" returned `active=true, source=database` with the acting
user's id; `GET /admin/emergency-stop` reflected it; the next real trade
submission — same process, no restart — came back
`status=rejected, block_reason=emergency_stop_active`; a blank reason was
rejected 422. The uvicorn process was then **killed and restarted** with a
byte-identical `.env` (md5 checked before and after) and the new process's
own startup log confirming `emergency_stop_settings_default: false`: the
status endpoint still reported `active=true` with the original reason,
actor and timestamp, and a real trade was still rejected with
`emergency_stop_active`. Deactivating with a reason then let a real trade
fill again. The `emergency_stop_events` table was inspected directly in
Postgres and held exactly the two expected rows with correct actor,
reason, and timestamps.
Alternatives: (a) an in-memory process flag flipped over HTTP — rejected,
it fails D014's "everything that matters survives a restart" bar and is
incoherent under multiple workers, where flipping the stop would halt one
process out of N; (b) a single mutable `system_config` row updated in
place with a separate audit table — rejected per (1) above; (c) Redis —
rejected, the platform's durable state of record is Postgres, and putting
a safety control in the one store that is explicitly a cache inverts the
durability requirement; (d) having `evaluate_trade()` read the state
itself — rejected outright, non-negotiable: the Risk Engine's zero-I/O
purity is what makes it work when everything else is down; (e) letting
`Settings=true` override a persisted `active=false` (a "belt and braces"
OR of the two) — rejected, because it would make a persisted deactivation
un-actionable without the `.env` edit-and-restart this phase exists to
remove, and an operator who cannot turn the halt back off will eventually
be tempted to bypass the control entirely.
Consequences: `docs/API.md` documents the three endpoints;
`docs/PROJECT_CONTEXT.md`'s emergency-stop open item is removed;
`docs/TRADING_SAFETY.md` moves the emergency stop out of "not yet
enforced" into "currently enforced in code". `Settings.emergency_stop_active`
keeps its name and its `.env` key for compatibility, but its docstring now
states plainly that it is a bootstrap default with no effect once a row
exists. There is still no listing endpoint for the flip history (the rows
are queryable directly, and a paginated `GET /admin/emergency-stop/history`
is the obvious next increment); the stop is also not yet checked anywhere
outside the trade path (e.g. the portfolio snapshot scheduler does not
consult it — it places no orders, so there is nothing there to halt).
Numbering: this worktree branched from `main` at D036 and claims **D039**
rather than the next free D037, because sibling phase-31 and phase-32
worktrees are running concurrently and are claiming D037/D038 — the same
parallel-worktree convention D035 recorded, leaving headroom rather than
risking a collision at merge time.
Status: Implemented and verified as above.

---

**D040 — Full post-merge backend verification (Phases 31/32/33): clean run, real merged test count confirmed at 304**

Date: 2026-08-29
Decision: Ran a from-scratch backend verification of the fully-merged
main branch at `05e6cc2` (Phase 31 backtest-frontend, Phase 32
portfolio-manager-UI, and Phase 33 emergency-stop-admin all merged),
following the exact D028/D033/D036 precedent: fresh Python 3.13 venv
(`.venv_verify3133`, discarded after the run — 3.13 chosen over the
system's 3.14 default for wheel-compatibility safety with `asyncpg` and
other compiled deps), `pip install -e ".[dev]"`, `ruff check .`,
`mypy apps`, real Postgres 16 (timescaledb image) and Redis 7 via
standalone throwaway containers (`tradingos-verify3133-pg` on host port
15532, `tradingos-verify3133-redis` on host port 16479 — a plain
`docker compose -p tradingos-verify3133 up` was tried first but its
port-remap override file merged rather than replaced the base compose
file's port list and collided with the user's own port-6379 Redis, so
the throwaway pair was instead started directly via `docker run` on the
remapped ports with no compose project at all), `alembic upgrade head`
against that database, then `pytest tests/ -q` with `DATABASE_URL` and
`REDIS_URL` pointed at the throwaway pair. The user's own dev stack
(`trading-os-postgres-1`/`trading-os-redis-1` on default ports
5432/6379, a `--reload` uvicorn on port 8000, a Next.js dev server on
port 3005, all using the real root `.env`) was left running and
untouched throughout, and confirmed still healthy/listening both before
and after this pass; no bare `docker compose down` was ever run and the
real `.env` file was never read for `DATABASE_URL`/`REDIS_URL` (env vars
take precedence over `.env` in pydantic-settings) or written to.
Verified live: `ruff check .` reported "All checks passed!" with zero
findings. `mypy apps` reported "Success: no issues found in 74 source
files" (74 vs. D036's 71 — the three new files are Phase 33's
`apps/api/app/safety/` module: `emergency_stop.py` and its
`__init__.py`, plus the new admin router additions) with zero findings.
`alembic upgrade head` applied all ten migrations in sequence — 0001
through Phase 33's new `0010_emergency_stop_events` — against a real,
freshly created database with no manual intervention; 0010 is now the
head. `pytest tests/ -q` collected and ran the entire suite against
that same real Postgres/Redis pair: **304 tests, all passing**, 3
warnings (the same two pre-existing `InsecureKeyLengthWarning`s and one
`StarletteDeprecationWarning` seen in every prior verification pass —
neither new nor actionable), zero failures, zero errors, in a clean run.
One transient failure surfaced on the first attempt
(`test_missing_jwt_secret_key_fails_closed`), the identical D036
shell-environment-leakage artifact recurring: this verification's own
shell had exported `JWT_SECRET_KEY` as an OS environment variable ahead
of the run, which leaked into that one test's `Settings(_env_file=None)`
construction (env vars are read regardless of `_env_file`); re-running
with only `DATABASE_URL` and `REDIS_URL` set — letting every other test
manage its own settings via fixtures/monkeypatch as the suite is
designed to, and letting that one test fall back to the real root
`.env`'s `jwt_secret_key` for a harmless, valid, non-empty secret value
with no database/network implication — reproduced the clean 304-pass
result with no code changes required. This is not a cross-phase
integration bug and nothing in `apps/` was touched. Frontend
(`apps/web`) was independently checked by file listing rather than a
full `npm test` run, since this was a backend-verification pass: the 12
`apps/web/test/*.test.tsx` files present are the same 12 that existed
after Phase 31 (D037), confirming Phase 32 and Phase 33 added no new
frontend test file and the previously reported 89-test frontend total
still holds.
Reason: this pass found no real cross-phase integration bug between
Phase 33's persisted, DB-read emergency-stop control (D039) and Phases
26 through 30's earlier trade-path, backtest, and portfolio-manager work
— ruff, mypy, all ten migrations, and the full test suite were all clean
once the verification's own environment leakage was corrected (the same
class of leakage D036 already documented, now confirmed to recur and to
have the same fix). This confirms Phase 33 composes correctly with the
rest of `main`, and confirms Phase 33's own independently-reported
304-in-worktree count already **was** the true post-merge total, because
Phases 31 and 32 were frontend-only and added zero backend tests on top
of D036's confirmed 288 baseline (288 + 16 new emergency-stop tests in
`tests/api/test_emergency_stop.py` = 304, matching exactly).
Alternatives: none considered — this is a verification pass, not a
design decision; the only question was whether Phase 33's self-reported
worktree count (304) coincidentally already equaled the true merged
total or only appeared to, and a clean-room run was the only way to be
sure rather than assume from the arithmetic alone.
Status: Implemented and verified as above.

**D041 — FIFO/LIFO cost basis as a per-request alternative to average-cost, on the read-only portfolio endpoint only**
Date: 2026-08-30
Numbering note: developed in parallel with sibling Phase 35 in a separate
worktree. Both branched from the same `main` (D040, the last entry on
that branch), so both may have claimed the next free D-number
independently; if the sibling also landed a D041, one of the two needs
renumbering at merge time. The content, not the digits, is the record.
Decision: `apps/api/app/portfolio/models.py` adds `CostBasisMethod`
(`average`/`fifo`/`lifo`, a `str` enum so FastAPI parses it straight from
the query string and 422s anything else). `snapshot.py`'s
`replay_symbol_fills()` keeps its exact signature and default but now
dispatches on a keyword-only `method`: `replay_symbol_fills_average()`
holds the verbatim D022 average-cost code, and
`replay_symbol_fills_lots()` implements FIFO and LIFO. Both are pure - no
DB, no I/O, no LLM - the same discipline D022 established, and the
existing `_replay_fills()` / `compute_portfolio_snapshot()` simply thread
the method through. `GET /brokers/{broker_id}/portfolio` gains an
optional `cost_basis_method` query parameter defaulting to `average`.
The lot tracker holds open lots as (signed_quantity, price). A fill
extending the current direction appends a lot; an opposing fill consumes
lots from the oldest end (FIFO) or newest end (LIFO), realizing
`(fill_price - lot_price) * qty` per long lot closed and the negation per
short lot closed, and any quantity left after the book empties opens lots
in the fill's own direction rather than realizing against a fabricated
zero cost. The reported basis is the weighted-average price of the lots
still open - the true basis of exactly the quantity still held - and is
zero when flat.
Reason: D022 deferred FIFO/LIFO on the grounds that "no per-lot records
exist anywhere (D006/D014)" and that retrofitting them would mean "either
a schema change or silently assuming an ordering the data doesn't
actually establish". That was correct about *stored state* and wrong
about *history*. `orders`/`fills` are append-only (D006) and already
record every individual buy as its own row with its own quantity, price
and `filled_at` - that IS a lot ledger, and `filled_at` IS the ordering,
the very same ordering D022's own average-cost replay already relies on.
So the lots are derived from data that was genuinely recorded, nothing is
invented, and **no migration is needed** - which is why this fits the
report-only shape D022 wanted to protect. Offering the choice matters
because average-cost is not what most brokers or tax regimes actually
report; a caller reconciling against a broker statement needs the method
that statement used. Realized P&L differing by method is not an
inconsistency to be smoothed over: for buy 10 @ 100, buy 10 @ 110, sell
15 @ 120 the correct answers are 225 (average), 250 (FIFO) and 200
(LIFO), and all three are asserted independently rather than one being
derived from another.
Backward compatibility was treated as the hard constraint, not a
nice-to-have. AVERAGE stays the default; the average code path is
physically the same function body D022 shipped, moved not rewritten; and
the guarantee is tested as full response-object equality (`default ==
average`), not merely as matching numbers, so an accidentally added or
reordered field would fail. Every pre-existing average-cost unit test
still calls `replay_symbol_fills()` with no `method` argument on purpose -
those tests are the compatibility guard.
Alternatives: (a) a new `fill_lots` table materializing lots, as D022
sketched - rejected, it would duplicate information `fills` already holds
and create a second thing that can disagree with the append-only history,
the exact failure mode D014's "Order/Fill's append-only history exists to
make [current state] reconstructible" comment warns about. Deriving on
read cannot drift. (b) a `cost_basis_method` field added to the
`PortfolioSnapshot` response so the answer is self-describing - rejected
for now, since adding a field changes the default response bytes and
therefore breaks the compatibility guarantee above; it belongs with the
migration in (c). (c) also accepting the parameter on
`POST .../portfolio/snapshots` and in the scheduler - **rejected, and this
is the load-bearing scope decision.** `portfolio_snapshots` (D027) has no
column recording which method produced a row, so a persisted FIFO
snapshot would read back through `GET .../history` as though it were an
average-cost one, silently mixing incomparable numbers in a single time
series - a misreporting hazard in exactly the append-only historical
record D027 built to be trustworthy. That needs a migration adding the
column, not a query parameter, so the write path stays average-only and a
test asserts that passing `cost_basis_method` to the POST changes
nothing. (d) supporting HIFO/specific-lot identification too - deferred,
neither is derivable without a caller-supplied lot selection this API has
no way to express yet; an unrecognised method is a 422 rather than a
fallback so adding one later is purely additive.
Consequences: three methods now have to be kept correct instead of one.
FIFO and LIFO share a single function differing only by a `newest_first`
flag specifically so they cannot diverge under later edits. The
`avg_cost` field name is now slightly inaccurate under FIFO/LIFO (it is a
lot-weighted basis, not a running average) - renaming it would break the
response contract, so the field docstring and docs/API.md carry the
distinction instead. Under FIFO/LIFO a fully-closed position reports a
basis of 0 where AVERAGE carries its last running average forward; that
divergence is deliberate and tested, since reporting a basis for a
position that no longer exists would be the fabrication. Persisted
history and scheduled snapshots remain average-cost only until the
migration in (c) is done - that is now the single tracked follow-on.
Status: Implemented, tested, verified live. 317/317 pytest passing (304
D040-confirmed merged baseline + 13 new: 10 pure lot-math unit tests in
`tests/portfolio/test_snapshot.py`, each hand-verified in a comment,
including three-lot orderings, partially-consumed lots, an over-sell that
opens a short, and the invariant that all three methods reconcile to the
same total once every lot is closed; plus 3 DB-backed HTTP tests in
`tests/api/test_portfolio.py`). `ruff check .` and `mypy apps` clean (74
source files). Verified live against this worktree's own Docker
Postgres/Redis and a rebuilt API container (host ports remapped to
5442/6389/8010 to avoid both the user's own dev stack and the sibling
Phase 35 worktree; torn down afterwards): a real seeded multi-lot history
- buy 10 @ 100, buy 10 @ 110, sell 15 @ 120, marked at 130 - returned
over real HTTP `average` basis 105 / realized 225 / unrealized 125,
`fifo` 110 / 250 / 100, `lifo` 100 / 200 / 150, each equal to the
hand-computed figure, with `cash` 99700 and `total_equity` 100350
identical across all three, the no-parameter response byte-identical to
`average`, and `?cost_basis_method=hifo` rejected 422.
Status: Implemented and verified as above.

---

**D042 — Market-hours gating for the snapshot scheduler: an honest weekend-only gate, not a fabricated exchange calendar**
Date: 2026-08-30
Decision: Added `apps/api/app/portfolio/market_hours.py` — a frozen,
I/O-free `MarketHoursGate` with one method, `evaluate(as_of) ->
MarketHoursDecision` (`RUN` / `RUN_GATE_DISABLED` / `SKIP_WEEKEND`).
`run_snapshot_cycle()` (D030) now evaluates that gate **first**, before
the eligible-broker query, and returns an empty `SnapshotCycleResult`
carrying the decision when it says skip — so a gated cycle issues zero SQL
and makes zero market-data vendor calls. `SnapshotCycleResult` gained a
typed `market_hours` field and a `gated` property (distinct from
`skipped_count`, which counts brokers skipped *within* a cycle that did
run). `PortfolioSnapshotScheduler` gained a `market_hours_gate` and an
injectable `clock`, read once per cycle rather than at construction so a
long-lived scheduler sees the weekend begin and end while it runs. New
setting `PORTFOLIO_SNAPSHOT_MARKET_HOURS_GATE_ENABLED`, **default true**,
wired in `apps/api/app/main.py` and reported in the startup log line. No
new pip dependency, no new table, no migration, no change to any HTTP
surface.
This closes — **partially, and only partially** — the gap D030's
"Consequences" recorded verbatim: "the interval is wall-clock, not
market-hours-aware: a scheduler left enabled overnight will keep recording
unchanged after-hours rows, or keep logging skips". Weekends are now
gated. Overnight-on-a-weekday and holidays are **not**, and this entry
does not claim otherwise.
Reason: the honest scope question is what "market hours" can mean in a
codebase that has no trading-calendar data source. Saturday and Sunday are
derived from the Gregorian calendar itself, not from any exchange's
policy: no venue in the supported set (Longbridge's US/HK/CN/SG equity
markets) holds a regular equity session on either. That makes the weekend
check a computable fact rather than a guess — the one part of
"market hours" implementable with no vendor, no dependency, and no
invention. It removes roughly two sevenths of an enabled scheduler's
wasted cycles, and it removes them at the cheapest possible point (before
the DB is touched).
The gate evaluates day-of-week **in UTC**, because UTC is the only clock
this process can read without a timezone database (`zoneinfo` needs system
tz data, absent on some deployment targets; adding `tzdata` would be a new
pip dependency — see Alternatives). That choice was checked rather than
assumed, and the check is pinned by a test: the earliest regular open
across supported markets (HK/CN 09:30 UTC+8 = 01:30 UTC) lands on a UTC
Monday, and the latest regular close (US 16:00 ET Friday = 20:00/21:00 UTC
in either DST state) lands on a UTC Friday, so no regular session is ever
suppressed. The one knowingly-clipped window is US *extended-hours*
trading late on a Friday (post-market to 20:00 ET = 00:00–01:00 UTC
Saturday), which is skipped. That is accepted, documented in the module
docstring, and pinned by its own test so it stays a deliberate tradeoff
rather than becoming a forgotten one — a missed low-liquidity
extended-hours cycle leaves an honest gap in an append-only series, and
anyone who objects can switch the gate off.
Default **true**, unlike `PORTFOLIO_SNAPSHOT_SCHEDULER_ENABLED`'s
fail-closed false. The two defaults point the same direction for the same
reason: the safe side is the side that does less. For the scheduler itself
that meant "don't run"; for the gate it means "on", because on is what
suppresses vendor calls and meaningless rows. Off restores D030's exact
unconditional behavior, which is the right setting for testing and for
anyone snapshotting 24/7 instruments.
Vendor capability, checked rather than assumed: the installed `longport`
package (v4.3.7) **does** expose real session data — direct introspection
of `AsyncQuoteContext` confirms `trading_session()` and
`trading_days(market, begin, end)`, plus the types `MarketTradingSession`,
`TradingSessionInfo` (`begin_time`/`end_time`/`trade_session`),
`MarketTradingDays` and the `Market` enum (US/HK/CN/SG/Crypto);
`market_status()` exists too, but on `AsyncMarketContext`, a context this
codebase has never constructed. So the honest answer is: a real
holiday-aware calendar source **is** reachable in principle, and it was
still not wired this phase — see the first Alternative for exactly why.
Alternatives: **Wiring `trading_session()` / `trading_days()` through a
new `TradingCalendarProvider` port** — the right eventual design, and
explicitly deferred rather than rejected. Three concrete blockers, none of
which is "it doesn't exist": (1) both APIs express times and dates in
each market's *local* terms, and the SDK does not supply the market's
timezone, so turning "09:30–16:00 in market X" into an instant this
process can compare against `utc_now()` needs a market→timezone map plus
`zoneinfo` tz data — i.e. either a hardcoded offset table (the exact
fabrication this entry refuses) or a new `tzdata` dependency; (2) it needs
a symbol→`Market` mapping derived from the `.US`/`.HK`/`.SH`/`.SZ`/`.SG`
suffix convention, which is defensible but is new inference code that must
fail closed on an unknown suffix; (3) `market_status()` lives on
`AsyncMarketContext`, which would be a second SDK context to construct,
credential-gate and safety-review, and its closed-hours semantics could
not be verified in this phase without live calls. Doing it properly is a
phase, not a footnote, and doing it half-way would produce a gate that
looks authoritative and is not. When it is built it must go through the
existing router/provider discipline with typed
`NOT_CONFIGURED`/`DATA_UNAVAILABLE` handling, and — critically — must
**fail open** (run the cycle) when the calendar is unavailable, because
failing closed there would silently stop recording real history on a
vendor outage.
**Hardcoding exchange sessions and a holiday list** — rejected outright.
It is fabricated data with a confident face: it would look authoritative,
it would rot silently the first time an exchange moved a session or a
government moved a holiday, and a wrong "the market is open" is
indistinguishable downstream from a real one. Exactly what
docs/TRADING_SAFETY.md and spec §57 forbid.
**Adding a market-calendar library (`pandas_market_calendars`,
`exchange_calendars`)** — deliberately NOT added, and recorded as such
per project policy (ask before adding a tool; no live answer was
obtainable mid-task, so the default is not to add). It would also import a
large transitive tree (pandas) into an API image for one boolean.
**Lengthening the sleep to the next market open instead of gating the
cycle** — rejected: it requires the very next-open calculation this module
has no authoritative source for, and it makes `interval_seconds` mean two
different things. Gating keeps the interval simple and means the first
weekday cycle fires within one interval of reopening.
**Making the gate skip *brokers* rather than the whole cycle** — rejected;
the gate's input is a clock, not a broker, and evaluating it once per
cycle before any query is what makes a weekend tick actually free.
**Defaulting the gate to off** — rejected; a market-hours feature that
nobody gets by default fixes nothing for the operator who enables the
scheduler and thinks no further about it.
Consequences: An enabled scheduler now no-ops through weekends at the cost
of one enum comparison per tick. `SnapshotCycleResult`'s new field
defaults to `RUN_GATE_DISABLED`, and both `run_snapshot_cycle()` and the
scheduler treat an omitted gate as "no gating", so every pre-Phase-35 call
site — including the D030 tests — keeps its exact previous meaning; this
is pinned by a test rather than left to inspection. The remaining, still
open, follow-ons from D030's Consequences are unchanged: intraday
after-hours and holiday awareness (needs the real calendar port described
above) and multi-worker safety. `docs/PROJECT_CONTEXT.md` and
`docs/IMPLEMENTATION_STATUS.md` were updated to state the partial scope in
those words, not as "market-hours awareness: done".
Numbering note: developed in a `phase-35-scheduler-market-hours` worktree
branched from `main` at D040, in parallel with a sibling phase-34
worktree that claims **D041**. This entry claims **D042** to avoid a
merge-time collision on the same number — the same parallel-worktree
convention D030/D031/D032 and D035 already established. The gap is
intentional and does not indicate a missing decision.
Verification: `ruff check .` clean ("All checks passed!"), `mypy apps`
clean (75 source files), and **336/336 tests passing** against a real
Postgres in this worktree — a measured 304 baseline (confirmed by
stashing this phase's changes and re-running the full suite in the same
environment) plus **32 new**: 20 unit in
`tests/portfolio/test_market_hours.py` (pure, no I/O — weekday/weekend
across a full real week, the disabled-gate escape hatch, naive-datetime
rejection, non-UTC conversion in both directions, the exact
Saturday-00:00/Monday-00:00 UTC boundary instants, the
no-regular-session-is-suppressed claim, and the knowingly-clipped Friday
US extended-hours window) and 12 DB-backed integration in
`tests/api/test_snapshot_scheduler_market_hours.py` (a real broker with a
real filled position placed through the real trade endpoint: a weekend
cycle writes no row *and* makes no vendor call; the identical cycle on a
Monday captures equity 100200; a disabled gate captures on a Saturday; an
omitted gate preserves pre-D042 behavior; and the real running asyncio
scheduler no-ops across a simulated weekend then captures once its clock
crosses into Monday). Where a weekend/weekday distinction is under test
the "as of" clock is **injected** — real Saturday/Monday instants, asserted
against the actual calendar by a guard test — not waited for.
Live-verified end to end against a real uvicorn process and a real
Postgres in this worktree, and by genuine coincidence on a real UTC
**Sunday** (2026-08-30T03:44Z), so the weekend path was exercised by the
real system clock, not a stub: with
`PORTFOLIO_SNAPSHOT_SCHEDULER_ENABLED=true`,
`PORTFOLIO_SNAPSHOT_INTERVAL_SECONDS=5` and a seeded broker made eligible
through the real `POST /brokers/{id}/trades` path, the startup line logged
`market_hours_gate: weekend_utc` and the loop emitted 12 consecutive
`portfolio_snapshot_cycle_gated` events with **zero** captures and zero
`portfolio_snapshots` rows. Restarting the same process against the same
broker with `PORTFOLIO_SNAPSHOT_MARKET_HOURS_GATE_ENABLED=false` produced
zero gated events, 6 `portfolio_snapshot_captured` events, and 6 real rows
at `total_equity = 100100.00000000` (100000 − 10×100 + 10×110, all real
fills) — confirming the suppression was the gate's doing and not a broken
setup. Seeded rows were removed afterwards.
Status: Implemented and verified as above. Scope is weekend-only by
design; per-exchange session and holiday awareness remains open.

**D043 — Full post-merge backend verification (Phases 34/35): clean run, real merged test count confirmed at 349**

Date: 2026-08-30
Decision: Ran a from-scratch backend verification of the fully-merged
main branch at `ba941cb` (Phase 34 FIFO/LIFO cost-basis and Phase 35
market-hours scheduler gate both merged), following the exact
D028/D033/D036/D040 precedent: fresh Python venv (`.venv-verify3435`,
discarded after the run), `pip install -e ".[dev]"`, `ruff check .`,
`mypy apps`, a real Postgres 16 (timescaledb image) and Redis 7 via a
standalone `docker-compose.verify3435.yml` (not an override of the base
compose file, to avoid D040's port-merge pitfall) under its own compose
project name `tradingos-verify34-35` on remapped host ports 55432/56379,
`alembic upgrade head` against that database, then `pytest tests/ -q`
with a throwaway `.env.verify3435` pointing `DATABASE_URL`/`REDIS_URL` at
the remapped pair. The user's own dev stack
(`trading-os-postgres-1`/`trading-os-redis-1` on default ports
5432/6379, a uvicorn process on port 8000, a Next.js dev server on port
3005, all using the real root `.env`) was left running and untouched
throughout, confirmed still up both before and after this pass; no bare
`docker compose down` was run and the real `.env` file was never read or
written.
Verified live: `ruff check .` reported "All checks passed!" with zero
findings. `mypy apps` reported "Success: no issues found in 75 source
files" with zero findings. `alembic upgrade head` applied all eleven
migrations in sequence — 0001 through Phase 33's `0010_emergency_stop_events`
— against a real, freshly created database with no manual intervention
and no new migration from either Phase 34 or 35, as expected; 0010 is
still the head. `pytest tests/ -q` collected and ran the entire suite
against that same real Postgres/Redis pair: **349 tests, all passing**, 3
warnings (the same two pre-existing `InsecureKeyLengthWarning`s and one
`StarletteDeprecationWarning` seen in every prior verification pass —
neither new nor actionable), zero failures, zero errors, in a clean run.
One transient failure surfaced on the first attempt
(`test_missing_jwt_secret_key_fails_closed`) — the same recurring D036/D040
shell-environment-leakage artifact: this verification's own shell had
exported `JWT_SECRET_KEY` via `set -a; source .env.verify3435`, which
leaked into that one test's `Settings(_env_file=None)` construction;
unsetting `JWT_SECRET_KEY` and re-running reproduced the clean 349-pass
result with no code changes required. This is not a cross-phase
integration bug and nothing in `apps/` was touched.
Reason: this pass found no real cross-phase integration bug between
Phase 34's FIFO/LIFO cost-basis method (D041) and Phase 35's market-hours
scheduler gate (D042) — ruff, mypy, all eleven migrations, and the full
test suite were all clean once the verification's own environment
leakage was corrected (the same class of leakage D036 and D040 already
documented, now confirmed to recur a third time with the same fix). The
confirmed 349 matches the arithmetic the task anticipated (304 D040
baseline + Phase 34's independently-confirmed 317 total's 13 new tests +
Phase 35's independently-confirmed 336 total's 32 new tests = 304 + 13 +
32 = 349), since the two phases' new test files are disjoint and neither
touches the other's code path.
Alternatives: none considered — this is a verification pass, not a
design decision; the only question was whether Phase 34's and Phase 35's
independently-reported worktree counts (317 and 336, each off the same
304 baseline) summed correctly to the true merged total, and a
clean-room run was the only way to be sure rather than assume from the
arithmetic alone.
Status: Implemented and verified as above.

---

**D044 — `portfolio_snapshots.cost_basis_method`: closing D041's single tracked follow-on, on the write path and in the historical record**
Date: 2026-08-30
Numbering note: developed in parallel with sibling Phase 37 in a separate
worktree, both branched from the same `main` (D043, the last entry on that
branch). Phase 37 is frontend-only (`apps/web`, Playwright E2E) and had
claimed no D-number at the time this was written — checked directly rather
than assumed — so a collision is unlikely; if one occurred anyway, the
content, not the digits, is the record.
Decision: migration `0011_portfolio_snapshots_cost_basis_method.py` adds
`cost_basis_method VARCHAR(16) NOT NULL DEFAULT 'average'` to
`portfolio_snapshots` (`PortfolioSnapshotRow` in
`apps/api/app/db/models.py`). `persist_portfolio_snapshot()` takes a
keyword-only `cost_basis_method` (default AVERAGE) and stores its `.value`.
`POST /brokers/{broker_id}/portfolio/snapshots` takes an optional
`cost_basis_method` in its **request body** (new
`PortfolioSnapshotCaptureRequest`, which extends `PortfolioMarksRequest`),
threads that one variable into both `compute_portfolio_snapshot()` and
`persist_portfolio_snapshot()`, and echoes it back.
`PortfolioSnapshotHistoryEntry` gains a `cost_basis_method` field, so
`GET .../portfolio/history` names the method behind every row. The
scheduler (`apps/api/app/portfolio/scheduler.py`) threads the same value
through `run_snapshot_cycle()` / `capture_scheduled_snapshot()` /
`PortfolioSnapshotScheduler`, sourced from a new
`portfolio_snapshot_cost_basis_method` setting that **defaults to
`average`** — the scheduler's behaviour is unchanged unless an operator
opts in.
Reason: D041 rejected exactly this on the write path — its alternative (c),
"the load-bearing scope decision" — because `portfolio_snapshots` had no
column recording which method produced a row, so a persisted FIFO snapshot
would read back through `GET .../history` as though it were average-cost,
silently mixing incomparable numbers in a single append-only time series.
D041 named the fix precisely: "That needs a migration adding the column,
not a query parameter." This phase is that migration and nothing more
ambitious; the FIFO/LIFO lot math is D041's, unchanged and untouched, and
the new tests assert the persisted figures against the same
independently-hand-computed answers (225/250/200 realized on buy 10 @ 100,
buy 10 @ 110, sell 15 @ 120) rather than against whatever the code stored.
`NOT NULL` with `server_default='average'` rather than a nullable column
is the load-bearing choice here. Every pre-existing row was written by the
average-only write path D041 deliberately scoped it to, so backfilling
them as `average` records a fact we actually have; a nullable column would
have made them read as "method unknown", which is *less* true than what is
known and would push a decision onto every future reader of the series.
The default is kept on the column rather than dropped after the backfill,
so an INSERT from any code path predating the model change still lands a
truthful non-null value instead of failing. This is verified explicitly
rather than asserted: a test INSERTs a row omitting the column entirely —
the exact shape of a pre-migration write — and asserts both the raw
Postgres value and the HTTP response read `average`, never null.
The method is a **body field on the POST but stays a query parameter on
the GET**. That asymmetry was chosen over uniformity: the GET's query
parameter is its shipped D041 contract and its body is entirely optional,
while the POST already has a body that this belongs in, next to the marks
the figures are computed against. The residual footgun — a caller copying
`?cost_basis_method=fifo` onto the POST, where FastAPI ignores it — is
answered by the response now echoing the method actually used, so the
mistake is visible instead of silently mislabelling stored history. That
is asserted by a test, not left to documentation.
The scheduler setting was the phase's genuinely optional item, and it was
added rather than deferred because it turned out to be threading one
default-preserving keyword argument through three call sites with no new
failure mode: the default is AVERAGE at every level, so an unconfigured
deployment is byte-identical to D030, and typing the setting as the real
`CostBasisMethod` enum (not `str`) makes a typo'd
`PORTFOLIO_SNAPSHOT_COST_BASIS_METHOD` fail at app startup rather than
producing a run of rows labelled with a method nothing can read back. That
import makes `core/config.py` depend on `portfolio/models.py`, which is
pure Pydantic/enum with no config, DB or I/O, so it cannot cycle back.
Alternatives: (a) a Postgres `ENUM` type for the column — rejected, every
other enum in this schema (`orders.side`, `orders.status`) persists as a
plain string, and adding a fourth method later would then be a migration
mutating a type every existing row depends on rather than an application
change. (b) backfilling with `NULL` and treating null as "legacy/unknown"
— rejected as above; it discards information we have. (c) adding
`cost_basis_method` to the `PortfolioSnapshot` response of the read-only
GET as well, making that endpoint self-describing — rejected, still:
D041's compatibility guarantee is asserted as *full response-object
equality* between the no-parameter and `average` responses, and adding a
field would break the default response bytes for every existing caller of
a read-only endpoint that gains nothing from it (the caller already knows
what it asked for). Persisted rows are different: nobody remembers what a
row from three weeks ago was captured under. (d) rewriting existing rows
when the scheduler's configured method changes — rejected,
`orders`/`fills` and these tables are append-only (D006/D027); each row
records the method it was captured under, so a series that changes method
mid-life stays honest and readable rather than being retroactively
falsified.
Consequences: `GET .../history` can now legitimately return a series whose
rows are not comparable to each other. That is strictly better than the
pre-D044 state, where the same thing could not happen only because the
choice was withheld — but it does move an obligation onto the client, so
docs/API.md states it as a requirement ("a client must group or filter by
this field before treating the series as one equity curve") rather than a
note. Nothing in `persist_portfolio_snapshot()` can detect a caller that
computes under FIFO and persists under AVERAGE; both callers thread a
single variable into both calls specifically so the two cannot drift, and
that module's docstring says so for the next caller.
Status: Implemented, tested, verified live. **359/359 pytest passing**
(349 D043-confirmed merged baseline + 10 net new: 6 in
`tests/api/test_portfolio.py` — which also *replaced* the now-obsolete
`test_persisted_snapshots_stay_average_cost_regardless_of_the_query_param`,
D041's guard against the very behaviour this phase deliberately adds,
hence 6 new for +5 net — 3 DB-backed scheduler tests in
`tests/api/test_snapshot_scheduler.py`, and 2 settings tests in
`tests/test_config.py`). `ruff check .` and `mypy apps` clean (75 source
files). Verified live against this worktree's own Docker Postgres/Redis
and a rebuilt API container (host ports remapped to 5452/6399/8020 via an
untracked `docker-compose.override.yml` using `!override`, to avoid both
the user's own dev stack and the sibling Phase 37 worktree; torn down
afterwards, and `docker-compose.yml` itself never modified). All eleven
migrations ran clean from empty, and `\d portfolio_snapshots` confirmed
`cost_basis_method | character varying(16) | not null | 'average'`. Over
real HTTP against the running container, on a real multi-lot history
seeded through the real trade endpoint (buy 10 @ 100, buy 10 @ 110, sell
15 @ 120, marked at 130): the default POST persisted and read back
`average` basis 105 / realized 225 / unrealized 125; `fifo` 110 / 250 /
100; `lifo` 100 / 200 / 150 — each matching D041's already-verified
hand-computed figure — with `cash` 99700 and `total_equity` 100350
identical across all three, `GET .../history` naming each row's method,
`cost_basis_method=hifo` rejected 422 with nothing written, a row INSERTed
without the column reading back `average` in both Postgres and the HTTP
response, the startup log line reporting
`portfolio_snapshot_cost_basis_method: average`, and
`GET .../portfolio?cost_basis_method=fifo` still returning D041's 250 with
no new field on its response. Every seeded row was deleted afterwards; the
`.venv36`, `.env`, `docker-compose.override.yml` and the containers/volume
created for this verification were removed.
Status: Implemented and verified as above.

---

**D045 — Playwright e2e coverage for `apps/web/`: the last remaining frontend candidate, against a real backend only**

Date: 2026-08-30
Decision: Added a Playwright end-to-end suite for `apps/web/` — the one
frontend candidate `docs/PROJECT_CONTEXT.md` had been carrying since
Phase 28, deliberately skipped there because it needs a new tool
dependency. The user granted permission for that dependency in this
phase, so `@playwright/test` (plus a single browser engine, Chromium —
not all three) is now a devDependency of `apps/web` and the only new
dependency this phase adds. This entry is D045, not D044: the sibling
phase-36 worktree, developed in parallel off the same `main` at
`ed02f1b`, already claimed D044 for `portfolio_snapshots.cost_basis_method`,
so the next free number was taken per this project's parallel-worktree
numbering convention.

Shape: `apps/web/playwright.config.ts` starts `next dev` itself via
`webServer` and points its route handlers at a configurable backend
(`E2E_API_BASE_URL`, defaulting to `http://localhost:8000`, passed
through as the `API_BASE_URL` the handlers already read), so a local run
and a CI run can target different backends without editing a file.
`E2E_WEB_PORT` (default 3100) picks the dev-server port. The suite lives
in `apps/web/e2e/`, is run by a new `npm run test:e2e`, and is
deliberately NOT wired into `npm test`: `vitest.config.ts` now excludes
`e2e/**`, because Vitest's default `**/*.spec.ts` glob would otherwise
collect specs that cannot run without a database. Unit/component and e2e
stay separate suites, as they are in most Next.js projects.

Nothing in this suite is mocked. That is the whole justification for it
existing alongside the 99 Vitest component tests, which already cover
rendering against fabricated responses — repeating that here would prove
nothing. Every spec drives a real Chromium against a real `next dev`,
whose route handlers proxy to a real FastAPI process, backed by a real
Postgres with real migrated tables and real seeded rows. The trades the
suite submits are real `orders`/`fills` rows produced by the real
deterministic Risk Engine (D004) and the real trade-path Portfolio
Manager (D029).

`scripts/seed_e2e.py` writes those fixtures by direct SQL — the same
bootstrap every phase has used since D013, because there is no user
holding `admin:manage` to call `/admin/*` with the first time. It creates
two roles, an admin and a non-admin trader, and three paper brokers: a
flat one (100,000 cash, no positions), a concentrated one (80,000 cash +
200 `AAPL.US`), and one no fixture user holds a grant for. The
concentrated broker is sized so that a 100-share buy at 100.00 passes the
Risk Engine (10,000 notional == the 10%-of-equity single-position cap
exactly) and is then shrunk to 50 by the Portfolio Manager's
25%-of-equity per-symbol cap — a genuine MODIFY produced by real code,
not a stubbed verdict. Playwright's `globalSetup` re-runs the seed before
every run (`E2E_SEED_COMMAND` / `E2E_SKIP_SEED`), because those trades
permanently change real broker rows and the MODIFY case only holds while
the concentrated broker still holds exactly its seeded position. For the
same reason the suite is single-worker, serial, and `retries: 0` — a
retry would re-submit real trades against a book the first attempt
already changed, so a "pass on retry" would mean nothing.

Every trade the suite submits supplies an explicit `estimated_price`,
i.e. D017's caller-authoritative path. No market-data vendor is consulted
and no quote is invented, which is what makes the asserted risk and
portfolio verdicts deterministic rather than dependent on whatever the
market was doing.

Two environment-level findings were required to make any of this work,
both recorded here because they will bite the next person:
1. The base URL must be `http://localhost:<port>`, not
   `http://127.0.0.1:<port>`. Next 16's dev server 403s asset requests
   whose Host is a bare IP not listed in `allowedDevOrigins`, which
   leaves the page server-rendered but never hydrated. A click then fires
   the browser's NATIVE form submit instead of the React handler, the
   page navigates to `/login?`, and the spec fails for a reason that has
   nothing to do with the product.
2. Even on `localhost`, specs must wait for hydration before interacting,
   for the same reason. `e2e/helpers.ts`'s `waitForHydration()` polls for
   the `__reactFiber$…` expando React stamps on every host node it
   hydrates — a real signal, not a sleep. `networkidle` is unusable here
   because the dev server holds an HMR socket open.
Timeouts are deliberately generous (120s per test, 30s per assertion):
`next dev` compiles each route and route handler on first request, and a
cold compile of a data-fetching component genuinely takes tens of seconds.
Tighter values produced a flake that said nothing about the product.

What the 11 specs cover: login with valid credentials reaching
`/dashboard` with a real httpOnly cookie the browser's own
`document.cookie` cannot see; login with invalid credentials rendering
the backend's real `Incorrect email or password.` with no redirect and no
cookie; no cookie at all being bounced off `/dashboard` by the proxy; an
invalid session on a protected page redirecting to
`/login?reason=session-expired` with D032's real explanation; a real
approved trade rendering a real fill and (per D038) NO Portfolio Manager
panel; a real `missing_stop_price` risk rejection rendering amber with no
portfolio panel; a real Portfolio Manager MODIFY rendering violet-adjacent
beside a green risk verdict with requested 100 / filled 50; a real 403 on
a broker the user holds no grant for; `BrokerDiscovery` listing the
user's real granted brokers and pushing one into the trade and
agent-trade forms; and `/admin` listing the real users, roles and grants
for an admin — with the same page rendering three real 403s, and no user
rows, for a non-admin.

Scope cuts, stated plainly rather than faked:
- No spec exercises `AgentTradeForm`'s success path, the quote lookup's
  success path, `PortfolioView`, `PortfolioHistoryChart`, or
  `BacktestPanel`'s success path. Each of those needs a real LLM provider
  or a real market-data vendor, and neither is configured in this
  environment. Writing specs that assert the `NOT_CONFIGURED:` sentinel
  would have been possible but adds nothing the Vitest suite doesn't
  already cover, and asserting a success path would have required
  fabricating vendor data (spec §57).
- The Portfolio Manager's REJECT and `max_open_positions` paths are not
  covered. Both require a broker holding positions in symbols other than
  the one being traded, and `TradeForm` has no "marks" input — such a
  trade fails with a real `DATA_UNAVAILABLE` before any verdict is
  reached. That is a true property of the current UI, so the gap is
  recorded rather than worked around by calling the API directly, which
  would no longer be an e2e test of the frontend.
Verified live: `npm run test:e2e` was run end-to-end against a stack
brought up for this phase — a real Postgres 16 (timescaledb image) and
Redis 7 under compose project `tradingos-e2e37` on remapped host ports
55437/56437, `alembic upgrade head` applying all eleven migrations, a
real uvicorn process on 127.0.0.1:8037 with a throwaway `.env.e2e37`, the
seed script, `next dev` on port 3137, and a real Chromium. **All 11 specs
passed, three times consecutively** (54.3s, 1.5m and 1.7m wall clock, the
last of those through `npm run test:e2e` itself). An earlier run
of the same 11 had 1 failure — the broker-listing assertion timing out on
a cold route-handler compile — which is what the timeout increase above
fixed; it is reported here rather than quietly re-run away. The user's own
dev stack (`trading-os-postgres-1`/`trading-os-redis-1` on 5432/6379, plus
their uvicorn on 8000 and Next dev on 3005) was left running and
untouched throughout, and this phase's own containers, venv, `.env`, and
compose file were torn down afterwards. Alongside: 99 Vitest component
tests still pass (unchanged — this phase adds no Vitest test, and the new
exclude correctly keeps the e2e specs out of that run), `npm run build`
has zero type errors, and `ruff check .` / `mypy apps scripts` are clean
with the new seed script included.
Alternatives: (a) mocking the backend with Playwright's `page.route()` —
rejected outright: that is exactly what the existing Vitest suite already
does, and an e2e suite that never touches the real API would be a more
expensive way to test less. (b) Installing all three browser engines —
rejected as unjustified cost for a suite whose assertions are about
application behaviour, not rendering-engine differences. (c) Adding
`allowedDevOrigins: ["127.0.0.1"]` to `next.config.ts` to work around
finding (1) — rejected because it changes production configuration to
suit a test; using `localhost` costs nothing. (d) Folding e2e into
`npm test` — rejected: it would make the unit suite unrunnable without
Docker, a database, and a browser.
Status: Implemented and verified as above.

---

**D046 — Full post-merge integration verification (Phases 36/37): clean run, real merged test count confirmed at 359 backend / 99 Vitest**

Date: 2026-08-30
Decision: Ran a from-scratch full backend integration verification of the
fully-merged main branch at `034f4c3` (Phase 36's new migration 0011,
`portfolio_snapshots.cost_basis_method`, and Phase 37's frontend-only
Playwright e2e suite both merged), following the exact
D028/D033/D036/D040/D043 precedent: fresh Python venv (discarded after
the run), `pip install -e ".[dev]"`, `ruff check .`, `mypy apps`, a real
Postgres 16 (timescaledb image) and Redis 7 via a standalone throwaway
`docker-compose.verify.yml` (not an override of the base compose file,
per D040's port-merge lesson) under its own compose project name
`tos-verify` on remapped host ports 15432/16379, `alembic upgrade head`
against that database, then `pytest tests/ -q` with a throwaway
`.env.verify` pointing `DATABASE_URL`/`REDIS_URL` at the remapped pair.
The user's own dev stack (`trading-os-postgres-1`/`trading-os-redis-1` on
default ports 5432/6379, a uvicorn process on port 8000, a Next.js dev
server on port 3005, all using the real root `.env`) was left running and
untouched throughout, confirmed still up both before and after this pass
via `docker ps` and live HTTP checks against 8000 and 3005; no bare
`docker compose down` was run and the real `.env` file was never read or
written. Separately, `cd apps/web && npm install && npm run build && npm
test` was run to check the frontend, deliberately skipping `npm run
test:e2e` per this task's scope — that Playwright suite needs its own
real backend/DB setup and was already verified live by Phase 37's own
agent (see D045).
Verified live: `ruff check .` reported "All checks passed!" with zero
findings. `mypy apps` reported "Success: no issues found in 75 source
files" with zero findings. `alembic upgrade head` applied all eleven
migrations in sequence — 0001 through Phase 36's
`0011_portfolio_snapshots_cost_basis_method` — against a real, freshly
created database with no manual intervention and no migration beyond
0011, as expected. `pytest tests/ -q` collected and ran the entire suite
against that same real Postgres/Redis pair: **359 tests, all passing**, 3
warnings (the same two pre-existing `InsecureKeyLengthWarning`s and one
`StarletteDeprecationWarning` seen in every prior verification pass —
neither new nor actionable), zero failures, zero errors, in a clean run.
One transient failure surfaced on the first attempt
(`test_missing_jwt_secret_key_fails_closed`) — the same recurring
D036/D040/D043 shell-environment-leakage artifact: this verification's
own shell had exported `JWT_SECRET_KEY` via `set -a; source .env.verify`,
which leaked into that one test's `Settings(_env_file=None)`
construction; unsetting `JWT_SECRET_KEY` and re-running reproduced the
clean 359-pass result with no code changes required. This is not a
cross-phase integration bug and nothing in `apps/` was touched. On the
frontend: `npm install` and `npm run build` both succeeded with zero
type errors, and `npm test` (Vitest only) reported **99 tests, all
passing** across 12 test files — exactly Phase 37's own reported count,
unchanged, since Phase 37 added no new Vitest test file.
Reason: this pass found no real cross-phase integration bug between
Phase 36's cost-basis-method persistence (migration 0011, D044) and Phase
37's frontend-only e2e work — ruff, mypy, all eleven migrations, and the
full backend test suite were all clean once the verification's own
environment leakage was corrected (the same class of leakage D036, D040,
and D043 already documented, now confirmed to recur a fourth time with
the same fix). The confirmed 359 matches Phase 36's own independently-
reported total exactly, since Phase 37 shipped no backend code and no new
`tests/*.py` file; the confirmed 99 Vitest tests likewise match Phase
37's own report exactly.
Alternatives: none considered — this is a verification pass, not a
design decision; the only question was whether Phase 36's independently-
reported 359 total held up in a real from-scratch clean-room run against
merged `main`, and whether Phase 37's frontend build and Vitest suite
still passed unchanged, and a clean-room run was the only way to be sure
rather than assume from either phase's own report alone.
Status: Implemented and verified as above.

---

**D047 — Multi-worker safety for the snapshot scheduler: a non-blocking Postgres advisory lock per cycle, closing D030's last open consequence with no new dependency**
Date: 2026-08-30
Decision: Added `apps/api/app/portfolio/cycle_lock.py` — a frozen,
connection-agnostic `SnapshotCycleLock` whose `hold(session)` async
context manager takes `SELECT pg_try_advisory_lock(classid, objid)` on a
session opened solely to own the lock, yields a typed
`SnapshotCycleLockDecision`, and releases with `pg_advisory_unlock` in a
`finally` on every path including an exception. `run_snapshot_cycle()`
now takes an optional `cycle_lock=` and, after the D042 market-hours gate
but *before* enumerating brokers, returns immediately with
`lock=SKIPPED_LOCK_HELD` and no outcomes when another process holds it.
`SnapshotCycleResult` gained a `lock` field plus `lock_skipped` and `ran`
properties; `PortfolioSnapshotScheduler` gained a `cycle_lock=`
constructor argument threaded into every cycle; `main.py` wires
`SnapshotCycleLock(enabled=settings.portfolio_snapshot_cycle_lock_enabled)`
from the new `PORTFOLIO_SNAPSHOT_CYCLE_LOCK_ENABLED` setting (**default
true**). No new pip dependency, no new table, no migration.
Reason: D030's "Consequences" named exactly one open trigger for
revisiting its design — "Each API worker process runs its own
independent loop, so running more than one uvicorn/gunicorn worker would
multiply snapshot rows". That is a real correctness bug in an
*append-only* table: four workers produce four near-simultaneous rows per
interval, none individually wrong, forming a series that is, permanently.
A session-level advisory lock is the smallest honest fix. It is
non-blocking on purpose (`pg_try_...`, not `pg_advisory_lock`): a
contender that waited would simply stack up behind the winner and then
write the duplicate row anyway. It is released with its connection, so a
SIGKILLed worker cannot wedge the schedule the way a hand-managed
`scheduler_leader` row with an expiry could — which is also why no
migration was needed. The lock guards a *cycle*, not a snapshot: the
manual `POST /brokers/{id}/portfolio/snapshots` endpoint (D027) is
untouched and still writes whenever a human asks, because a deliberate
request with a caller watching the result is not the failure mode being
suppressed.
The two-int4 key form was chosen over
`hashtext('portfolio_snapshot_scheduler')::bigint`: `hashtext` is an
undocumented internal whose output is not contractually stable across
major versions, and a value that shifted under a rolling upgrade would
silently split the lock in two during precisely the window where two
worker vintages run at once. Fixed constants (`classid` = ASCII `b"trad"`
= 1953653092, `objid` = ASCII `b"snap"` = 1936613744) are also what
`pg_locks` shows an operator, verbatim and greppable, and leave a free
namespace for any future background job in this repo.
Alternatives: **A Redis lock** — rejected. Redis is *provisioned*
(docker-compose.yml, `Settings.redis_url`) but as of this phase no Python
code in the repo opens a Redis connection; the `redis` package is an
unused declared dependency. Building the codebase's first Redis client to
protect a snapshot loop would introduce a whole new runtime dependency
edge and a new "what if Redis is down" failure mode. Postgres is the one
service the cycle cannot run without anyway, so a Postgres lock adds
nothing that can fail independently of the work it guards.
**APScheduler / Celery / a leader-election library** — rejected for the
same reasons D030 rejected them, now with evidence: the multi-worker case
was the one thing D030 conceded might justify them, and it turned out to
need nine lines of raw SQL rather than a new tool with its own
configuration surface and safety review.
**A `scheduler_leader` table with a lease/expiry** — rejected: it needs a
migration, a clock the app must trust, and an expiry long enough to
survive a slow cycle yet short enough to recover from a crash. Advisory
locks get crash recovery free from the connection lifetime.
**Blocking `pg_advisory_lock`, or an xact-scoped
`pg_advisory_xact_lock`** — rejected: the blocking form serializes the
losers into writing the duplicates anyway, and the xact-scoped form would
tie the lock's lifetime to a transaction boundary the cycle does not
otherwise have (each broker deliberately gets its own session, D030).
**Deduplicating after the fact (a unique index on
`(broker_id, captured_at)` or similar)** — rejected: `captured_at` is a
server `now()` with microsecond resolution, so near-simultaneous rows do
not actually collide, and any coarser bucketing would start rejecting
legitimate manual snapshots.
Consequences: One new setting, defaulting true. With a single worker the
lock is always acquired and the cycle proceeds into byte-identical code —
verified — at the cost of one pooled connection held for the cycle and
two trivial statements. The lock session is deliberately never committed
(a committed session returns its connection to the pool and would hand
the lock away), so a cycle holds one idle-in-transaction connection for
its duration; at this scale, with one cycle per hour by default, that is
immaterial, but it is the reason the lock session issues no other query.
Setting `PORTFOLIO_SNAPSHOT_CYCLE_LOCK_ENABLED=false` restores D030's
unguarded behaviour exactly (no lock statement at all) and should only be
done with one worker. This does NOT make the scheduler
restart-persistent or cron-capable, and it does not coordinate anything
except this one cycle; a second background job would need its own objid.
Verification: `ruff check .` clean, `mypy apps` clean (76 source files),
**379/379 tests passing** against real Postgres (359 pre-existing + 20
new: 8 unit in `tests/portfolio/test_cycle_lock.py` covering the key
constants, the decision enum and the release discipline including the
exception path; 2 settings tests in `tests/test_config.py`; 10 DB-backed
integration in
`tests/api/test_snapshot_scheduler_multiworker.py`, where the contending
"other worker" is a genuinely separate real database session really
holding the real advisory lock — nothing about the contention is
simulated). Live-verified in this worktree against a real Postgres
(isolated docker compose project `tos38` on port 55432, so the user's own
dev stack on 5432 was never touched) with fixtures from
`scripts/seed_e2e.py`: **two separate OS processes**, each running one
real `run_snapshot_cycle`, released against the same database at the same
instant. With the lock enabled, pid 20872 logged
`portfolio_snapshot_captured` and pid 11072 logged
`portfolio_snapshot_cycle_lock_not_acquired` and returned
`{"lock": "skipped_lock_held", "ran": false, "captured": 0}` — **one
row** in `portfolio_snapshots`. The identical race with
`SnapshotCycleLock(enabled=False)` produced **two rows** from two
`captured` outcomes, which is the pre-D047 bug reproduced live and the
control proving the lock is what prevents it. In both runs the
market-data-less broker was skipped honestly with
`skipped_market_data_not_configured`, never valued.
Status: Implemented and verified as above.

---

**D048 — Full post-merge integration verification (Phase 38): clean run, real merged test count confirmed at 379**

Date: 2026-08-30
Decision: Ran a from-scratch full backend integration verification of the
fully-merged main branch at `b0040ea` (Phase 38's multi-worker
snapshot-scheduler advisory-lock code, D047, merged), following the exact
D028/D033/D036/D040/D043/D046 precedent: fresh Python venv (discarded
after the run), `pip install -e ".[dev]"`, `ruff check .`, `mypy apps`, a
real Postgres 16 (timescaledb image) and Redis 7 via a standalone
throwaway `docker-compose.verify38.yml` (not an override of the base
compose file, per D040's port-merge lesson) under its own compose project
name `tosverify38` on remapped host ports 15532/16479, `alembic upgrade
head` against that database, then `pytest tests/ -q` with `DATABASE_URL`
and `REDIS_URL` exported directly in-shell (no throwaway `.env` file
needed, since `apps/api/app/core/config.py`'s `Settings` reads environment
variables ahead of its `.env` file). The user's own dev stack
(`trading-os-postgres-1`/`trading-os-redis-1` on default ports 5432/6379,
a uvicorn process on port 8000, a Next.js dev server on port 3005, all
using the real root `.env`) was left running and untouched throughout,
confirmed still up both before and after this pass via `docker ps` and
live HTTP checks against 8000 and 3005; no bare `docker compose down` was
run and the real `.env` file was never read or written.
Verified live: `ruff check .` reported "All checks passed!" with zero
findings. `mypy apps` reported "Success: no issues found in 76 source
files" (76 vs. D046's 75 reflects Phase 38's new
`apps/api/app/portfolio/cycle_lock.py`) with zero findings. `alembic
upgrade head` applied all eleven migrations in sequence — 0001 through
Phase 36's `0011_portfolio_snapshots_cost_basis_method`, still the
current head — against a real, freshly created database with no manual
intervention and no new migration from Phase 38, exactly as expected for
an advisory-lock feature that adds no schema. `pytest tests/ -q` collected
and ran the entire suite against that same real Postgres/Redis pair:
**379 tests, all passing**, 3 warnings (the same two
`InsecureKeyLengthWarning`s and one `StarletteDeprecationWarning` seen in
every prior verification pass — neither new nor actionable), zero
failures, zero errors, in a clean run. One transient failure surfaced on
the first attempt (`test_missing_jwt_secret_key_fails_closed`) — the same
recurring D036/D040/D043/D046 shell-environment-leakage artifact: this
verification's own shell had exported `JWT_SECRET_KEY` ahead of `alembic
upgrade head` and the first `pytest` run, which leaked into that one
test's `Settings(_env_file=None)` construction; unsetting
`JWT_SECRET_KEY` (keeping only `DATABASE_URL`/`REDIS_URL` exported) and
re-running reproduced the clean 379-pass result with no code changes
required. This is not a cross-phase integration bug and nothing in
`apps/` was touched.
Reason: this pass found no real cross-phase integration bug in Phase 38's
advisory-lock cycle guard (D047) against any earlier phase's work — ruff,
mypy, all eleven migrations, and the full backend test suite were all
clean once the verification's own environment leakage was corrected (the
same class of leakage D036, D040, D043, and D046 already documented, now
confirmed to recur a fifth time with the same fix). The confirmed 379
matches Phase 38's own independently-reported total exactly, since no
other phase merged in between D047 and this verification.
Alternatives: none considered — this is a verification pass, not a
design decision; the only question was whether Phase 38's independently-
reported 379 total held up in a real from-scratch clean-room run against
merged `main`, and a clean-room run was the only way to be sure rather
than assume from the phase's own report alone.
Status: Implemented and verified as above.

---

**D049 — Failed-login lockout for `POST /auth/login`: a Postgres-persisted per-account counter, and a 423 that is only ever shown to a caller who already knows the password**

Date: 2026-08-30
Decision: `POST /auth/login` — until now the one unthrottled credential
endpoint in the app — now counts CONSECUTIVE failed password attempts per
account and locks the account for a configured window once a threshold is
reached. Two new columns on `users` carry the whole mechanism
(migration `0012`): `failed_login_count` (NOT NULL, `server_default='0'`)
and `locked_until` (nullable `timestamptz`; NULL means "never locked",
which is deliberately distinguishable from a past timestamp meaning "was
locked, expired"). Two new settings configure it:
`AUTH_MAX_FAILED_LOGIN_ATTEMPTS` (default 5, `0` disables the feature
entirely) and `AUTH_LOCKOUT_DURATION_MINUTES` (default 15). A negative
threshold, or a non-positive duration while the lockout is enabled, fails
at app startup rather than at the first failed login — both mistakes fail
in the dangerous direction (no lockout, or a lock that expires the instant
it is set). The arithmetic lives in a pure module,
`apps/api/app/auth/lockout.py`: no session, no request, no clock of its
own — `now` is always passed in, through the module's single `utcnow()`
seam, which is what lets the expiry path be tested in milliseconds instead
of by sleeping for fifteen real minutes.

The response shape is the part that needed the most care, and it is not
the obvious one. The lockout answer is **423 Locked**, and it is raised
**only after the presented password has been verified as correct**. The
full ordering in `login.py` is: look up the user; bcrypt-verify against
the real hash or `_DUMMY_HASH` (unchanged — the existing timing-parity
discipline still runs on every request, including locked ones); if the
credentials are bad, answer the generic 401 and, only for a real active
account with a real wrong password that is not already locked, advance the
counter; then, and only then, check the lock and answer 423. The
consequence is that 423 is unreachable by anyone who does not already know
the password: an unknown email always 401s (there is no row to count
against), a registered email with a wrong password always 401s whether or
not it is locked, and only the legitimate account holder — or an attacker
who has already won — ever sees the word "locked". The lockout therefore
adds no email-enumeration oracle on top of what `_DUMMY_HASH` was written
to prevent, while still telling the real user the truth: "Account
temporarily locked after repeated failed login attempts. Try again later."
The message names the cause and the remedy but not the remaining time,
which would be a free signal on exactly when to resume guessing. A wrong
password against an already-locked account does **not** extend the lock;
otherwise an attacker could hold a victim out of their own account
indefinitely by guessing forever. An expired lock resets the run to zero
before counting the next failure, so a user who was locked once, waited it
out, and then mistyped is at 1-of-N rather than N-and-instantly-relocked.
A successful login always clears both columns.

`apps/web/` needed no change to surface this correctly: D020's login route
handler already forwards the backend's real status and its `detail`
verbatim, and `app/login/page.tsx` renders that `detail` rather than a
generic string, so the 423 message reaches the user as written. Three new
Vitest tests (`test/LoginForm.test.tsx`) pin exactly that, in the spirit
of D020's "never a generic 'error occurred' swallowing which case fired" —
including the negative assertion that the 423 case does not render as
"Incorrect email or password", which would send a locked-out user hunting
for a typo that isn't there.

Reason: rate limiting was the right fix and a *per-account persisted
lockout* was the right shape of it. Three alternatives were rejected. (1)
A pip rate-limit library (`slowapi` et al.): this repo's policy is to ask
before adding a tool, and the fix does not need one — the entire mechanism
is two columns and ~90 lines of pure arithmetic. (2) An in-process
sliding-window counter using only the stdlib: cheaper still, but its state
dies on restart (an attacker gets a fresh budget from any deploy or crash)
and is per-worker, so running the API under N uvicorn workers multiplies
the real threshold by N — the exact class of multi-worker defect Phase 38
had just finished closing for the snapshot scheduler (D047). Documenting
that as an honest limitation, the way D030/D042 documented theirs, was
considered and rejected: unlike a skipped weekend snapshot, a silently
5x-weakened brute-force defence is a limitation whose whole cost lands on
the security property the change exists to provide. (3) Redis: it is
provisioned in `docker-compose.yml` and `Settings.redis_url` exists, but
no Python code in this repo has ever opened a Redis connection. Being the
first to wire in a Redis client — with its connection lifecycle, its
failure-mode question (does login fail open or closed when Redis is
down?), and its new hard runtime dependency for the auth path — is a
larger architectural decision than a MEDIUM-severity finding warrants, and
would have been made in passing rather than deliberately. Postgres is
already a hard dependency of this endpoint (the user row is read there
anyway), so the counter costs no new infrastructure, no new dependency,
and no new failure mode; it survives restarts; and it is correct under any
number of workers for free, because the database is the shared state.
Per-account rather than per-IP is the deliberate axis: per-IP is trivially
defeated by a botnet and punishes shared NATs, whereas the asset being
protected here is a specific account's password.
Alternatives: the three above (a rate-limit dependency, in-process
stdlib-only counters, Redis). Also considered and rejected: returning 429
instead of 423 (429 means "you sent too many requests", which is about the
caller; the account is what is locked, and 423 says so); returning the
generic 401 even for a locked account (safest against enumeration, but the
ordering above already achieves that without lying to the real user, who
would otherwise be left permanently unable to explain why their correct
password stopped working); exposing the remaining lock time in the
response or a `Retry-After` header (a scheduling hint for an attacker,
worth more to them than to the user, who only needs "later"); an
admin unlock endpoint (deferred — the lock is 15 minutes and expires on
its own; an unlock route is new authenticated surface with no current
demand); and clearing an expired `locked_until` eagerly on every login,
which would mean a database write on every successful login by every user
who was ever locked, to save reading a timestamp.
Status: Implemented and verified. `apps/api/app/auth/lockout.py`,
`apps/api/app/auth/routes/login.py`, `apps/api/app/core/config.py`,
`apps/api/app/db/models.py`, `migrations/versions/0012_users_login_lockout.py`,
`.env.example`. 21 new backend tests (9 pure-arithmetic in
`tests/auth/test_lockout.py`, 8 real-Postgres integration in
`tests/api/test_login_lockout.py`, 4 configuration-validation in
`tests/test_config.py`); full backend suite 400 passed, `ruff check .`
clean, `mypy apps` clean (77 source files). Verified live against a real
stack (own compose project on ports 55432/56379, all 12 migrations
applied, uvicorn on 8039, a real user row inserted directly per D010): a
legitimate login returned 200; three real wrong-password POSTs each
returned 401 and left `failed_login_count=3` with a real `locked_until` in
the `users` row; the correct password then returned **423** with the
lockout message; a wrong password at that same moment still returned
**401**, confirming the no-enumeration ordering; and after waiting out a
deliberately short **1-minute** configured window on the real wall clock
(`AUTH_LOCKOUT_DURATION_MINUTES=1`, not a test clock), the same correct
password returned 200 and the row was back to `failed_login_count=0`,
`locked_until=NULL`. The 15-minute default is exercised by the test-clock
path in `tests/api/test_login_lockout.py`; only the short window was
walked in real time, because the two share one code path and the
difference is a `timedelta`.

---

**D050 — CSRF posture recorded as deliberate: `SameSite=Lax` is the defence, and it covers this app's entire mutation surface. Plus the vitest 2 to 4 bump**

Date: 2026-08-30
Decision: Two smaller Phase 39 items, recorded together because both are
"the existing posture is right, say so" rather than new mechanism.

**CSRF.** The `SameSite=Lax` attribute on D020's httpOnly auth cookie is
and remains this app's sole anti-CSRF defence; no anti-CSRF token was
added. That is a deliberate choice, not an oversight, and this entry
exists because a security review correctly flagged that it had never been
written down. `SameSite=Lax` withholds the cookie from any cross-site
request that is not a top-level GET navigation. Every state-changing route
in `apps/web/` is a POST, PATCH, or DELETE to a same-origin Next.js route
handler (`app/api/auth/login`, `app/api/auth/logout`,
`app/api/trades/[brokerId]`, `app/api/agent-trades/[brokerId]`,
`app/api/backtests`, and the six `app/api/admin/*` handlers); not one
mutation is reachable by GET, and none is reachable cross-origin with the
cookie attached. Lax's known gap — cross-site top-level GET navigation
still carries the cookie — is therefore not a gap here, because a GET on
this app changes nothing. The browser never calls the backend directly
(D020), so the backend's own bearer-token API surface is not
cookie-authenticated at all and is structurally immune to CSRF regardless.
Adding a double-submit-cookie token on top would add a token to mint,
rotate, thread through eleven route handlers, and fail loudly on
expiry — new surface and new failure modes, for a class of attack the
current posture already closes. It becomes the right call the day a
state-changing GET route exists, or the day a cookie has to be sent to an
origin the app does not control; until then it would be ceremony that
implies a boundary that is already elsewhere. `docs/API.md` and this entry
are the record; the invariant to preserve is **"no state-changing GET"**,
which is what the whole argument rests on.

**vitest.** `apps/web`'s `vitest@^2.1.8` carried a critical advisory
(arbitrary file read/execute while the Vitest **UI** server is listening)
plus transitive high/moderate `vite`/`esbuild` dev-server advisories. The
project has never used `vitest --ui`, and none of this ships to
production — it is dev tooling only, so this was routine hygiene, not an
exposure. Bumped to `vitest@^4` (4.1.11) and `@vitejs/plugin-react@^6`
(6.1.1, needed so the plugin speaks Vite 8's `oxc` transform instead of
emitting deprecation warnings for the removed `esbuild` options), then
`npm audit fix` for the remainder: **0 vulnerabilities**. Two real config
migrations were required rather than suppressed — `vitest.config.ts` was
renamed to `vitest.config.mts` (Vite's native config loader will not load
ESM syntax from a `.ts` file treated as CommonJS) and its `__dirname`
replaced with `import.meta.dirname` (the native loader injects no CommonJS
globals). Both are the documented fixes, not warning suppressions.
Reason: recording a defensible security posture is itself the deliverable
for the CSRF finding — an undocumented tradeoff is indistinguishable from
an accident to the next reviewer, which is precisely how the finding was
raised. For vitest, the dependency was genuinely stale and the upgrade was
cheap; leaving a critical advisory in the tree because "we don't use that
flag" is an argument that stops being true the first time someone runs
`vitest --ui` to debug a failing test.
Alternatives: a double-submit-cookie CSRF token (rejected above);
`SameSite=Strict` (rejected — it would break the ordinary case of a user
following a link into the app while signed in, for no gain given no
state-changing GET exists); pinning vitest to a patched 2.x (no such
release for this advisory chain — `npm audit` itself routes to 4.x);
suppressing the two new Vite config warnings with
`VITE_CONFIG_NATIVE_IGNORE_WARNING` (rejected — both warnings describe
real removals in a future major, and the fixes are one rename and one
identifier).
Status: Implemented and verified. CSRF: documentation only, no code
change (`docs/DECISIONS.md`, `docs/API.md`). vitest:
`apps/web/package.json`, `apps/web/package-lock.json`,
`apps/web/vitest.config.ts` renamed to `apps/web/vitest.config.mts`. `npm audit`
reports **0 vulnerabilities** (from 5: 1 critical, 1 high, 3 moderate);
`npm test` **102 passed, 13 files** (99 pre-existing, all still green,
plus 3 new `LoginForm` tests from D049) with no warnings; `npm run build`
succeeds. `npm run lint` reports the same 2 errors / 1 warning it reported
before this phase — verified by stashing the change and re-running — all
pre-existing and untouched here. The Playwright e2e suite was not run: it
needs a full running backend and browser engines and was out of scope for
this security-fix phase.

**D051 — Full post-merge integration verification (Phase 39): clean run, real merged test count confirmed at 400, backend + frontend**

Date: 2026-08-31
Decision: Ran a from-scratch full integration verification of the
fully-merged main branch at `c2e0ec4` (Phase 39's login-lockout
migration `0012` and the vitest 2→4 major bump, D049/D050, merged),
covering both backend and frontend, following the exact
D028/D033/D036/D040/D043/D046/D048 precedent. Backend: a fresh Python
venv, `pip install -e ".[dev]"`, `ruff check .`, `mypy apps`, a real
Postgres 16 (timescaledb image) and Redis 7 via a standalone throwaway
`docker-compose.verify.yml` (not an override of the base compose file,
per D040's port-merge lesson) under its own compose project name
`tradingos-verify39` on remapped host ports 55432/56379, `alembic
upgrade head` against that database, then `pytest tests/ -q` with
`DATABASE_URL`/`REDIS_URL`/`JWT_SECRET` exported directly in-shell (no
throwaway `.env` file needed, since `apps/api/app/core/config.py`'s
`Settings` reads environment variables ahead of its `.env` file). Only
Python 3.13 and 3.14 were available on the host (no 3.11/3.12); 3.13 was
used since it is within the project's `>=3.11` requirement and every
dependency provided prebuilt wheels for it. The user's own dev stack
(`trading-os-postgres-1`/`trading-os-redis-1` on default ports 5432/6379,
a uvicorn process on port 8000, a Next.js dev server on port 3005, all
using the real root `.env`) was confirmed untouched throughout — no bare
`docker compose down` was run, the real `.env` file was never read or
written, and those two containers were independently observed via
`docker ps` to already be in an exited state at the point this session
resumed after an interruption, which this verification pass did not
cause (it never issued a stop/restart against them) and did not attempt
to fix, since restarting the user's own stack is explicitly out of scope
for this task.
Verified live: `ruff check .` reported "All checks passed!" with zero
findings. `mypy apps` reported "Success: no issues found in 77 source
files" (77 vs. D048's 76 reflects Phase 39's new login-lockout code) with
zero findings. `alembic upgrade head` applied all twelve migrations in
sequence — 0001 through Phase 39's new
`0012_users_login_lockout` (`users.failed_login_count` /
`users.locked_until`) — against a real, freshly created database with no
manual intervention. `pytest tests/ -q` collected and ran the entire
suite against that same real Postgres/Redis pair: **400 tests, all
passing**, 3 warnings (the same two `InsecureKeyLengthWarning`s and one
`StarletteDeprecationWarning` seen in every prior verification pass —
neither new nor actionable), zero failures, zero errors, on the first
clean run — no shell-environment-leakage artifact this time.
Frontend: `cd apps/web && npm install` (0 vulnerabilities) `&& npm run
build` (Turbopack, Next.js 16.3.3 — compiled successfully, TypeScript
checked clean, all 16 routes generated) `&& npm test` (`vitest run`,
which is what `npm test` invokes — Playwright e2e was correctly excluded
and not run, since it needs its own real backend). Vitest reported
**102 tests across 13 files, all passing**, exactly matching Phase 39's
own D050 report, unchanged. A standalone `npm audit` was run again after
the build/test pass and independently confirmed **0 vulnerabilities**,
verifying D050's CVE-clearing claim held after the full merge.
No real cross-phase integration bug was found anywhere in this pass —
migration 0012's new columns, the login-lockout logic, and the
vitest/vite toolchain bump all integrate cleanly with every earlier
phase's code and tests.
Status: Verified, no code changes required. Cleanup completed: the
`tradingos-verify39` docker compose stack was torn down
(`docker compose -p tradingos-verify39 down -v`), the throwaway
`.venv_verify` Python venv was removed, and no `.env` file was ever
created (env vars were shell-exported for this session only, never
written to disk). The user's own stack
(`trading-os-postgres-1`/`trading-os-redis-1`, uvicorn on 8000, Next.js
dev server on 3005) was left exactly as found.

**D052 — CI hardening (Phase 40): a frontend job for `apps/web`, two long-standing CI-breaking bugs fixed, and the migration round-trip confirmed genuinely sound**

Date: 2026-08-31
Decision: Four related changes to `.github/workflows/ci.yml` and the code
it exercises, plus one deliberate scope cut. All of them came out of
actually running every CI step locally against a real Postgres rather than
reading the workflow and assuming it worked.

**1. The migration round-trip was already fine — no bug, and this is
recorded so nobody re-investigates it.** The suspicion going in was that
`alembic downgrade base` had never been exercised end-to-end since the
later migrations were added, and that one of the twelve `downgrade()`
functions would fail or leave residue. It does not. Against a real
`timescale/timescaledb:2.15.3-pg16` instance, `upgrade head` →
`downgrade base` → `upgrade head` → `downgrade base` → `upgrade head` all
completed cleanly, in order, with no manual intervention. More than "it
did not error": after `downgrade base` the `public` schema was verified by
direct `psql` to contain exactly one table (`alembic_version`), zero enum
types, and one index — every table, every composite index from 0007/0008/
0010, and all four enums (`assetclass`, `brokerkind`, `side`,
`orderstatus`) were genuinely removed, and the re-upgrade rebuilt the full
13-table schema. The `postgresql.ENUM(...).drop(..., checkfirst=True)`
calls in 0001 and 0002 are what make the re-upgrade work, since a leftover
type would collide with the recreate. No migration was changed.

**2. The secret-scan step has been failing every build since Phase 1/7,
and is now fixed.** The step inlined its own regex into the workflow file
and then ran `git grep` for it across the repo excluding only `*.md`. That
matched two lines: `.github/workflows/ci.yml` itself (a scanner cannot
spell out the pattern it looks for and then search itself for it), and
`tests/test_logging.py`, whose redaction test necessarily carries an
`sk-live-`-shaped `api_key` fixture — that string existing in the source
is the entire point of the test. `git grep` exited 0 on both, so the
`if ...; then exit 1` fired and the job died before ruff ever ran. The
pattern was never wrong; the harness around it was. The scan now lives in
`scripts/secret_scan.sh`, which excludes only itself by path and skips any
single line carrying a `pragma: allowlist secret` marker. Per-line rather
than per-file on purpose: excluding `tests/test_logging.py` wholesale
would blind the scan to a real credential added to that file later.
`tests/test_logging.py` gained that marker on the one line that needs it.
Verified in both directions locally: clean on the current tree, and a
throwaway `packages/_scan_probe.py` containing `sk-live-deadbeefcafe` was
correctly reported and exited 1.

**3. `tests/test_config.py::test_missing_jwt_secret_key_fails_closed` was
also failing under CI's own environment, and is now hermetic.**
`Settings(_env_file=None)` suppresses the `.env` file but not the process
environment, and pydantic-settings still reads `JWT_SECRET_KEY` from
there. The CI job sets `JWT_SECRET_KEY` at job level for every step, so
the "fails closed when the key is missing" assertion ran against an
environment where the key was present, and the expected `ValueError` never
arrived. The test now calls `monkeypatch.delenv("JWT_SECRET_KEY",
raising=False)` and arranges the absence it is asserting about instead of
assuming it. This is the same shell-environment-leakage class of artifact
D036/D040/D043/D051 noted in passing during verification passes — the
difference is that it is not an artifact of the verifier's shell, it is a
real property of the CI job, so it is fixed in the test rather than
worked around in the runner.

**4. `apps/web` now has a CI job.** Twenty-plus phases of frontend work
had zero automated coverage on push or PR. A second `web` job was added to
the existing `ci.yml` rather than a separate `ci-web.yml`: one workflow,
one status check, and the two jobs are visibly siblings under the same
trigger. It runs `npm ci`, `npm run build`, `npm test` on Node 22 with
`working-directory: apps/web` and npm caching keyed on
`apps/web/package-lock.json`. It declares no `services` and no `env`,
because none of those three commands touch Postgres, Redis, or the API —
Vitest mocks `fetch`. It is intentionally not `needs:` the backend job, so
a Python failure does not hide a frontend failure or vice versa. No new
SaaS, no new secrets, GitHub-hosted runners only.

**Deliberate scope cut: the Playwright e2e suite is NOT in CI.** This is
an explicit omission, not an oversight. Wiring it up needs a running
uvicorn backend, a migrated Postgres, `npx playwright install chromium`,
and `scripts/seed_e2e.py` against that database — a materially bigger CI
task than the three commands above, and one whose failure modes (the
`localhost`-not-`127.0.0.1` hydration trap and the serial, no-retry,
real-broker-state-mutating design documented in
`docs/DEVELOPMENT_WORKFLOW.md`) are exactly the kind that produce a job
that is green for the wrong reason or flaky for a real one. `act` was not
available here, and a CI-equivalent run could not be genuinely verified
end to end, so the honest choice was the safe cut over an unverified
guess. It remains an open item.

**Also noted, also deliberately not done: `npm run lint` is not in the
web job.** It currently fails — two `react-hooks/set-state-in-effect`
errors in `apps/web/components/SessionStatus.tsx` and one
`@next/next/no-location-assign-relative-destination` warning in
`apps/web/lib/session.ts`. Adding a step known to be red would make the
new job useless on arrival. Fixing those is real frontend work and belongs
in its own change; recorded here so it is not rediscovered as a surprise.

Verified live, every CI step run locally with its exact command, against a
private `tradingos-p40` docker compose stack on remapped ports
(55432/56379) so the user's own stack on 5432/6379 was never touched, and
a throwaway `.venv_p40`:
- `pip install -e ".[dev]"` — succeeded, resolving and installing every
  runtime and dev dependency (alembic 1.19.1, ruff 0.16.5, mypy 2.3.1,
  pytest 9.1.1, pytest-asyncio 1.4.0). Nothing added across forty phases
  needs a dependency the `[dev]` extra does not already cover. Note the
  local interpreter is Python 3.14, not the 3.11 CI pins; the install and
  the whole suite pass on both sides of that gap, so the pin is a floor
  the code clears, not a requirement it depends on.
- `bash scripts/secret_scan.sh` — clean (exit 0), and correctly exit 1 on
  the planted probe.
- `ruff check .` — "All checks passed!"
- `mypy apps` — "Success: no issues found in 77 source files".
- `alembic upgrade head` / `alembic downgrade base` / `alembic upgrade
  head` — all twelve migrations, both directions, twice, as described
  above.
- `pytest` — **400 passed**, 287 warnings, zero failures, run with
  `JWT_SECRET_KEY` exported exactly as the CI job sets it (the run that
  exposed bug 3; before the fix this was 399 passed / 1 failed).
- `cd apps/web && npm ci` (0 vulnerabilities) `&& npm run build`
  (Next.js 16.3.3, TypeScript clean, all 22 routes generated) `&& npm test`
  — **102 tests across 13 files, all passing**.
Cleanup: the `tradingos-p40` compose stack was torn down with `-v`, the
`.venv_p40` venv was removed, and no `.env` file was created at any point
(env vars were shell-exported for this session only).

**D053 — Independent full from-scratch integration re-verification of Phase 40: CI secret-scan fix and JWT test fix both confirmed genuinely working**

Date: 2026-08-31
Decision: Ran a second, fully independent from-scratch integration
verification of the merged `main` branch at `cd847ea` (Phase 40's CI
hardening — secret-scan fix, JWT test fix, new `apps/web` CI job, D052 —
already merged), specifically to confirm D052's own self-reported results
rather than trust them, following the same
D028/D033/D036/D040/D043/D046/D048/D051 precedent. This pass used its own
fresh Python 3.13 venv (`.venv-verify40`, 3.11/3.12 unavailable on the
host, 3.13 satisfies the project's `>=3.11` floor and every dependency
built clean wheels), its own docker compose project (`trading-os-verify40`,
a standalone `docker-compose.verify40.yml` rather than an override of the
base file, per D040's port-merge lesson) on remapped host ports
15432/16380, and its own throwaway `.env.verify40` — never the repo's real
`.env`. The user's own dev stack (`trading-os-postgres-1`/
`trading-os-redis-1` on default ports 5432/6379, a uvicorn `--reload`
process on port 8000, a Next.js dev server on port 3005, all against the
real root `.env`) was confirmed healthy and running via `docker ps` and
`netstat` both before this pass touched anything and again after cleanup —
untouched throughout, no `docker compose down` without `-p` was ever run
against it.
Verified live:
- `bash scripts/secret_scan.sh` — "Secret scan clean.", exit 0. This is
  the exact newly-fixed CI step from D052 bug 2; it ran clean here with no
  modification, confirming the fix holds independent of D052's own run.
- `ruff check .` — "All checks passed!"
- `mypy apps` — "Success: no issues found in 77 source files" (unchanged
  from D051/D052 — Phase 40 touched CI/test/docs files only, no
  `apps/**` source).
- `alembic upgrade head` → `alembic downgrade base` → `alembic upgrade
  head` — all twelve migrations (0001 through Phase 39's
  `0012_users_login_lockout`, still head — Phase 40 added none, as
  expected for a CI-only phase) applied cleanly in both directions against
  a real Postgres 16 (timescaledb image), independently reconfirming
  D052's bug-1 finding that the round-trip was never actually broken.
- `pytest tests/ -q` with `JWT_SECRET_KEY` exported in-shell exactly as
  the CI job does (via `.env.verify40`, sourced into the environment) —
  **400 passed**, 3 warnings (the same two `InsecureKeyLengthWarning`s and
  one `StarletteDeprecationWarning` seen in every prior verification
  pass), zero failures, zero errors, on the first attempt — no transient
  `test_missing_jwt_secret_key_fails_closed` failure this time, which is
  exactly what D052 bug 3's fix predicts: that test now calls
  `monkeypatch.delenv("JWT_SECRET_KEY", raising=False)` instead of relying
  on the key being absent from the ambient shell, so exporting it for the
  whole run — the CI-realistic condition that broke the test before the
  fix — no longer breaks it. Re-ran `tests/test_config.py` alone under the
  same exported `JWT_SECRET_KEY` as an extra check: 12 passed, confirming
  the fix in isolation as well as inside the full suite.
- `cd apps/web && npm ci` — 462 packages installed, 0 vulnerabilities.
  Run in an isolated copy of `apps/web` (source files only, `node_modules`
  and `.next` excluded) rather than in place, because `apps/web`'s real
  `node_modules` is shared with the user's live Next.js dev server on
  3005 and `npm ci`'s delete-and-reinstall step hit a live file lock on
  the first in-place attempt (`EPERM` unlinking
  `lightningcss-win32-x64-msvc`) — proof the server was still actively
  running, and reason enough to move the whole frontend check to a copy
  rather than risk that server's state.
- `npm run build` — Next.js 16.3.3 (Turbopack), compiled successfully,
  TypeScript checked clean, all 16 routes generated.
- `npm test` (`vitest run`) — **102 tests across 13 files, all passing**,
  exactly matching D051's and D052's own reports, unchanged.
No real cross-phase integration bug was found anywhere in this pass. Both
of D052's headline claims — the secret-scan fix and the JWT test fix — are
independently confirmed to work exactly as claimed, not just self-reported.
Status: Verified, no code changes required. Cleanup completed: the
`trading-os-verify40` compose stack was torn down with
`docker compose -p trading-os-verify40 -f docker-compose.verify40.yml down
-v`, `docker-compose.verify40.yml` and `.env.verify40` were deleted, the
`.venv-verify40` venv was removed, and the isolated `apps/web` copy used
for the frontend check was deleted from the scratch directory it lived in
— none of it ever touched the tracked working tree (`git status --short`
was empty throughout). The user's own stack
(`trading-os-postgres-1`/`trading-os-redis-1`, uvicorn on 8000, Next.js
dev server on 3005) was confirmed still running and untouched after
cleanup.

**D054 — Production readiness (Phase 41): the fake `/health` replaced by a real liveness/readiness split, and the API image made multi-stage and non-root**

Date: 2026-08-31
Decision: Two independent production-readiness gaps found by review, fixed
together because both are about what happens to this service when it is
run by something other than a developer's laptop.

**(1) `GET /health` was a confident wrong answer.** Since Phase 1 it
returned `{"status": "ok", ...}` built entirely from in-process `Settings`,
touching no dependency. An instance whose Postgres was unreachable
advertised itself as healthy, so any orchestrator, load balancer, or
uptime monitor wired to it would have kept routing traffic to an instance
that could not serve a single database-backed request. That is strictly
worse than having no probe at all: a missing probe is a known unknown,
whereas this one answered the question wrong with full confidence.

The fix is a split, not a patch. `/health` stays exactly as it was — same
three fields, same values, docs/API.md's documented shape untouched
because `apps/web/app/api/health/route.ts` and `tests/test_health.py`
already depend on it — and is now explicitly the **liveness** probe. A new
`GET /health/ready` is the **readiness** probe: it runs a real `SELECT 1`
through the application's own engine and returns 503 when it cannot.

Why not simply make `/health` check the database, which was the obvious
one-line fix: an orchestrator *restarts* a container whose liveness probe
fails. Restarting a Python process does not repair an unreachable
Postgres, so a dependency-checking liveness probe converts a database
outage into an unbounded crash-loop across every replica simultaneously —
and destroys the in-process state and log continuity needed to diagnose
the outage. Readiness is the probe that is supposed to fail on a
dependency outage: it removes the instance from rotation and puts it back,
with no restart, the moment the dependency returns. The semantics are
Kubernetes-shaped but nothing in the implementation is Kubernetes-specific;
both are plain HTTP endpoints that work for a compose healthcheck or an
ALB target group equally well.

Three implementation choices worth recording:

- **The probe uses `db.base.get_engine()`, the app's own engine, not a
  connection of its own.** A probe that dialled Postgres independently
  could report "ready" while the pool every real request draws from was
  exhausted or broken — the same class of false positive this whole entry
  is about. `get_engine()` is new (previously only `get_session` and
  `get_session_factory` were exposed) and returns the same singleton.
- **`HEALTH_READINESS_TIMEOUT_SECONDS` (default 3.0) hard-caps the
  check.** A readiness probe that can hang is nearly as bad as one that
  lies: the caller then times out at *its* layer on *its* schedule, while
  the hung probe holds a pooled connection real requests want. Bounding it
  here means the app decides the check failed, fast and explicitly, with
  `reason: "timeout"`. This was not hypothetical — see the live
  verification below, where stopping the Postgres *container* (packets
  blackholed rather than refused) produced exactly the timeout branch, not
  the connection-failure branch.
- **The 503 body has the same shape as the 200 body**
  (`{"status": ..., "checks": {"database": {...}}}`), set via
  `response.status_code` rather than by raising `HTTPException`, so a
  consumer does not parse one schema on success and FastAPI's
  `{"detail": ...}` on failure. `reason` is a fixed vocabulary
  (`connection_failed` / `timeout`). On failure the exception's **type
  name** is reported and its **message never is**: a driver or DSN error
  message can carry the connection string, and the connection string
  carries the database password (spec §38, docs/TRADING_SAFETY.md). This
  is an unauthenticated endpoint, so it is the last place that may leak.

**No Redis check, and that is a finding rather than an omission.** The
Phase 38 audit recorded that Redis is provisioned in `docker-compose.yml`
with `Settings.redis_url` set but is not wired into any Python code; that
was re-confirmed by grep for this phase and is still true — `redis` remains
an unused declared dependency in `pyproject.toml`. A readiness check for a
connection the application never makes would be fabricated signal in the
sense docs/TRADING_SAFETY.md forbids: it would report on a client whose
configuration has never been exercised, and it could take the whole
service out of rotation over a dependency no request path touches. The
check belongs in `_check_database`'s sibling slot the day a real Redis
client lands, and not one phase earlier.

**(2) `apps/api/Dockerfile` was single-stage and ran as root.** Now a
`builder` stage resolves dependencies from `pyproject.toml` into
`/opt/venv`, and a `runtime` stage copies that venv plus the app code,
`packages/`, `migrations/`, and `alembic.ini`, then `USER appuser` before
`CMD`. The exposed port (8000), the uvicorn command, and the
environment-variable contract are all unchanged, as required.

Two non-obvious details:

- **The builder installs the project and then uninstalls it.**
  `pip install .` is how `pyproject.toml`'s dependency list gets resolved
  (there is no requirements file, and adding one would create a second
  source of truth), but it also copies `apps/` and `packages/` into
  site-packages. Leaving that copy in place would ship two copies of the
  application code in one image, with `sys.path` ordering silently
  deciding which one runs. `pip uninstall -y trading-os` immediately after
  keeps the dependencies and drops the duplicate; `PYTHONPATH=/app` then
  makes `apps.api.app.main` resolve to the single copied tree — the same
  import target the old editable install produced, with no pip, setuptools,
  or build backend needed at runtime.
- **pip/setuptools/wheel are removed from both the venv and the base
  image's `/usr/local`.** Nothing at runtime imports them and nothing in
  the runtime stage installs anything, so shipping them only ships an
  installer able to fetch and execute arbitrary code off the network
  inside the running container.

An honest note on image size, since "multi-stage builds shrink the image"
is the usual claim: the first working version of this Dockerfile came out
**larger** than the single-stage one it replaced (387MB vs 373MB),
because a venv duplicates pip and the project's own source on top of what
the base image already provides. Only after the two removals above did it
land at **354MB, a real 19MB reduction** — measured, both images built
from this same tree. The `/usr/local` pip removal contributes no size at
all (deleting files from a lower layer does not shrink an image) and is
purely attack-surface. The genuine wins of this change are the non-root
user, the absent installer, the dependency/app-code layer split for cache
behaviour, and only then the 19MB.

`alembic.ini` and `migrations/` are shipped deliberately. Verified from
`docker-compose.yml` rather than assumed: the `api` service runs uvicorn
directly and has no entrypoint or command that runs migrations, so the
container never migrates on start and the schema is expected to be
migrated out-of-band (docs/DEVELOPMENT_WORKFLOW.md). They ship anyway so
`docker compose run --rm api alembic upgrade head` remains a working
one-command path from the same image that serves traffic.

Alternatives rejected: (a) making `/health` itself check the database —
the crash-loop failure mode above; (b) a separate `/ready` at the root
rather than `/health/ready` — nested reads better next to the existing
route and groups the two probes under one prefix, and nothing depended on
a root `/ready`; (c) a distroless or Alpine runtime base — a real size
win, but Alpine's musl changes the wheel set for asyncpg/bcrypt and
distroless removes the shell that `docker compose exec ... alembic` and
this phase's own verification rely on; both are larger changes than "make
the build multi-stage and drop root", and neither was asked for; (d)
adding a `HEALTHCHECK` instruction to the image — deliberately not done,
because the orchestrator, not the image, should own probe cadence and
thresholds, and `docker-compose.yml`'s `api` service is unchanged by this
phase.

No new dependency, Python or system, was added by either half.
No migration. `apps/web` untouched.

Verified live: an isolated `tradingos-p41` compose stack (Postgres on host
port 15441, Redis on 16441, the rebuilt API on 18441) and a throwaway
`.venv-p41`, kept entirely off the user's own dev stack
(`trading-os-postgres-1`/`trading-os-redis-1` on 5432/6379, uvicorn on
8000, Next.js on 3005), which was never touched — no `docker compose down`
was ever run without `-p tradingos-p41`.
- `ruff check .` — "All checks passed!"
- `mypy apps` — "Success: no issues found in 78 source files" (77 before;
  the new file is `apps/api/app/api/routes/health.py`).
- `alembic upgrade head` against the isolated Postgres — all twelve
  migrations applied, `alembic current` reported `0012 (head)`. Phase 41
  adds no migration.
- `pytest tests/ -q` — **404 passed**, 3 warnings, zero failures: four new
  tests in `tests/test_health.py` over the 400 baseline D051/D053 measured.
- **The readiness happy path is genuinely un-mocked.** It drives the real
  route through `httpx.ASGITransport` (not `TestClient`, whose per-request
  event loop cannot reuse asyncpg connections the session-scoped loop
  pooled — a harness artifact, not a product one; the running app has a
  single loop) against the real engine and the real Postgres. Proof it is
  not vacuous: stopping the Postgres container and re-running that one
  test makes it **fail**, which was checked explicitly rather than
  assumed. Only the two failure branches use substitutes — a real engine
  aimed at a dead port for `connection_failed`, and a hanging stub for
  `timeout`, since a shared Postgres may not be wedged on demand.
- **The rebuilt image was actually run, not just built.**
  `docker compose -p tradingos-p41 ... up -d --build api`, then
  `exec api whoami` → **`appuser`** (and `id` → `uid=1000(appuser)`);
  `command -v pip` → **absent**; `alembic current` → `0012 (head)`,
  confirming the migration files really are in the final image and usable
  by the non-root user.
- **End-to-end probe semantics, from inside that container against the
  isolated Postgres**: `/health` → 200 and `/health/ready` → 200
  `{"status": "ready"}` while healthy; then, with the Postgres container
  stopped, `/health` **stayed 200** (liveness correctly indifferent to the
  dependency — the whole point of the split) while `/health/ready`
  returned **503** `{"status": "not_ready", "checks": {"database":
  {"status": "error", "reason": "timeout", "timeout_seconds": 3.0}}}`;
  then, with Postgres restarted and **the API container never restarted**,
  `/health/ready` returned **200 `ready`** again on its own. That last
  step is the readiness contract working exactly as designed.
Cleanup: the `tradingos-p41` stack was torn down with `-v`,
`docker-compose.p41.yml`, `.venv-p41`, and the `tradingos-p41-api` images
were removed. No `.env` file was created at any point — every variable was
shell-exported for this session only.

**D055 — Independent full from-scratch integration re-verification of Phase 41: readiness probe and multi-stage non-root image both confirmed genuinely working**

Date: 2026-08-31
Decision: Ran a third, fully independent from-scratch integration
verification of the merged `main` branch at `db36ba8` (Phase 41's
production-readiness work — the real `/health`/`/health/ready` split and
the multi-stage, non-root API image, D054 — already merged), specifically
to independently rebuild D054's own artifacts and confirm its two headline
claims rather than trust its self-report, following the same
D028/D033/D036/D040/D043/D046/D048/D051/D053 precedent. This pass used its
own fresh Python 3.13 venv (`.venv-verify41`, 3.11/3.12 unavailable on the
host, 3.13 satisfies the project's `>=3.11` floor and every dependency
built clean wheels), its own docker compose project (`verify41`, a
standalone `docker-compose.verify41.yml` rather than an override of the
base file) on remapped host ports 55432/56379, and its own throwaway
`.env.verify41` — never the repo's real `.env`. The user's own dev stack
(`trading-os-postgres-1`/`trading-os-redis-1` on default ports 5432/6379, a
uvicorn `--reload` process on port 8000, a Next.js dev server on port 3005,
all against the real root `.env`) was confirmed healthy and running via
`docker ps` and `netstat` both before this pass touched anything and again
after cleanup — untouched throughout, no `docker compose down` without `-p`
was ever run against it.
Verified live:
- `bash scripts/secret_scan.sh` — "Secret scan clean.", exit 0.
- `ruff check .` — "All checks passed!"
- `mypy apps` — "Success: no issues found in 78 source files" (unchanged
  from D054 — this pass touched no `apps/**` source).
- `alembic upgrade head` against the isolated Postgres — all twelve
  migrations applied cleanly, `alembic current` reported `0012 (head)`,
  independently reconfirming Phase 41 adds no migration.
- `pytest tests/ -q` with the throwaway env exported in-shell — **404
  passed**, the same 3 pre-existing warnings seen in every prior pass, zero
  failures, zero errors — exactly D054's own reported total.
- **The Dockerfile was rebuilt from scratch, not reused.** `docker build -f
  apps/api/Dockerfile -t tradingos-verify41 .` completed cleanly through
  both the `builder` and `runtime` stages on a build that had never seen
  D054's own image or layer cache assumptions beyond Docker's normal layer
  reuse.
- **The non-root claim was re-checked against this independently-built
  image.** `docker run --rm tradingos-verify41 whoami` → **`appuser`**.
- **The image's migration path was re-checked against this independently-
  built image.** `docker run --rm --network verify41_default
  tradingos-verify41 alembic current` (with `DATABASE_URL` pointed at the
  isolated Postgres over the shared compose network and `JWT_SECRET_KEY`
  supplied, since `Settings()` fails closed without it — expected, not a
  bug) → **`0012 (head)`**, confirming the shipped `migrations/`/
  `alembic.ini` make the image independently migration-capable.
- **End-to-end probe semantics, from a real container started from this
  image against the isolated Postgres**: `GET /health` →
  `{"status":"ok","trading_mode":"research","live_trading_enabled":false}`
  (200); `GET /health/ready` →
  `{"status":"ready","checks":{"database":{"status":"ok"}}}` (200). Then,
  with the database made unreachable (a second container pointed at a bad
  Postgres port, no restart of any running container), `GET /health`
  **stayed 200** with the identical body (liveness correctly indifferent to
  the dependency) while `GET /health/ready` returned **503**
  `{"status":"not_ready","checks":{"database":{"status":"error","reason":"connection_failed","error_type":"ConnectionRefusedError"}}}`
  — the readiness contract working exactly as D054 designed it, verified
  from an image this pass built itself.
No real cross-phase integration bug was found anywhere in this pass. Both
of D054's headline claims — the fake-`/health` fix and the non-root
multi-stage image — are independently confirmed genuinely fixed, not
merely self-reported.
Cleanup: the `verify41` compose stack was torn down with `-v` (removing its
named Postgres volume), the `tradingos-verify41` image was removed with
`docker rmi`, `docker-compose.verify41.yml` and `.env.verify41` were
deleted, and the `.venv-verify41` venv was removed. No `.env` file was
created at any point — every variable was shell-exported or drawn from the
throwaway file for this session only. The user's own
`trading-os-postgres-1`/`trading-os-redis-1` containers and their uvicorn
(8000) and Next.js (3005) dev servers were confirmed still running via
`docker ps` and `netstat` after cleanup completed.

**D056 — Observability and lifecycle (Phase 42): per-request correlation IDs bound into structlog, and an explicit ordered shutdown that disposes the DB engine**

Two independent gaps, both found by review of the merged Phase 41 state,
both fixed here because both are about the service being *operable*, not
merely functional.

**(1) Structured logs were unlinkable to the request that produced them.**
Every line this service emits is JSON, but none carried a correlation key.
One trade submission can emit a risk decision, a portfolio decision, and a
fill on three separate lines; in a production stream those three are
interleaved with every other concurrent request's lines and nothing
distinguishes them. The most common production question — "the user says
their order was rejected at 14:03, what happened?" — was therefore
unanswerable from the log stream alone.

`apps/api/app/core/request_id.py` adds `RequestIDMiddleware`, registered as
the outermost middleware in `main.py` so the ID is bound before routing,
auth, or exception handling runs and is present even on requests that never
reach a route handler. It binds the ID under the log key `request_id` via
`structlog.contextvars.bind_contextvars`. **No call site changed.** That
works because `configure_logging()` has had
`structlog.contextvars.merge_contextvars` as its *first* processor since the
logging pipeline was written, so a contextvar-bound key is merged into every
event dict automatically. The installed structlog is **26.1.0** — verified
against the installed package's own API, not from memory
(`bind_contextvars` / `unbind_contextvars` / `get_contextvars` all present),
well past the 20.1 that introduced `contextvars`. **No new dependency was
added**: this is structlog, Starlette's own ASGI protocol, and stdlib
`uuid`.

*Pure ASGI, not `BaseHTTPMiddleware`.* `BaseHTTPMiddleware` runs the
downstream app in a child anyio task; a contextvar bound before
`call_next()` does reach that child (the task copies the context at spawn),
but the bind and its cleanup then straddle two contexts. A pure ASGI
middleware keeps the whole request in one context, so bind/unbind are
exactly paired, and it rewrites response headers directly from the
`http.response.start` message. Cleanup is `unbind_contextvars`, not
reset-by-token, because uvicorn reuses a task context across requests on a
connection — leaving the key bound would stamp one request's ID onto the
next one's lines and onto background work that has no request at all.

*Caller-supplied IDs are honored when well-formed; a bad one is replaced,
not rejected.* An inbound `X-Request-ID` is reused verbatim when it matches
`^[A-Za-z0-9._-]{8,128}$`, so a load balancer, ingress, or the Next.js
frontend can propagate one ID across a hop and have both sides' logs join.
Anything else — too short, too long, whitespace, control characters, CR/LF
— is discarded and a fresh UUID4 substituted, with a single
`request_id_header_rejected` warning so a misconfigured upstream is visible
rather than silent. The rejected value is *not* echoed into that log line;
repeating unvalidated caller-controlled text into the log stream is
precisely what the rejection just prevented.

Replace-don't-reject is the defensible direction because this value is a
correlation label and nothing else: it grants no access, gates no code
path, and is never compared against anything. It is emphatically **not an
auth token** and must never be treated as one. Failing a real trading
request because a proxy sent an oddly-shaped header would convert a
cosmetic observability concern into an outage. The narrow charset is what
makes it safe to echo into a response header at all (no CR/LF injection),
and the length bound is what stops a hostile caller writing unbounded
attacker-controlled text into every log line of a request.

*The redaction processor still runs, and there is no field-name collision.*
`_SECRET_KEY_PATTERN` in `core/logging.py` matches
`secret|password|passwd|api_key|token|private_key|access_key`. `request_id`
matches none of them — asserted explicitly in
`tests/core/test_request_id.py`, not assumed, because a collision would
deliver every correlation ID to the sink as `***REDACTED***` and render the
whole mechanism silently useless. The same test confirms an `api_key` field
logged *alongside* a bound request ID is still redacted, i.e. the two
processors compose correctly.

**(2) The async SQLAlchemy engine was never explicitly disposed.** The
lifespan's shutdown half stopped the portfolio snapshot scheduler and
nothing else. The process-wide engine created at import time in
`apps/api/app/db/base.py` owns a live asyncpg pool; process exit reclaims
those sockets, but that is the OS cleaning up after us, not the service
shutting down. Under an orchestrator issuing SIGTERM with a grace period,
"probably fine because the OS handles it" is not a shutdown contract.

The lifespan now performs an explicit ordered teardown — **stop background
work, then dispose the engine** — and logs `trading_os_shutdown_complete`.
It reuses Phase 41/D054's `get_engine()` accessor rather than constructing
a second engine, so it disposes the exact pool every request and the
readiness probe draw from.

*The order is load-bearing, and was traced rather than assumed.*
`PortfolioSnapshotScheduler.stop()` cancels its task and then `await`s it
to completion (`await self._task`, catching `CancelledError`) — it does not
merely signal. So by the time `stop()` returns, no cycle can still hold a
session from the pool, and disposing afterwards cannot race a mid-flight
cycle. Reversing the order would dispose the pool out from under a running
cycle. `tests/core/test_shutdown.py` pins this at the seam that encodes it:
spies on `stop()` and `dispose()` share one call log, so the assertion is
on the real relative order of the two real calls the teardown block makes;
a separate test drives the *real* `stop()` against a task that needs a loop
turn to unwind and asserts it is genuinely finished on return. A full
SIGTERM cannot be asserted on from inside the process being terminated, so
that half was verified live instead (below).

**Verification.** **424 backend tests passing** (404 baseline plus **20
new**: 14 in `tests/core/test_request_id.py`, 6 in
`tests/core/test_shutdown.py`), `ruff check .` and `mypy apps` clean (79
source files). Live-verified against an isolated docker compose stack
(project `phase42`; Postgres on 55432, Redis on 56379, the real multi-stage
API image on 58000 — the user's own 5432/6379/8000/3005 dev stack was never
touched):

- Two consecutive `GET /health` calls returned **two different**
  `x-request-id` response headers:
  `683e9914-aa16-405f-94c0-afbd1c9a1823`, then
  `220c42b6-2e34-4f72-a122-8f31a3f81290`.
- A caller-supplied `X-Request-ID: lb-edge-phase42-abc123` came back
  **unchanged**. A malformed `X-Request-ID: bad` was **replaced** with
  `23d09c56-a118-44f3-a6b2-29017ebf9c84` and the request still returned
  **200** — the documented replace-don't-reject contract.
- **Correlation across modules within one request, from real container
  logs.** A single `GET /health/ready` (Postgres deliberately stopped, a
  malformed ID supplied) produced two lines from two different modules
  carrying the same ID, matching that response's
  `x-request-id: 6d1f0a5e-596a-4d49-8662-dcfc493705a8`:

  ```
  {"supplied_length": 2, "reason": "malformed", "event": "request_id_header_rejected", "request_id": "6d1f0a5e-596a-4d49-8662-dcfc493705a8", "level": "warning", "timestamp": "2026-08-31T02:09:54.107936Z"}
  {"check": "database", "reason": "timeout", "error_type": null, "event": "readiness_check_failed", "request_id": "6d1f0a5e-596a-4d49-8662-dcfc493705a8", "level": "warning", "timestamp": "2026-08-31T02:09:57.111063Z"}
  ```

- **Graceful shutdown under a real SIGTERM.** With Postgres healthy and
  `GET /health/ready` returning 200, `docker stop` on the API container
  produced `Waiting for application shutdown.` →
  `{"event": "trading_os_shutdown_complete", "level": "info", "timestamp": "2026-08-31T02:10:43.874641Z"}`
  → `Application shutdown complete.` and a clean exit inside the grace
  period, with no error output.

Cleanup: the `phase42` compose stack was torn down with `-v`, the image it
built was removed, and `docker-compose.phase42.yml` plus the verification
venv were deleted. No `.env` file was created at any point — every variable
was shell-exported or set inline in the throwaway compose file.

**D057 — Independent full from-scratch integration re-verification of Phase 42: request-ID middleware and ordered shutdown both confirmed genuinely working, one real cross-phase bug found and fixed**

Date: 2026-08-31
Decision: Ran a fourth, fully independent from-scratch integration
verification of the merged `main` branch (Phase 42's request-correlation-ID
middleware and ordered graceful shutdown, D056, already merged), following
the same D028/D033/D036/D040/D043/D046/D048/D051/D053/D055 precedent. This
pass used its own fresh Python 3.13 venv (`.venv_verify`, 3.11/3.12
unavailable on the host, 3.13 satisfies the project's `>=3.11` floor and
every dependency built clean wheels), its own docker compose project
(`trading-os-verify`, a standalone `docker-compose.verify.yml`) on remapped
host ports 55432/56379/58000, and its own throwaway `.env.verify` — never
the repo's real `.env`. The user's own dev stack
(`trading-os-postgres-1`/`trading-os-redis-1` on default ports 5432/6379, a
uvicorn `--reload` process on port 8000, a Next.js dev server on port 3005,
all against the real root `.env`) was confirmed healthy and running via
`docker ps`/`netstat` both before this pass touched anything and again after
cleanup — untouched throughout, no `docker compose down` without `-p` was
ever run against it.

**One real cross-phase bug was found and fixed.** `bash scripts/
secret_scan.sh` failed on its first run: Phase 42's own
`tests/core/test_request_id.py` added a fixture value
`api_key="sk-live-should-not-appear"` to prove the redactor still runs
alongside a bound request ID, but never added the `pragma: allowlist
secret` marker the scan script (D052) requires for a deliberate look-alike
fixture — the same convention the existing `tests/test_logging.py`
redaction fixture already follows. Fixed by assigning the literal to a
named `secret` variable and appending `# pragma: allowlist secret` on that
same line (the marker must sit on the exact matched line, not a preceding
comment line, and the original single line would have exceeded the
100-column `ruff` limit once the marker was appended). Re-ran clean after
the fix.

Verified live:
- `bash scripts/secret_scan.sh` — failed once (bug above), then "Secret scan
  clean.", exit 0 after the fix.
- `ruff check .` — "All checks passed!"
- `mypy apps` — "Success: no issues found in 79 source files" (unchanged
  from D056).
- `alembic upgrade head` against the isolated Postgres — all twelve
  migrations applied cleanly, confirming Phase 42 adds no migration.
- `pytest tests/ -q` with the throwaway env exported in-shell — **424
  passed**, 3 pre-existing warnings (the same `httpx`/`starlette` deprecation
  and two `InsecureKeyLengthWarning`s seen in every prior pass), zero
  failures, zero errors — exactly matching Phase 42's own reported total.
- **Live request-ID behavior**, first against a bare `uvicorn` process and
  then (after Windows was confirmed to have no way to deliver a true,
  handler-invoking SIGTERM cross-process — `os.kill(pid, SIGTERM)` on
  Windows calls `TerminateProcess`, which bypasses Python's signal handler
  entirely) against the real multi-stage API image run as a container on
  port 58000, so the later SIGTERM test would be genuine: two plain
  `GET /health` calls returned two different `x-request-id` headers; a
  supplied `X-Request-ID: my-test-id-12345` came back unchanged, byte for
  byte; a malformed `X-Request-ID: a` was replaced with a freshly generated
  UUID4 and the request still returned 200 — exactly the documented
  behavior in `docs/API.md` ("discarded and replaced with a freshly
  generated UUID4. The request is not rejected"). The container's captured
  stdout showed the matching `request_id_header_rejected` warning line
  carrying `"request_id"` equal to that response's header and
  `"supplied_length": 1` rather than the offending value itself, confirming
  the field reaches the logs without leaking the malformed input.
- **Graceful shutdown under a real SIGTERM.** With the container healthy
  and serving, `docker stop trading-os-verify-api-1` produced, in order,
  `Shutting down` → `Waiting for application shutdown.` →
  `{"event": "trading_os_shutdown_complete", "level": "info", "timestamp": "2026-08-31T02:25:17.517794Z"}`
  → `Application shutdown complete.` → `Finished server process [1]`, with
  container exit code **0** and no unhandled exception anywhere in the log.

No other cross-phase integration bug was found. Both of D056's headline
claims — request-ID propagation/generation semantics and the ordered,
engine-disposing shutdown — are independently confirmed genuinely working,
not merely self-reported; the one real defect found (the missing secret-scan
pragma on Phase 42's own new test fixture) has been fixed and is included in
this verification's commit.

Cleanup: the `trading-os-verify` compose stack (postgres, redis, and the
built api container) was torn down with `-v` (removing its anonymous
volume), the `trading-os-verify-api` image was removed with `docker rmi`,
`docker-compose.verify.yml`, `.env.verify`, and the stray local
`uvicorn_verify.log`/`.pid` files were deleted, and the `.venv_verify` venv
was removed. No `.env` file was created at any point — every variable was
shell-exported or drawn from the throwaway file for this session only. The
user's own `trading-os-postgres-1`/`trading-os-redis-1` containers and their
uvicorn (8000) and Next.js (3005) dev servers were confirmed still running,
healthy, and untouched via `docker ps` and `netstat` after cleanup
completed.

**D058 — The live-trading execution path (Phase 43): a real Longbridge `LiveBrokerAdapter`, separate tighter live risk limits, a mandatory per-trade `confirm` flag, and a per-broker paper/live toggle — all built and tested with `LIVE_TRADING_ENABLED` permanently false**

Date: 2026-09-01

Numbering note: developed in parallel with sibling phases 44 and 45 in
separate worktrees. D-numbers were reserved sequentially from the state of
`main` at commit `9c0660c` (latest entry D057), so this is D058; phases 44
and 45 will claim the next free numbers when they merge. Nothing in this
entry depends on either sibling.

**The headline constraint, stated before anything else:** no real order was
ever placed, no real broker was ever contacted, and `LIVE_TRADING_ENABLED`
was never set to `true` — not in a default, not in a test fixture, not
transiently during verification, not in the smoke test. What was built and
proven here is the CODE PATH. The system's committed configuration is
exactly as inert after this phase as before it: on a default checkout, a
request against a live-kind broker is refused and no trade context is ever
constructed.

Decision: build the live execution path structurally parallel to the paper
one, so that the deterministic safety machinery gates a live order because
it is the *same code*, not because a second implementation was written to
agree with the first.

**1. `apps/api/app/execution/live_broker.py` — `LiveBrokerAdapter`.**
Implements the same `BrokerAdapter` Protocol as `PaperBrokerAdapter`, so
`submit_trade()` and everything above it cannot tell them apart.

Three sub-decisions worth recording:

*The synchronous `TradeContext`, not `AsyncTradeContext`.* The SDK ships
both with identical method signatures. `BrokerAdapter` is synchronous
because the paper adapter, the OMS, and the backtest engine are, so the
sync context is what keeps the two adapters interchangeable instead of
forcing the entire order path async purely to accommodate a second
implementation. The cost is real and accepted: a live submission briefly
blocks the serving event loop. That is fine for one human-confirmed order
at a time and would not be fine for a high-rate automated path; moving the
order path to async is the right fix if such a path is ever built, and is
not this phase's work.

*Never a fabricated fill.* A real `submit_order` returns an order id, not a
fill — `SubmitOrderResponse`'s only attribute is `order_id`. The adapter
therefore reads the order back once via `order_detail()` and builds its
`Fill` from the broker's own `executed_quantity`/`executed_price`. If
nothing is executed at that moment it raises `LiveOrderNotFilledError`
carrying the real order id, and the route answers `502
LIVE_ORDER_UNCONFIRMED` naming that id. The caller's `market_price`
argument exists only to keep the Protocol signature identical to the paper
adapter's; it is never sent to the broker and never written into a `Fill`.
There is deliberately no retry/poll loop — silently re-reading a live order
in a loop hides a real money-moving order behind a request timeout, and
reconciling an accepted-but-unfilled live order is its own unbuilt problem
that must not be papered over here.

*One book per trade.* The account snapshot (cash + positions) is fetched
from the broker once per adapter instance and cached for that instance. The
route builds one adapter per request, so the Risk Engine, the buying-power
check, and the Portfolio Manager all reason about the same book. A second
mid-trade fetch could hand two gates two different accounts, which is
precisely what fail-closed exists to prevent. The cache is dropped after a
fill rather than patched locally: the venue, not this process, is the source
of truth for a live book (spec §62).

SDK surface was verified by direct introspection of the installed `longport`
package, never from documentation or memory — the same discipline D008/D015/
D021 established. That check earned its keep immediately: `TradeContext` has
no `.create()` (only `AsyncTradeContext` does), and `submit_order` returns an
order id rather than any fill information. Both would have been wrong if
assumed.

Construction is gated by `build_live_broker_adapter()`, which returns `None`
— never a degraded or simulated stand-in — unless `trading_mode=live` AND
`live_trading_enabled=true` AND all three `LONGPORT_LIVE_*` credentials are
present together (D015's all-or-nothing trio pattern). The live credentials
are deliberately SEPARATE settings from the read-only `LONGPORT_*` quote
credentials: a quote key must never be able to become a trading key by
accident, which is exactly what reusing `longport_app_key` would have
allowed.

**2. Separate, tighter live risk limits.** New settings
`live_risk_max_position_pct_of_equity` (0.05),
`live_risk_max_portfolio_exposure_pct_of_equity` (0.20), and
`live_risk_max_risk_pct_of_equity_per_trade` (0.01), kept entirely separate
from the existing `risk_*` set rather than replacing or reinterpreting it.
Per-trade risk stays at 1% because that limit is already a conservative,
well-understood fraction and changing it would retune position sizing for a
reason unrelated to "this is real money."

Separate rather than shared for two reasons. First, paper trading's
behaviour must be *provably* unchanged by the existence of a live path;
sharing one set would mean any future tightening for live money silently
retunes every paper backtest and every existing test's expectation. Second,
real money warrants tighter defaults than a simulator.

The branch itself lives in `apps/api/app/risk/limits.py` as
`build_risk_limits(settings, *, live)`, extracted out of the route so the
paper/live distinction is one pure, directly unit-testable function rather
than a branch buried in an async handler that needs a database, a user, and
a broker row to reach. The live branch reads only `live_risk_*` and the
paper branch only `risk_*`, so "neither can see the other's numbers" is
structural, not a convention. The three non-money-scaled limits (stop
requirement, market-data freshness, duplicate-order window) are shared
deliberately: they are correctness rules, not risk appetite, and a live
trade must never be held to a looser version of them than a paper one.

An interaction worth knowing, found while writing the tests: with live
exposure capped at 20% of equity, no single symbol can reach the Portfolio
Manager's 25% per-symbol concentration cap — the tighter live limit above it
always binds first. On the live path `max_symbol_concentration` and
`min_cash_reserve` are therefore effectively unreachable, and
`max_open_positions` is the Portfolio Manager constraint that can actually
bind. That is a consequence of the chosen numbers, not a defect, but it
means the Portfolio Manager does less work on the live path than on the
paper one and should be revisited if the live exposure cap is ever loosened.

**3. A mandatory per-trade `confirm: true` for live orders.**
`TradeSubmissionRequest` gains `confirm: bool = False`. It is REQUIRED for a
live-kind broker and ignored entirely for a paper one; missing or false
against a live broker is a `400 LIVE_CONFIRMATION_REQUIRED`.

Why this is a structural safeguard and not a UX nicety, since that was the
question worth answering: `docs/TRADING_SAFETY.md` requires explicit
confirmation before a real order, and a confirmation that lives only in a
frontend dialog is not a confirmation the server can enforce. Any caller — a
script, a retried request, an agent, a future UI — has to state its intent to
spend real money in the payload itself. The default of `false` also means a
request body that would place a paper trade can never place a live one
merely by being pointed at a different broker id.

Check ORDER inside `_authorize_live_trade` is deliberate. Confirmation is
checked FIRST, so an unconfirmed request is refused for the reason actually
true of it regardless of how the server is configured — and, usefully, so
that branch is testable without ever enabling live trading anywhere. The
configuration gate is second (on a default deployment the trade stops here,
having touched no broker). The permission check is last, because it is the
only one needing the resolved role.

`trade:submit:live` is required IN ADDITION to the `trade:submit:paper` the
route's dependency already enforces, rather than instead of it. That is
strictly more demanding than either alone, which is the correct direction
for a control that spends real money, and it avoids reordering the existing
permission-before-broker-lookup checks in `require_broker_access` — a change
that would have altered 403/404 responses on paths unrelated to this phase.
`Permission.SUBMIT_LIVE_TRADE` existed as a reserved, unenforced member since
Phase 6; this phase is what its own docstring said had to happen before it
could be wired, and it is now enforced.

**4. The emergency stop and Portfolio Manager gate a live trade — verified,
not assumed.** `_execute_trade()` is one function serving both kinds. The
ONLY two differences below the gates are which adapter is used and which
limits are read; the emergency-stop read (D039), the duplicate-order read
(D024), the Risk Engine, the Portfolio Manager (D029), the re-evaluation of
a shrunk proposal, and order/fill persistence are literally the same code.
Each is nonetheless covered by its own explicit live test, because "it
should follow from the structure" is not verification. The emergency-stop
test asserts the decisive fact directly: the fake broker double received
nothing.

The agent route (`POST /brokers/{id}/agent-trades`) is deliberately
UNCHANGED and remains paper-only. The human `confirm: true` is a
confirmation of a specific side, quantity and stop that a human chose; no
equivalent exists when an agent invents those, so an LLM-originated live
order has no confirmation to give (`docs/AGENT_POLICY.md`, spec §3/§46).

**5. The per-broker paper/live toggle.** A broker row's `kind` IS the
trading-mode switch, and this phase made it manageable. Before it, broker
rows could only be created by direct SQL insert — the field deciding which
adapter real orders reach was unreachable through the API.

Added `POST /admin/brokers` (create with an explicit kind) and `PATCH
/admin/brokers/{broker_id}/mode` (flip an existing broker), both
`admin:manage`-gated. There is ONE trade-submission endpoint, and the broker
row — never anything in the request body — decides paper vs live routing. No
body field selects the execution mode: `confirm` gates a live trade, it does
not choose one, and a live-kind broker with no configured live path is
refused outright rather than falling back to the simulator.

Three guards on the toggle:

- Designating a broker live (on create or flip) needs `confirm_live: true`.
  Only that direction is made awkward; live -> paper never needs it, because
  that direction can only make the system safer.
- A broker with ANY recorded order can no longer be flipped (409).
  `orders`/`fills` are append-only and keyed by `broker_id` with no
  per-order kind, so flipping a traded broker would retroactively make its
  simulated and real history indistinguishable. That is an audit-integrity
  failure, and the remedy — a new broker row — costs nothing.
- A broker with a simulated cash/position book cannot be flipped either, so
  a fake 100,000 balance can never become the identity of a real account.

The mode change is logged at WARNING with the actor, previous kind and new
kind. A no-op flip (already that kind) succeeds without the guards, since it
changes nothing.

Two smaller changes fell out of this. `cash` and `positions` were promoted
into the `BrokerAdapter` Protocol — the trade route always needed both to
build the Portfolio Manager's view, so they were always part of the real
contract; before a second adapter existed, only the concrete
`PaperBrokerAdapter` happened to declare them. And `save_paper_broker()` is
now skipped on the live path: writing a live account's cash and positions
into `broker_accounts`/`broker_positions` would create a second, immediately
divergent book. The order/fill rows are still written for both kinds, since
those record what THIS system did, which is a different claim from "this is
the account state."

Deliberately NOT built this phase: any frontend (sibling phase 45 owns
frontend, and a live-trading UI with its own "you are about to spend real
money" dialog deserves a dedicated, carefully reviewed phase rather than
being bolted on here); limit orders on the live path; a reconciliation
process for an accepted-but-unfilled live order; fractional shares (refused
explicitly rather than silently rounded); and multi-currency live accounts
(a single `live_account_currency` is used, and a missing balance in it fails
the trade rather than substituting another currency at an unknown rate).

Verification (own Docker Postgres on remapped port 5443, own Python 3.13
venv, own compose project `trading-os-phase43`; the user's 5432/6379/8000/
3005 stack and the sibling phase-44/45 worktrees were never touched):

- `ruff check .` clean; `mypy apps` clean, 81 source files.
- Full suite **487 passed**, 0 failed, 3 pre-existing warnings (the same
  `starlette`/`httpx` deprecation and two `InsecureKeyLengthWarning`s every
  prior phase reports) — 424 pre-existing (D057's exact total) plus 63 new:
  23 in `tests/execution/test_live_broker.py`, 6 in
  `tests/risk/test_limits.py`, 17 in `tests/api/test_live_trades.py`, 17 in
  `tests/api/test_broker_mode_toggle.py`.
- One pre-existing test was updated rather than added to:
  `test_live_broker_is_rejected_since_no_live_execution_path_exists` became
  `test_live_broker_is_rejected_on_the_default_configuration`. A live-broker
  request on the default configuration is still refused with a 400; the
  detail now names the confirmation requirement, because that gate is
  checked first.
- Live smoke test against a real `uvicorn` on port 58043 in
  `TRADING_MODE=paper`, `LIVE_TRADING_ENABLED=false`. Twelve checks passed:
  the paper path returned results identical to before this phase (`filled`,
  fill price 100, quantity 10; an oversized order still
  `exceeds_max_position_size`); `confirm: true` was inert on a paper broker;
  creating a live broker without `confirm_live` was refused and created no
  row; an unconfirmed live trade returned `LIVE_CONFIRMATION_REQUIRED`; a
  CONFIRMED live trade still returned `400 NOT_CONFIGURED: no live execution
  path is available` — the decisive line, showing the path stays inert even
  when a caller asks for it correctly; the live broker accumulated zero
  orders and zero paper-account rows; the flip to paper then routed the same
  request to the simulator; and the now-traded broker could no longer be
  flipped (409). The startup log line read
  `"trading_mode": "paper", "live_trading_enabled": false, "live_broker": "NOT_CONFIGURED"`.
- The `LiveBrokerAdapter` itself is exercised only against an injected fake
  SDK client that has no network access. `build_live_broker_adapter()` is
  tested exclusively in its REFUSING direction — research mode, paper mode,
  the flag off, each partial credential trio, and the quote-credential trio
  — because the accepting direction would require enabling live trading,
  which this repository's tests must never do. The HTTP-level live tests
  supply a real `LiveBrokerAdapter` over that fake client via FastAPI's
  `dependency_overrides`, so the production class runs its production code
  without any `Settings` value being touched.

Cleanup: the `trading-os-phase43` compose project was torn down with `-v`,
the `.venv` and the smoke-test log/script were removed, and no `.env` file
was created at any point — every variable was shell-exported for this
session only.
Status: Implemented and verified as above.

---

**D059 — Phase 44: FundamentalAnalyst and NewsAnalyst built on the EXISTING Longbridge vendor relationship, closing the "no real data source" block; SentimentAnalyst deliberately cut**

Date: 2026-09-01

Numbering note: this repo is currently being worked in parallel git
worktrees. `main` was at D057 when this phase branched; sibling worktree
phase43 is claiming D058, so this phase takes D059. The number is
allocated at branch time, not at merge time.

Decision: Built the parallel analyst layer's second and third members —
`FundamentalAnalyst` (`apps/api/app/agents/fundamental_analyst.py`) and
`NewsAnalyst` (`apps/api/app/agents/news_analyst.py`) — mirroring D019/
D021's `TechnicalAnalyst` structure exactly. This closes the block
recorded in PROJECT_CONTEXT and IMPLEMENTATION_STATUS since Phase 16:
those analysts were never built because there was no real data source and
spec §57 forbids fabricating one.

**The block is closed without adding any vendor.** The key finding of this
phase is that the `longport` SDK already installed and already
credentialed for quotes (D008/D015) and daily candlesticks (D021) also
exposes company fundamentals and news. So this is the same vendor
relationship, the same three environment variables
(`LONGPORT_APP_KEY`/`_APP_SECRET`/`_ACCESS_TOKEN`), no new external
service, no new credentials, and no new tool permission.

**SDK surface verified by introspection, not assumed** (the same
discipline as D008/D015/D021, and it mattered here). Direct runtime
introspection of the installed `longport` 4.3.7 plus its shipped
`openapi.pyi` established:
- `AsyncFundamentalContext.create(config)` (synchronous, like
  `AsyncQuoteContext.create`), with `ctx.company(symbol)` returning a
  `CompanyOverview` (`.company_name`, `.name`, `.category`, …) and
  `ctx.valuation(symbol)` returning a `ValuationData` whose `.metrics`
  carries `pe`/`pb`/`ps`/`dvd_yld`, each an optional `ValuationMetricData`
  holding `.list[ValuationPoint]` where a point is `(.timestamp, .value)`
  and `.value` is a **string**.
- `AsyncContentContext.create(config)` with `ctx.news(symbol)` returning
  `list[NewsItem]` (`.title`, `.published_at`, `.url`, `.description`).
- A real, load-bearing discrepancy: the SDK's own `openapi.pyi` is
  **incomplete**. It declares the synchronous `FundamentalContext` but
  omits `AsyncFundamentalContext`, which genuinely exists at runtime. The
  `# type: ignore[attr-defined]` on that one import is annotated as being
  about the vendor's missing stub, not about an unverified attribute —
  had this been taken from documentation or memory rather than
  introspection, the mismatch would have surfaced only at runtime.

Structure, mirroring the established pattern:
- Two new ports, each its own `Protocol` in its own module rather than an
  overload of an existing one, exactly as `HistoryProvider` is separate
  from `MarketDataProvider`: `marketdata/fundamentals_provider.py`
  (`FundamentalsProvider` + a `CompanyFundamentals` model) and
  `marketdata/news_provider.py` (`NewsProvider` + a `NewsHeadline`
  model). Both reuse `DataUnavailableError`/`VendorError` from
  `marketdata/provider.py` — identical failure semantics, so a parallel
  error hierarchy would have been noise.
- Two concrete implementations in the existing
  `marketdata/providers/longbridge.py`, each behind our own narrow
  `Protocol` at the vendor boundary so tests inject a fake and never touch
  real credentials or the network.
- **Every field on `CompanyFundamentals` is `| None` on purpose.** A real
  vendor routinely has a P/E but no P/S for the same symbol. A missing
  metric stays missing all the way into the prompt, where it renders as
  the literal string "not reported by the vendor" — never zero, never an
  interpolation, never an industry norm. Unparseable metric strings
  (`""`, `"--"`) are treated as missing rather than coerced.
- **No LLM computes anything.** The vendor returns P/E, P/B, P/S, and
  dividend yield as real reported values. The one genuinely derived
  quantity, earnings yield, is exact arithmetic (`100/PE`) in
  `marketdata/fundamental_metrics.py` — pure, no I/O, no LLM — which is
  the same role `indicators.py` plays for `TechnicalAnalyst`. That module
  also owns `latest_point()`, which selects a metric's current value **by
  timestamp**, never by the vendor's list position (the vendor's ordering
  is not contractual — the same reasoning behind
  `LongbridgeHistoryProvider`'s explicit sort).
- For `NewsAnalyst` the fabrication risk has a different shape: not a
  wrong number but an invented article. So the headline **count and date
  range are computed deterministically in `format_headlines()`** from the
  exact list being shown, and the system prompt forbids citing anything
  outside the numbered list — explicitly including the model's own
  training knowledge of the company. News items lacking a title or a real
  publication date are dropped at the provider, never back-filled with
  "now" or "(untitled)".
- Both reads use the same `stance`/`summary`/`confidence` contract as
  `TechnicalRead`, with **no side, quantity, price, or stop field** — there
  is nothing a caller could mistake for a trade proposal. `Stance` and
  `AnalystOutputError` are now shared analyst-layer types (still defined
  in `technical_analyst.py`, documented as shared rather than duplicated).

Wiring (`POST /brokers/{broker_id}/agent-trades`): both are optional,
additive context on `TraderAgent.propose()`, identical to D019's posture.
No new endpoint, no new request or response field, no new error response.
Each analyst requires **both** its LLM analyst and its data vendor to be
configured — unlike `TechnicalAnalyst`, which can still comment
qualitatively on the live quote it is always handed, there is no
fundamentals or news equivalent of "one price" to fall back on, so
without vendor data there is nothing real to narrate. Absent analyst,
absent vendor, an uncovered symbol, a vendor failure, or an unparseable
LLM response each silently omit that one paragraph. The three analysts now
run **concurrently** via `asyncio.gather` (docs/AGENT_POLICY.md:
"Parallelize agents with no cross-dependency") and are independently
failure-isolated — a dead news vendor cannot cost the trade its perfectly
good fundamental read. The Risk Engine, the Portfolio Manager, and the
deterministic price path are all untouched.

**Scope cut: `SentimentAnalyst` was deliberately NOT built.** The
Longbridge SDK exposes no real sentiment score. The only way to produce
one would be to ask the LLM to score news headlines itself — which would
convert this layer's rule from "the LLM narrates real data" into "the LLM
invents a number," precisely what spec §57 forbids and what D019/D021's
whole deterministic-indicator design exists to prevent. A sentiment
analyst therefore stays blocked on a real sentiment data source, exactly
as fundamentals and news were until this phase. Documenting the cut is the
honest outcome; shipping a fabricated score would not be.

Verified live:
- `ruff check .` — "All checks passed!"
- `mypy apps` — "Success: no issues found in 84 source files"
- `bash scripts/secret_scan.sh` — "Secret scan clean." (run through an
  LF-normalized copy; the checked-in script has CRLF line endings, which
  Git Bash on this Windows host rejects — a pre-existing host quirk, not
  a change from this phase, and left alone rather than "fixed" in a way
  that would churn the file for other platforms).
- `alembic upgrade head` against an isolated Postgres — all twelve
  migrations applied cleanly, confirming this phase adds no migration.
- `pytest tests/ -q` — **481 passed**, 3 pre-existing warnings (the same
  `httpx`/`starlette` deprecation and two `InsecureKeyLengthWarning`s seen
  in every prior pass), zero failures, zero errors. 57 of those are new in
  this phase: 10 fundamentals-provider, 11 news-provider, 7
  fundamental-metrics, 12 fundamental-analyst, 14 news-analyst, and 7
  real-Postgres integration tests of the agent-trades wiring.
- One real regression was caught by the existing suite and fixed:
  widening `TraderAgent.propose()` broke
  `tests/api/test_technical_analyst_wiring.py`'s `CapturingTraderAgent`
  subclass, whose narrower override no longer matched the call. The
  subclass now accepts and forwards all three contexts; D019's asserted
  behavior is unchanged.

**NOT verified against a real vendor response in this environment — stated
plainly, per the D021/D025/D030 precedent.** No `LONGPORT_*` credentials
exist anywhere in this project: the repo's `.env` contains only
`DATABASE_URL`/`REDIS_URL`/`JWT_*`/mode settings, and no `.env` was
created for this phase. The session's connected Longbridge MCP server was
tried as an independent check and returned `401103: token is expired`, so
it could not confirm anything either (and it is a different transport from
the Python SDK anyway — its field names differ, e.g. `publish_time` vs the
SDK's `published_at` — so it would have been weak evidence at best).

Concretely, what **is** verified: the SDK's method names, call signatures,
context-construction pattern, and response attribute names, all by direct
introspection of the installed package; and the full provider and analyst
logic against fakes at the vendor boundary, matching this project's
established no-live-network-call-in-CI discipline. What is **not**
verified: that a real Longbridge account returns populated
fundamentals/news for any given symbol, and that the real response objects
carry the exact attribute *values* (as opposed to attribute *names*) this
code reads. The providers are written defensively for that gap —
`_optional_decimal`/`_as_utc`/`_non_empty_str` treat anything unexpected
as absent rather than coerced — so the realistic worst case on first
contact with live data is that an analyst contributes no context, which is
already a fully-supported, non-blocking state. A first live run against
real credentials remains outstanding follow-up work for this phase.

Cleanup: the `trading-os-phase44` compose stack (a single Postgres on
remapped host port 5444, chosen to avoid the user's own stack on 5432 and
sibling worktree phase43's on 5443) was torn down with `-v`,
`docker-compose.phase44.yml` was deleted, and the `.venv_p44` venv was
removed. No `.env` file was created at any point — every variable was
shell-exported for this session only. The user's own
`trading-os-postgres-1`/`trading-os-redis-1` containers and
`trading-os-phase43-postgres-1` were confirmed still running and untouched
via `docker ps` after cleanup.
Status: Implemented and verified as above.

---

**D060 — Dashboard redesign (Phase 45): a real layout shell and a design-token system, with every backend-driven state preserved exactly**

Numbering note: this entry is D060, not D058. Phases 43 and 44 are being
built in sibling worktrees off the same `main` at `9c0660c` and are
claiming D058 and D059 respectively, following the parallel-worktree
convention this project has used since D028 — each phase reserves its
number up front so three branches can merge in any order without a
renumber.

**What this phase is, and what it deliberately is not.** The dashboard
worked and was honest, but it was a single `max-w-2xl` column of eight
identically-weighted bordered boxes stacked vertically, styled with
ad-hoc `border-neutral-300` / `dark:border-neutral-700` classes repeated
at every call site. An operator could not tell at a glance which panel
mattered, the equity chart carried the same visual weight as an optional
"stop price" input, and `globals.css` held nothing but the two-variable
`--background`/`--foreground` pair `create-next-app` ships.

This is a **visual and layout pass only**. No endpoint changed, no fetch
changed, and no condition governing whether something renders changed.
That constraint was the hard part and is worth stating precisely: every
`NOT_CONFIGURED:` / `DATA_UNAVAILABLE:` / `AGENT_OUTPUT_INVALID:`
sentinel is still rendered verbatim as the backend sent it; every 401
still routes through `handleExpiredSession` (D031/D032); and the
Portfolio Manager verdict still appears if and only if the backend
actually reported a `portfolio_action`, never for an `approve` and never
for a null (D029). Colour is chrome, never a claim: a P&L figure is
tinted from the sign of the number the backend returned and is left
**uncoloured** when the value is absent or unparseable, and
`live_trading_enabled` renders `unknown` in the neutral tone rather than
a reassuring green when the field is missing.

**(1) Design tokens, dark-first, both modes real.** `app/globals.css`
now defines a semantic token set — a four-step surface scale
(`--canvas` / `--surface` / `--raised` / `--well`), two line weights,
three text weights, an accent, and `--pos` / `--neg` / `--grid` for data
— exposed to Tailwind v4 through `@theme inline`. Dark is the primary
palette (a `#060b16` ground, deliberately not `#000000`), and light is a
separately contrast-checked palette rather than an inversion: every text
token clears 4.5:1 against `--surface` in **both** modes, which is why
the accent is `#047857` in light and `#34d399` in dark. Both were
verified in a real browser under emulated `prefers-color-scheme`.

The semantic verdict colours are the one deliberate exception. The Risk
Engine's amber and the Portfolio Manager's violet/blue keep explicit
Tailwind palette classes in the components rather than becoming tokens,
because those hues carry meaning fixed by D029 and must not drift with a
theme edit. `riskVerdictTone` still keys on `approved`, so a trade that
is `approved: true` with `status: "rejected"` still gets a *neutral*
block beside a violet portfolio panel — amber still means, and only
means, "the Risk Engine said no".

**(2) A shell instead of a column.** `components/shell/AppShell.tsx`
adds a persistent left rail (brand, navigation, live `SessionStatus`,
sign-out) and a sticky page header carrying the title and the real
`/health` status strip. `components/shell/SideNav.tsx` marks the current
route with `aria-current="page"`. `/admin` is still listed for every
authenticated user: hiding it would imply a client-side security
boundary that does not exist, when the real `admin:manage` gate is the
backend's — as the live check below re-confirmed by rendering a genuine
`HTTP 403: Missing required permission: admin:manage`.

The dashboard is now a 12-column grid under three section headings. The
ordering is not arbitrary. `BrokerDiscovery` stays ahead of everything
broker-scoped because it is where the `broker_id` those panels need
comes from (D034); portfolio state and the equity curve lead and take
the wide column; the two order-entry panels share a row so neither reads
as the default action; the backtest is last and visually separate
because it alone is not broker-scoped — a run builds its own throwaway
paper broker and writes nothing (D025). Desktop-primary, as an operator
tool should be, but the grid collapses cleanly at 1024px and every wide
table scrolls inside its own `overflow-x-auto` container so the page
body never scrolls sideways.

**(3) Charts, still hand-rolled.** `components/ui/ChartFrame.tsx` is
shared by both equity charts and adds *chrome only* — a plot ground,
three low-contrast gridlines, a gradient area fill, and the min/max axis
labels the previous version left the reader to infer. It draws no data
of its own: `buildPoints` and `buildBacktestPoints` are untouched, the
area polygon is the same vertices closed to the baseline, and nothing is
interpolated, smoothed, or extended past the last real point. Point
markers keep their `<title>` tooltips and both charts keep their full
data table, which remains the accessible fallback.

**No new dependency was added.** `package.json` is byte-identical: Inter
and JetBrains Mono come from `next/font/google`, which is part of Next
itself, and every component is hand-built Tailwind. The `ui-ux-pro-max`
skill that informed the palette and typography recommends a component
library; that recommendation was translated into local components rather
than installed, per this project's ask-before-adding-a-tool rule.

**Tests.** 102 frontend tests over 13 files — the same 102 that passed
before the redesign. No test was deleted, skipped, or weakened. Two
initially failed, both in `TradeForm.test.tsx`, and the failure was
correct: the assertion is that when `portfolio_action` is null *nothing
on screen mentions the Portfolio Manager*, and a new static panel
description had named it. The fix was to reword the chrome, not the
assertion — standing text naming that gate would read as "it looked at
this and was fine", which is exactly the fabrication D029 exists to
forbid. The shared `Field` primitive keeps its label as the only text
inside the `<label>` element, with hints as siblings, so every input's
accessible name is still exactly its label; this was confirmed live by
enumerating all 21 labels in the running page.

**Live verification.** An isolated stack (compose project
`trading-os-p45`; Postgres 5445, Redis 6445, uvicorn 8045, Next 3045 —
chosen to avoid the user's own 5432/6379/8000/3005 and the phase43/44
worktrees on 5443/5444), migrated to head and seeded with
`scripts/seed_e2e.py` plus a real trade and five real portfolio
snapshots. With no market-data vendor and no LLM provider wired, the
following were driven in a real browser and all rendered faithfully:

- `HTTP 503: NOT_CONFIGURED: no market data vendor is wired (see
  docs/DECISIONS.md D008/D015).` — quote lookup
- `HTTP 400: NOT_CONFIGURED: no LLM provider is wired (see
  docs/DECISIONS.md D018).` — agent trade
- `HTTP 400: NOT_CONFIGURED: no history provider.` — backtest
- `HTTP 403: No access grant for broker 3e2e0000-...-0003.` — the real
  ungranted-broker 403, not a 404 for a non-existent id
- `HTTP 403: Missing required permission: admin:manage` — the admin
  listings, as the non-admin trader user
- `Incorrect email or password.` — the backend's own login 401
- The **D029 case in full**: a real response carrying `approved: true`,
  `status: "rejected"`, `portfolio_action: "reject"` and
  `portfolio_binding_constraint: "symbol_concentration"`, rendered as a
  neutral Risk Engine block beside a violet Portfolio Manager panel
  reading "Rejected by the Portfolio Manager (not the Risk Engine)".

The five seeded snapshots also produced a real equity curve, plotted
with its five real vertices and labelled with its true `112288` maximum.

One thing is worth recording so it is not mistaken for a defect later:
the preview pane's screenshot compositor mis-places `position: sticky`
layers once the page is scrolled, producing blank captures. This was
diagnosed rather than assumed — `getBoundingClientRect` on the running
page showed the rail correctly pinned at `y: 0` with `scrollY: 765` on a
2702px document, i.e. correct sticky behaviour. Captures were taken with
the sticky positioning temporarily neutralised from the console; the
shipped CSS is unchanged.

Cleanup: the compose stack was torn down with `-v`, the `.venv_p45`
virtualenv removed, and the throwaway compose file kept outside the repo
entirely. No `.env` file was created at any point — every variable was
shell-exported for this session only. The user's own
`trading-os-postgres-1` / `trading-os-redis-1` containers and their
uvicorn (8000) and Next.js (3005) dev servers were confirmed still
running and untouched.

---

**D061 — Full post-merge verification of Phases 43+44+45: live-trading path, FundamentalAnalyst/NewsAnalyst, and dashboard redesign merged together**

Date: 2026-09-01
Decision: After merging `phase-43-live-trading-path`,
`phase-44-fundamental-news-analysts`, and `phase-45-dashboard-redesign`
sequentially into `main` (D058/D059/D060), ran a full from-scratch
integration pass. Phase 43 and Phase 44 both independently modified
`apps/api/app/api/dependencies.py` (adding `get_live_broker_adapter` and
`get_fundamentals_provider`/`get_news_provider`/`get_fundamental_analyst`/
`get_news_analyst` respectively) — a clean additive conflict, resolved by
keeping every new function from both sides. `docs/DECISIONS.md` had the
same additive-append conflict pattern established since D030/D031 across
all three merges.
This verification ran directly in the primary session rather than via a
delegated subagent: an attempt to launch one was refused by this
environment's safety classifier, almost certainly because the prompt's
own language (live-trading path, real broker credentials) tripped a
heuristic even though the actual task was read-only verification with
`LIVE_TRADING_ENABLED` never touched. Doing the verification directly
avoided re-describing that context to a fresh agent.
Verified: `bash scripts/secret_scan.sh` clean; `ruff check .` clean;
`mypy apps` clean (86 source files); `alembic upgrade head` applied
cleanly against a fresh isolated Postgres (no new migration from any of
the three phases); `pytest tests/ -q` → **544 passed, 0 failed, 3
pre-existing warnings** — exactly 424 (D057 baseline) + 63 (Phase 43) + 57
(Phase 44), confirming the two branches' new test files were genuinely
disjoint. Phase 45 added no backend tests (frontend-only). Re-confirmed
the live-trading safety invariant survived the merge: `git grep` for
`live_trading_enabled\s*=\s*True|LIVE_TRADING_ENABLED=true` across `apps/`
and `tests/` matches only docstrings/error-message text plus two
pre-existing unit tests (`tests/core/test_execution_context.py`,
`tests/test_config.py`) that construct a `Settings` object to exercise the
fail-closed validator — neither starts an app or attempts a broker call.
No frontend re-verification was performed in this pass (Phase 45's own
agent already ran `npm run build`/`npm test` = 102/102 live before
merging, and neither Phase 43 nor 44 touched `apps/web/`).
Infrastructure: isolated docker compose project `tosverifymain` (ports
55499/56499, via an untracked `docker-compose.verify-main.yml` override —
deleted afterward, `docker-compose.yml` itself never modified), torn down
with `-v` on completion. No `.env` file was created; all config was
shell-exported for this session only. The user's own
`trading-os-postgres-1`/`trading-os-redis-1` containers (holding the real
paper-trading Longbridge credentials in the root `.env`, never read or
logged by this verification) were confirmed still running and untouched
before and after.
Status: Implemented and verified as above.

---

**D062 — Broker paper/live mode UI (Phase 47): closing D058's deliberately-deferred frontend, and refusing to invent the analyst breakdown the API does not return**

Numbering note: this entry is D062, the next free number after D061 at the
time of writing. A sibling Phase 46 worktree is building off the same
`main`; if it also lands a decision, the numbers stay disjoint because
each phase reserves its own up front (the convention since D028).

**Part A — the mode UI D058 deferred.** D058 built and tested `POST
/admin/brokers` and `PATCH /admin/brokers/{broker_id}/mode` backend-side
and shipped no frontend at all, on the explicit grounds that "a
live-trading UI deserves its own reviewed phase". Until now the only way
to designate a broker `kind: "live"` — the single row-level switch that
decides which adapter a real order reaches — was a hand-rolled HTTP call.
`apps/web/components/admin/BrokerModeAdmin.tsx` is that UI. It is
frontend-only: no backend route, schema, or business rule was touched,
and the two new route handlers under `apps/web/app/api/admin/brokers/`
are pass-through proxies in the identical shape as every sibling admin
proxy.

Three properties of the confirmation gate are load-bearing, not styling:

1. **`confirm_live` is sent only when the operator ticked the dedicated
   checkbox AND the target kind is `live`.** For a paper target the key is
   omitted from the payload entirely rather than sent as `false`, so a
   body can never carry a live-trading affirmation nobody made. The
   checkbox is never pre-checked and is disabled while the target is
   paper. Neither proxy injects or defaults the flag either — a proxy that
   supplied it would turn a server-enforced safeguard into one this layer
   could satisfy on the user's behalf.

2. **Submitting without the tick is deliberately NOT blocked
   client-side.** The request goes out without `confirm_live` and the
   backend's own 400 `LIVE_KIND_CONFIRMATION_REQUIRED` is rendered
   verbatim. This is D058's own argument applied to its UI: a confirmation
   that exists only in the frontend is not one the server can enforce, so
   the server must remain the thing that refuses — and the operator should
   see it refuse rather than have a disabled button imply the rule lives
   in the browser. A greyed-out submit would also have made the real 400
   unreachable through the product, which is precisely the response this
   phase most needed to prove renders honestly.

3. **Changing the target kind clears the tick.** Otherwise a confirmation
   made while `live` was selected could survive a switch to `paper` and
   back, letting a stale affirmation authorise a designation the operator
   never re-considered.

Backend refusals are rendered as the backend worded them, prefixed with
the real status and nothing else — the 409 for a broker with recorded
orders keeps its full explanation *and its remedy sentence* ("Create a new
broker instead"), because that sentence is the only part that tells the
operator what to actually do. No local paraphrase, no friendlier
substitute.

**The broker listing went into `AdminListings.tsx`, not the new file.**
There is no platform-wide admin broker listing endpoint, so the list reads
D034's `GET /brokers` — which is scoped to the brokers the *calling* user
holds a grant for. That is stated in the panel rather than left implicit:
an admin without a grant for a broker will not see it, and a list that
silently implied completeness would be exactly the wrong thing to trust
when deciding what is or is not designated live. (Observed live: the two
brokers created through the new form never appear in it, because the admin
holds no grant for them.) It reuses the existing `useAdminList` hook
instead of a second hand-rolled fetch — which also kept the frontend lint
baseline unchanged, since a fresh mount-load effect would have added a
third `react-hooks/set-state-in-effect` error to the two already present.

**Part B — what was deliberately NOT built, and why.** The phase brief
asked for a per-analyst breakdown in the agent-trade result and a trade
history panel. Both were investigated against the real code and both are
blocked on the backend, so neither was fabricated:

- **`AgentTradeResponse` carries no analyst field of any kind.** It is
  `TradeSubmissionResponse` plus `side`, `quantity`, `rationale`
  (`apps/api/app/api/schemas.py`). D059's three analysts do run
  server-side — `_technical_context` / `_fundamental_context` /
  `_news_context` in `apps/api/app/api/routes/trades.py` — but their
  output feeds the TraderAgent's prompt and is never returned. The
  response cannot even say *whether* any of them ran, since each is
  failure-isolated and returns `None`. So `AgentTradeForm` now states that
  plainly next to the result instead of showing three panels built from
  nothing. Surfacing real analyst reasoning requires a response-shape
  change, which is backend work and out of scope for a frontend phase.
- **No order/fill list endpoint exists.** Phase 4 persists `orders` and
  `fills`, but every `GET` route in `apps/api/app/api/routes/` was
  enumerated and confirmed against `docs/API.md`: there is no
  `GET /brokers/{id}/orders`, no `/trades`, and no listing schema. A
  `TradeHistory.tsx` could therefore only have been built against an
  endpoint that does not exist, so it was not built at all. This is
  flagged as the single largest remaining product gap: the platform
  records an append-only audit trail no user can read back.

Verification: `npm test` in `apps/web` → **116 passed, 14 files**, up from
the 102/13 baseline this branch started from (D060's count, re-confirmed
by running the suite before any edit); 14 tests added, **none deleted,
skipped or weakened**. `npm run build` exits 0 with both new route
handlers registered (`ƒ /api/admin/brokers`, `ƒ /api/admin/brokers/
[brokerId]/mode`). `npm run lint` ends at the **same 3 problems (2 errors,
1 warning)** as before this phase, all in files it does not touch
(`BrokerDiscovery.tsx`, `SessionStatus.tsx`, `lib/session.ts`); the one new
error an earlier draft introduced was removed by the `useAdminList` reuse
above rather than suppressed. The pre-existing `tsc --noEmit` failure in
`app/layout.tsx` (`Cannot find name 'LayoutProps'`, a Next-generated type)
is unchanged and unrelated.

Live-verified in a real browser against an isolated throwaway stack
(Postgres 55447, Redis 63447, uvicorn 8047, Next 3047; `/health` reported
`trading_mode: paper`, `live_trading_enabled: false` throughout), seeded
via the existing `scripts/seed_e2e.py` plus one **real** paper trade
submitted through `POST /brokers/{id}/trades` so a broker genuinely had a
recorded order. Seven scenarios, all against the real backend: create
paper → 201; create live **without** the tick → the real 400
`LIVE_KIND_CONFIRMATION_REQUIRED` verbatim; create live **with** the tick
→ 201 `kind: live`; the stale-tick guard (live → paper → live leaves the
box unticked); flip a broker holding a real order → the real 409 with its
remedy sentence intact; flip a clean broker to live without the tick →
400; and with the tick → 200 with `kind` genuinely changed paper → live.
Both colour schemes checked at 1280px. `AgentTradeForm` was exercised
live and returned the real `NOT_CONFIGURED: no LLM provider is wired`
sentinel, with the analyst note correctly absent (it renders only
alongside a real result); the note's own rendering is covered by component
test rather than live, since no LLM provider is wired in a throwaway
stack and none was going to be.

Safety: `LIVE_TRADING_ENABLED` was never touched and no live credential
was ever set, so the live execution path stayed inert for the whole phase;
the only broker ever designated `live` was a throwaway row in a throwaway
database with no credentials behind it, which is a DB designation and not
a trade. No `.env` was created — all config was shell-exported for the
session. Stack, venv and containers were removed afterwards; the sibling
phase's `tos-p46-pg` container and the user's own Supabase containers were
confirmed untouched.
Status: Implemented and verified as above.

---

**D063 — Phase 46: self-service password reset, with email delivery as an optional NOT_CONFIGURED vendor**

Numbering note: this worktree branched from `main` at D061 and originally
claimed D062, the next free number at branch time. The concurrently-run
sibling Phase 47 worktree also branched at D061, claimed D062 for its own
entry, and merged to `main` first — so this entry is renumbered to **D063**
at merge time to avoid a collision, following the same convention used
since D028/D030/D031/D032 for parallel worktrees landing on the same
D-number.

Date: 2026-09-02

Decision: Build a real forgot-password flow — a `password_reset_tokens`
table (migration `0013`), `POST /auth/password-reset/request`, `POST
/auth/password-reset/confirm`, an admin-only `POST
/admin/users/{id}/password-reset`, and the `/forgot-password` and
`/reset-password` pages — and treat outbound email as an optional,
all-or-nothing vendor under the `EMAIL_PROVIDER_*` trio, exactly like
`LONGPORT_*` (D008/D015) and `LLM_PROVIDER_*` (D018).

Until this phase the only recovery path for a forgotten password was a
direct SQL update, and D049 had made that worse rather than better: a user
who mistypes five times is locked out for fifteen minutes with, in its own
words, "no unlock endpoint and no email flow". That is a defensible
position for a lockout and an indefensible one for a whole account.

**Why email is a NOT_CONFIGURED vendor rather than a dependency.** A
password reset that required a transactional-email account before it worked
at all would mean the committed default of this repo — a self-hosted
deployment with no vendor relationships — has no recovery path, which is
the state we were trying to leave. So the token is created either way, and
the two delivery paths are:

- `EMAIL_PROVIDER_*` unset (the default): the public endpoint issues a real
  token and sends nothing. An admin holding `admin:manage` reads the actual
  link out of `POST /admin/users/{id}/password-reset`, whose response says
  `"delivery": "NOT_CONFIGURED_returned_directly"` and carries
  `reset_link`, and relays it over a channel they already trust.
- `EMAIL_PROVIDER_*` set: the same endpoints send the mail, and the admin
  response becomes `"delivery": "SENT"` with `reset_link: null`. Withholding
  the link once email works is the point — otherwise configuring a provider
  would not have changed who can obtain one.

The rejected alternative was the tempting one: log the link at INFO on an
unconfigured deployment and call the flow "working". That puts a live
account-takeover credential in a log aggregator forever, for every user,
including ones nobody is currently helping. The admin endpoint puts the
same string behind a permission check, in a response that is not persisted
anywhere, only when a human asks for it.

**The wire contract** (documented in
`apps/api/app/notifications/transactional_email.py`, restated here so it
can be judged without reading code): `POST {base}/emails`, bearer auth,
JSON `{"from", "to": [...], "subject", "text"}`, any 2xx meaning accepted.
That is Resend's shape and close enough to several lookalikes to be
reachable through a one-file adapter, but nothing is named after a vendor —
`apps/api/app/notifications/provider.py` is the Protocol, and a vendor that
disagrees implements it as a second adapter rather than loosening the
first. `to` is a list even for one recipient because a bare string is
silently misread by several of these APIs. Plain text only, no HTML part: a
reset email is one sentence and one URL, and an HTML template would add a
second place for the link to diverge from the text.

**Returning normally is a claim.** Every other delivery mechanism in this
app hands the caller something inspectable; a sent email does not. So
`EmailProvider.send` has exactly two outcomes — accepted, or
`EmailProviderError` — with no third state in which delivery may be assumed
because nothing complained. And "accepted" is the strongest word used
anywhere in the code, the API docs or the UI: a 2xx from a transactional
API means the vendor queued the message, and nothing in this stack can see
an inbox. This is `docs/TRADING_SAFETY.md`'s no-fabrication rule applied to
notifications rather than to market data.

**Anti-enumeration, and the one place it cost us something.**
`POST /auth/password-reset/request` is the only unauthenticated route in
this app that takes a user identifier, so it is the only place an
enumeration oracle can be built. Five branches — address not registered,
account deactivated, per-account throttle tripped, delivery
NOT_CONFIGURED, send failed — converge on one status and one byte-identical
body, which is a module constant rather than a literal per branch so a
future edit cannot casually diverge one of them. Storage is held to the
same rule: no row is written for an unknown or inactive address, or the
table itself would become the oracle the response refuses to be.

That constraint forced the acknowledgement's wording. "A reset link has
been sent to you" would be a fabrication in three of the five branches, so
the message says a link "has been **issued**", and names the real reason
one might never arrive ("this deployment may not have email delivery
configured"). It reads as hedging and is not: it is the only sentence true
in all five cases while telling the caller nothing about which one they
hit. The frontend renders that sentence verbatim and is tested for not
saying "check your inbox".

`POST /auth/password-reset/confirm` answers every failure with one 400
sentinel, `INVALID_OR_EXPIRED_TOKEN`. Unknown, expired, already-used and
deactivated-user are deliberately indistinguishable: "this expired" would
confirm that a real reset was requested for a real account, and the remedy
is identical in all four cases anyway. The admin endpoint, by contrast, IS
specific (404 unknown id, 400 `USER_INACTIVE`, 502
`EMAIL_DELIVERY_FAILED`) — the caller already holds `admin:manage` and
already named an id, so there is nothing left to enumerate.

**SHA-256, not bcrypt, for the token.** `token_hash` stores an unsalted
SHA-256 digest, and this is not a weakening of the `users.hashed_password`
precedent — it is the same reasoning applied to a different input. Bcrypt's
cost factor buys resistance to offline guessing of LOW-entropy secrets; a
reset token is 32 bytes from `secrets.token_urlsafe`, so there is nothing
to guess and no rainbow table can exist over that space. Bcrypt would also
make redemption unindexable: every digest carries its own salt, so "find
the row for this token" degrades from one indexed lookup to a bcrypt
verification against every outstanding row. What the hash defends against
is exactly one thing — a database dump being a set of working reset links —
and it defends against it completely.

**Two throttle layers, honestly scoped.** They exist because the endpoint
is unauthenticated and every call can cost an email send.

- Per **account**: at most `AUTH_PASSWORD_RESET_MAX_REQUESTS_PER_HOUR`
  (default 5) tokens per rolling hour, counted from `password_reset_tokens`
  itself. Counting persisted rows rather than an in-process tally is what
  makes it hold across uvicorn/gunicorn workers and across restarts, with
  no new table and no new dependency — the same reasoning D049 used to
  reject Redis for the login lockout (Redis is provisioned in
  `docker-compose.yml` and still unwired). Tripping it never changes the
  response, or the limit would fire only for registered addresses and
  become a louder version of the oracle.
- Per **client IP**: `AUTH_PASSWORD_RESET_MAX_REQUESTS_PER_IP_PER_HOUR`
  (default 20), answered with 429. This is the layer that bounds a flood of
  addresses that do not exist, which the row count by construction cannot
  see. It is an in-process fixed-window counter, so N workers means N times
  the ceiling and it resets on restart; `apps/api/app/auth/reset_throttle.py`
  says so in its own docstring rather than overselling. It reads
  `request.client.host` and deliberately ignores `X-Forwarded-For`: trusting
  a forwarding header without knowing the proxy depth makes the limit
  bypassable by spoofing one, and the failure mode of the honest version
  (over-throttling everyone behind a shared proxy) is strictly better than
  the failure mode of the dishonest one (no throttle at all). Key tracking
  is capped at 10,000 with LRU eviction so an attacker cycling addresses
  cannot turn the mitigation into a memory leak.

**Redemption invalidates siblings, but issuance does not.** A user who
clicks "forgot password" twice because the first email was slow cannot tell
which of the two messages they are looking at, so issuing a second token
leaves the first alive. Every outstanding unused token for that user is
consumed at REDEMPTION time instead — the moment the account is provably
back under someone's control and old links stop being useful. `used_at`
therefore means CONSUMED (redeemed, or invalidated by a sibling's
redemption), never NULL again; the password write, the lockout clear and
the sibling invalidation happen in one transaction, so a crash cannot leave
a changed password beside a still-live token.

A successful reset also clears `failed_login_count`/`locked_until` (D049).
Someone who forgot their password very likely locked themselves out
guessing at it first, and leaving that lock in force would strand them
behind a fresh, correct password for fifteen more minutes with no way to
tell why.

**Deliberately NOT built**, each for a reason rather than for time:

- **An admin endpoint that sets a password directly.** An admin who could
  type a user's new password would know it, which is strictly worse than
  handing over a single-use link the user redeems themselves.
  `PATCH /admin/users/{id}` still has no password field.
- **A session issued on successful reset.** The user signs in afterwards
  with the password they just chose, which proves the new credential works
  and keeps "proved you control the link" separate from "is now signed in".
- **Password strength rules beyond the existing `min_length=8`.** That is
  the same floor `CreateUserRequest` already enforces, chosen so a reset
  cannot set a password the create endpoint would have refused. Inventing a
  richer policy here would have made the two endpoints disagree.
- **Email verification, self-service registration, account deletion.** Out
  of scope; D013's admin-creates-users model is unchanged.
- **A separate `invalidated_at` column** distinguishing "redeemed" from
  "superseded by a sibling". Both are consumed and neither is redeemable,
  so a second column would carry audit nuance nothing currently reads.
- **HTML email, delivery receipts, retries, a queue.** Each would add a
  claim the system cannot verify or a dependency the flow does not need.
- **Rendering these pages inside `components/shell/AppShell`.** That shell
  carries `SessionStatus` and `LogoutButton`, which poll `GET /auth/session`
  and bounce a caller to `/login` on the 401 they are guaranteed to get
  here — every user on these pages is by definition signed out. Wrapping
  them in it would produce a redirect loop out of the one page that exists
  to break one. `components/auth/AuthCard.tsx` instead lifts `/login`'s own
  Phase 45 layout (D060) verbatim so the three pages read as one flow; it
  is a move, not a redesign.
- **A per-row "reset" button on the admin users listing.** That listing is
  read-only (D030), every mutating action in `UsersAdmin.tsx` is already an
  explicit type-the-id form, and a one-click button beside every row makes
  it very easy to issue a live credential for the wrong account with a
  mis-aimed click.

**Verified.** `bash scripts/secret_scan.sh` clean. `ruff check .` clean.
`mypy apps` clean (93 source files, up from 86). `alembic upgrade head`
applied `0013` cleanly against a real Postgres 16, and the
`downgrade base` → `upgrade head` round-trip CI runs was exercised on the
same database. `pytest tests/ -q`: **615 passed**, against a 544 baseline
re-measured on this same branch before any change — 71 new tests across six
files (11 pure token-arithmetic, 8 throttle, 16 email-adapter, 5 config,
22 public-flow integration, 9 admin-endpoint integration), 3 pre-existing
warnings, zero failures. `npm test` in `apps/web`: **126 passed over 15
files**, against a 102/13 baseline measured the same way — 24 new, none
deleted, skipped or weakened. `npm run build` clean, with
`/forgot-password`, `/reset-password`,
`/api/auth/password-reset/{request,confirm}` and
`/api/admin/users/[userId]/password-reset` all present in the route
manifest.

Both email-delivery branches are covered by a fake `EmailProvider` double
implementing the real Protocol, and the HTTP adapter itself is exercised
through `httpx.MockTransport` so every line of `send()` — URL joining,
headers, body shape, status handling — runs unmodified against a scripted
server. **No real email vendor was contacted and no email credential exists
anywhere in this branch**; the `EMAIL_PROVIDER_*` values in `.env.example`
are commented out and empty, and every key-shaped literal in the tests is a
fixture carrying the D052 `pragma: allowlist secret` marker.

**Live-verified over real HTTP**, not only through the test client, across
three `uvicorn` instances against the same real Postgres (8046
NOT_CONFIGURED, 8047 pointed at a local stub vendor on 9046, 8048 pointed
at a dead port to force a send failure), with two real seeded users:

- The app booted logging `email_provider=NOT_CONFIGURED` on 8046.
- A registered and an unknown address returned **byte-identical** 200
  bodies. The unknown-address flood wrote **zero** rows.
- `POST /admin/users/{id}/password-reset` returned 201 with a real
  `reset_link` for the admin and **403 `Missing required permission:
  admin:manage`** for the ordinary user.
- That link redeemed once (200); the new password logged in (200), the old
  one did not (401); replaying the link and submitting a fabricated token
  returned **identical** 400 `INVALID_OR_EXPIRED_TOKEN` bodies.
- Two outstanding links were issued; redeeming the newer made the older
  fail with the same sentinel.
- The per-IP throttle returned a real 429, and returned it for a
  *registered* address too — confirming it cannot be used to probe which
  addresses exist.
- On 8047 the stub vendor received exactly the documented contract:
  `POST /emails`, `Authorization: Bearer …`, `{"from", "to": ["…"],
  "subject", "text"}` with `to` as a list, plain text carrying the link and
  the real 30-minute TTL. The admin response was `"delivery": "SENT"` with
  `reset_link: null`, and the **emailed** link then redeemed successfully.
- On 8048 the admin endpoint returned a real **502 `EMAIL_DELIVERY_FAILED`**
  naming the underlying connection error, while the public endpoint held
  its generic 200 — and the same run incidentally confirmed the silent
  per-account throttle live, logging `password_reset_throttled` and
  writing no row while still returning that same 200.
- Postgres was inspected directly afterwards: every `token_hash` is 64
  hex characters, no raw token appears in any row, and grepping all three
  servers' stdout for the raw tokens and for `reset-password?token=`
  returned **zero** matches — the link never reaches a log.

One real bug was found by the tests rather than by review: the app's
sessionmaker sets `expire_on_commit=False` (`apps/api/app/db/base.py`), so
an integration test that commits and re-selects gets its own
identity-mapped object back rather than the row the route just wrote. The
first draft of `test_a_successful_reset_clears_a_login_lockout` therefore
read a stale `failed_login_count` and failed. Fixed in the test harness
(`_refresh` = commit + `expire_all`), not by relaxing the assertion — the
assertion was correct and the read was lying.

Infrastructure: an isolated `postgres:16-alpine` container on remapped host
port 55446, created solely for this branch and removed with its volume
afterwards. No `.env` file was created at any point — every variable was
shell-exported for this session only. `LIVE_TRADING_ENABLED` was never
touched, no live-trading route or broker path was modified, and this
branch's diff contains nothing under `apps/api/app/execution/`.

Status: Implemented and verified as above. Email delivery is
NOT_CONFIGURED on a default checkout, which is a working state for this
feature, not a missing one.

---

**D065 — Phase 48: read-only order/fill history endpoints, closing D062's "append-only audit trail no user can read back"**

Numbering note: `docs/DECISIONS.md` ended at **D063** when this work began
and this entry originally claimed **D064**, the next free number. A
concurrently-running sibling phase (watchlists — `migrations/versions/
0014_watchlists.py`, `apps/api/app/api/routes/watchlists.py`,
`apps/api/app/api/schemas_watchlists.py`) had already stamped D064
throughout its own source files, so this entry took **D065** instead and
every D-reference in this phase's files was renumbered to match. Same
convention used since D028/D030/D031/D032 and again at D063 for parallel
work landing on one number — except that here the two phases shared a
single checkout, so the collision was visible immediately rather than at
merge time, and this phase yielded rather than the one that had already
written the number into a migration.

Date: 2026-09-03

Decision: Add three read-only, broker-scoped endpoints over the `orders`
and `fills` tables that have been written on every trade since Phase 4
(D006) and never exposed:

  GET /brokers/{broker_id}/orders             — paginated, newest first
  GET /brokers/{broker_id}/orders/{order_id}  — one order plus its fills
  GET /brokers/{broker_id}/fills              — flat execution blotter

Reason: Phase 47 (D062) named this while building the frontend for it —
"the platform keeps an append-only audit trail that no user can read back
through the product … this is the largest remaining product gap." Every
control this system is built around (the Risk Engine's block reasons, the
Portfolio Manager's resize and reject verdicts, who submitted what, and
what actually executed) has been recorded faithfully and been completely
unreadable outside `psql`. A recorded decision nobody can see is not an
audit trail in any useful sense.

Alternatives considered and rejected:

- **Adding the routes to `routes/trades.py`.** That module's docstring
  opens by claiming to hold "the only HTTP entrypoints that can move a
  trade toward the broker", and it is the file a reviewer opens to check
  that claim. Putting listings there would make the sentence false at a
  glance for no benefit. These routes live in a new
  `apps/api/app/api/routes/orders.py`, registered next to the portfolio
  router, for the same reason `routes/portfolio.py` is separate: it is a
  broker-scoped read, not an execution path.
- **A new `orders:view` permission.** Rejected as a boundary that does not
  correspond to a real difference in capability. Order history is the
  history *behind* exactly the positions and P&L `portfolio:view` already
  authorizes, so these routes use
  `require_broker_access(Permission.VIEW_PORTFOLIO)` — literally the
  dependency every other broker-scoped read uses. The task of inventing a
  permission scheme was deliberately not undertaken.
- **Cursor pagination.** Rejected for consistency: D027, D031 and D034 all
  use `limit`/`offset` in an `{items, limit, offset}` envelope, and D062's
  own gap note asked for that convention by name. `limit` defaults to 50
  and is capped at 500; 501, 0 and a negative `offset` are 422s from
  FastAPI's own validation, never silently clamped.

Two shape decisions worth recording, both about not inventing facts:

1. **No `order_type` field, because there is no order-type column.**
   `orders` records `side`, `quantity`, `estimated_price` and an optional
   `stop_price`; it has no `order_type` or `time_in_force`. Emitting a
   constant `"market"` would read as a recorded fact about each row rather
   than as a property of the whole system, so the field is omitted
   entirely and the omission is documented in `docs/API.md` and in
   `schemas_orders.py`'s module docstring. A future frontend that wants to
   display an order type must get a schema change first.
2. **`broker_kind` is a real join, not an inference.** `orders` has no
   paper/live column — `Broker.kind` is the one discriminator (spec §51,
   D058) — so the paper/live label on every row is the actual `brokers.kind`
   for that row's broker, taken from the `Broker` row
   `require_broker_access` has already loaded. Nothing infers "this was
   probably a paper order" from which adapter ran or from the presence of a
   `broker_accounts` row. It is repeated per row rather than once per
   response so a client merging pages from several brokers still labels
   each line honestly.

`OrderResponse` is rendered by one function shared by the listing and the
detail route, so an order read one way cannot disagree with the same order
read the other — the same discipline as `_to_history_entry` in
`routes/portfolio.py`, and asserted in a test rather than left to
convention. Rejected orders are deliberately **included** in the orders
listing: an order the Risk Engine or Portfolio Manager refused is exactly
the part of the trail that shows the controls working, and filtering them
out would turn the endpoint into a record of successes. The fills blotter
is a different grain rather than the same list filtered — a rejection
contributes zero rows there and exactly one row to the orders listing.

404 vs 403 is unchanged from `require_broker_access`: **404** for a broker
id that does not exist, **403** for one that exists but which the caller
holds no `BrokerGrant` for. The detail route additionally filters on
`broker_id`, so a real order id belonging to another broker returns 404 —
indistinguishable from an id that does not exist, so a caller with a grant
on any one broker cannot use the route to probe order ids on every other.

Fills for a page of orders are loaded with one explicit second `SELECT`
grouped by `order_id`, **not** by adding an `Order.fills` relationship to
`db/models.py`. A lazy-loading relationship on the ORM class the write path
constructs is exactly the kind of change that surfaces as a `MissingGreenlet`
somewhere inside `submit_trade_and_record()` rather than here, and this
phase's whole premise is that it cannot touch the write path. The cost is
one extra round trip per page.

Deliberately NOT built: any write, cancel or amend route (`orders` is
append-only by design — a re-attempt is a new row, D006); symbol/date/status
filtering (nothing needs it yet, and the endpoints would have to grow query
parameters this phase has no consumer for); a cross-broker "all my orders"
listing (every existing read in this codebase is broker-scoped, and
un-scoping one is an authorization design question, not a listing feature);
an index tuned for `ORDER BY submitted_at DESC` on `broker_id` alone (the
existing `ix_orders_broker_symbol_submitted` is a `(broker_id, symbol,
submitted_at)` composite, so this sorts rather than scans it — a migration
for that is a real but separate change, and current row counts do not
justify one); and any frontend, which is a follow-up phase against this
contract. `LIVE_TRADING_ENABLED`, the Risk Engine, the Portfolio Manager,
the emergency stop, `apps/api/app/execution/`, `apps/api/app/oms/` and
`apps/web/` were not touched at all — this branch's diff contains no
change to any of them.

Verified: **21 new integration tests** in `tests/api/test_order_history.py`
against a real Postgres (empty listing; a filled order's every persisted
field plus its real fill; newest-first ordering; a genuinely
risk-rejected order carrying its block reason and zero fills; both D029
quantities; the pagination boundary reassembling two pages into one; 422s
on `limit=501`/`limit=0`/`offset=-1` across both listings with `limit=500`
still accepted; detail byte-identical to the listing row; per-order fill
grouping across a three-order page; a live-kind broker's order labelled
`live`; unknown order id 404; a real order id from another broker 404 while
still readable on its own broker; one broker's listing never containing
another's; the fills blotter's grain, ordering and pagination; 403 without
the permission, 403 without a grant, 403 for a second user holding a grant
elsewhere, 404 for an unknown broker, 401 without a token; and 405 on
POST/DELETE with the trail unchanged afterwards). Every order these tests
read back was created by a real POST through the real trade path, never by
a fixture INSERT — the one exception is the live-kind labelling test, which
inserts the row directly because submitting a live trade would require an
actually-configured live execution path (D058) and nothing in this phase
may enable one.

Also **live-verified over real HTTP**, not only through the test client:
a real `uvicorn` on 127.0.0.1:8148 against a real Postgres on remapped
port 55448 and Redis on 63448, seeded with three real users, two real
brokers and three real grants. 33 checks, all passing — including four real
filled orders and one genuinely risk-rejected order
(`exceeds_max_position_size`, returned by the real engine, not simulated);
the two-page reassembly; `limit=501` → 422 and `limit=500` → 200; detail
byte-identical to its listing row; a real order id from broker B returning
404 on broker A and 200 on B; 403 for a grant-less user on all three routes
while that same user could still read the broker they *do* hold a grant for;
405 on POST/DELETE with the four-row trail intact afterwards; and
`GET /health` reporting `live_trading_enabled: false` throughout.

Infrastructure: an isolated `timescale/timescaledb:2.15.3-pg16` + `redis:7`
pair under a throwaway `tos-p48` compose project on host ports 55448/63448,
and a fresh Python 3.13 venv outside the repo — never the user's own
5432/6379/8000/3005 stack, following the D028/D033/D036/D043/D046/D063
precedent. No `.env` file was created; every variable was passed
per-command. No live credential exists in this branch and no external
vendor was contacted.

Status: Implemented and verified as above.

---

**D066 — Phase 49: reconciliation of accepted-but-unexecuted live orders — the unconfirmed order is now RECORDED, and a background job resolves it from the broker's own answer**

Numbering note: this worktree (`worktree-agent-a4db540a896c3de5e`) branched
from `main` at `ba04187`, where the last entry was **D063**. Re-checked
immediately before finalizing, `main` had advanced to `5ae3a98`
("read-only order/fill history endpoints (Phase 48, D065)"), and D064 is
absent from `main` — i.e. claimed by a sibling worktree still in flight.
This entry therefore claims the next free number, **D066**, and **Phase
49**. Following the convention D030/D031/D032 established and D035/D039
restated for concurrent worktrees: the number is claimed at merge time, and
if a sibling merges a D066 first this entry is renumbered on the way in
rather than after the fact.

**Interaction with Phase 48/D065, which landed on `main` while this was in
progress.** That phase added read-only order/fill history endpoints. This
phase adds two `OrderStatus` values and three `orders` columns those
endpoints will encounter, so whichever merges second must check that the
history serializer renders `submitted_unconfirmed` /
`broker_closed_unfilled` (and, ideally, surfaces `broker_order_id` /
`broker_status` / `reconciled_at`) rather than assuming a two-value enum.
Nothing here depends on that work and no file it touches was modified from
this branch; this is flagged so the merge is deliberate rather than
discovered.

## The gap this closes, stated precisely

D058 (Phase 43) built the live execution path and got the hardest part
right: `LiveBrokerAdapter.submit_order()` reads the order back once and, if
the broker has not executed it, raises `LiveOrderNotFilledError` carrying
the **real** broker order id rather than inventing a fill at the caller's
estimated price. The route renders that as `502 LIVE_ORDER_UNCONFIRMED`.
That refusal to fabricate is unchanged by this phase.

What was missing turned out to be worse than "no reconciler was built",
which is how D058 and docs/TRADING_SAFETY.md both described it. Reading the
actual code first (rather than trusting that description) showed that
**nothing was persisted at all**:

- `LiveOrderNotFilledError` is raised from inside `submit_trade()`, i.e.
  *before* `submit_trade_and_record()` reaches the line that constructs the
  `OrderRow`.
- `trades.py` catches it and raises `HTTPException`, so its
  `await session.commit()` is never reached and the request's session is
  rolled back on close.

So the single most consequential thing this system can do — hand a real
venue a real order — left **no row in its own append-only audit trail**.
The broker order id existed in one log line and one HTTP response body. The
premise "the order is left in a pending status forever" was not accurate:
there was no order row to be in any status, and `OrderStatus` had only
`FILLED` and `REJECTED`, neither of which is true of an order whose outcome
is unknown.

Closing the reconciliation gap therefore required building the producing
half first. Both halves are this phase.

## Part 1 — the producing half: `SUBMITTED_UNCONFIRMED`

Two new `OrderStatus` values, three new nullable `orders` columns, one
migration (`0014`).

**`SUBMITTED_UNCONFIRMED` — the only non-terminal status.** A real order
exists at a real broker, every gate approved it, and the outcome is
genuinely unknown. `broker_order_id` is never null on such a row and
`fills` never has a row for one.

**`BROKER_CLOSED_UNFILLED` — terminal.** The broker reports the order
finished having executed nothing. Deliberately *not* folded into the
existing `REJECTED`, which means **this system** blocked the trade and it
never reached a venue. Conflating "our risk engine stopped it" with "the
venue cancelled it" in a money-moving audit trail would destroy the one
distinction a reader most needs.

**Three cancel-ish statuses, one enum value, and `orders.broker_status`.**
The obvious alternative was `BROKER_CANCELLED` / `BROKER_REJECTED` /
`BROKER_EXPIRED`. Rejected: this system acts identically on all three (stop
watching, write no fill), so three values would encode a distinction the
code does not make, and would still be a *paraphrase* of the venue's word.
Instead `orders.broker_status` records the venue's own status string
verbatim — `Canceled`, `Expired`, `Rejected`, `Filled` — which is strictly
more information and follows the same "carry the vendor's own message
rather than a paraphrase" habit as `ScheduledSnapshotOutcome.detail`
(D030).

**`_record_unconfirmed_order()` commits, and nothing else in
`apps/api/app/oms/persistence.py` does.** That module's docstring
previously said it deliberately never commits (D014: committing early would
release `load_paper_broker()`'s `SELECT ... FOR UPDATE` before the paper
book is saved, reopening a double-spend race). The exception is safe for a
*structural* reason, not a lucky one: `OrderNotConfirmedError` can only come
from an adapter talking to a real venue, and the **live** branch of
`_execute_trade()` never calls `load_paper_broker()` — a live book lives at
the venue, not in `broker_accounts` (spec §62). No row lock is outstanding
and no paper write is pending; the session holds exactly the row just
added. On the paper path the code is unreachable, because
`PaperBrokerAdapter` fills synchronously.

**The quantity is the one actually sent, never the proposal's.** The
exception unwinds past every local in `submit_trade()`, including the
post-Portfolio-Manager `effective` proposal. Recording `proposal.quantity`
would put a number in an append-only trail that was never sent to any
venue — precisely the class of error this codebase exists to avoid. So
`submit_trade()` attaches an `UnconfirmedSubmissionContext` to the
exception on the way out and re-raises unchanged.

That context lives on a new **port-level** `OrderNotConfirmedError`
(`apps/api/app/execution/broker.py`), which `LiveOrderNotFilledError` now
subclasses. The pure, DB-free OMS must not import a concrete live adapter,
and every existing `except LiveOrderNotFilledError` / `except
LiveBrokerError` arm — including the two in `trades.py` — catches exactly
what it caught before. **`trades.py` was not modified in this phase.**

If the context is somehow absent, `_record_unconfirmed_order()` writes **no
row at all** and logs loudly, rather than substituting the proposal's
quantity.

## Part 2 — `LiveOrderReconciler`

`apps/api/app/execution/reconciliation.py`. An opt-in interval loop, a
sibling of `PortfolioSnapshotScheduler` (D030) rather than a modification
of it, following that phase's pattern deliberately rather than inventing a
second one: an in-process `asyncio` task owned by the FastAPI lifespan, no
new dependency, started and stopped in `main.py` alongside the existing
scheduler.

**Three independent things must be true before it touches a broker.**
`LIVE_ORDER_RECONCILER_ENABLED` defaults to **false**. Even enabled, every
cycle short-circuits with `SKIPPED_DISABLED` unless a real
`LiveBrokerAdapter` exists, which still needs `TRADING_MODE=live` **and**
`LIVE_TRADING_ENABLED=true` **and** the `LONGPORT_LIVE_*` trio (D058's gate,
unchanged and never re-implemented — the reconciler is *handed* whatever
the lifespan already built, and never constructs an adapter or reads a
credential itself). The short-circuit runs **before** the lock and before
any SQL, so an enabled-but-unconfigured deployment costs one log line per
interval: no database round-trip, no connection, no broker call. Same
ordering rationale as D042 putting the market-hours gate ahead of the
snapshot cycle's broker query.

**Nothing is ever inferred about an order from the passage of time.** An
order the broker still reports as working is left *completely* untouched —
not aged out, not timed out, not assumed cancelled, and `reconciled_at` is
not even stamped, so "we have not looked yet" stays distinguishable from
"we looked and it was still open". `_TERMINAL_STATUSES` is a closed set read
off the installed SDK by direct introspection of
`longport.openapi.OrderStatus` (the D008/D015/D021 discipline, which caught
that the SDK spells it `Canceled`); **everything else — `New`, `Unknown`,
`PartialFilled`, and any status a future SDK adds — is treated as still
open.** That asymmetry is the fail-closed direction: another cycle costs one
API call, whereas declaring an order finished on a status this code does not
understand would permanently record an outcome the broker never reported.

`PartialFilled` is non-terminal here even though `submit_order()` accepts it
as a real fill. The two answer different questions: at submit time a caller
is synchronously waiting and a partial execution is the best true answer
available; at reconcile time nobody is waiting, so freezing a half-done
quantity into an append-only trail would record a number that was true for
a moment and wrong forever.

**A broker call that fails skips that order and changes nothing** — a
failure to *observe* is not evidence about the thing observed. Same
discipline D030 applies to a broker it cannot price.

**The one permitted UPDATE, guarded in SQL.** `orders` is documented
append-only. The reconciler applies exactly one transition,
`SUBMITTED_UNCONFIRMED` → `FILLED` | `BROKER_CLOSED_UNFILLED`, and its
UPDATE carries `WHERE status = 'submitted_unconfirmed'`; the outcome is
decided by `rowcount`. A terminal row therefore matches nothing and can
never be rewritten — by this job, by a second worker whose lock lapsed, or
by a re-run — and the `fills` insert happens only when that UPDATE actually
matched, with `fills.order_id`'s UNIQUE constraint as an independent
backstop. The guarantee lives in the database, not in this code being
careful.

This is not a softening of the append-only rule so much as an admission of
what it protects. `SUBMITTED_UNCONFIRMED` does not record a *decision* —
the decision was complete and unchanging when the row was written. It
records that the outcome was, at that instant, unknown. Resolving an
unknown outcome into the one the broker later reported does not rewrite
history; refusing to would leave the audit trail permanently, knowably
wrong instead.

**The fill is built entirely from the broker's own figures** —
`executed_quantity` / `executed_price` from `order_detail`, never the
order's `estimated_price`. A terminal status carrying no price is *not* a
fill: `LiveOrderStatus.executed` requires both, because a fill is a claim
about a price and completing it would mean inventing the one number it is
about. `filled_at` uses the venue's own timestamp when it gave one, falling
back to the reconciliation instant — the earliest moment this system can
prove the fill existed — never to `submitted_at`, which would claim a time
nobody reported.

**The work query enforces `kind = live` in SQL**, joined to `brokers`. A
paper order can never legitimately reach this status, and if one somehow
did, sending its id to a real venue would be asking a live account about a
simulated order. Structural, not conventional (spec §51).

## Multi-worker safety: D047's mechanism, this job's own key

Reused rather than re-litigated, and reused *literally* —
`SnapshotCycleLock` gained a `job_name` field (defaulting to
`portfolio_snapshot`, so D047's log event names are reproduced
byte-for-byte and no existing construction site or log query changes) and
the reconciler takes the same `classid` namespace with its own
`RECONCILER_LOCK_OBJID` (`b"recn"`). That is exactly the "future second
background job" the module's TWO int4 KEYS section anticipated, and the
concrete payoff of having chosen the two-key form over a hashed bigint.

**Sharing the objid would have been a subtle disaster**, which is why there
is a dedicated test for it: a reconciliation cycle and a snapshot cycle are
unrelated work, and one key would make every reconciler cycle silently skip
whenever a snapshot was in flight — a failure indistinguishable from "there
was nothing to do".

The lock matters more here than for snapshots. Two workers duplicating a
snapshot writes two rows in a chart; two workers resolving the same live
order means two calls to a real venue's API and two racing UPDATEs. The
guarded UPDATE above means correctness does not depend on the lock alone.

## Observability

`/health` gains `live_order_reconciler: "DISABLED" | "enabled:<n>s"`, and
the startup log gains that plus `live_order_reconciler_cycle_lock`, in the
same vocabulary as `market_data_vendor` / `llm_provider` /
`portfolio_snapshot_scheduler`. Adding a key to `/health` is additive and
every existing consumer reads named keys, but more importantly it does not
weaken the property D054's docstring actually protects: it is a plain read
of in-process `Settings`, no I/O, no dependency. It earns its place because
the reconciler is the only background job that reaches a real trading
venue, and whether it is running was otherwise visible only in a startup log
line that has long since scrolled away.

## What was deliberately NOT built

- **No reconciliation endpoint.** No `GET`/`POST` route lists or forces
  reconciliation of unconfirmed orders. D062 already flagged "the platform
  records an append-only audit trail no user can read back" as the largest
  product gap; that is one coherent piece of work (an order-history API),
  not something to half-build here.
- **No frontend.** Backend-only phase.
- **Partial-fill progression is still not tracked.** `fills.order_id` is
  UNIQUE, so the schema expresses one fill per order. A `PartialFilled`
  order stays under observation until the venue calls it done, and the
  final figures are what get recorded. Making partials first-class is a
  schema change and its own phase.
- **`LIVE_TRADING_ENABLED` was not touched**, in any default, fixture, or
  test — not transiently. Every test drives the production
  `LiveBrokerAdapter` against a fake SDK client via
  `dependency_overrides`, exactly as D058's tests do.

Status: Implemented and verified. On a default checkout the reconciler is
not constructed, `/health` reports `live_order_reconciler: "DISABLED"`, and
the live path is exactly as inert as it was before this phase.

---

**D067 — Phase 50: watchlists, the research half of the research-to-trade dashboard, and one shared quote-resolution path**

Numbering note: this worktree branched from `main` at D063 and originally
claimed D064, the next free number at branch time. Two concurrently-run
sibling worktrees (Phase 48's order/fill history and Phase 49's live-order
reconciliation) branched around the same point and merged to `main` first
as **D065** and **D066** — so this entry is renumbered to **D067** at
merge time to avoid a collision, following the same convention used since
D028/D030/D031/D032 for parallel worktrees landing on the same D-number.
D064 is left unused rather than reassigned: renumbering a decision after
the fact is exactly what that convention exists to prevent.

Date: 2026-09-03

Decision: Build real watchlists — a `watchlists` / `watchlist_items`
schema (migration `0015` — this worktree also branched before Phase 49's
migration `0014` merged and originally claimed `0014` itself; renumbered
at merge time for the same reason as the D-number above), five
user-scoped endpoints under `/watchlists`,
a `GET /watchlists/{id}/quotes` that prices a whole list, and a
`Watchlist.tsx` panel on the dashboard — and, as part of it, factor the
single-symbol quote decision out of `routes/marketdata.py` into
`apps/api/app/marketdata/resolution.py` so the list endpoint and the
single-quote endpoint resolve prices through one function rather than two
copies of one idea.

Until this phase the dashboard could answer "what is AAPL trading at" one
symbol at a time and nothing else. The trade side of the research-to-trade
loop was complete (D058/D062) while the research side was a single input
box, so the thing an operator actually does — follow a handful of names
and look at them together — had no representation in the product at all.

**Why user-scoped and not broker-scoped.** Every other id-addressed
resource in this codebase hangs off a broker: `/brokers/{id}/trades`,
`/brokers/{id}/portfolio`, and even D034's discovery listing is scoped by
`BrokerGrant`. A watchlist is not like those. It is a list of symbols
someone is reading about, it authorizes nothing, and gating it on a grant
would mean a user with no broker access cannot research anything — which
is backwards, since researching is what you do *before* you have a
position. So these routes are authentication-only (`get_current_user`),
with ownership checked against `watchlists.user_id`, and they are the
first resource here scoped that way. The frontend follows the same logic:
`Watchlist` sits with `QuoteLookup` under a new "Research" heading rather
than inside the broker-scoped block, and `QuoteLookup` moved out of the
"Position & performance" grid with it, since it was never broker-scoped
either and only lived there for width.

**Why "not yours" is 403 and not 404.** The tempting privacy answer is a
blanket 404 that refuses to confirm a watchlist exists. This codebase
already answers that question, in `require_broker_access` and again in
D034's `GET /brokers/{id}`: look the row up first (404 if there is no such
id), then check access (403 if there is but it isn't yours). Answering
differently here would make watchlists the one resource whose status codes
mean something else, and the information a 403 leaks — that some UUID
someone already holds is a real watchlist — is not worth a second
convention. Ownership is enforced in exactly one helper
(`_owned_watchlist`) which all four id-addressed routes call, so there is
no route where the check could be forgotten.

**Why explicit creation, with no auto-created default.** The alternative —
create "My watchlist" lazily on first read — makes a GET write to the
database, and leaves an empty row behind for every user who ever merely
loaded the dashboard. `GET /watchlists` on a fresh account returns `[]`,
which is a true statement, and the panel renders a create form. A test
asserts that reading twice still creates nothing.

**Why two tables rather than a `symbols text[]` column.** The single
invariant this feature has is that a symbol appears at most once per list,
and on an array column that is an application-level check two concurrent
adds can both pass. `uq_watchlist_item_watchlist_symbol` makes it the
database's problem: the route pre-checks only to return the 409 cheaply,
and the constraint is what actually guarantees it. Symbols are normalized
(trimmed, upper-cased) on the way in — and on the DELETE path segment on
the way out — so the constraint is real rather than one `"aapl"` walks
around.

**The no-fabrication decision, which is the point of the whole endpoint.**
A list-of-quotes endpoint has three tempting ways to lie when a vendor
cannot price a symbol: drop the row, return zero, or return the last thing
it saw. All three are forbidden by spec §57 and `docs/TRADING_SAFETY.md`,
and none of them is reachable in this design — `WatchlistQuote` carries
either a price block copied off a real `MarketSnapshot` or a
`DATA_UNAVAILABLE:`-prefixed sentinel, never neither and never both, and
the response is always exactly as long as the watchlist. The underlying
cause is preserved after the prefix rather than replaced, so
`NOT_CONFIGURED:` (no vendor wired at all) stays distinguishable from
`NO_DATA_AVAILABLE:` (this symbol has no price).

The one genuinely debatable call: with **no** vendor configured, this
endpoint returns **200** with every row unavailable, where
`GET /market-data/{symbol}/quote` returns **503** for the identical
condition. That is deliberate and the two are both right. There, the quote
is the entire response, so there is nothing to return; here it is one
column of a list the user is still entitled to read, and a 503 would hide
their own symbols from them to report a fact the payload already states in
`market_data_configured`.

**Why `resolution.py` exists.** Writing the list endpoint meant needing
the same two distinctions the single-quote route makes, and the honest
options were a second copy or a shared function. A second copy is how the
two would eventually disagree about what "no price" means. So the decision
returns a value (`ResolvedQuote`) instead of raising an HTTP error, and
each caller maps it to its own contract — which is what let the
single-quote route keep its exact 503/404 strings while gaining a second
caller. Two tests pin that: one asserts the watchlist and the single-quote
endpoint report the same price and source for the same symbol under the
same vendor, the other that the single-quote route still 503s on
NOT_CONFIGURED and 404s on NO_DATA_AVAILABLE.

**Sequential resolution, and a per-list cap.**
`GET /watchlists/{id}/quotes` resolves symbols one at a time rather than
with `asyncio.gather`, and a watchlist holds at most 200 symbols. The
vendors behind `MarketDataRouter` are rate-limited third parties; fanning
one page refresh out into hundreds of simultaneous upstream requests is
how one user's dashboard becomes everyone's rate-limit rejection. If a
real latency problem ever appears, bounded concurrency is the fix — not
unbounded.

Alternatives rejected: (a) a `GET /watchlists/{id}` detail route — the
listing already carries each list's symbols, so it would have been a
second way to ask one question; (b) unique watchlist names per user — a
constraint on how a person organizes their own reading, when the id is
what every route addresses anyway; (c) validating symbols against the
vendor at add time — it would refuse research on anything not currently
priceable, and would silently change behaviour the moment a vendor was
configured.

Verification (all against an isolated throwaway stack on remapped ports —
Postgres `55432`, Redis `56379`, API `18050`/`18051` — never the dev
stack's 5432/6379/8000/3005):

- `alembic upgrade head`, then a full `downgrade base` → `upgrade head`
  round-trip, clean, with `0014` at the head.
- Backend: **615 → 635** tests passing (20 new in
  `tests/api/test_watchlists.py`). The 615 figure was measured on this
  branch *after* the `resolution.py` refactor and before the new test file
  existed, so it also demonstrates the refactor changed no existing
  behaviour.
- Frontend: **140 → 152** tests passing (12 new in
  `apps/web/test/Watchlist.test.tsx`), 17/17 files. No existing test was
  weakened, skipped or deleted. `npm run build` clean, with all five
  `/api/watchlists/...` route handlers registered.
- `ruff check .` and `mypy apps` (96 source files) clean;
  `bash scripts/secret_scan.sh` clean.
- Live-verified over real HTTP against a running instance: create, add
  (including a lower-case symbol proving normalization), duplicate → 409,
  list, quotes, cross-user → 403 on all four id-addressed routes, remove
  via a lower-case path segment → 204, missing watchlist → 404, symbol not
  on list → 404, delete → 204 then 404, unauthenticated → 401. Quotes were
  verified twice: once on the committed default (no vendor credentials on
  this machine — every row came back
  `DATA_UNAVAILABLE: NOT_CONFIGURED: …`, not one fabricated price), and
  once against a second instance with a *stub* vendor injected by a
  dependency override, which produced two real prices and one
  `DATA_UNAVAILABLE: NO_DATA_AVAILABLE: …` row in the same response, with
  the single-quote endpoint returning 200/404 for the same two symbols.

Scope discipline: this branch's diff contains nothing under
`apps/api/app/execution/` and does not touch
`apps/api/app/api/routes/trades.py` — both were being edited by the
concurrent Phase 48/49 worktrees. `LIVE_TRADING_ENABLED`, the Risk Engine,
the Portfolio Manager and the emergency stop are all untouched; no route
added here can place, size, or influence an order.

Status: Implemented and verified as above.

---

**D068 — Phase 51: the frontend order/fill history panel, and the end of the two-status fiction**

Numbering note: verified immediately before writing. `main` at 81a18fa
holds D067 (Phase 50) as its highest decision and Phase 50 as its highest
phase; the concurrently-run sibling worktree
`phase-52-agent-trade-analyst-reads` had committed nothing at that point
and had claimed no number. **D068** and **Phase 51** were free.

Date: 2026-09-07

Decision: Build the frontend half of Phase 48 — three broker-scoped route
handlers proxying D065's listing endpoints, and a `TradeHistory.tsx` panel
on the dashboard that renders the append-only order trail as a paginated
table. Frontend only: no backend logic was modified, and the two prose
corrections below are the sole exception.

This closes the follow-up Phase 48 named for itself. D065 exposed `orders`
and `fills` over HTTP and then said so explicitly — "the frontend
trade-history panel ... is a follow-up phase against this now-documented
contract". Until now the audit trail was readable with `curl` and a bearer
token, which is not the same as readable through the product.

**The four statuses are rendered as four things, and that is the point of
this phase.** Phase 49 (D066) added `SUBMITTED_UNCONFIRMED` and
`BROKER_CLOSED_UNFILLED` to `OrderStatus`, but both `docs/API.md` and
`schemas_orders.py`'s own docstring still asserted that `status` "is
`filled` or `rejected`" and that there is "no pending state". Those
sentences were true when written and were left behind by D066. A panel
built from that stale prose would have shipped a filled/rejected binary
that silently mislabels the two most consequential rows in the table, so
both were corrected here as part of the work — they are prose, not logic,
and they were wrong on the exact point this phase depends on.

The distinction the UI is required to preserve is `rejected` vs
`broker_closed_unfilled`. Both have `fills: []` and both executed nothing,
which makes them tempting to merge; they mean opposite things. `rejected`
means **this system** stopped the trade — the Risk Engine, the emergency
stop, the duplicate check or the Portfolio Manager — and it never reached
a venue. `broker_closed_unfilled` means it **did** reach a real broker,
which then ended it unexecuted. Collapsing them would misreport whether
the platform's own controls fired, which is the one question a trade
blotter exists to answer. Each status therefore gets its own pill tone and
its own sentence, and `submitted_unconfirmed` — the only non-terminal
status — is shown as pending rather than as a failure.

A status string the component does not recognise is rendered verbatim with
no tone and no invented meaning, rather than bucketed into whichever known
member looks closest. A future enum member should show up as itself and
look unfamiliar, not quietly wear another status's colour.

**Nothing is invented where the response has no answer.** The fill-price
column shows an em-dash for any order with `fills: []` — never `0.00`, and
never a fallback to `estimated_price`, which is the price the proposal was
*evaluated* at and would read as an execution that did not happen. Two
things the panel deliberately cannot show, because `OrderResponse` does
not carry them: an order *type* (there is no such column — D065), and the
venue's own wording for a `broker_closed_unfilled` order (that lives in
`orders.broker_status`, which D065's response shape does not expose).
Surfacing `broker_status` would be a real follow-up, and a backend change,
so it is not smuggled in here.

**Pagination is real.** Prev/Next re-request the backend with a new
`offset`; they do not slice a cached array. The backend returns no total
count, so Next is offered only while the current page came back full — a
short page is the only honest end-of-list signal available, and a page
count derived from a total that is not returned would be a fabrication.
The pager is rendered outside the table block on purpose: paging past the
last order returns a real empty page, and controls that lived with the
rows would vanish exactly then, stranding the reader one click beyond the
end of the trail with no way back. That was found by live-clicking the
real panel, not by reading the code, and it is covered by a test.

**Why a proxy and not a direct call.** Same reason as every other route
handler here: the auth cookie is httpOnly (D020) and the backend base URL
is server-side. The three handlers follow
`app/api/portfolio/[brokerId]/history/route.ts` exactly — awaited
`params`, a 401 before any network call when the cookie is absent,
`limit`/`offset` allowlisted rather than forwarded blindly, a real
`503 DATA_UNAVAILABLE:` when the API is unreachable, and the backend's own
status and body passed through unchanged. The detail route forwards no
query params at all and passes D065's deliberate 404-for-both-cases
through untouched: distinguishing "no such order" from "another broker's
order" is precisely what that 404 exists to prevent.

`GET /brokers/{id}/fills` is proxied too, though the panel reads the
orders listing. It is a different grain, not the orders list with
rejections filtered out, and leaving the proxy out would have made the
next phase re-derive it.

Verified: `apps/web` component suite **152 → 168 passing** (17 → 18 files;
16 new, none deleted, skipped or weakened), `npm run build` clean, and the
three new handlers appear in the build's route table. ESLint reports 5
problems, all in files this phase did not touch (`BrokerDiscovery.tsx`,
`SessionStatus.tsx`, `Watchlist.tsx`, `lib/session.ts`); every file added
or modified here lints clean.

Live-verified in a real browser against an isolated throwaway stack —
Postgres 55453, Redis 63453, API 8153, `next dev` 3153, never the dev
stack's 5432/6379/8000/3005 — with `LIVE_TRADING_ENABLED=false` and
`/health` reporting `live_trading_enabled: false` throughout. The two
paper statuses were produced by the **real** trade path: a real filled
order (AAPL.US, 10 @ 100) and a real risk rejection
(`exceeds_max_position_size`, "Proposed notional 90000 exceeds the max
single-position notional 10000") from the real deterministic engine. The
two live-path statuses cannot be produced without enabling live trading,
so their rows were inserted directly against a `kind=live` broker row —
the same device Phase 48 used for its live-kind labelling test, and stated
here rather than glossed. All four then rendered under their own names
with `broker_kind` showing the real joined `live`/`paper`, unfilled rows
showing no fill price, real 403 ("No access grant for broker …") and 404
("No broker with id …") passed through with no rows drawn, the empty page
rendered as a real empty result, and Prev/Next walking offsets 0 → 1 → 2 →
1 over real HTTP with Prev still reachable past the end. The stack was
torn down afterwards; no `.env` was created, no live credential exists in
this branch, and no external vendor was contacted.

Scope discipline: `apps/api/app/api/routes/trades.py` and
`apps/api/app/api/routes/orders.py` were NOT modified — the first was
owned by a concurrent sibling worktree, the second is the backend contract
this phase consumes. Nothing under `apps/api/app/execution/`,
`apps/api/app/risk/`, `apps/api/app/oms/` or
`apps/api/app/portfolio_manager/` was touched, no migration was added, and
`LIVE_TRADING_ENABLED` was never set. The only backend file in this diff
is `schemas_orders.py`, and the only change to it is the corrected
docstring sentence described above.

**D069 — Phase 52: the agent-trade response reports what each analyst actually said, nullable per analyst, closing D062's last recorded gap**

Numbering note: this worktree branched from `main` at `81a18fa`, where
`docs/DECISIONS.md` ended at **D067**, and originally claimed **D068** —
the next free number at branch time, still free when re-checked just
before this entry was written. The concurrently-run sibling worktree
(`worktree-agent-aa6de4e558e172c5b`, the frontend order/fill history panel
that Phase 48/D065 deferred) branched from the same `81a18fa`, claimed
D068 for its own entry, and **merged to `main` first** as
`87d6882 feat: frontend order/fill history panel (Phase 51, D068)`. This
entry is therefore renumbered to **D069**, exactly as D063, D065 and D067
were renumbered for the same reason. The phase number is **52**, and that
is now settled rather than predicted: the sibling took 51.

The two branches overlap only in `docs/API.md`, `docs/DECISIONS.md` and
`docs/IMPLEMENTATION_STATUS.md` — no source file is touched by both. In
particular the Phase 51 panel is `apps/web/components/TradeHistory.tsx`
plus `apps/web/app/dashboard/page.tsx`, neither of which this phase edits,
and this phase's `apps/web/components/AgentTradeForm.tsx` is not one that
phase edits.

Reason: D062 (Phase 47) built the agent-trade frontend and recorded what
it could not honestly show — "`AgentTradeResponse` carries no analyst field
at all… nothing about them is returned; the response can't even say
whether any ran (each is failure-isolated and may return `None`)". It also
prescribed the fix: "a future backend phase widening the shape should make
each analyst field nullable **per analyst**, not imply all three always
ran." This phase is that shape change, taken exactly as prescribed. The
last of the two gaps D062 recorded is now closed (the first closed at
D065).

**What was added.** Four schemas in `apps/api/app/api/schemas.py` and
three fields on `AgentTradeResponse`:

- `AnalystReadOut` — `stance`/`summary`/`confidence`, mirroring
  `TechnicalRead`/`FundamentalRead`/`NewsRead` field for field. Those three
  domain models were already deliberately identical (D059), so the HTTP
  projection is **one** shape rather than three near-copies.
- `TechnicalAnalystReadOut` adds `indicator_context`,
  `FundamentalAnalystReadOut` adds `data_source` + `fundamentals_as_of`,
  `NewsAnalystReadOut` adds `headline_count`.

**Nothing here is derived for the response.** Every added field is a value
the route already had in hand and already gave the analyst:
`indicator_context` is the verbatim string built from
`indicators.sma`/`indicators.rsi` (D021) — reported, not recomputed;
`data_source`/`fundamentals_as_of` come straight off the vendor's own
`CompanyFundamentals`; `headline_count` is `len(headlines)` on the exact
list shown to the analyst, which is the same number the prompt tells the
model to cite. `AnalystReadOut` deliberately carries **no** side, quantity,
price or stop field, so the read is as unmistakable-for-a-proposal on the
wire as it already is in memory (spec §62).

**Why nullable per analyst, and why null carries no reason.** The three
analysts run concurrently and each is independently failure-isolated
(D059), so any subset of them may have produced a read. A single
"analysts" object, or three fields that move together, would misrepresent
that. So each field is independently `X | None`, and a null means exactly
one thing: *this* analyst produced no read on this request.

Null deliberately does **not** distinguish "not configured" from "ran and
failed". Six different real conditions collapse to it — no analyst, no
vendor, a symbol the vendor doesn't cover, a vendor failure, an LLM call
failure, an unparseable response — and every one of them is already
handled identically inside the trade path (omit the context, proceed).
The specific reason is where it has always been: the structured log
(`technical_analyst_unavailable`, `fundamentals_provider_unavailable`,
`news_analyst_unavailable`, …). Putting a reason string in the response
would invite a consumer to render it as a finding, and the failure mode
one step past that is a synthesised "no signal" read — a neutral stance
with a zero confidence — which is precisely the fabrication spec §57
forbids. **A missing read is `null`, never a manufactured one.**

**All three keys are always present**, never omitted. "The field is absent"
and "the analyst produced nothing" are different claims, and only the
second is ever true of a Phase 52 response. The frontend uses that
distinction: an absent key means an older-shaped body it cannot speak for,
and it renders nothing at all — the same rule `PortfolioVerdict` already
applies to a response with no `portfolio_*` fields.

**Strictly additive, and provably so.** The three `_*_context()` helpers in
`routes/trades.py` were renamed to `_*_read()` and now return the
structured read instead of pre-rendered prompt text; `_render_read()`
became `_prompt_context()`, which renders that same text from the read.
The reads are now used twice — appended to the prompt, then returned —
rather than rendered once and discarded. Nothing else moved: the analysts
run at the same point, on the same conditions, with the same failure
isolation, and the trade decision above them is the same
`TradeSubmissionResponse` `_execute_trade()` has always produced. The
prompt text is byte-identical, checked directly rather than assumed: 24
stance × `Decimal` combinations (including trailing-zero cases like
`0.70`, where a re-validation could plausibly have normalised the string)
render identically through the old and new code paths.

**The flaky duplicate-detection test is fixed here, not merely documented.**
`tests/api/test_agent_trades.py` overrode `get_trader_agent` but never the
three analysts, so every test in the module could make a real call to
whatever `LLM_PROVIDER_BASE_URL` was set in `.env`. A `_no_real_analysts`
autouse fixture now pins all three analysts *and* their three market-data
providers to "not configured" for the whole module, and an `analysts()`
context manager opts specific ones back in with fakes. The scope is the
module rather than the one named test on purpose: the other five tests had
the identical exposure and only differed in not being timing-sensitive
enough to have failed yet. The Known Issues entry is deleted rather than
rewritten — a fixed issue is gone, not renamed.

Alternatives rejected:

- **A single `analysts` object with three sub-keys.** Reads as a unit that
  either ran or didn't. The whole point of D062's note is that they don't.
- **`analyst_status: "not_configured" | "failed" | "ok"` per analyst.**
  Rejected above: the server does not currently distinguish those two
  failure classes at the point of return, and inventing a distinction the
  code doesn't make is a fabrication of a different kind.
- **Structured `sma`/`rsi` numeric fields instead of `indicator_context`.**
  The string is what the analyst was actually handed; splitting it would
  mean the response reports something subtly different from what the read
  was based on. If a consumer ever needs the numbers separately, the honest
  change is to make the *route* compute them structurally and hand the
  analyst the same structure — not to re-parse them for display.
- **Returning the raw `TechnicalRead`/`FundamentalRead`/`NewsRead` domain
  models directly.** `apps/api/app/api/schemas.py` exists precisely so the
  HTTP contract can move independently of the domain layer (its own module
  docstring says so). The projection reuses `Stance` — the real shared
  enum — but not the models.
- **Omitting the key when an analyst produced nothing.** Cheaper on the
  wire and strictly worse: it makes "absent" ambiguous between "no read"
  and "old server", which is the one distinction the frontend needs.

Verification (2026-09-07, branch `phase-52-agent-trade-analyst-reads`, cut
from `main` at `81a18fa`), on an isolated Postgres/Redis on remapped ports
55432/56379 — never the 5432/6379/8000/3005 stack, following the
D028/D033/D036/D043/D046/D063/D065/D067 convention:

- Backend: **705 passing, up from a 698-test baseline measured on this
  branch before any edit.** That baseline was **697 passed and 1 failed** —
  and the one failure was the documented flake itself, which reproduced on
  the very first run because this machine's `.env` really does carry
  `LLM_PROVIDER_BASE_URL=http://127.0.0.1:20128`. The post-change run is
  **705 passed, 0 failed**: +7 new tests in
  `tests/api/test_agent_trades.py` (6 → 13) and the pre-existing failure
  gone. No test was deleted, skipped or weakened, and no existing
  assertion about an existing response field was changed.
- The previously-flaky
  `test_an_identical_agent_trade_submitted_twice_is_blocked_as_a_duplicate`
  was then run **standalone six times, passing all six**, in 2.1–10.4s
  each — against the same `.env` that produced the ~36s hangs the Known
  Issues entry recorded. It is hermetic now, not lucky.
- Prompt-identity check: 24 stance × `Decimal` combinations rendered
  through both the pre-Phase-52 `_render_read()` and the new
  `_prompt_context()`; **zero mismatches**, including `0.70` and `.5`,
  where a pydantic re-validation could plausibly have normalised the
  string. This is what makes "no trader-agent prompt changed" a measured
  claim rather than an argument.
- Frontend: **152 → 154** tests passing, 17/17 files. The two obsolete
  Phase 47 tests (which asserted the absence of any analyst field) were
  replaced by four: all three reads present, a null read rendering an
  honest no-read note while a sibling read still renders in full, an
  older-shaped response rendering no analyst section at all, and nothing
  rendered before a trade is proposed. `npm run build` clean.
- `ruff check .` clean; `mypy apps` clean (99 source files);
  `bash scripts/secret_scan.sh` clean. (Note for Windows checkouts: the
  scan script is checked out CRLF here and needs LF to run under bash —
  a pre-existing checkout artifact, not a change from this phase; CI runs
  it on Linux unaffected.)
- **Live-verified over real HTTP**, not only through the test client: a
  real `uvicorn` on 127.0.0.1:8152 against a second isolated Postgres on
  55433 / Redis on 56380, migrated and seeded with `scripts/seed_e2e.py`'s
  real users, brokers and grants. Two runs against the same real trade
  path, differing only in dependency overrides for the optional agents
  (the technique D067's live verification used):
  - **All three analysts configured** — 200, `status: filled`, and all
    three fields populated with the analysts' own stances, summaries and
    confidences, plus `indicator_context` carrying genuinely-computed
    `SMA(20)=105.5, RSI(14)=66.66666666666666666666666667` from
    `indicators.py`, `data_source`/`fundamentals_as_of` off the vendor
    record, and `headline_count: 4` matching the exact list shown. No
    analyst read carried a `side`/`quantity`/`price`/`stop_price`/`order_id`
    field.
  - **No analyst configured** — 200, `status: filled`, `fill_price: "100"`,
    and all three fields **present and `null`**. The trade succeeded with
    no analyst context at all, proving an absent analyst is not a
    degraded trade path.
  - `GET /health` reported `live_trading_enabled: false` on both runs. No
    external vendor or LLM endpoint was contacted (every provider was a
    stub), no `.env` was created, and both throwaway stacks were removed
    afterwards.

Re-verified **after merging `main` at `87d6882`** (Phase 51/D068) into this
branch, since the numbers above were measured against the pre-Phase-51
base `81a18fa`:

- Backend **705 passed, 0 failed** — unchanged, as expected: Phase 51 added
  no backend test (its only backend edit is a docstring in
  `schemas_orders.py`).
- Frontend **170 passed, 18/18 files** — this phase's 154 plus Phase 51's
  16 in `apps/web/test/TradeHistory.test.tsx`. `npm run build` clean.
- `ruff check .`, `mypy apps` (99 source files) and
  `bash scripts/secret_scan.sh` all still clean on the merged tree.

The merge conflicted only in `docs/DECISIONS.md` and
`docs/IMPLEMENTATION_STATUS.md`, and only because both phases appended a
new entry at the same anchor; `docs/API.md` auto-merged. Every conflict was
resolved additively — Phase 51's text kept alongside Phase 52's — with one
substantive choice: in the D062 gap list, gap 1 takes Phase 51's richer
"FULLY CLOSED" wording (including the narrower `OrderResponse` gap it
leaves behind), and gap 2 takes this phase's "CLOSED". The Known Issues
flake entry stays deleted; `main` still carried it because Phase 51 did
not touch it.

Scope discipline: the diff is six files — `apps/api/app/api/schemas.py`,
`apps/api/app/api/routes/trades.py`, `tests/api/test_agent_trades.py`,
`apps/web/components/AgentTradeForm.tsx`,
`apps/web/test/AgentTradeForm.test.tsx`, and docs. Nothing under
`apps/api/app/execution/`, `apps/api/app/oms/`, `apps/api/app/risk/`,
`apps/api/app/portfolio_manager/` or `apps/api/app/safety/` is touched;
there is no migration; `LIVE_TRADING_ENABLED` is untouched and the route
remains paper-only. `apps/web/app/dashboard/page.tsx` was deliberately not
touched — the concurrent sibling worktree is editing it.

Status: Implemented and verified as above.

**D070 — Phase 53: persisted historical OHLCV bar store + manual, on-demand ingestion — the prerequisite the entire Strategy Lab initiative (spec-external, user-supplied master prompt, 2026-09-08) sits on**

Reason: a new 70-section master prompt asked for a full Strategy Lab —
pluggable strategies, a real backtesting engine, walk-forward validation,
Monte Carlo, robustness testing, strategy ranking, universe scanning, a
signal engine, and more, phased as this project's Phase 53 onward. Three
parallel codebase explorations (backend architecture, frontend
architecture, existing strategy/backtest/signal infra) converged on one
finding: **no persistent historical OHLCV bar storage exists anywhere in
this codebase**, even though the Postgres image has been
`timescale/timescaledb` since Phase 1. `HistoryProvider` (D021) only ever
exposes "the most recent N daily closes as of now" — closes only, no OHLV,
no arbitrary date range, fetched live from the vendor on every call, never
cached or persisted. `backtesting/engine.py` (D025) is hard-limited to
`end_date == today` for exactly this reason. Almost nothing else in the
master prompt (real backtesting over arbitrary windows, walk-forward,
Monte Carlo, universe scanning at scale, correlation-aware position
sizing) can be built honestly under this project's "never fabricate data"
rule (`docs/TRADING_SAFETY.md`) without this gap closed first. This phase
closes it, and only it — no strategy/backtest code is touched.

What was built:

**(1) `market_data_bars`** (migration `0016`) — one real OHLCV bar per
`(symbol, bar_interval, ts)`. Primary key is that natural composite triple,
not this schema's usual `_uuid_pk()` — the one deliberate departure from
that helper in the whole schema. A Timescale hypertable's constraints must
include the partitioning column (`ts`), and a surrogate UUID PK would add
nothing a natural key doesn't already give: idempotent re-ingestion via
`ON CONFLICT (symbol, bar_interval, ts) DO UPDATE`
(`apps/api/app/marketdata/store.py`), and no possibility of two rows ever
describing the same bar. This is the **first hypertable in this codebase**
(`CREATE EXTENSION IF NOT EXISTS timescaledb;` runs in this migration,
verified for real — `SELECT * FROM timescaledb_information.hypertables;`
shows `market_data_bars` after `alembic upgrade head`). `bar_interval` is a
plain `VARCHAR(8)`, not a Postgres ENUM, so an intraday interval later is
an application change, not a migration that mutates a type every existing
row depends on — only `'1d'` is written this phase.

**(2) `market_data_backfill_jobs`** (same migration) — an audit row per
manual backfill attempt. Written once, updated exactly once in place
(PENDING/RUNNING → SUCCEEDED/FAILED) inside the same request that created
it — there is no background worker in this codebase (outside the snapshot
scheduler's own documented single-worker limitation) that could race that
update. `PARTIAL` exists in the enum as reserved future-work for a
multi-symbol/batch job; this phase's single-symbol synchronous job never
sets it.

**(3) `HistoricalBarProvider`**
(`apps/api/app/marketdata/bar_provider.py`) — a new Protocol, deliberately
separate from `HistoryProvider`, not a replacement for it: the live
analyst layer still reads through `HistoryProvider` for its one
most-recent-N-closes use case, unaffected. `MarketDataStore`
(`apps/api/app/marketdata/store.py`) implements the read side against
`market_data_bars`, and — unlike every vendor `Provider` class in this
package, which is built once at startup around a long-lived client — is
constructed fresh per call around whichever `AsyncSession` the caller
already holds, since a Postgres session is request-scoped, not
process-scoped (same reasoning `execution/persistence.py`'s plain
session-taking functions already follow; `MarketDataStore` is a thin class
instead only because `HistoricalBarProvider`'s Protocol shape calls for
one).

**(4) `LongbridgeBarBackfillProvider`**
(`apps/api/app/marketdata/providers/longbridge.py`) — wraps the vendor
SDK's `history_candlesticks_by_date(symbol, period, adjust_type, start,
end)`, verified against the installed `longport` package (v4.3.7) by
direct introspection of `openapi.pyi`: it returns the same `Candlestick`
objects `candlesticks()` does, but over an arbitrary date range instead of
"most recent N" — real `.open`/`.high`/`.low`/`.close`/`.volume`, not just
`.close`. Added to the existing per-vendor-file convention (one file per
vendor, one class per capability) rather than the separate
`ingestion/providers/` subpackage an earlier sketch of this plan
considered — kept for consistency with how `LongbridgeHistoryProvider`/
`LongbridgeFundamentalsProvider`/`LongbridgeNewsProvider` already live
alongside each other in this one file. Same all-or-nothing
`LONGPORT_APP_KEY`/`LONGPORT_APP_SECRET`/`LONGPORT_ACCESS_TOKEN` credential
gate as every other Longbridge builder; `None` when unconfigured, never a
degraded provider.

**(5) `apps/api/app/marketdata/ingestion/backfill.py`** — orchestrates one
job: create the row → call the vendor-boundary `BarBackfillProvider`
Protocol → upsert via `MarketDataStore` → record the outcome, all inside
one transaction. A vendor `VendorError`/`DataUnavailableError` is recorded
as `FAILED` with the real error message — never silently treated as "zero
bars, success." No automatic/scheduled backfill exists — this is manual,
on-demand, exactly once per call, the same posture D025's backtest engine
takes toward its own execution.

**(6) `POST /admin/market-data/backfill`** — gated by the existing
`Permission.ADMIN` rather than a new permission (this is operational
data-management, not a trading capability, matching every other action in
`routes/admin.py`). Runs synchronously to completion within the request —
no job queue exists to hand it off to. Returns **201 whether the job
succeeded or failed** — like a backtest result, a completed attempt is a
real resource, not an HTTP-level error condition, so a
`DATA_UNAVAILABLE`/vendor-failure outcome still returns the audit row with
`status: "failed"` rather than discarding it behind a 5xx. `bar_interval`
is `Literal["1d"]` at the schema layer, so anything else is a 422, not a
silently-ignored request.

Alternatives rejected:

- **A background job queue for backfill.** No such infrastructure exists
  in this codebase (the snapshot scheduler is the only precedent, and it's
  explicitly documented as having a single-worker limitation). Building
  one to backfill a handful of symbols once is exactly the kind of
  half-built indirection D025 warned against for a pluggable strategy
  interface — deferred to whichever later phase's scale actually needs it
  (universe scanning, Phase 59+).
- **A surrogate UUID primary key on `market_data_bars`.** Rejected in (1)
  above — the natural composite key is strictly better here and a
  hypertable's constraints need to include `ts` anyway.
- **Reusing/extending `HistoryProvider` instead of a new
  `HistoricalBarProvider`.** `HistoryProvider`'s contract ("most recent N
  as of now," closes-only) is fundamentally different from "bars in
  [start, end]" — bolting a date-range parameter onto it would leave
  every existing caller (the analyst layer) needing to reason about a
  parameter it never uses, for no benefit; a second, narrower Protocol is
  the smaller, more honest change.

Verification (2026-09-08, `main` @ pre-Phase-53 tip, isolated Postgres
55432/Redis 56379 — never the shared dev stack's 5432/6379, which turned
out to hold an unrelated stale broker row from earlier ad hoc debugging
that made the *pre-existing* `test_snapshot_scheduler_market_hours.py`
weekday-control test flake on an unrelated symbol list assertion; the same
suite is **726 passed, 0 failed** on the freshly-migrated isolated stack,
confirming that failure was shared-DB pollution, not anything this phase
touched):

- `alembic upgrade head` on an empty database: clean run through `0016`;
  `SELECT * FROM timescaledb_information.hypertables` confirms
  `market_data_bars` is a real hypertable. `alembic downgrade -1` then
  `upgrade head` round-trips cleanly.
- **726 passed, up from the 705-test baseline** (+21: 6 in
  `tests/marketdata/providers/test_longbridge_bar_backfill.py`, 4 in
  `tests/marketdata/test_store.py`, 4 in `tests/marketdata/test_backfill.py`,
  7 in `tests/api/test_admin_market_data.py`) — none deleted, skipped, or
  weakened.
- `ruff check` and `mypy apps` (103 source files, up from 99) both clean;
  `bash scripts/secret_scan.sh` clean.
- A real, credential-gated vendor smoke test was attempted and honestly
  reported rather than faked: this checkout's `.env` documents the
  `LONGPORT_APP_KEY`/`LONGPORT_APP_SECRET`/`LONGPORT_ACCESS_TOKEN` trio in
  comments but does not set real values, so
  `build_longbridge_bar_backfill_provider()` correctly returned `None` and
  the live-network half of verification could not run in this environment.
  This is the same NOT_CONFIGURED path every other market-data provider in
  this app already takes when unconfigured, and it is itself the behavior
  under test in `test_no_provider_configured_is_not_configured_not_a_fabricated_success`.

Scope discipline: no strategy/backtest/signal code exists yet and none is
touched by this phase. `apps/api/app/backtesting/`,
`apps/api/app/marketdata/history_provider.py`,
`apps/api/app/marketdata/indicators.py`, `apps/api/app/risk/`,
`apps/api/app/portfolio_manager/`, and `apps/api/app/execution/` are all
untouched. `LIVE_TRADING_ENABLED` is untouched. No frontend file is
touched — this phase has no UI surface by design (see the approved plan's
Phase 53 scope boundary).

Status: Implemented and verified as above.

**D071 — Phase 54: user-owned strategies with immutable-once-validated versions, a closed-vocabulary structural validator, and a form-based builder UI**

Reason: continuing the Strategy Lab arc D070 unblocked. This phase gives
the platform its first notion of a *strategy* at all — until now,
"strategy" meant exactly one hard-coded SMA(20) crossover (D025). Built by
two agents working in parallel against one frozen contract (backend:
FastAPI/SQLAlchemy; frontend: Next.js) — no code was shared between them
during the build, only the contract, and the result matched on every field
name and status code without a reconciliation pass being needed.

**(1) `Strategy` / `StrategyVersion`** (migration `0017`) — `strategies`
owns `owner_user_id` (`ON DELETE SET NULL`, not `Watchlist`'s `CASCADE`:
a strategy is not ephemeral personal research state, and Phase 55's
`backtest_runs.strategy_version_id` will be `ON DELETE RESTRICT`, so a
version must be able to outlive the deletion of whoever created it, the
same reasoning `orders.submitted_by_user_id` already follows).
`strategy_versions.definition` is **the first JSONB column in this
schema** — every other model uses typed columns — justified because the
rule vocabulary is still evolving across the phases ahead (walk-forward,
Monte Carlo and universe scanning will each want new indicator/operator
types), and a migration per new rule type would be worse than one JSONB
column plus application-level structural validation. A version is
immutable once `status != 'draft'`, enforced as a 409 at the API layer
(`routes/strategies.py`), not a DB trigger — deliberately, so the failure
mode is an ordinary HTTP error rather than a raw SQL exception, matching
D025's "never silently mutate a validated strategy" language.

**(2) A closed-vocabulary `StrategyDefinition` and a purely structural
validator** (`apps/api/app/strategies/models.py` + `validation.py`).
Indicators this phase: `sma`, `rsi` (exactly what
`marketdata/indicators.py` already implements — no new indicator math was
written). Rule operators: `crosses_above`, `crosses_below`, `gt`, `gte`,
`lt`, `lte`, each over two operands that are the literal `"close"`, a
declared indicator id, or a fixed numeric threshold — deliberately flat,
no nested expressions, so there is no recursion depth to bound. Position
sizing: `all_in`, `fixed_fraction` (`0 < fraction ≤ 1`), `fixed_notional`
(`amount > 0`). **`validate_definition()` never calls `eval`/`exec`/
`compile` or executes any part of a definition — it is a pure structural
comparison against this closed vocabulary, and that is a hard security
boundary on this platform, not a style choice: a definition is untrusted
input submitted by anyone holding `strategy:manage`, on a system that
places real orders through a real broker.** It collects *every* problem
in one pass (never stops at the first), reporting a precise path and the
offending value per error (e.g. `"indicators[1].period must be a positive
integer, got -5"`) — a builder UI showing five unrelated typos as five
sequential round trips would be unusable. Phase 55's backtest executor
inherits the identical no-execution rule: it walks this same vocabulary
and dispatches to deterministic functions, and it will never compile
anything either.

**(3) New permission `STRATEGY_MANAGE = "strategy:manage"`**
(`auth/permissions.py`) — gates the whole `/strategies` router at the
FastAPI-dependency level, exactly like `admin:manage` gates `/admin`. It
answers a different question than the per-row ownership check every route
also performs (`_load_owned_strategy`): the permission says an account may
work with strategies *at all*; the `owner_user_id` check says *which*
strategies. Not-yours is 403, not-there is 404, and a version that exists
under a *different* strategy than the one named in the URL is a 404 too
(never a 403) — a 403 there would confirm a version's existence under a
strategy the caller never asked about. No `ADMIN` override this phase —
deliberately deferred.

**(4) 8 routes on `apps/api/app/api/routes/strategies.py`** (`POST/GET
/strategies`, `GET/PATCH /strategies/{id}`, `GET/PATCH
/strategies/{id}/versions/{id}`, `POST /strategies/{id}/versions`
[fork — deep-copies the source definition, never a shallow copy, so
editing a fork can never rewrite the version it came from], `POST
.../versions/{id}/validate` [the *only* route that ever writes `status`
or `validated_at`]). Editing a draft (`PATCH .../versions/{id}`)
deliberately does **not** run the validator — a definition under
construction passes through many invalid intermediate states (an
indicator declared before the rule that uses it), and a save that
refused all of them would make the builder unusable. Validation is its
own explicit, idempotent-by-refusal act: an already-`validated` version
is not silently re-validated even though it would pass, because
re-running it would imply the result could differ, which the whole
immutability rule says it cannot.

**(5) Frontend**: `app/strategies` (list + create) and
`app/strategies/[strategyId]` (header edit, version history, fork,
builder), a new `StrategyBuilderForm` — a form-based rule composer (typed
indicator rows, operator/operand selects, conditional sizing fields), not
a raw JSON textarea and not a visual node/flow editor, matching the
approved plan's "keep v1 scope real" instruction. A non-draft version
renders every control disabled client-side, in addition to (not instead
of) the backend's 409 — defense in depth, and better UX than waiting for
a rejected request. `SideNav` gained one entry ("Strategy Lab") and one
inline icon, following the existing `IconTerminal`/`IconShield` pattern.
No chart library, no new dependency — Recharts is still scoped to start
at Phase 56.

Alternatives rejected:

- **A visual node/flow editor for the builder.** Real, useful, and far
  more UI investment than one indicator list plus two rules plus a sizing
  block justifies at this phase's actual vocabulary size. The form-based
  composer produces the identical JSON; nothing about the definition
  format assumes one editor over the other, so this can be revisited
  later without a data migration.
- **A raw JSON textarea instead of a form.** Faster to build, but pushes
  every syntax and vocabulary error onto the itemized-validator round
  trip that this phase's UX is trying to avoid making the primary
  authoring loop.
- **Validating on every `PATCH .../versions/{id}` save.** Rejected in (4)
  above — a draft must be allowed to exist in an incomplete state.
- **A `PARTIAL`-style "some fields valid" version status.** Rejected: the
  whole point of the immutability rule is a clean boundary between "not
  yet checked" and "checked and correct." A version is `draft` until
  `validate_definition` returns nothing wrong, full stop.

Scope discipline: `apps/api/app/backtesting/`,
`apps/api/app/marketdata/`, `apps/api/app/risk/`,
`apps/api/app/portfolio_manager/`, `apps/api/app/execution/`, and every
Phase-53 file are untouched — nothing yet reads a `StrategyDefinition` to
run it; that is Phase 55. No e2e Playwright spec was added, matching
Phase 50's watchlists (the closest precedent: a full user-owned CRUD
resource with a real UI) — flagged, not silently skipped, in
`docs/IMPLEMENTATION_STATUS.md`.

Verification (2026-09-08, two parallel subagents against one frozen
contract, reconciled and independently re-verified centrally on a
freshly-migrated, isolated Postgres/Redis on remapped ports 55432/56379 —
never the shared dev stack):

- `alembic upgrade head` from empty through `0017` clean; `downgrade -1`
  → `upgrade head` round-trips clean.
- Backend: **778 passed, 0 failed** (705 → 726 at D070 → 778 here; +52:
  37 pure-unit in `tests/strategies/`, 15 DB-backed integration in
  `tests/api/test_strategies.py`), confirmed on the clean isolated stack —
  the one failure the backend agent saw while developing against the
  long-lived shared dev DB (the same pre-existing `AAPL.US`-position
  contamination D070 already documented) does **not** reproduce here,
  which is itself the confirmation that it is shared-DB pollution and not
  anything this phase touched.
- `ruff check` clean; `mypy apps` clean, **109 source files** (up from
  103); `bash scripts/secret_scan.sh` clean.
- Frontend: **183 passed across 20 files** (170/18 → 183/20; +13: 6 in
  `StrategyList.test.tsx`, 7 in `StrategyBuilderForm.test.tsx`), `npm run
  build` clean — every new route (`/strategies`, `/strategies/[id]`, and
  the 5 new `/api/strategies/**` proxies) present in the build manifest
  with the expected static/dynamic rendering mode. No new dependency
  (`package.json`/`pnpm-lock.yaml` diff is empty).

Status: Implemented and verified as above.

**D072 — Phase 55: a pluggable, persisted backtesting engine (v2) that generalizes D025's hard-coded strategy without touching it**

Reason: Phase 54 gave the platform a `StrategyDefinition` nothing could run
yet. This phase is what runs one — for real historical windows, with real
position sizing, with the result actually saved. Built by two agents in
parallel against one frozen interface
(`executor.generate_signals(bars, definition) -> list[Signal]`); by the
time the persistence-side agent's route-level tests ran, the evaluator
already existed and every test exercised the real thing end to end, not a
stub — no reconciliation pass was needed between the halves, only the
central full-suite/migration verification every phase gets.

**(1) `apps/api/app/strategies/expressions.py`** — reusable evaluation
primitives over a `StrategyDefinition` and a `list[Bar]`, deliberately
*not* backtest-specific (Phase 60's live signal engine will reuse this
same module against fresh bars). `compute_indicator_series` computes a
full per-bar series for one declared indicator by calling
`marketdata.indicators.sma`/`rsi` at every index — no new indicator math,
the existing pure functions are reused unchanged, exactly as D025's own
strategy did. `evaluate_rule` resolves the six operators from D071's
closed vocabulary: `gt`/`gte`/`lt`/`lte` are instantaneous per-bar
comparisons; `crosses_above`/`crosses_below` additionally need the
*previous* bar and are proven, by test, **not** to be sugar for the
instantaneous pair (a bar can satisfy `gt` while not `crosses_above`, if
the previous bar was already above). `None` — never a guessed value —
propagates through every layer whenever an operand's indicator has
insufficient history at that index, exactly matching
`InsufficientDataError`'s existing "never pad, never approximate" rule.
**No code execution anywhere in this module** — same hard boundary
`validation.py` (D071) already states; this is the second and last module
in the whole Strategy Lab arc that interprets a definition, and neither
one ever calls `eval`/`exec`/`compile`.

**(2) `apps/api/app/backtesting/executor.py`** — `generate_signals(bars,
definition) -> list[Signal]`, reusing `Signal` from D025's `strategy.py`
rather than defining a second enum. Precedence when both `exit_rule` and
`entry_rule` fire on the same bar: exit wins (the function has no notion
of current position, so a bar where a currently-held exit condition
happens to coincide with a fresh entry condition should still read as
"get out"; a caller that is flat simply ignores a SELL signal, same as
v1 already does). **Proven, not asserted, to reproduce D025's own engine
exactly**: a `StrategyDefinition` logically equivalent to the hard-coded
SMA(20) crossover (`entry_rule: close crosses_above sma_20`, `exit_rule:
close crosses_below sma_20`, `position_sizing: all_in`) produces an
**elementwise-identical** signal list to `backtesting.strategy.
generate_signals` on the same 56-point synthetic series across 7 warmup-
period values (2, 3, 5, 19, 20, 21, 55) — including the exact warmup-
boundary bar. Same output, two structurally different mechanisms (v1
counts a fixed loop bound; v2 arrives at the identical HOLD prefix because
a crossing needs `sma[index-1]`, which is `None` at the same boundary) —
now pinned by a real regression test, not merely argued to be equivalent.

**(3) `BacktestRun` / `BacktestEquityPoint` / `BacktestTrade`** (migration
`0018`). `strategy_version_id` is `ON DELETE RESTRICT` — deliberately, per
`StrategyVersion`'s own D071 docstring, which already anticipated this: a
run's exact, immutable input must never be able to disappear out from
under a persisted result, and nothing in this phase or the next several
deletes a version. A row is written `RUNNING`, then updated exactly once
to a terminal status inside the same request that created it — the
Phase-53/D070 `MarketDataBackfillJob` posture, not D025's v1 (which raises
an exception and persists nothing on failure). **This is the one
deliberate behavioral difference from v1 worth naming explicitly: v2
*returns* a failed run, it never raises one** — v1 has nothing to persist
either way, so an exception costs nothing; v2 always has a row it created
before doing any work, and a failed attempt (a real data gap, a vendor
error) is exactly as auditable a fact as a successful one. An **empty
requested window** (real bars exist for warmup but none at all fall
within `[start_date, end_date]`) is likewise a `FAILED` run with a real
`InsufficientHistoryError` message, not a `SUCCEEDED` one reporting a
fabricated 0% return — the same no-fabrication reasoning that already
governs every other data-gap case in this codebase.

**(4) Real position sizing** — the one piece of D025's engine this phase
could not simply reuse, because v1 has no such concept. `all_in` (`cash /
price`) reproduces v1's hard-coded behavior exactly, so an `all_in`
definition's backtest is byte-for-byte what v1 would have done for the
same window. `fixed_fraction` sizes against **equity**, not cash,
deliberately: "risk 10% of the portfolio" describes portfolio size, and
sizing off cash alone would silently shrink every subsequent entry as
positions accumulate. `fixed_notional` is capped at available cash so an
oversized `amount` proposes what can actually be afforded rather than a
quantity certain to be refused downstream. All three floor to a whole
share via `Decimal.to_integral_value(rounding="ROUND_FLOOR")`, matching
v1's own rounding — this system models no fractional shares anywhere.

**(5) The RISK → PORTFOLIO → BROKER sequence is imported from `engine.py`,
not re-derived.** `engine_v2.py` imports `_attempt_trade` directly across
the module boundary despite its leading underscore — a deliberate,
commented exception to the usual privacy convention, because reusing the
exact sequence D025/D035 already pinned with tests is strictly safer than
a second copy that could quietly drift out of agreement with the first
while both kept passing their own tests. `engine.py` itself — and
`strategy.py`/`metrics.py`/`errors.py`/`models.py` alongside it — is
**untouched**, and stays that way indefinitely: it is what `POST
/backtests` still runs, and what every number engine_v2 produces is
measured against.

**(6) New permission `STRATEGY_BACKTEST = "strategy:backtest"`**,
deliberately separate from `STRATEGY_MANAGE` (D071) — a role could run
backtests without authoring strategies, or the reverse. Gates a new
router pair (`/strategies/{id}/versions/{id}/backtests`,
`/backtest-runs/{id}`) at the FastAPI-dependency level; per-row ownership
(via the run's `strategy_version → strategy → owner_user_id` chain) is
still checked on top, the same two-part shape D071 established. `POST
.../backtests` 409s (`VERSION_NOT_VALIDATED: …`) unless the target
version's status is `validated` — a strategy is backtested only once it
has passed the deterministic structural check, never a draft.

Alternatives rejected:

- **Reimplementing the RISK → PORTFOLIO → BROKER sequence for v2.**
  Rejected in (5) — importing the tested private function is safer than a
  parallel copy.
- **Raising on a failed run, matching v1.** Rejected in (3) — v2 always
  has a row to finish, so returning it is more honest than discarding the
  attempt.
- **A single indicator-agnostic warmup rule keyed off indicator type.**
  Rejected: `_warmup_bar_count` uses `max(period) + 1` for every indicator
  uniformly rather than branching on `sma` needing `period` and `rsi`
  needing `period + 1` — a wrong type-specific rule would silently shorten
  a warmup and produce a `None` indistinguishable from a real data gap;
  one bar of slack for every indicator costs nothing and cannot be gotten
  wrong the same way.
- **Sizing `fixed_fraction` off cash instead of equity.** Rejected in (4)
  — would silently shrink over a run with open positions.

Scope discipline: `apps/api/app/backtesting/engine.py`,
`strategy.py`, `metrics.py`, `errors.py`, `models.py`,
`apps/api/app/strategies/validation.py`/`models.py`/`service.py`, and
`apps/api/app/api/routes/strategies.py` are all untouched. No frontend
file is touched — the analytics dashboard for these results is Phase 56,
which is also where a real charting library gets introduced per the
earlier user decision.

Verification (2026-09-08, two parallel subagents against one frozen
interface, re-verified centrally on a freshly-migrated, isolated
Postgres/Redis on remapped ports 55432/56379 — never the shared dev
stack):

- `alembic upgrade head` from empty through `0018` clean; `downgrade -1` →
  `upgrade head` round-trips clean.
- **840 passed, 0 failed** (778 → 840; +44 in `tests/strategies/
  test_expressions.py` + `tests/backtesting/test_executor.py`, +18 in
  `tests/backtesting/test_engine_v2.py` + `tests/api/
  test_strategy_backtests.py`), confirmed on the clean isolated stack —
  the one failure seen during development against the long-lived shared
  dev DB was, again, the same pre-existing `AAPL.US`-position
  contamination D070/D071 already documented, and does not reproduce
  here.
- `ruff check` clean; `mypy apps` clean, **114 source files** (up from
  109); `bash scripts/secret_scan.sh` clean.

Status: Implemented and verified as above.

**D073 — Phase 56: Recharts introduced (scoped to Strategy Lab only), a backtest analytics dashboard, and a jsdom+Recharts rendering pitfall worth remembering**

Reason: Phase 55 produced real, persisted backtest results with nothing to
look at. This phase is the first frontend surface for any of it, and the
first use of a real charting library in this codebase — a decision made
explicitly with the user during Phase 53's planning, not decided
unilaterally here: every existing chart (`PortfolioHistoryChart.tsx`,
`BacktestPanel.tsx`'s equity curve) is hand-rolled SVG via
`components/ui/ChartFrame.tsx`, deliberately scoped for a single-series
line, and a monthly-returns heatmap plus a multi-run overlay comparison
are a different visualization class than that was ever meant to carry.
Built by two agents in parallel, fully decoupled by design (no shared new
file, no frozen interface between them beyond the already-shipped D072
API) — the orchestrator installed Recharts and built the two shared proxy
routes (`GET /api/strategies/{id}/versions/{id}/backtests`, `GET
/api/backtest-runs/{id}`) first, as the one piece both halves needed.

**(1) Recharts `^3.10.1`** (`apps/web/package.json`) — the only new
dependency this phase, and it stays scoped to the five new Strategy-Lab
chart components below. `ChartFrame.tsx`/`PortfolioHistoryChart.tsx`/
`BacktestPanel.tsx` are untouched and stay hand-rolled SVG; nothing forces
a migration.

**(2) `apps/web/lib/backtestMetrics.ts`** — pure, unit-tested functions
(`computeDrawdownSeries`, `computeMonthlyReturns`) that derive a
drawdown series and a monthly-returns grid from a run's `equity_curve` on
the client, rather than the backend computing and persisting either. Kept
client-side deliberately: both are pure functions of data the backend
already returns in full, so computing them again on every later phase's
UI change would be free, whereas persisting a second derived
representation server-side would be one more thing that could disagree
with the equity curve it was computed from.

**(3) Five new chart/table components** —
`EquityCurveChart`/`DrawdownChart`/`MonthlyReturnsHeatmap`/
`BacktestTradeLedger` (single-run detail page) and
`BacktestRunComparison` (multi-run overlay + comparison table, built with
its own self-contained Recharts usage rather than reusing
`EquityCurveChart`, a deliberate small duplication traded for full
agent-parallelism safety — see Alternatives below). Every chart color
comes from this app's existing CSS custom properties (`--pos`/`--neg`/
`--accent`/`--grid`/`--ink-muted`/etc. in `app/globals.css`), never a
hard-coded hex value, so Strategy Lab charts respect light/dark mode the
same way every hand-rolled SVG chart already does. A monthly-returns
heatmap is plain CSS grid, not Recharts — a heatmap is not really a
"chart" in the axes-and-data-points sense, and a library brings nothing
to it.

**(4) A real Recharts+jsdom rendering pitfall, found and worked around
independently by both agents, worth recording so a future phase doesn't
rediscover it the hard way**: in this app's Vitest/jsdom test
environment, a Recharts `<LineChart>` with **both** a `<Legend>` and 2+
`<Line>` series present silently fails to render `<Line>`/`<YAxis>`/
`<CartesianGrid>` at all — bisected by one agent directly against the
installed package (`<Tooltip>` alone is fine, `<CartesianGrid>` alone is
fine, `<Legend>` combined with ≥2 lines is the trigger). Two independent,
compatible fixes landed: `EquityCurveChart`'s own tests scope a
`getBoundingClientRect` mock to only the `.recharts-responsive-container`
element rather than every `HTMLElement` (a global mock also inflates the
Legend's own wrapper, which Recharts then subtracts from the available
plot height); `BacktestRunComparison` sidesteps the whole class of bug in
production code, not just tests, by rendering its own small colored-dot
legend instead of Recharts' `<Legend>` component, keyed to the same
per-series color function each `<Line>` uses. Neither agent found this
reproduces in a real browser (not chased further) — noted here so the
next phase that adds a multi-series Recharts view knows to check this
first rather than assume its own test failure is a new bug.

**(5) Failed runs render honestly, with nothing invented.** Every metric
field is `null` on a `status: "failed"` `BacktestRunSummary`/
`BacktestRunDetailResponse` (D072) — the detail page stops at the real
`error_detail` and skips charts/ledger entirely rather than rendering a
zero-flat curve; the run list renders `—` for a failed row's metrics, never
`0%`; the comparison view excludes a failed run from the equity overlay
(nothing to plot) but still lists it in the metrics table with its error,
rather than silently dropping it from the comparison altogether.

**(6) Version selector: all versions listed, non-validated ones disabled
with an inline hint**, not merely the validated subset — a user can see
their strategy's whole version history from this page and understands
*why* an option is unavailable ("(not backtestable)" plus a hint to
validate first) rather than wondering why an older draft simply isn't
there.

Alternatives rejected:

- **`BacktestRunComparison` importing and reusing `EquityCurveChart`.**
  Considered, and explicitly designed for (the component's `series` prop
  accepts 2+ named series precisely so it *could* be reused this way) —
  but doing so would have made the comparison agent depend on a component
  the detail-page agent might not have finished yet, reintroducing the
  cross-agent coordination this phase's split was designed to avoid. The
  small duplication (two independent Recharts `<LineChart>` usages) was
  judged cheaper than the coordination risk; `EquityCurveChart` remains
  available for a future phase to consolidate onto if that trade-off ever
  looks wrong in hindsight.
- **Persisting drawdown/monthly-returns server-side.** Rejected in (2) —
  both are pure derivations of data already fully returned; nothing is
  gained by computing them twice in two different places that could
  disagree.
- **Recharts' own `<Legend>` in the comparison view.** Rejected once (4)
  was found — a hand-rolled legend sidesteps the bug class entirely
  rather than working around one specific trigger of it.

Scope discipline: no backend file is touched this phase (D072's routes are
consumed as-is); `components/ui/ChartFrame.tsx`,
`components/PortfolioHistoryChart.tsx`, and `components/BacktestPanel.tsx`
(D025's v1 UI) are all untouched.

Verification (2026-09-08, two parallel subagents building fully disjoint
files, reconciled centrally):

- Frontend: **232 passed across 28 files** (183/20 → 232/28; +49: 24 in
  the detail-page agent's 5 test files, 25 in the list/comparison agent's
  3 test files), confirmed by re-running the merged suite centrally, not
  just trusting each agent's own report. `npm run build` clean — every new
  route (`/strategies/[id]/backtests`, `/strategies/[id]/backtests/
  [runId]`) and both new proxy routes present in the manifest.
- Exactly one new dependency (`recharts@^3.10.1`); confirmed via `git diff
  package.json` — nothing else added by either agent.

Status: Implemented and verified as above.

**D074 — Phase 57: walk-forward validation + Monte Carlo simulation, and a mid-phase scope split worth recording**

Reason: the original roadmap bundled "walk-forward, robustness, Monte
Carlo" into one phase. Walk-forward and Monte Carlo both build directly
on Phase 55's persisted `BacktestRun` and share nothing conceptually with
parameter-perturbation robustness testing (which requires generating
variant `StrategyDefinition`s, a genuinely different piece of work) — so
this phase covers only the first two, and robustness testing becomes its
own **Phase 58**, shifting every phase after it in the saved plan
(`~/.claude/plans/iterative-orbiting-shamir.md`, updated to match) by one
from the original numbering. Built by two agents in parallel; the
orchestrator built the shared DB schema first (both features needed a new
migration in the same phase, which is exactly the kind of concurrent-
migration collision this project's own history warns about — D028/D030/
D032/D063/D065/D067/D069's renumbering sagas were all a version of this
same problem one layer up, at the decision-number level rather than the
migration-number level).

**(1) Walk-forward is deliberately NOT "walk-forward optimization."** The
classic technique re-fits a strategy's parameters on each in-sample window
before testing the next one out-of-sample; this platform has no
parameter-fitting step anywhere (a `StrategyDefinition`'s rules are fixed
by whoever authored it, D071), so there is nothing to re-fit. What
`walk_forward_runs`/`walk_forward_windows` (migration `0019`) actually
record is narrower and just as useful: the same fixed, validated
definition replayed across sequential, non-overlapping historical windows,
to see whether its performance holds up across periods or is an artifact
of the one window an ordinary backtest happened to cover. Calling it
"optimization" would claim a capability that doesn't exist —
`apps/api/app/backtesting/walk_forward.py`'s own docstring makes the same
distinction at the point it matters most.

**(2) Each window is a real, ordinary `BacktestRun` — not a second
execution path.** `run_walk_forward()` calls `engine_v2.run_strategy_backtest()`
unchanged, once per window; `walk_forward_windows.backtest_run_id` points
at a genuine row in the table migration 0018 created. This is the same
"reuse the tested implementation rather than risk two that drift apart"
reasoning D072 used for `_attempt_trade` — applied here one layer up, to
the whole backtest engine rather than one function inside it. Every window
starts fresh at the same `starting_cash` rather than compounding a running
balance across windows: a consistency test asks "does this perform
similarly across periods," not "what would compounding through all of
them produce" — a single ordinary backtest over the full range already
answers the second question, and conflating the two would make "windows
behaved inconsistently" indistinguishable from "an early loss shrank later
windows' capital."

**(3) Aggregates are computed only over windows whose own backtest
succeeded**, never averaging in a fabricated number for one that failed
(e.g. a bar-store gap specific to one sub-period) — `num_windows` counts
every window attempted, `num_succeeded_windows` says how many numbers the
statistics are actually built from. `stddev_return_pct` is `NULL`, not
`0`, when exactly one window succeeded — one observation has no spread,
and reporting `0.0000` would claim a measurement that was never made. A
request spanning fewer than 2 complete windows, or where every window's
own backtest failed, is a `FAILED` walk-forward run naming the real reason
— "consistency" is not a claim that can be made about zero or one data
point.

**(4) Monte Carlo answers a different question than a backtest or a
walk-forward run does**: not "what happened," but "how much would the
result have varied by chance alone if this same set of trade outcomes had
occurred in a different order?" — a statement about the sequence risk
already latent in an existing `BacktestRun`'s trades, resampled with
replacement, never a claim about a different or better trading history.
`monte_carlo_runs` (migration `0020`) persists only aggregate percentile
statistics, never every simulated path — with 1,000+ simulations each
implying a full equity curve, storing all of them would dwarf every other
table in this schema for comparatively little benefit, and a persisted
`random_seed` is sufficient to regenerate the exact distribution later,
since resampling is deterministic given a seed and the same input trade
list. **This reproducibility claim is proven, not assumed**: a test calls
`run_monte_carlo` twice with an identical explicit seed over the same
trade list and asserts the two runs' eight aggregate columns are
bit-for-bit identical, and that a different seed produces a different
result.

**(5) Both features reuse `Permission.STRATEGY_BACKTEST` (Phase 55)
rather than adding a new permission each.** Walk-forward and Monte Carlo
are both further analyses OF a backtest, not a new capability class —
`permissions.py`'s own rule (never add a permission without a real,
distinct enforcing need) argues against two more members that would gate
the identical thing `strategy:backtest` already gates.

Alternatives rejected:

- **Compounding capital across walk-forward windows.** Rejected in (2) —
  would conflate strategy-consistency with early-window capital effects.
- **Persisting every Monte Carlo simulated path.** Rejected in (4) — the
  seed already makes the full distribution reproducible on demand; storing
  it too is pure cost with no offsetting benefit at this phase's scale.
- **A new permission per new analysis type.** Rejected in (5) — would
  multiply role-management surface for gates that would always be granted
  together in practice.
- **Building parameter-perturbation robustness testing in this same
  phase**, as the original roadmap line implied. Rejected — moved to its
  own Phase 58 (see Reason above); the saved plan file is updated to match
  so it stays an accurate reference for what's actually been decided.

Scope discipline: `apps/api/app/backtesting/engine.py`/`engine_v2.py`/
`executor.py`/`strategy.py`/`metrics.py`/`errors.py`, `apps/api/app/strategies/*`,
and `apps/api/app/api/routes/strategies.py`/`strategy_backtests.py` are all
untouched — both new features are pure additions that call the existing
engine and reuse its metrics functions unchanged. No frontend file is
touched this phase.

Verification (2026-09-09, two parallel subagents building fully disjoint
files against a DB foundation built centrally first, re-verified centrally
on a freshly-migrated, isolated Postgres/Redis on remapped ports
55432/56379 — never the shared dev stack):

- `alembic upgrade head` from empty through `0020` clean; `downgrade -2` →
  `upgrade head` round-trips clean.
- **878 passed, 0 failed** (840 → 878; +19 in `tests/backtesting/
  test_walk_forward.py` + `tests/api/test_walk_forward.py`, +19 in
  `tests/backtesting/test_monte_carlo.py` + `tests/api/test_monte_carlo.py`),
  confirmed on the clean isolated stack — the shared dev stack's now-
  familiar `AAPL.US`-position contamination (D070/D071/D072) surfaced
  again during development and, again, does not reproduce here.
- `ruff check` clean; `mypy apps` clean, **120 source files** (up from
  114); `bash scripts/secret_scan.sh` clean.

Status: Implemented and verified as above.

**D075 — Phase 58: parameter-sensitivity (robustness) testing, and an interrupted agent's work resumed and verified rather than redone**

Reason: closes the scope this initiative's own roadmap split out of the
original "walk-forward, robustness, Monte Carlo" line at D074. Given a
validated `StrategyVersion`, replay it once exactly as authored (the
baseline), then once more per numeric parameter nudged ±`magnitude_pct`
(one factor at a time — every other parameter held at its original value),
and report how far the result moved. This is the platform's first answer
to the master spec's explicit anti-overfitting demand: a strategy whose
result swings wildly from a small, honest nudge to its own numbers is
exhibiting a textbook overfitting symptom, and this phase is what makes
that visible rather than assumed.

Built by two agents in parallel against a frozen interface, mirroring
Phase 55's executor/engine split: `apps/api/app/strategies/perturbation.py`
(pure, no I/O, no DB) generates the perturbed variants; `apps/api/app/backtesting/robustness.py`
replays each one and persists the result. Unlike Phase 57, only one of the
two needed a migration this time — no coordination was required to avoid a
collision, and none happened.

**Mid-task interruption, resumed rather than restarted.** The
orchestrator/persistence agent hit a session rate limit mid-way through
writing its own DB-backed integration tests. Its work through that point —
the migration, both ORM models, `robustness.py`, the schemas, the routes,
`main.py`'s registration, and complete (not stub) test files — was already
on disk and was substantively finished; only one thing was actually wrong:
one integration test used `"right": "open"` as a rule operand, but this
platform's closed vocabulary (D071's `PRICE_OPERAND_CLOSE`) only recognizes
the literal `"close"` for a bar price, never `"open"`/`"high"`/`"low"` — so
that one test's own fixture definition failed structural validation before
robustness testing ever ran. Fixed by changing the test's fixture to a
`"close"` vs. fixed-number rule (which is what the test actually needed:
any structurally valid definition with nothing numeric to perturb), not by
touching `perturbation.py`, `robustness.py`, or the closed vocabulary
itself — the bug was in one test's own input, not in anything that
shipped. Every other file the interrupted agent produced needed no
changes at all, confirmed by running the exact verification steps its own
instructions specified.

**(1) One-factor-at-a-time, not a combined search.** Each perturbation
changes exactly one parameter and holds every other one fixed, so a
result's movement can be attributed to the one thing that changed. A
combined perturbation (nudging every parameter simultaneously) would
conflate several effects into one number and answer a fuzzier, less
actionable question. `apps/api/app/strategies/perturbation.py` generates
two variants per perturbable parameter (nudged down, nudged up) —
`indicators[i].period` (rounded, floored at 1) and, when the sizing type
carries one, `position_sizing.fraction`/`position_sizing.amount` (clamped
into their own valid ranges). A direction whose clamp produces no actual
change from the original is skipped entirely — a "perturbation" that
changed nothing would misleadingly read as evidence of robustness. A
definition with `all_in` sizing and no indicators has nothing perturbable
at all and correctly returns an empty list — a real, valid, structurally-
legal case this phase must not treat as an error.

**(2) The warmup is recomputed per perturbed definition, not shared from
the baseline.** A +10% nudge on SMA(20) needs a different number of
warmup bars than the original — reusing the baseline's shorter warmup
would hand the evaluator too few leading bars and risk a quietly wrong
signal rather than a loud, honest `InsufficientHistoryError`. The extra
cost is one more query against the persisted bar store (D070) per
perturbation — never a vendor call — which is cheap and is exactly what
buys every row meaning what it says.

**(3) A perturbation is never persisted as a `BacktestRun`.** This is the
one structural difference from Phase 57's walk-forward, which DOES record
each window as a real `BacktestRun` because a window replays the version's
own definition. A perturbation replays a definition no `StrategyVersion`
actually contains, and a `backtest_runs` row is specifically a record OF a
version's definition — writing one anyway would file a backtest of rules
nobody authored under a version whose stored definition says something
else. `robustness.py` instead reaches directly for `engine_v2.py`'s two
already-tested, no-persistence helpers (`_load_warmup_and_window`,
`_replay_window`) — the same cross-module underscore-import precedent
D072 established for `_attempt_trade` — and its own `robustness_perturbations`
rows (migration `0021`) are where a variant's result lives instead.

**(4) The baseline is re-run here, never read off a prior `BacktestRun`.**
Every number this phase reports comes from the same two engine helpers,
over the same window, cash, and limits, so "perturbed vs. baseline" is
guaranteed to differ in exactly the one parameter that was nudged — never
confounded by a stale prior run's possibly-different window or an engine
that has since changed.

**(5) `max_return_deviation_pct` is one plain, honest number — deliberately
not a composite "robustness score."** This phase does not attempt to
define a weighted figure across return, drawdown, and whatever else might
matter, because any such weighting would encode a risk preference nobody
has stated (echoing D071's identical refusal to invent a scoring model
where none was asked for). A future phase can build a score on top of
these raw columns; it could not recover the raw columns from a score. Zero
succeeded perturbations out of several attempted, with a successful
baseline, is left as a `SUCCEEDED` run with null aggregates and real
counts — "the strategy works exactly as authored and every nudge of it
fell over" is itself the strongest finding this module can produce, not an
error to hide.

Alternatives rejected:

- **Perturbing every parameter simultaneously.** Rejected in (1) — would
  conflate multiple effects and not tell a reader which parameter actually
  drove any change observed.
- **Persisting each perturbation as a `BacktestRun`.** Rejected in (3) — a
  `backtest_runs` row's meaning is specifically "this version's own
  definition, replayed"; a perturbed definition is not that.
- **Sharing the baseline's warmup fetch across all perturbations.**
  Rejected in (2) — a materially different fetch requirement per
  perturbed period makes sharing actively wrong, not just imprecise.
- **A composite robustness score.** Rejected in (5) for the same reason
  D071 gave for not inventing a strategy-quality score: any weighting
  would be an opinion this phase does not have the standing to assert.
- **Restarting the interrupted agent's work from scratch** after the rate
  limit. Rejected once inspection showed the work was substantively
  complete and correct — redoing ~2,100 lines of already-correct,
  already-tested code to fix one test fixture's invalid operand would have
  cost far more than verifying and fixing the one real defect.

Scope discipline: `apps/api/app/backtesting/engine.py`/`engine_v2.py`/
`executor.py`/`strategy.py`/`metrics.py`/`errors.py`,
`apps/api/app/strategies/validation.py`/`models.py`/`service.py`/`expressions.py`,
and every existing route file are untouched — this phase imports from
`engine_v2.py`/`metrics.py` and adds new sibling files only. No frontend
file is touched.

Verification (2026-09-09, two parallel subagents against one frozen
interface — one interrupted mid-task by a session limit and resumed by
inspection rather than restarted — re-verified centrally on a
freshly-migrated, isolated Postgres/Redis on remapped ports 55432/56379):

- `alembic upgrade head` from empty through `0021` clean; `downgrade -1` →
  `upgrade head` round-trips clean.
- One test fixture fixed (an invalid `"open"` operand → a valid `"close"`-
  vs-fixed-number rule); zero production code changed.
- **916 passed, 0 failed** (878 → 916; +38: 22 in `tests/strategies/
  test_perturbation.py`, 16 in `tests/backtesting/test_robustness.py` +
  `tests/api/test_robustness.py`), confirmed on the clean isolated stack —
  the shared dev stack's familiar stale-broker-row flake (D070–D074)
  surfaced again during verification and, again, does not reproduce here.
- `ruff check` clean; `mypy apps` clean, **124 source files** (up from
  120); `bash scripts/secret_scan.sh` clean.

Status: Implemented and verified as above.

**D076 — Phase 59: strategy ranking / leaderboard — a transparent read-model, no persisted score, and a real cross-phase query bug caught by real cross-phase data**

Reason: the master spec's own words — "NEVER optimize solely for maximum
ROI… the platform should be willing to return 'No sufficiently robust
strategy found.' That is preferable to presenting a misleading strategy."
This phase turns the raw numbers Phases 55/57/58 persist into a ranked
leaderboard of the caller's own strategies, scored on four equally-weighted
dimensions (return, risk, out-of-sample consistency, parameter stability)
where return is capped at a quarter of the total — a strategy cannot rank
well on return alone. Built by two agents in parallel (backend scoring +
`GET /strategies/leaderboard`; frontend `/strategies/leaderboard` page),
mirroring Phase 54's backend/frontend split.

**(1) No `strategy_scores` table, no migration, no ORM model —
deliberately.** The score is a read-model, recomputed on every request
from `backtest_runs`/`walk_forward_runs`/`monte_carlo_runs`/`robustness_runs`
rows that already exist. A persisted score would be a second, staler copy
of numbers the source tables already hold — wrong the moment a newer run
lands, and capable of showing a verdict whose inputs no longer exist.
Recomputation is four indexed single-row lookups per strategy version,
over a set bounded by how many strategies one person has authored — cheap,
and always exactly as current as the runs it summarizes. First phase of
this whole initiative to add a route with no schema change at all.

**(2) Never a black box.** Every score component carries its own `detail`
string naming the actual column value it came from and the scale that
turned that value into points (`"total_return_pct=12.45% (scale: 0%→0pts,
20%+→25pts)"`); the status carries a `status_reason` naming the actual
counts behind it. `ScoreComponent.max_points` is a real field on the wire,
not a constant a reader has to look up in the source — the response is
self-describing, and a future phase reweighting components must not
silently invalidate every already-rendered score. The four
`latest_*_run_id` fields make it auditable: a reader who doubts a
component fetches the exact run it was computed from.

**(3) Absent is never zero.** A component whose underlying run does not
exist is LEFT OUT of `components` entirely, never scored 0 — "nobody has
run a walk-forward test on this" and "this strategy failed its
walk-forward test" are opposite findings, and a zero would state the
second. `max_possible_points` counts only the present components (25
each), and `percentage` — never raw `total_points` — is the ranking key,
so a strategy is not rewarded for skipping the tests that could have gone
badly. `components_measured` (0-4) is surfaced next to every score so a
reader always sees how much was actually measured. A strategy with no
succeeded backtest at all has no score — `compute_strategy_score` returns
`None` and the leaderboard excludes it entirely, rather than ranking it
last with a fabricated zero.

**(4) The status heuristic is a documented, deliberate first pass.**
`insufficient_data` (only return+risk measured), `promising` (one of
consistency/parameter-stability measured and clean), `validated` (both
measured, neither flagged), `overfit_risk` (either one measured raises a
flag — a walk-forward profitable-window ratio under 0.5, or a robustness
return deviation over 20 percentage points). One warning sign is enough
for `overfit_risk`, which takes priority over `validated` — a strategy
that holds up under parameter nudges but falls apart out of sample is not
validated, it is flagged. The three thresholds live in one place, are
labeled as adjustable, and `validated` is explicitly documented to mean
"both available checks ran and neither raised a flag," not "this will make
money." An `overfit_risk` strategy satisfies no `min_status` filter above
the floor — it must not surface for `min_status=promising` merely because
the components that happened to be measured scored well.

**(5) An empty leaderboard is a 200, never an error.** `min_status=validated`
with nothing meeting it returns `{"items": [], ...}` — the master spec's
"no sufficiently robust strategy found" IS that answer, and turning the
correct result into a 404/422 would be exactly the misleading presentation
the spec warns against.

**(6) A real cross-phase query bug, caught only by real cross-phase data.**
The contract said "most recent SUCCEEDED `BacktestRun` by `created_at`."
Taken literally that is wrong: D074's walk-forward orchestrator persists
one *real* `backtest_runs` row per window (it calls the ordinary engine,
not a second path), each carrying the strategy version's own
`strategy_version_id`. So for every version that has ever been
walk-forward tested — exactly the ones we most want ranked correctly — the
newest `backtest_runs` row is its final *window*, covering a fraction of
the range, and scoring `return`/`risk` off it would describe a fragment
the user never requested. The backend agent's integration test (driving
the real Phase 55/57/58 routes over really-persisted bars) caught the
score being computed from a −0.12% third window instead of the +0.24%
headline backtest. Fixed by excluding
`BacktestRun.id IN (SELECT backtest_run_id FROM walk_forward_windows)`,
and pinned by a regression test that asserts the chosen id is the
requested backtest and none of the window run ids.

**(7) `strategy:manage`, not a new permission.** A leaderboard of your own
strategies is a read over the resource `strategy:manage` already gates —
it exposes no strategy and no run that `GET /strategies` and the run
listings do not already show the same caller. Route registration order
matters: `/strategies/leaderboard` is registered before
`routes/strategies.py`'s `/strategies/{strategy_id}` (a `uuid.UUID` path
param), or the static path would 422 as a malformed UUID — the one place
in `main.py` where registration order is load-bearing, and commented as
such.

Alternatives rejected:

- **Persisting the score in a `strategy_scores` table.** Rejected in (1) —
  a second, staler copy of numbers the source tables already hold.
- **Ranking `overfit_risk` strategies numerically among the others.**
  Rejected in (4) — the flag exists precisely to keep them out of a
  `min_status=promising` result they'd otherwise satisfy on component
  scores alone.
- **A weighted composite score (return heavier than stability, or the
  reverse).** Rejected — equal 25-point weighting is a stated choice, not
  an absent one; any other weighting would encode a risk preference nobody
  here has stated, the same refusal D075 recorded.
- **Scoring Monte Carlo into the total.** Rejected — a Monte Carlo run
  answers "how much would this have varied by chance," a statement about
  sequence risk rather than a pass/fail signal; inventing a points scale
  for it would be exactly the unstated preference this module refuses
  elsewhere. `latest_monte_carlo_run_id` is surfaced so a reader can go
  look at it.

Scope discipline: no migration, no ORM model, no change to any existing
route file, no change to `apps/api/app/backtesting/*` or
`apps/api/app/strategies/validation.py`/`models.py`/`expressions.py`/
`perturbation.py`. `main.py` gains one import + one `include_router` (plus
a comment on the ordering). Frontend adds one page, one proxy route, one
component, one `SideNav` entry — no new dependency.

Verification (2026-09-10, two parallel subagents, re-verified centrally on
a freshly-migrated, isolated Postgres/Redis on remapped ports
55432/56379):

- No migration this phase; `alembic upgrade head` still reaches `0021`
  cleanly on an empty database.
- **954 backend tests** (916 → 954; +38: 32 unit in
  `tests/strategies/test_scoring.py`, 6 DB-backed integration in
  `tests/api/test_leaderboard.py` driving the real four-phase route
  chain); `ruff check` clean; `mypy apps` clean, **127 source files** (up
  from 124); `bash scripts/secret_scan.sh` clean. Confirmed on the clean
  isolated stack — the shared dev stack's familiar stale-broker-row flake
  (D070–D075) surfaced again during verification and, again, does not
  reproduce here.
- **240 frontend tests across 29 files** (232/28 → 240/29; +8 in
  `test/StrategyLeaderboard.test.tsx`), `npm run build` clean with
  `/strategies/leaderboard` and its proxy route in the manifest. No new
  dependency.

Status: Implemented and verified as above.

**D077 — Phase 60: universe scanning — one strategy across many symbols, ranked, reusing the backtest engine unchanged**

Reason: the master spec's "MODE B — RESEARCH & DISCOVER TICKERS" — run a
validated strategy across a universe and rank which markets it actually
suits. Where Phase 57's walk-forward asks "does this hold up across
PERIODS," this asks "which MARKETS does it suit." The two are the same
shape (parent row, loop, one child row per unit of work, aggregate)
because they are the same kind of job: an orchestration OF backtests, not
a second way to run one. Built by two agents in parallel (backend
orchestration + migration `0022`; frontend scan form + ranked results),
one migration, no collision.

**(1) `run_strategy_backtest` is CALLED, never re-implemented.** Every
scanned symbol is an ordinary, fully-persisted `BacktestRun` from
`engine_v2.py`, and `universe_scan_results.backtest_run_id` points at that
real row — same risk engine, portfolio manager, fill math, bar store as
`POST .../backtests`. This is the right structure here for exactly the
reason it was right for walk-forward and NOT right for Phase 58's
robustness: a scanned symbol replays the version's OWN unaltered
definition, so a `backtest_runs` row filed under that version accurately
records what ran. Only a perturbed definition — rules no version contains
— had to stay out of that table.

**(2) Every symbol starts fresh at the same `starting_cash`.** Symbols do
not share a balance or compound through one. "How did this strategy do on
each of these markets, comparably" is the question; a shared portfolio
would make every symbol's figure depend on which other symbols were in
the list and in what order, turning a comparison into a path-dependent
simulation. Running a strategy as one portfolio across a universe is a
genuinely different feature and is not this one.

**(3) A `MAX_SCAN_SYMBOLS = 50` cap, honestly scoped.** A scan runs
synchronously in-request, one backtest per symbol — 50 modest-window
backtests is a few seconds of pure in-memory simulation, fine inline,
matching every other analysis job in this codebase (no queue). A larger
universe is a real need that requires a job runner this codebase does not
have yet; until then the cap is honest about what a synchronous request
can do. The route 422s (before creating any row) for an explicit list
over the cap, an explicitly-passed `[]` (with the helpful message "pass
symbols to scan, or omit the field entirely to scan all ingested
symbols"), and "all ingested" mode when the bar store holds either 0 or
more than 50 distinct symbols for the interval — each naming the real
count and what to do about it.

**(4) Two modes, one resolver.** `symbols` omitted/null →
`scan_mode="all_ingested"`: the universe is `SELECT DISTINCT symbol FROM
market_data_bars WHERE bar_interval = :interval` — whatever has actually
been backfilled, which is the only universe a backtest could run over
anyway. A non-null list → `scan_mode="explicit_list"`, normalized (trim,
upper-case, dedupe preserving order — `schemas_watchlists.normalize_symbol`'s
rule applied to a list). `resolve_scan_symbols` is one exported function
called twice per request — once by the route to answer 422 before any row
exists, once by the orchestrator for the run it records — one definition
of "the universe," not two that could disagree. `requested_symbols` (an
`ARRAY(String)` column — the first since `Role.permissions`) is `NULL` in
all-ingested mode rather than the resolved list, keeping the "what was
requested" / "what was scanned" distinction the column exists for.

**(5) A per-symbol backtest failure is a normal, recorded outcome.**
Scanning a list someone typed routinely hits a symbol with no ingested
bars over the window — that symbol gets its own result row with its real
FAILED `backtest_run_id` and `error_detail`, its metrics stay NULL (never
0), it counts toward `num_symbols` but not `num_succeeded`, and the scan
continues. A `SUCCEEDED` scan with `num_succeeded == 0` ("none of these
symbols have enough data for this window") is a real, informative result,
not an error — same posture as walk-forward's per-window failures and
D076's empty leaderboard.

**(6) Ranking computed on read, `num_qualified` a stated simple filter.**
No `rank` column — `order_and_rank_results` derives it on the detail
response (SUCCEEDED first, best `total_return_pct` first, symbol tiebreak;
FAILED get `rank=None`, never a last place), matching D076's leaderboard.
`num_qualified` = "succeeded AND profitable over this window" — a
deliberately simple first-pass filter, documented as adjustable, same
framing D075/D076 use for their own heuristics.

**(7) Reuses `strategy:backtest`** — a scan is a batch of backtests, not a
new capability class.

Alternatives rejected:

- **A shared portfolio across the scanned universe.** Rejected in (2) —
  path-dependent, and a different feature.
- **Persisting a `rank` column.** Rejected in (6) — a second copy of an
  ordering the data already fully determines.
- **A 4xx when a listed symbol has no bars.** Rejected in (5) — one bad
  symbol should not lose the other forty-nine; a real FAILED result row
  saying exactly what went wrong is more useful.
- **Scanning the whole universe synchronously with no cap.** Rejected in
  (3) — honest about what an in-request job can do; the cap moves when a
  job runner exists.

Scope discipline: `apps/api/app/backtesting/engine.py`/`engine_v2.py`/
`executor.py`/`strategy.py`/`metrics.py`/`walk_forward.py`/`monte_carlo.py`/
`robustness.py` and every existing route file are untouched — the
orchestrator imports `run_strategy_backtest` and adds new sibling files
only. Frontend adds two pages, two proxy routes, three components, three
test files — no new dependency, and `SideNav` is deliberately not touched
(universe scans live under a strategy, reached from its detail page like
backtests).

Verification (2026-09-10, two parallel subagents, re-verified centrally
on a freshly-migrated, isolated Postgres/Redis on remapped ports
55432/56379):

- `alembic upgrade head` from empty through `0022` clean; `downgrade -1` →
  `upgrade head` round-trips clean.
- **976 backend tests** (954 → 976; +22: 11 in `tests/backtesting/
  test_universe_scan.py`, 11 in `tests/api/test_universe_scan.py`); `ruff
  check` clean; `mypy apps` clean, **130 source files** (up from 127);
  `bash scripts/secret_scan.sh` clean. Confirmed on the clean isolated
  stack — the shared dev stack's familiar stale-broker-row flake
  (D070–D076) surfaced again during development and, again, does not
  reproduce here.
- **259 frontend tests across 32 files** (240/29 → 259/32; +19 across
  `test/UniverseScan{Form,List,Results}.test.tsx`), `npm run build` clean
  with all four new routes in the manifest. No new dependency. (Note: on
  this slow test machine the frontend suite at default worker concurrency
  produces spurious 5s-timeout failures in pre-existing tests too — run
  with `--maxWorkers=2 --testTimeout=20000` for a clean signal; that's an
  environmental artifact, not a regression.)

Status: Implemented and verified as above.

---

**D078 — Phase 61: signal engine — a validated strategy's present-tense verdict for a symbol, off the latest bars, with the reasoning kept**

Reason: the master spec's section 25 — turn a validated strategy into "what
should I do about this symbol right now," and never show a BUY (or a HOLD)
without the numbers behind it. Where Phase 55's backtest asks "what would
this have done over that window" and Phase 60's scan asks "which markets
does it suit," this asks "what does it say to do at the newest bar." Built
by one agent (backend engine + routes + migration `0023`) plus one agent
(frontend page + form + table); one migration, no collision.

**(1) The headline signal IS the backtest's, not a second opinion.**
`signals/engine.py::evaluate_current_signal` takes
`generate_signals(bars, definition)[-1]` — the last element of the exact
signal series `backtesting/executor.py` would produce over those same bars,
already carrying the exit-before-entry precedence that module owns. The
per-rule breakdown (`entry_rule_held` / `exit_rule_held`) comes from
`strategies/expressions.py::evaluate_rule`, also unchanged — the module
whose own docstring anticipated this reuse. This file adds exactly one
thing neither has: a human-readable `explanation` naming the concrete
numbers, which is the section-25 requirement. A signal engine that
recomputed the verdict its own way could disagree with a backtest over
identical bars; this one structurally cannot.

**(2) Insufficient data is an answer, persisted, never an exception or a
fabricated HOLD.** A symbol with zero ingested bars, or with fewer than its
indicators need at the latest bar, comes back as a real `SignalEvaluation`
row: `signal="hold"`, `insufficient_data=True`, and an `explanation` naming
how many bars actually exist ("sma_20 (period 20) has no value at the
latest bar (2026-06-30): it needs more than the 12 bars ingested for this
symbol"). The bar count and boundary wording defer to
`marketdata/indicators.py` rather than restating a warmup formula — the
same off-by-one drift `compute_indicator_series` refuses to risk. Same
"never pad, never guess" posture as `expressions.py`'s `None`-means-cannot-
evaluate rule and universe-scan's recorded per-symbol FAILED rows.

**(3) `entry_rule_held` / `exit_rule_held` are THREE-VALUED.** `true`,
`false`, or `null` for "could not be evaluated at that bar" (an operand
still inside its warmup, or a crossing operator with no previous bar).
`null` is deliberately not collapsed to `false`: "the entry condition is
absent" and "we could not tell" are different facts about a strategy
someone may be about to act on, and `schemas_signals.py` states a client
must not render `null` as "no". `insufficient_data` is
`not (exit_held is True or (exit_held is False and entry_held is not None))`
— an exit rule that HOLDS settles the verdict on its own (exit beats
entry), so a SELL stays fully determined even when the entry rule had too
little history; flagging that would grey out a determined exit signal in
the one direction where being wrong leaves an unmanaged position.

**(4) `strategy:signal` is a NEW permission, not `strategy:backtest`.**
First surface since Phase 55 to add one rather than reuse. The reason is on
`Permission.STRATEGY_SIGNAL`: a backtest is a historical what-if; a signal
is a present-tense instruction, and is the input Phase 63's paper-trading
runner will act on each cycle. A role that may study a strategy's past must
not automatically be able to ask what it says to do right now. No ADMIN
override; two-part authorization (router permission + per-request ownership
via the imported `_load_owned_strategy` / `_load_version`) unchanged from
D071. `GET /signal-evaluations/{id}` re-derives ownership by joining
evaluation → version → strategy → owner, never trusting an id to be
unguessable.

**(5) On demand, not scheduled; synchronous in-request.** Each `POST`
evaluates and persists inside the request — at most 50 symbols, each one
indexed read of a few dozen bars plus an in-memory rule evaluation, no
replay, no fills, no capital modelling. There is no recurring job in this
phase; the plan's "first component needing real recurring job scheduling"
is about Phase 63's deployed-strategy runner, which will call this same
engine. `count = _warmup_bar_count(definition) + EVALUATION_BAR_BUFFER`
(5) — the buffer is a cheap over-fetch so a genuine crossing on the newest
bar is never missed for want of one row of context; `_warmup_bar_count`
stays the single definition of warmup, reused not restated.

**(6) The persisted bar store (D070) is the only data source** — never a
live vendor call, exactly as every Strategy Lab surface since Phase 55
requires. `MarketDataStore.get_latest_bars(symbol, bar_interval, count)`
was added: newest-N selection, returned oldest-first (the
`HistoricalBarProvider` contract every evaluator expects), never padded.

**(7) `indicator_values` and `latest_close` as strings, `null` never 0.**
Each declared indicator's value at the latest bar, `"105.50"` not `105.5`,
so no Decimal is routed through a JSON float; `null` where the indicator
has no value yet. `as_of_bar_date` / `latest_close` are `null` only in the
zero-bar case — never back-filled with the request time or a last-known
price (`docs/TRADING_SAFETY.md`).

**(8) No summary/detail split** — unlike every other Strategy Lab listing.
A `BacktestRun` has an equity curve and a trade list to leave out of a
listing; a signal evaluation's whole content is a verdict, a few scalars
and one small indicator map. The `explanation` a summary would drop is the
single most useful field on it, so the list route returns the full shape.
`SignalEvaluationBatchResponse` (the POST's response) has no `limit`/
`offset`: the batch is exactly the symbols the caller asked for, in request
order, nothing paged and nothing omitted.

Alternatives rejected:

- **Reusing `strategy:backtest`.** Rejected in (4) — present-tense
  instruction vs historical study; Phase 63 acts on the former.
- **Collapsing unevaluable rules to `false`.** Rejected in (3) — hides the
  warmup/absent distinction from someone about to trade.
- **Raising / 4xx for a symbol with no bars.** Rejected in (2) — a
  persisted HOLD row naming the gap is the honest answer and keeps the
  other symbols in the batch.
- **A live vendor fetch when the store is short.** Rejected in (6) —
  every other surface reads the store; a signal that silently pulled
  fresher data than a backtest could see would not be checkable against
  one.
- **Recomputing the verdict in the engine.** Rejected in (1) — it could
  disagree with a backtest over identical bars.

Scope discipline: `backtesting/executor.py` / `strategy.py` /
`engine_v2.py` / `expressions.py` and every existing route file are
untouched — the engine imports what it needs and lives in a new
`apps/api/app/signals/` package; `main.py` gains two `include_router`
lines. `MarketDataStore` gains one additive method. Frontend adds one page,
two proxy routes, two components, two test files — no new dependency,
`SideNav` untouched (signals live under a strategy, reached from its detail
page like backtests and scans).

Verification (2026-09-10, isolated Postgres/Redis on remapped ports
55432/56379, freshly migrated from empty):

- `alembic upgrade head` from empty through `0023` clean; `downgrade -1` →
  `upgrade head` round-trips clean.
- **1021 backend tests** (976 → 1021, +45: `tests/signals/test_engine.py`
  — 9 cases plus two parametrized families, 3 in
  `tests/marketdata/test_store.py`, 7 in `tests/api/test_signals.py`);
  `ruff check apps tests migrations` clean; `mypy apps` clean, **134 source
  files** (up from 130); `bash scripts/secret_scan.sh` clean; full run
  16m49s, exit 0. The shared dev stack's stale-broker-row flake
  (D070–D077) does not reproduce on this clean stack.
- **273 frontend tests across 34 files** (259/32 → 273/34; +14 across
  `test/CheckSignalsForm.test.tsx` and `test/SignalTable.test.tsx`),
  `npm run build` clean with the new routes in the manifest. No new
  dependency. (Same slow-machine frontend flake as D077 — run with
  `--maxWorkers=2 --testTimeout=20000`.)

Status: Implemented and verified as above.

---

**D079 — Phase 62: risk / position-sizing v2 — correlation- and volatility-aware portfolio constraints, computed from the bar store, gating the live trade path**

Reason: D029 built the trade-path Portfolio Manager with exactly three
constraints and its own docstring named the reason the rest of spec §18
was deferred: "no persisted price series a covariance or a volatility could
be computed from." Phase 53 (D070) added that series — `market_data_bars`.
Phase 62 closes the two the bar store now supports, `PORTFOLIO_VOLATILITY`
and `POSITION_CORRELATION`, and leaves sector concentration / expected
return / drawdown still deferred (this repo still has no sector data and
makes no return forecast). Built by two agents against a frozen interface
the orchestrator wrote first (`portfolio_manager/models.py` +
`portfolio_manager/manager.py` + `marketdata/portfolio_risk.py`); one agent
did the trade-path wiring + integration tests, the other the unit tests +
docs; no migration, no collision.

**(1) `decide()` stays zero-I/O — the route hands it the finished numbers.**
D029's rejected alternative (f) was "add a market-data dependency inside
the component"; that stays rejected. `decide(proposal, portfolio, limits,
market_risk=None)` gains one optional parameter carrying a pre-computed
`MarketRiskInputs` (per-symbol annualized volatility + pairwise
correlation). `apps/api/app/api/routes/trades.py` reads the trailing year
of daily bars for the proposed symbol and every held symbol
(`marketdata/portfolio_risk.py::load_market_risk_inputs`), computes the
statistics, and passes them in as plain data — exactly where and how it
already reads `recent_orders` and the emergency-stop boolean for the Risk
Engine. The Portfolio Manager remains a pure function with no session, no
clock, no network.

**(2) The two new checks run only when there is a real number for them, and
every backtest is byte-identical.** `market_risk` defaults to `None`;
`backtesting/engine.py` (the one call site the whole backtest subsystem
funnels through — walk-forward, universe scan, robustness and engine_v2 all
reuse `_attempt_trade`) never passes it, so `PORTFOLIO_VOLATILITY` /
`POSITION_CORRELATION` never appear in a backtest decision and no Phase
55–61 result moves. On the trade path each check also requires its
`PortfolioLimits` field (`max_portfolio_volatility_pct` /
`max_position_correlation`) to be set; `config.py` always sets them there.

**(3) Fail-open, audited, on thin history.** When a symbol the check needs
has fewer than `portfolio_market_risk_min_observations` (60) overlapping
daily returns in the trailing `portfolio_market_risk_lookback_days` (365),
the check is recorded as a `PortfolioCheck` with `skipped=True`,
`passed=False`, `worsened_by_trade=False` — it appears in the audit trail
saying exactly which symbol and window fell short, and it does not block
the trade. The three D029 constraints still gate. This is the same
"`docs/TRADING_SAFETY.md` no-fabrication beats a convenient default"
reasoning D029 and D075/D078 already applied: a fabricated covariance
dressed as a real limit is worse than a check that honestly did not run,
and fail-closed would block trading on every symbol not yet backfilled (the
bar store is deliberately sparse, and the market-data vendor is
NOT_CONFIGURED in local dev). `LIVE_TRADING_ENABLED` is false throughout;
this decision would be revisited before it is turned on.

**(4) `PORTFOLIO_VOLATILITY` — projected `sqrt(wᵀ Σ w)`, sized down by
bisection, never blocks a de-risking sell.** Weights `w` are post-trade
market values over (invariant) equity; Σ is built from the annualized
vols and pairwise correlations, ρ_ii = 1. `worsened_by_trade` is true only
for a BUY that raises projected vol above the pre-trade level, so a sell
that lowers book volatility is never refused by the very measure it
improves (the D029 rule). When it binds a MODIFY, the largest integer
quantity keeping projected vol ≤ limit is found by bisection on `[0,
requested]` — projected vol is monotonic in the traded weight over that
range, so no closed-form quadratic solve is attempted and the search is
obviously correct. If any weighted symbol is uncovered, or any needed
pairwise correlation is missing, the whole check skips (3) rather than
treating an unknown as zero covariance.

**(5) `POSITION_CORRELATION` — max pairwise vs held OTHERs, opening-only,
REJECT not MODIFY.** The measure is the largest Pearson correlation
between the proposed symbol and any *other* currently-held symbol.
Correlation does not depend on quantity, so `worsened_by_trade` is true
only when the trade OPENS a new position (`held_quantity == 0`) whose
correlation exceeds the limit; adding to a position already in the book
cannot worsen it (that is the volatility check's job). There is no partial
way to open a less-correlated position, so the cap is 0 — a binding
correlation is a REJECT. With no other holdings the check passes trivially;
with the proposed symbol uncovered or every pair unmeasurable it skips (3).

**(6) No migration.** `orders.portfolio_binding_constraint` is `String(64)`
(migration 0009), so the two new enum values persist as-is; the full
`PortfolioCheck` list (including `skipped`) is in the API response but was
never a persisted column, so nothing schema-level changes. Frontend
`PortfolioVerdict.tsx` / `TradeHistory.tsx` already render the binding
constraint as a raw string with no label map, so the new values surface
with zero frontend change — consistent with how `symbol_concentration`
etc. already display.

**(7) Config: 0.40 annualized book-volatility ceiling, 0.80 max position
correlation, 365-day lookback, 60-observation floor, 252 trading days for
annualization (a constant, not a setting — changing it would silently move
every volatility figure).** A `_enforce_sane_market_risk_settings`
validator rejects a non-positive or out-of-range value at startup rather
than as a 500 from inside the route, matching the file's other
`_enforce_*` validators.

Alternatives rejected:

- **Fail-closed on thin bar history.** Rejected in (3) — blocks trading on
  every un-backfilled symbol; fabrication is the worse failure.
- **Wiring the constraints into the backtest engines now.** Rejected in
  (2) — it would move every existing backtest result and force re-baselining
  Phases 55–61; can be its own phase later if wanted.
- **A market-data fetch inside `decide()`.** Still rejected (D029 (f)) —
  it breaks the zero-I/O property that makes the gate testable.
- **A persisted covariance / correlation table.** Rejected — the numbers
  are a deterministic function of bars already stored; a second copy could
  only drift, the same reasoning D076/D077 gave for not persisting a rank.
- **A naive same-ticker-prefix correlation heuristic.** Already rejected in
  D029 as a fabricated risk number; a real Pearson correlation over
  overlapping daily returns, or an honest skip, is the only option.
- **A closed-form quadratic solve for the volatility MODIFY cap.** Rejected
  in (4) — bisection on integer quantity is exact here and unarguable.

Scope discipline: `risk/engine.py` untouched (this is a second gate it does
not know about). `backtesting/*` untouched — the new parameter is optional
and no backtest caller passes it. `oms/service.py` and `oms/persistence.py`
each gain one optional keyword parameter, threaded to the existing
`portfolio_decide` call. `routes/trades.py` gains the bar read + input
build beside the two reads already there. `core/config.py` gains four
settings + one validator. New file `marketdata/portfolio_risk.py` (pure
stats + a thin async loader). No new dependency, no migration, no frontend
change.

Verification (2026-09-10, isolated Postgres/Redis on remapped ports,
freshly migrated from empty; `alembic downgrade -1` → `upgrade head`
round-trips clean):

- **1050 backend tests** (1021 → 1050, +29 test functions across
  `tests/portfolio_manager/test_manager_market_risk.py` (15),
  `tests/marketdata/test_portfolio_risk.py` (10),
  `tests/api/test_trades_portfolio_market_risk.py` (4), plus one assertion
  updated in `tests/portfolio_manager/test_manager.py` for the grown enum);
  every existing `tests/oms/`, `tests/backtesting/` and `tests/api/test_trades*`
  test unchanged and green. `ruff check apps tests migrations` clean;
  `mypy apps` clean, **135 source files** (up from 134);
  `bash scripts/secret_scan.sh` clean; full run 5m58s, exit 0.
- Frontend unchanged — **273 tests / 34 files**, `npm run build` clean. No
  frontend file was touched (the binding constraint renders as a raw
  string).

Status: Implemented and verified as above.


---

**D081 — Phase 63: paper-trading execution — a validated strategy on a scheduled runner, behind a mandatory human-approval gate**

Reason: the master spec's §25/§52 — a validated strategy is not only something
to backtest and check signals on; it is something you put to work. Phase 61
(D078) turned a strategy into a present-tense verdict for a symbol; this phase
acts on that verdict on a timer. It is the FIRST order-placement path in the
codebase with no HTTP request behind the specific order and no human in the
loop for that specific order — every earlier path (`POST /brokers/{id}/trades`,
the agent-trade endpoint, a backtest replay) had a person or a request per
order. Built by one agent (backend: models + migration `0024` + state machine +
runner + routes + tests) and one agent (frontend: page + proxies + components +
tests); one migration, no collision.

**(1) The mandatory human gate is a column, not a convention.**
`strategy_deployments.status` starts every row at `pending_approval`. The
runner's enumeration query is `WHERE status = 'active'` — a `pending_approval`
row is invisible to it, not merely skipped. The only transition into `active`
is the explicit `POST /deployments/{id}/approve` action, which records
`approved_by_user_id` / `approved_at`. The state machine
(`deployments/service.py`) is `pending_approval → active ⇆ paused`, and any
non-terminal state → `stopped` (terminal); every transition is a
separately-authorized route action and there is no code path that constructs an
`active` deployment directly.

**(2) Approval is split onto its OWN stricter permission.** Creating, pausing,
resuming, stopping, listing and reading a deployment need `strategy:deploy`.
Approving one needs `strategy:approve_deployment` — a caller holding only
`strategy:deploy` gets a 403 on `/approve`. The two come apart deliberately: an
organisation can let a quant author and wire up a deployment while requiring a
second person to authorise it going live, which is exactly the "second set of
eyes before automation trades" §25 asks for. Neither carries an ADMIN override;
ownership is still re-derived per request (deployment → version → strategy →
owner), unchanged from D071.

**(3) `STRATEGY_RUNNER_ENABLED` defaults false, like every other background
loop.** The snapshot scheduler, the reconciler, live trading and the emergency
stop all fail closed the same way. This one is if anything more consequential —
it places real (paper) orders on a timer with no per-order human — so opt-in is
deliberate. Off, the lifespan never starts the task and no deployment is ever
evaluated; a `pending_approval` / `active` row just sits there.
`STRATEGY_RUNNER_INTERVAL_SECONDS` defaults 300 (strategies here evaluate on
daily bars — a shorter interval buys nothing but load); a zero or negative
value fails app startup.

**(4) The runner is a headless backtest replay loop, reusing the sanctioned
pieces and inventing nothing.** Per cycle, per `active` deployment, per symbol:
read the latest bars from the persisted store (`MarketDataStore.get_latest_bars`,
Phase 53 — never a live vendor call, exactly as Phase 61);
`evaluate_current_signal(bars, definition)` — the SAME evaluator
`POST .../signals` uses, so a deployment's action can never disagree with what
the signal route would have said for that bar; if the signal is actionable
given the current position (BUY while flat, SELL while long — the strategy is
long/flat like the backtest executor), size it with `_desired_quantity` /
`_warmup_bar_count` from `engine_v2` (the D072 cross-module-reuse precedent
walk-forward and universe-scan already set) and submit it through
`oms.persistence.submit_trade_and_record` — the one sanctioned RISK → PORTFOLIO
→ BROKER path, unchanged; then persist a real Phase-61 `SignalEvaluation` per
symbol linked to the run. `load_market_risk_inputs` (Phase 62, D079) composes
in on the trade path exactly as `routes/trades.py` does it.

**(5) The single risk-engine resize retry is the exact policy
`backtesting/engine.py::_attempt_trade` applies.** If a submission is REJECTED
solely because of position size and the risk engine reported a
`max_quantity_allowed`, the runner retries once at that quantity — so an
`all_in` deployment sizes into the 10%-of-equity per-trade cap rather than
never trading. A portfolio REJECT is not retried, for the same reason
`_attempt_trade` does not retry one. The engine itself never resizes.

**(6) `require_stop_price=False` (D035): the strategy's exit rule IS its stop.**
A deployment runs under the same `RiskLimits` a backtest of that definition
uses. The exit rule is re-evaluated every cycle; demanding a separate stop
price here would force fabricating one. Everything else in the limits is the
production trade-path value.

**(7) Close-of-bar execution.** The `TradeProposal.estimated_price` and the risk
engine's `now` are both the evaluated bar's timestamp, so the market-data
staleness check measures the trade against the bar it was actually decided on —
the same framing the backtest uses — rather than rejecting every daily-bar
trade as stale.

**(8) The run row always resolves, never stranded (the D072 posture).** One
transaction per deployment. The `strategy_deployment_runs` row is written and
committed first (provisional `failed`), the work runs, the row resolves to
`succeeded` / `failed`; a broad outer `except` rolls back and re-opens a fresh
session to mark it `failed` with the error; one deployment's failure is
returned, not raised, so the rest of the cycle still runs. Run statuses:
`succeeded` / `failed` / `skipped_not_active` / `skipped_emergency_stop` /
`skipped_market_closed` / `skipped_lock_held` — "the cycle ran and placed
nothing because no rule fired" is never indistinguishable from "the cycle did
not run".

**(9) The global emergency stop halts every cycle**, checked before any order; a
stopped cycle writes `skipped_emergency_stop` and places nothing.
`submit_trade_and_record` re-checks it inside the Risk Engine — belt and
braces. A UTC-weekend gate (`MarketHoursGate`, D042) no-ops the whole cycle
before any deployment is enumerated. Cross-worker exclusion is the same
Postgres advisory lock the snapshot scheduler and reconciler use, on this job's
own third objid (`DEPLOYMENT_RUNNER_LOCK_OBJID`) — two workers each running an
`active` deployment would mean two real paper orders where the strategy asked
for one.

**(10) An unpriceable held position FAILS the cycle, visibly.** Before
evaluating, the runner marks every held symbol from its latest ingested bar. A
held symbol with no bar cannot be valued and this system does not fabricate a
price (`docs/TRADING_SAFETY.md`) — that deployment's cycle fails with an
`error_detail` naming the symbol, so an operator notices, rather than the
account being silently mismarked.

**(11) `mode='paper'` only.** `CreateDeploymentRequest.mode` is
`Literal["paper"]`; anything else is a 422 before a row exists. The runner
asserts it and re-asserts it per cycle. The column exists now so Phase 64's
`live` (behind the existing `TRADING_MODE=live` / `LIVE_TRADING_ENABLED` /
live-credential triple gate) needs no migration, and so a reader sees the
distinction was always intended.

**(12) Migration `0024`.** `strategy_deployments` (version / broker
`ON DELETE RESTRICT` — the exact rules a paper position was opened under, and
the account it was opened in, must not vanish from the audit trail;
`requested_by_user_id` / `approved_by_user_id` `SET NULL`) and
`strategy_deployment_runs` (`deployment_id` `CASCADE`). Two existing tables gain
a nullable `deployment_run_id` FK, both `ON DELETE SET NULL`: `orders` (an order
a runner placed is attributable to the cycle that placed it — the `Order`
docstring already anticipated a non-human order path) and `signal_evaluations`
(the runner persists a real Phase-61 row per symbol per cycle; the FK is what
separates a deployment's signal trail from an ad-hoc `POST .../signals` call,
whose column stays NULL). Two new enum types; downgrade drops the FK columns,
then the two tables, then the two enums. Round-trips clean.

Alternatives rejected:

- **Auto-unwinding open positions when a deployment is stopped.** Rejected — a
  stopped deployment's paper positions stay in the broker account exactly as
  they are. Closing them is a separate, deliberate act, not a side effect of
  switching off the automation; conflating the two would make "I stopped the
  runner" silently place sell orders.
- **Per-order human approval on top of the deployment-level gate.** Rejected —
  the approval gate is at the deployment: a human authorises "this validated
  version may trade these symbols on this account on a timer", once. A per-cycle
  prompt would make a scheduled runner pointless. The deployment-level gate plus
  the emergency stop plus pause / stop is the control surface.
- **A cross-user approval workflow** (requester ≠ approver enforced, a request
  queue). Rejected for this phase — the permission split already lets an org
  require a second role; modelling an approval-request object is scope for later
  if it is wanted.
- **`live` mode.** Deferred to Phase 64 — building the paper runner is not
  enabling live automation, the same standing rule Phases 43 / 49 followed.
- **A `SideNav` entry.** Rejected, consistent with Phases 55–62 — deployments
  live under a strategy and are reached by URL / from the strategy page, like
  backtests, scans and signals.
- **Recomputing the verdict in the runner.** Rejected — it reuses
  `evaluate_current_signal` so a deployment's action can never disagree with
  `POST .../signals` over identical bars, the same reasoning D078 (1) gave.

Scope discipline: `signals/engine.py`, `backtesting/*`, `oms/*`, `risk/*`,
`portfolio_manager/*` untouched — the runner imports what it needs and lives in
a new `apps/api/app/deployments/` package; `main.py` gains the lifespan wiring
and three `include_router` lines; `core/config.py` gains four settings + one
validator; `portfolio/cycle_lock.py` gains one objid constant. Frontend adds
one page, eight proxy routes, four components and two test files — no new
dependency, `SideNav` untouched.

Verification (2026-09-10, isolated Postgres/Redis, freshly migrated from empty;
`alembic downgrade -1` → `upgrade head` round-trips clean):

- **1071 backend tests** (1050 → 1071, +21: 9 in `tests/deployments/test_runner.py`,
  7 in `tests/deployments/test_service.py`, 5 in `tests/api/test_deployments.py`);
  `ruff check apps tests migrations` clean; `mypy apps` clean, **140 source
  files** (up from 135); `bash scripts/secret_scan.sh` clean; full run 7m11s,
  exit 0. Migration `0024` round-trips clean.
- **292 frontend tests across 36 files** (273 / 34 → 292 / 36; +19 across
  `test/CreateDeploymentForm.test.tsx` and `test/DeploymentList.test.tsx`),
  `npm run build` clean with the new routes in the manifest. No new dependency.
  (Same slow-machine frontend flake as D077 — run with `--maxWorkers=2
  --testTimeout=20000`.)

Status: Implemented and verified as above.


---

**D082 — Phase 64: broker abstraction + controlled live execution — scaffolding only, the runner refuses every live order unconditionally**

Reason: the roadmap's Phase 64 line calls for "a strategy-scoped live-approval
flow on top of the existing `TRADING_MODE=live` / `LIVE_TRADING_ENABLED` /
live-credential gate." Phase 43 (D058) already built that gate and the
`LiveBrokerAdapter` it protects, for a single interactive trade: a human
calls `POST /brokers/{id}/trades` with `confirm=true` for that one order, and
`trade:submit:live` is required in addition to `trade:submit:paper`. Phase
63's deployment runner has no equivalent of `confirm=true` — it is a
scheduler with no human present at any given cycle — and this project's
non-negotiable rule (docs/TRADING_SAFETY.md: "never enable live trading
without explicit user approval given in that moment") is written for a human
in the moment, not a human who once approved a deployment and then leaves.
So this phase does not attempt to reuse Phase 43's per-order confirmation for
an unattended loop; it builds the data model and permission split a live
deployment needs to exist, and makes the runner refuse to act on one, full
stop, until a future phase deliberately designs and the user explicitly
approves a mechanism actually suited to unattended execution. `live_broker.py`
/ `execution/broker.py` are untouched — there was nothing to reuse, because
the runner never calls them.

**(1) `mode` widens to `"paper" | "live"`, symmetrically validated.**
`CreateDeploymentRequest.mode` was `Literal["paper"]`; it is now
`Literal["paper", "live"]`. `deployments/service.py::create_deployment`
already required a `PAPER` broker for a `paper` deployment (`NOT_A_PAPER_BROKER`);
it now requires a `LIVE` broker for a `live` one (`NOT_A_LIVE_BROKER`), and any
other mode string is `UNSUPPORTED_MODE` before a row exists — the same
guardrail shape as every other create-time check in that function, just
widened rather than replaced. (In passing: `create_deployment` had a latent
bug where the persisted row's `mode` was hardcoded to `"paper"` regardless of
the validated input — harmless while `"paper"` was the only legal value, now
fixed to `mode=mode`.)

**(2) A second permission gates live approval, additively.** `STRATEGY_DEPLOY`
still covers create/list/read/pause/resume/stop for both modes — deploying a
live-mode row is exactly as much of an operational action as deploying a
paper one; nothing about it can trade anything by itself. `STRATEGY_APPROVE_DEPLOYMENT`
still moves any deployment `pending_approval → active`. A new
`STRATEGY_APPROVE_LIVE_DEPLOYMENT` is required *in addition* for a `live`
deployment specifically — checked in the route handler after loading the
deployment (its mode isn't known until then), not at the router-dependency
level. This is deliberately the same "strictly more demanding than either
alone" shape D058 gave `trade:submit:live` on top of `trade:submit:paper`.
Holding the new permission does not, by itself, let anything trade — see (3).

**(3) The runner refuses a live deployment unconditionally, before touching
anything else.** `run_deployment_cycle`'s enumeration query widens from
`mode == 'paper'` to `mode IN ('paper', 'live')` — a live deployment is
enumerated like any other `ACTIVE` row, for the audit trail this decision
cares about (see (4)) — but `_run_deployment_isolated` checks `mode == 'live'`
immediately after re-confirming the deployment is still `ACTIVE`, before the
emergency-stop check, before touching `market_data_bars`, before loading a
broker, before consulting `settings.trading_mode` or
`settings.live_trading_enabled` at all. It writes exactly one row —
`StrategyDeploymentRunStatus.SKIPPED_LIVE_TRADING_DISABLED`, with an
`error_detail` naming the actual reason (no per-trade human, not "disabled by
config") — and returns. This is the load-bearing safety property of this
phase: the refusal is not contingent on any setting staying at its default,
so it cannot be defeated by flipping `LIVE_TRADING_ENABLED=true` for an
unrelated reason (e.g. to let a human place one interactive live trade via
the existing D058 path) and having deployments start firing unattended as a
side effect. Proven by test with `TRADING_MODE=live` and
`LIVE_TRADING_ENABLED=true` both set on the settings object passed to the
cycle — still `SKIPPED_LIVE_TRADING_DISABLED`, still zero orders, still zero
bars read for that deployment's symbols.

**(4) The live deployment is still visible, deliberately.** An alternative
design would filter live deployments out of the enumeration query entirely,
so the runner is structurally blind to them. Rejected: a `live` deployment
that a cycle never even looks at is indistinguishable, from the outside, from
one nobody remembered exists — silence that this project's own
"'no robust strategy found' is a valid, auditable result, never silence"
posture (D076) argues against. Enumerating it and writing an explicit skip
every cycle means an operator who lists a live deployment's runs sees an
honest, continuous record: "the system saw this and correctly refused,"
every single cycle, forever, until a future phase changes that on purpose.

**(5) No migration for `strategy_deployments.mode`** — it was already
`VARCHAR(8)`, wide enough for `"live"`. **One migration (`0025`) for the new
`StrategyDeploymentRunStatus` label** — it is a native Postgres enum, so
Postgres must know the label before the ORM can write it. Same technique as
migration 0014: `op.execute("COMMIT")` before `ALTER TYPE ... ADD VALUE IF
NOT EXISTS`, and a downgrade that rebuilds the type from scratch after
refusing if any row already uses the new label (no `DROP VALUE` exists in
Postgres, and silently rewriting a persisted run outcome to allow a
downgrade would destroy exactly the audit record this decision protects).

**(6) No frontend change.** `CreateDeploymentForm.tsx` still only ever POSTs
`mode: "paper"` and its copy still says so; `DeploymentList.tsx` already
rendered `mode` as a raw string, so a `live` row (created via the API
directly) would display correctly if one existed, but the web app gives no
way to create one. This is a deliberate, separate scope decision from (1)–(4):
the API-level plumbing is real and tested because a future live-execution
phase needs it to already exist and be correct; exposing "create a live
deployment" as a button in this UI, while it still can never actually trade,
would invite exactly the confusion (1)–(4) exist to prevent. Same
"no frontend surface this phase" precedent as D079.

Alternatives considered:
- *Reuse D058's `confirm=true` + `trade:submit:live` machinery inside the
  runner.* Rejected outright — `confirm` is a per-request field an
  interactive HTTP caller sets; a scheduled cycle has no caller and no
  request to carry it. Threading a stored "pre-confirmed" flag through would
  not be "reusing the existing gate," it would be building a new one that
  looks like the old one while removing the one property (a human, right
  now) that makes the old one safe.
- *Gate live execution on `LIVE_TRADING_ENABLED` alone, like the interactive
  path.* Rejected — that setting is a legitimate way to enable a human
  clicking "confirm" on one order at a time; reusing it to also arm every
  `ACTIVE` live deployment's unattended runner would make one config flag do
  two very different jobs, one of which (arming an automated live-trading
  loop) is far more consequential than the other and deserves its own,
  separately-designed control.
- *Don't build Phase 64 at all yet; wait until a real live-execution design
  is ready.* Rejected — the roadmap explicitly scopes this phase as
  scaffolding-only, and the permission split, the symmetric broker-kind
  validation, and the honest audit trail in (4) are real, useful, safe
  groundwork regardless of when (or whether) a future phase adds actual
  unattended live execution on top.

Files: `db/models.py` (`StrategyDeploymentRunStatus.SKIPPED_LIVE_TRADING_DISABLED`,
docstring updates), migration `0025_deployment_run_live_skip.py`,
`auth/permissions.py` (`STRATEGY_APPROVE_LIVE_DEPLOYMENT`),
`api/schemas_deployments.py` (`mode` literal widened),
`deployments/service.py` (`create_deployment`'s mode/broker-kind checks,
the `mode=mode` fix), `api/routes/deployments.py` (the in-handler live-approval
check), `deployments/runner.py` (the unconditional live-mode skip branch,
enumeration query, docstrings). Tests: `tests/deployments/test_service.py`
(+4: live create succeeds pending-approval, live-on-paper-broker and
paper-on-live-broker guardrails, bogus-mode rejection), `tests/deployments/test_runner.py`
(+1: the unconditional-skip proof with a nominally-live-enabled settings
object), `tests/api/test_deployments.py` (+3: missing-live-permission 403,
successful live approval with the permission, the broker-kind guardrail pair
via HTTP; one existing test's stale 422 expectation updated to match the
now-legal `"live"` literal). `tests/deployments/conftest.py` gains an opt-in
LIVE broker in `deployment_world`.

Verification (2026-09-11, isolated Postgres/Redis, freshly migrated from
empty; `alembic downgrade -1` → `upgrade head` round-trips clean for
migration `0025`):

- **1078 backend tests** (1071 → 1078, +7: 3 in `tests/deployments/test_service.py`,
  1 in `tests/deployments/test_runner.py`, 3 in `tests/api/test_deployments.py`;
  one pre-existing `test_deployments.py` assertion updated to match the now-legal
  `"live"` literal); `ruff check apps tests migrations` clean; `mypy apps`
  clean, **140 source files** (unchanged from D081 — no new source file this
  phase); `bash scripts/secret_scan.sh` clean; full run 19m29s, exit 0.
  Migration `0025` round-trips clean.
- Frontend unchanged — no frontend file was touched; `mode` already rendered
  as a raw string.

Status: Implemented and verified as above.

---

**D083 — Phase 65: strategy monitoring — actual (real fills) vs. expected (real backtest), read-only, never blended**

Reason: Phases 63/64 (D081/D082) gave a strategy a way to run unattended and
an honest audit trail of every cycle, but no answer to the question an
operator of a running deployment actually asks: "is this doing what the
backtest said it would?" This phase answers it without placing a single
order or writing a single row — a pure read that joins two things this
codebase already computes honestly (this deployment's own real fills, and
the strategy version's own real backtest) and reports them side by side,
never combined into one number that would misrepresent either.

**(1) "Actual" is scoped to THIS deployment's own orders, never the whole
broker account.** `build_deployment_monitoring` joins `orders` to
`strategy_deployment_runs` and filters to `deployment_id ==
deployment.id` — a broker account can hold positions and orders from other
sources (a human trading the same paper account directly, a different
deployment sharing it), and reporting off the whole account would silently
attribute someone else's trade to this strategy's track record. Only
`FILLED` orders whose `deployment_run_id` traces back to a run of THIS
deployment ever enter the calculation.

**(2) Round trips are built from the FILL's price/time, never
`Order.estimated_price`.** The estimate is what the proposal was priced at
when the Risk Engine evaluated it (the latest bar's close); the fill is
what the paper broker actually executed at — the same distinction
`execution/reconciliation.py` draws for the live path ("a fill is written
only from the broker's own answer"). `_build_round_trips` walks
`(Order, Fill)` pairs — already filtered to `status == FILLED` and ordered
by `submitted_at` ascending by the caller — maintaining one open lot per
symbol from a BUY fill's quantity/price/time, and closing it on the next
SELL fill in the same symbol: `realized_pnl = (exit_price - entry_price) *
quantity`, `return_pct = (exit_price - entry_price) / entry_price * 100`.

**(3) A BUY-while-open or a SELL-while-flat is skipped, never merged.** This
runner's own long-or-flat discipline (`want_buy`/`want_sell` in
`deployments/runner.py`) means neither should ever appear in a real
deployment's order history, but `_build_round_trips` does not assume that
held: if either happens anyway, the offending order is skipped rather than
raising, and it is never averaged into an existing open lot — this system
does not fabricate a cost basis it did not explicitly choose to compute.

**(4) The reference backtest is `_latest_succeeded_backtest`, imported and
reused VERBATIM from `strategies/scoring.py`** — the exact function the
Phase 59 leaderboard uses to define "this version's headline backtest" (the
D072 cross-module private-helper-reuse precedent `deployments/runner.py`
already set). Writing a second, monitoring-specific query — even one that
tried to be smarter about picking a per-symbol run — would create two
competing definitions of "this strategy's backtest" in the same codebase,
and a reader comparing this screen to the leaderboard would have no way to
know they disagreed. `expected.status` is `"no_reference_backtest"` (every
other field `null`) when that function returns `None`, or `"available"`
with `reference_backtest_run_id`/`symbol`/`total_return_pct`/
`max_drawdown_pct`/`win_rate_pct`/`num_trades` copied verbatim from the
`BacktestRun` row otherwise.

**A known, deliberate scope limit, called out rather than glossed over**: a
deployment can trade several symbols, but `_latest_succeeded_backtest`
returns ONE run over ONE symbol. `expected.symbol` names exactly which
symbol the comparison describes; a multi-symbol deployment's `expected` is
not a per-symbol comparison for every symbol it trades, only "what the
version's headline backtest showed for that one symbol." A per-symbol
reference-backtest lookup is future work, not silently pretended to already
exist by omitting the field.

**(5) A rate is `None`, never a fabricated number, at zero round trips.**
`win_rate_pct` and `avg_return_pct` are `None` — not `0%` (reads as "every
trade lost") and not `100%` (reads the opposite way) — when
`num_round_trips == 0`, computed instead only when there is at least one
completed round trip, as `Decimal` arithmetic quantized to four decimal
places with `ROUND_HALF_UP` (the same precision and rounding
`strategies/scoring.py::_quantize` uses, so two percentages from different
modules round identically). `total_realized_pnl` stays `0` with zero round
trips because it is a true sum over an empty set, not a rate — the honest
statement is "no realized P&L yet," not a withheld sentinel.

**(6) No DB writes, no migration.** `apps/api/app/deployments/monitoring.py`
is a new module with four frozen dataclasses (`RoundTrip`,
`DeploymentActualPerformance`, `DeploymentExpectedPerformance`,
`DeploymentMonitoringResult`) and two functions
(`_build_round_trips`, `build_deployment_monitoring`) — no new table, no new
column, nothing appended to any append-only audit trail, because this phase
observes, it does not decide or record anything. The route
(`GET /deployments/{id}/monitoring`) sits on the existing
`deployments_router`, gated by the same `strategy:deploy` permission and
`_load_owned_deployment` ownership check as `.../runs` and `.../signals` —
no new permission, because reading a deployment's own performance is no
more privileged than reading its run history.

Alternatives considered:
- *Compute "actual" from the whole broker account's positions/trades.*
  Rejected — see (1). A shared paper account would make one deployment's
  monitoring page show another deployment's (or a human's) trades as its
  own track record.
- *A new, monitoring-specific backtest lookup that could pick the "best"
  or "most relevant" run per symbol.* Rejected — see (4). Consistency with
  the one existing definition of "this version's backtest" is worth more
  than a theoretically more precise per-symbol number computed a different
  way in a second place.
- *Report `0%` for `win_rate_pct` at zero round trips, matching some
  dashboards' convention of treating an empty rate as zero.* Rejected — see
  (5); this codebase's standing "sentinel, not a guess" rule
  (`docs/TRADING_SAFETY.md`) applies to a missing statistic exactly as it
  does to missing market data.
- *Blend `actual` and `expected` into one combined score or delta field.*
  Rejected — the phase's own goal is to keep them "never blended" so a
  reader always knows which number came from real fills and which came from
  a backtest; a derived delta can be computed by whoever reads the response,
  from two numbers whose provenance stays separately labeled.

Files: `apps/api/app/deployments/monitoring.py` (new — `RoundTrip`,
`_build_round_trips`, `DeploymentActualPerformance`,
`DeploymentExpectedPerformance`, `DeploymentMonitoringResult`,
`build_deployment_monitoring`), `apps/api/app/api/schemas_deployments.py`
(`RoundTripResponse`, `DeploymentActualPerformanceResponse`,
`DeploymentExpectedPerformanceResponse`, `DeploymentMonitoringResponse`),
`apps/api/app/api/routes/deployments.py`
(`GET /deployments/{deployment_id}/monitoring`, docstring update). Tests:
`tests/deployments/test_monitoring.py` (new — unit tests of
`_build_round_trips` plus integration tests of `build_deployment_monitoring`
against real Postgres, reusing `deployment_world`/`run_deployment_cycle`
from `tests/deployments/conftest.py` and `tests/deployments/test_runner.py`'s
buy-then-sell pattern), `tests/api/test_deployments.py` (+1: the monitoring
route's 200 shape plus a 403 for another user's deployment folded into the
existing ownership test). Frontend (built by a second, parallel agent against
this frozen JSON contract, entirely inside `apps/web/`):
`apps/web/app/api/deployments/[deploymentId]/monitoring/route.ts` (new proxy,
mirrors `.../runs`/`.../signals` exactly), `apps/web/components/
DeploymentMonitoringPanel.tsx` (new — fetches its own data on first open,
unlike the sibling run/signal tables, since `/monitoring` isn't part of
`DeploymentList`'s shared `Promise.all` history call; renders the
"Expected"/"Actual" blocks with `—` for null rates and the honest
`no_reference_backtest` message), `apps/web/components/DeploymentList.tsx`
(a third lazy-loaded `<details>` section, "Performance"), `apps/web/test/
DeploymentMonitoringPanel.test.tsx` (new, 4 tests).

Verification (2026-09-11, isolated Postgres/Redis, freshly migrated from
empty; no new migration this phase):

- **1089 backend tests** (1078 → 1089, +11: 10 in
  `tests/deployments/test_monitoring.py` [6 unit + 4 integration], +1 in
  `tests/api/test_deployments.py`); `ruff check apps tests migrations`
  clean; `mypy apps` clean, **141 source files** (up from 140);
  `bash scripts/secret_scan.sh` clean; full run 27m36s, exit 0.
- **296 frontend tests across 37 files** (292 / 36 → 296 / 37; +4 in
  `test/DeploymentMonitoringPanel.test.tsx`), `npm run build` clean with
  `/api/deployments/[deploymentId]/monitoring` in the manifest. No new
  dependency.

Status: Implemented and verified as above.

---

**D084 — Phase 66: drift detection with strategy-scoped auto-pause, opt-in and always audited**

Reason: Phase 65 (D083) gave an operator a read they could open and check by
hand. This phase asks the same question automatically, once per successful
runner cycle, and gives the codebase an honest, append-only memory of every
answer it gave - "checked, found nothing wrong" as much as "checked, found
drift" - plus a strictly opt-in way to act on a bad answer without a human
opening the deployment first.

**(1) The drift signal is win-rate deviation on closed round trips - the
one number Phase 65 already computes honestly on both sides of the
comparison.** `deployments/drift.py::evaluate_deployment_drift` calls
`build_deployment_monitoring` (D083) VERBATIM and compares
`actual.win_rate_pct` to `expected.win_rate_pct` - no second, competing
computation of either number. A signal-distribution comparison (live
signals vs. the backtest's own bar-by-bar signals over the same stretch)
would be a richer drift measure, but the backtest engine
(`backtesting/engine_v2.py`) does not persist a per-bar signal log, only a
`BacktestRun` summary - building that persistence is out of scope for this
phase and is named as a real, deliberate limitation, not glossed over.

**(2) `INSUFFICIENT_DATA` is a first-class verdict, never a guessed
`NO_DRIFT`.** Two conditions produce it: `expected.status !=
"available"` (the strategy version has no reference backtest at all) or
`actual.num_round_trips < strategy_drift_min_round_trips` (default 10 -
judging drift from 2-3 round trips is judging it from noise). In either
case `actual_win_rate_pct` / `expected_win_rate_pct` / `deviation` are all
`None` in the result and in the persisted row - this codebase's standing
"sentinel, not a guess" rule (docs/TRADING_SAFETY.md, and D058/D075's own
applications of it) applied to a missing statistic instead of missing
market data.

**(3) `strategy_drift_max_win_rate_deviation_pct` defaults to 30 percentage
points - deliberately generous.** A live paper-trading sample is smaller
and noisier than the backtest it is compared against; a tight threshold
would flag ordinary small-sample variance as drift and either auto-pause or
loudly flag a deployment that is not actually broken. 30 points is chosen
to catch a deployment that has genuinely diverged from its backtest
(50%+ actual vs. 20% backtest, say) while tolerating the kind of swing a
handful of trades can produce on its own.

**(4) Auto-pause defaults OFF
(`strategy_drift_auto_pause_enabled=false`) - the same fail-closed posture
as every other piece of automation in this codebase** (the snapshot
scheduler, the reconciler, the strategy runner itself, live trading). With
it off, `DRIFT_DETECTED` still writes a row - `action_taken="observed_only"`
- and the deployment keeps running exactly as before; an operator who
agrees pauses it by hand through the existing `POST
/deployments/{id}/pause`. With it on, `_check_and_record_drift`
(`deployments/runner.py`) calls the EXISTING
`deployments/service.py::pause_deployment` - never a reimplementation of
pausing - with a reason that quotes the actual numbers
(`action_taken="paused"`). Either way, this feature can only ever make a
deployment MORE conservative: it never places or sizes a trade differently,
so it does not touch docs/TRADING_SAFETY.md's live-trading gate at all.

**(5) Every check writes a row, every time - `strategy_drift_checks`
follows `emergency_stop_events`' own audit philosophy.** A `NO_DRIFT`
cycle and an `INSUFFICIENT_DATA` cycle write a row exactly like a
`DRIFT_DETECTED` one; a missing row must never be the only evidence a
check happened, and "nothing was wrong this cycle" is itself a fact worth
keeping. The new table is `strategy_deployment_runs`' sibling in shape
(`deployment_id` FK, `ON DELETE CASCADE` matching that table's own FK
behavior, an `(deployment_id, created_at)` index) - this is diagnostic
history scoped to the deployment's own lifecycle, not a result whose
inputs must be pinned independently of it.

**(6) The check runs inside the SAME transaction as the trading cycle it
evaluates, immediately after `_run_one_deployment` returns SUCCEEDED and
before that transaction commits.** No new scheduler, no new advisory-lock
key, no new cross-worker exclusion mechanism - it reuses the runner's own
per-deployment lock and transaction boundary (Phase 63, D081) exactly.
Either both the cycle's trading effects and its drift check persist, or (on
a crash) neither does, matching `runner.py`'s existing all-or-nothing-per-
deployment posture. It never runs for a FAILED, a SKIPPED_*, or a live-mode
cycle (which never reaches SUCCEEDED at all) - only a real SUCCEEDED cycle
has real closed trades worth judging.

Alternatives considered:
- *Run drift-checking on a separate schedule with its own interval and
  advisory lock.* Rejected - see (6). The runner already produces exactly
  one SUCCEEDED cycle per deployment per interval with its own lock and
  transaction; a second scheduler checking the same deployments on a
  different cadence would need to reconcile with the first for no real
  benefit, and would let a drift check and the trading cycle it describes
  land in different transactions, breaking the all-or-nothing guarantee.
- *Default `strategy_drift_auto_pause_enabled` to `true`.* Rejected - see
  (4). Every other piece of unattended automation in this codebase
  (snapshot scheduler, reconciler, strategy runner, live trading) defaults
  fail-closed; a drift feature that defaulted to acting on a deployment
  without an operator's opt-in would be the one exception, for no reason
  strong enough to justify breaking the pattern - even though the action it
  would take (pausing) is a conservative one.
- *Compare signal distributions instead of win rate.* Rejected/deferred -
  see (1). The backtest engine does not persist per-bar signal data to
  compare against; building that persistence is real, additional scope,
  named here rather than silently gapped by pretending win-rate deviation
  is a complete substitute for it.

Files: `apps/api/app/core/config.py` (`strategy_drift_min_round_trips`,
`strategy_drift_max_win_rate_deviation_pct`,
`strategy_drift_auto_pause_enabled`, validator addition),
`apps/api/app/deployments/drift.py` (new - `DriftCheckResult`,
`evaluate_deployment_drift`), `apps/api/app/db/models.py`
(`DriftCheckStatus`, `StrategyDriftCheck`), `migrations/versions/
0026_strategy_drift_checks.py` (new table + `driftcheckstatus` enum),
`apps/api/app/deployments/runner.py` (`_check_and_record_drift`, wired into
`_run_deployment_isolated`'s success path, docstring update),
`apps/api/app/api/routes/deployments.py`
(`GET /deployments/{deployment_id}/drift-checks`, docstring update),
`apps/api/app/api/schemas_deployments.py` (`DriftCheckResponse`,
`ListDriftChecksResponse`). Docs: `docs/TRADING_SAFETY.md` (Phase 66
sub-point under the Phase 63/64 `runner.py` bullet), `docs/API.md` (new
section after `GET /deployments/{deployment_id}/monitoring`),
`docs/IMPLEMENTATION_STATUS.md` (new top `## Completed` entry). Tests:
`tests/deployments/test_drift.py` (new - `evaluate_deployment_drift` against
real Postgres: too-few-round-trips and no-reference-backtest both →
`INSUFFICIENT_DATA`; close win rates → `NO_DRIFT`; far-apart win rates
beyond the configured threshold → `DRIFT_DETECTED`), `tests/deployments/
test_runner.py` (+3: `DRIFT_DETECTED` with auto-pause on flips the
deployment to `PAUSED` with `action_taken == "paused"`; the same scenario
with auto-pause off - the default - leaves it `ACTIVE` with
`action_taken == "observed_only"`; too few round trips writes an
`INSUFFICIENT_DATA` row with `action_taken == "none"` and never touches
deployment status), `tests/api/test_deployments.py` (+1: the drift-checks
route's 200 shape). Frontend (built by a second, parallel agent against this
frozen JSON contract, entirely inside `apps/web/`):
`apps/web/app/api/deployments/[deploymentId]/drift-checks/route.ts` (new
proxy, mirrors `.../runs/route.ts`), `apps/web/components/
DeploymentDriftTable.tsx` (new — prop-fed table like `DeploymentRunTable`;
`—` for null win-rate fields, a visually distinct `"paused"` `action_taken`,
full untruncated `detail` text, a plain empty-state message rather than an
empty table shell), `apps/web/components/DeploymentList.tsx` (a fourth
lazy-loaded `<details>` section, "Drift checks", with its own independent
fetch-on-open — confirmed by test not to also trigger the runs/signals
fetch), `apps/web/components/CreateDeploymentForm.tsx` (the shared
`DriftCheckStatus`/`DriftCheckActionTaken`/`DriftCheckResponse`/
`ListDriftChecksResponse` types, alongside the existing run/signal ones),
`apps/web/test/DeploymentDriftTable.test.tsx` (new, 5 tests),
`apps/web/test/DeploymentList.test.tsx` (+2).

Verification (2026-09-11, isolated Postgres/Redis, freshly migrated from
empty; `alembic downgrade -1` → `upgrade head` round-trips clean for
migration `0026`):

- **1097 backend tests** (1089 → 1097, +8: 4 `test_drift.py`, 3
  `test_runner.py`, 1 `test_deployments.py`); `ruff check apps tests
  migrations` clean; `mypy apps` clean, **142 source files** (up from 141);
  `bash scripts/secret_scan.sh` clean; full run 15m30s, exit 0.
- **303 frontend tests across 38 files** (296 / 37 → 303 / 38; +7: 5 in
  `test/DeploymentDriftTable.test.tsx`, 2 in `test/DeploymentList.test.tsx`),
  `npm run build` clean with `/api/deployments/[deploymentId]/drift-checks`
  in the manifest. No new dependency.

Status: Implemented and verified as above.

---

**D085 — Phase 67: AI strategy research assistant — advisory draft proposal, structurally re-validated, never persisted**

Reason: the Strategy Lab so far requires a person to already know the exact
closed-vocabulary shape of `apps/api/app/strategies/models.py` before they
can write anything down. This phase adds a fifth agent,
`StrategyResearchAssistant`, that turns a short natural-language research
brief ("a mean-reversion idea using RSI") into a candidate
`StrategyDefinition` draft plus a plain-English rationale — advisory input a
person reviews, edits, or discards, exactly the same relationship
`TechnicalAnalyst`'s commentary has to a trade a person or `TraderAgent`
might act on.

**(1) No order or execution path at all — one step further removed from a
trade than any existing agent.** `StrategyProposal` carries no
side/quantity/price/stop field (same as `TechnicalRead`), and
`propose()` never opens a database session, calls a broker, or touches the
Risk Engine. But it is stricter than that: even `TraderAgent`'s output can
become a real order once the Risk Engine approves it, while this agent's
output can only ever become a `StrategyVersion` DRAFT row — which itself
does nothing until a human validates it, deploys it, and the existing
paper-trading runner (Phase 63, D081) executes it behind its own mandatory
approval gate. Three separate human decisions (save the draft, validate it,
approve its deployment) and two deterministic gates (`validate_definition`,
the Risk Engine via the runner's own trade path) sit between this agent's
output and a single order.

**(2) `validate_definition` is reused VERBATIM — never re-specified as a
separate LLM-side check.** The closed vocabulary in
`apps/api/app/strategies/models.py` is the actual safety boundary on this
platform: `apps/api/app/strategies/validation.py`'s own docstring is explicit
that a strategy definition must never become a vector for code execution.
The system prompt states that vocabulary in full (every `IndicatorType`,
every `RuleOperator` with the crossing-vs-instantaneous distinction from that
enum's own docstring, every `PositionSizingType` and its
`REQUIRED_SIZING_KEYS` parameter, built into the prompt programmatically from
those same constants so the prompt cannot silently drift from the
vocabulary it describes) so the model has the best chance of a draft that
validates on the first try — but the prompt is advice to the model, not a
contract this code trusts. Every proposed `definition`, however well the
model followed instructions, is run through the identical
`validate_definition` a human-authored strategy must pass before this agent
hands anything back. This agent must be structurally unable to return
something that looks like a valid `StrategyDefinition` but isn't.

**(3) An invalid draft is a normal, expected result — never an exception,
never a 4xx/5xx at the route.** `validate_definition`'s own docstring already
establishes that an empty `{}` is a legal draft to store but not a valid
definition; the same posture extends here. `StrategyProposal.validation_errors`
carries whatever `validate_definition` returns (empty means valid), and
`propose()` does not raise over a non-empty list — raising would turn an
ordinary "the model's draft has these three problems" into an agent outage,
when a human-authored draft with the same three problems is simply told
`422 {"errors": [...]}"` at `POST .../versions/{id}/validate`. The route
answers the equivalent case with `200 {"is_valid": false, "validation_errors":
[...]}"` for the same reason: this is a normal, informative outcome, not a
failure of the request. `AnalystOutputError` is reserved for a strictly
different failure class — the response could not even be parsed into the
expected `{name, definition, rationale}` shape (unparseable JSON, or a
response missing one of those three fields entirely) — which is a
provider/format failure, matching `TechnicalAnalyst`'s identical
try/except shape and every other analyst sharing that same exception type.

**(4) Nothing is persisted by this endpoint — zero database writes.**
`POST /strategies/research/propose` requests no `AsyncSession` dependency at
all, so there is nothing for it to commit. A `Strategy` and its `StrategyVersion`
are created, exactly as before, only through the existing
`POST /strategies` + `POST .../versions/{id}/validate` path — at which point
`validate_definition` runs again, for real, against the row that will
actually be used. Showing a draft and saving one are deliberately two
different, human-gated acts, continuing the "no code path skips a
deliberate human action" thread D071 and D081 already established for this
initiative: nothing about a plausible-sounding LLM idea should be able to
become a persisted, runnable strategy without a person choosing to put it
there.

**(5) No new permission — `strategy:manage` reused verbatim.** Proposing a
draft for review is a strictly weaker capability than the permission a
caller already needs to save what it proposes (create a `Strategy`); a
separate permission would gate nothing a caller who already holds
`strategy:manage` doesn't already have the stronger ability to do anyway.

Alternatives considered:
- *Auto-create the `Strategy`/`StrategyVersion` row when the proposed draft
  validates.* Rejected — see (4). A human must still deliberately choose to
  save an idea they were only shown; validating cleanly is not the same
  thing as a person wanting it saved, and doing so automatically would be
  the first code path in this whole initiative that skips a deliberate
  human action between "the system produced something plausible" and "a
  runnable row exists."
- *Let the LLM free-write JSON with no closed-vocabulary system prompt and
  rely on `validate_definition` alone to catch problems.* Rejected/inferior,
  not unsafe — see (2). `validate_definition` alone is still a complete
  safety boundary (it is the SAME function either way), so this alternative
  would not have been a security gap. It is worse practically: a
  well-specified prompt materially reduces how often a caller gets back an
  invalid draft and has to re-ask, but the vocabulary reminder is a quality
  optimization, not the safety mechanism — that distinction is the reason
  (2) states the prompt is "advice to the model, not a contract this code
  trusts" rather than treating a good prompt as sufficient on its own.
- *A new, narrower permission for research proposals only.* Rejected — see
  (5). It would gate a capability strictly weaker than one every eligible
  caller already holds, adding a second permission to check for no access
  control it actually changes.

Files: `apps/api/app/agents/strategy_research_assistant.py` (new —
`StrategyProposal`, `StrategyResearchAssistant`,
`build_strategy_research_assistant`; reuses `AnalystOutputError` from
`apps/api/app/agents/technical_analyst.py` verbatim, per that module's own
"shared across the whole analyst layer" docstring), `apps/api/app/main.py`
(`app.state.strategy_research_assistant` wiring + readiness-log entry,
alongside the existing `technical_analyst` lines), `apps/api/app/api/
dependencies.py` (`get_strategy_research_assistant`), `apps/api/app/api/
routes/strategies.py` (`POST /strategies/research/propose`),
`apps/api/app/api/schemas_strategies.py` (`ProposeStrategyRequest`,
`ProposeStrategyResponse`). No new migration — no schema change this phase.
Docs: `docs/AGENT_POLICY.md` (agent list updated), `docs/API.md` (new
section), `docs/IMPLEMENTATION_STATUS.md` (new top `## Completed` entry).
Tests: `tests/agents/test_strategy_research_assistant.py` (new — a
vocabulary-valid draft → empty `validation_errors`; a vocabulary-violating
draft → real itemized errors, no raise; non-JSON response → `AnalystOutputError`;
JSON missing a required top-level field → `AnalystOutputError`;
`build_strategy_research_assistant(None) is None`), `tests/api/
test_strategies.py` (+5: 400 `NOT_CONFIGURED` with no provider wired; 403
without `strategy:manage`; a wired-in stub's valid-draft case is 200
`is_valid: true` and writes no `Strategy` row; its invalid-draft case is
still 200 `is_valid: false` with real errors and also writes no row; a
provider error is 502 `AGENT_OUTPUT_INVALID`). Frontend (built by a second,
parallel agent against this frozen JSON contract, entirely inside
`apps/web/`): `apps/web/app/api/strategies/research/propose/route.ts` (new
POST proxy), `apps/web/components/StrategyResearchAssistant.tsx` (new — the
brief textarea, and all four rendered states: valid draft, invalid-but-
parsed draft with its errors listed verbatim, calm `NOT_CONFIGURED`
messaging, and a distinct 502/thrown-failure message), `apps/web/app/
strategies/research/page.tsx` (new, mirrors `leaderboard/page.tsx`'s thin-
wrapper shape), `apps/web/app/strategies/page.tsx` (a link to the new page),
`apps/web/test/StrategyResearchAssistant.test.tsx` (new, 8 tests).

Verification (2026-09-11, isolated Postgres/Redis, freshly migrated from
empty; no new migration this phase):

- **1109 backend tests** (1097 → 1109, +12: 7 in `tests/agents/
  test_strategy_research_assistant.py` (new file), +5 in `tests/api/
  test_strategies.py`); `ruff check apps tests migrations` clean; `mypy
  apps` clean, **143 source files** (up from 142); `bash
  scripts/secret_scan.sh` clean; full run 19m40s, exit 0.
- **311 frontend tests across 39 files** (303 / 38 → 311 / 39; +8 in
  `test/StrategyResearchAssistant.test.tsx`), `npm run build` clean with
  `/strategies/research` and `/api/strategies/research/propose` in the
  manifest. No new dependency.

Status: Implemented and verified as above.

---

**D086 — Phase 68: final hardening — fuzz-testing, a permission-matrix audit,
one real capped-load test, and closing out the Strategy Lab initiative**

Reason: Phases 53-67 built the Strategy Lab's entire surface — the closed
vocabulary, the evaluator, backtesting (v1 and v2), walk-forward,
robustness, universe scans, the signal engine, the paper-trading runner,
live-execution scaffolding, monitoring, drift detection, and the research
assistant — each verified against well-formed examples as it shipped. This
phase is different in kind from D070-D085: it adds no API surface, no
database schema, and no user-facing capability. It exists to throw
adversarial input at the pieces the rest of the suite never had reason to
misuse, to prove — mechanically, not by inspection — that every mounted
route actually requires authentication, and to run one real test at the
one hard numeric cap (`MAX_SCAN_SYMBOLS`) this initiative has always
described but never load-tested. Where it found real gaps, it closed them;
where it only found what it was designed to prove, it documents that too.

**(1) Fuzz-testing (`tests/strategies/test_fuzz.py`) found and fixed THREE
real crash bugs, not zero.** This matters more than a clean pass would
have: it means the exercise was a real test of the "never raises" claims
`validation.py` and `expressions.py` make of themselves, not a formality.

- **A static no-code-execution guard**, AST-walking `validation.py`,
  `expressions.py`, `perturbation.py`, and `engine_v2.py` for a call to
  `eval`/`exec`/`compile`/`__import__` (as a bare name or an attribute) and
  for an `import`/`from ... import` statement nested inside a function body
  (as opposed to the ordinary module-level imports every one of these files
  legitimately has). This turns each module's own "no code execution,
  ever" docstring claim into an automated regression guard a future edit
  cannot silently violate — grep would catch the same literal names, but
  cannot distinguish a legitimate top-level import from one chosen at
  runtime inside a function, or a name that merely appears in a comment.
  Zero violations found, as expected — this guard exists for the NEXT
  change to one of these files, not this one.
- **`validate_definition` never raises, for any input** — a hand-crafted
  table of 42 adversarial cases (non-dict top-level values, deeply nested
  garbage, huge strings, unicode/control characters, NaN/Infinity as both
  strings and actual floats, negative/zero/huge periods, a dict made to
  contain itself via runtime mutation since dict literal syntax cannot
  self-reference, and every legal top-level key present with a completely
  wrong type). `hypothesis` is not a dependency of this project
  (`pyproject.toml` checked) and none was added for this one table, per the
  phase's own instruction. **This found a real bug**: `_validate_indicators`,
  `_validate_rule`, and `_validate_position_sizing` each checked a
  vocabulary value with `value not in {member.value for member in EnumCls}`
  — a bare `set` membership test, which hashes its argument before
  comparing. An unhashable `value` (a `list`, `dict`, or `set` submitted
  where a string was expected — e.g. `{"op": {"nested": "dict"}}`) raised
  `TypeError: unhashable type` straight out of a function whose entire
  contract is "returns a list of strings, never raises." Fixed with one
  helper, `_in_vocabulary()`, that catches `TypeError` and treats an
  unhashable value as simply not-in-vocabulary — correct as well as
  crash-safe, since an unhashable value could never legally equal one of
  these string members anyway. All three call sites now go through it.
- **The evaluator (`compute_indicator_series` / `evaluate_rule`) never
  raises against a validated definition plus adversarial bar data** — three
  valid definitions (SMA crossing, RSI threshold, notional-sized crossing)
  against an empty bar list, a single bar, bars with a `None` close, bars
  with duplicate timestamps, bars with a negative/zero close, and 3,000
  bars for a wall-clock sanity check (`< 5s`, a generous bound, not a
  benchmark — it passed in a small fraction of that). `Bar.close` is
  Pydantic-validated as `Field(gt=0)`, so a `None`, NaN, or non-positive
  close cannot reach the evaluator through the normal constructor — the
  adversarial cases that need one use `Bar.model_construct()` to bypass
  that validation deliberately, proving the evaluator's OWN defenses hold
  independently of Pydantic's, rather than relying solely on an upstream
  guarantee. **This found two more real bugs**: a `None` close raised
  `TypeError` out of `compute_indicator_series` (`sum(..., Decimal(0))`
  cannot add `None`), and a Decimal `NaN` close raised
  `decimal.InvalidOperation` out of `evaluate_rule`'s ordering comparisons
  (`<`, `<=`, `>`, `>=`) — unlike IEEE-754 float, which quietly returns
  `False` against NaN, Decimal raises. Both are now caught at the point
  they occur and answered with this module's own existing `None` — "cannot
  be evaluated here," exactly like insufficient indicator history — rather
  than propagating an exception type no caller of this "never raises"
  module was expecting.

**(2) The permission-matrix audit is a real, mechanical route walk over
the ACTUAL constructed `app`, not a table transcribed by hand.**
`tests/auth/test_permission_matrix.py` walks `app.routes` down to every
real `fastapi.routing.APIRoute` — recursing through the `_IncludedRouter`
wrapper the installed FastAPI version (0.141.1, well past this project's
`>=0.115` floor) uses for `app.include_router(...)`, via
`.original_router.routes`, since a naive `isinstance(route, APIRoute)`
filter over `app.routes` alone finds NOTHING on this version and would
make the whole test vacuously pass — and for every route not on an
explicit, source-cross-checked public allowlist (`GET /health`,
`GET /health/ready`, `POST /auth/login`, `POST /auth/password-reset/request`,
`POST /auth/password-reset/confirm`), asserts `get_current_user` appears
somewhere in that route's full resolved dependency tree
(`route.dependant`, recursed through every nested `.dependencies` entry —
where FastAPI merges a router-level `dependencies=[Depends(require_permission
(...))]` and a per-parameter `Depends(...)` alike, covering both shapes this
codebase uses without needing to special-case either one). This is what the
phase asked for specifically: a test that would catch a FUTURE route added
with zero auth dependency at all. It is **not**, and its own docstring says
so explicitly, a claim that the permission checked is the CORRECT one for
that route — verifying authorization LOGIC is what each route's own
dedicated tests already do — and it does not reach two in-handler checks
that run AFTER the dependency graph resolves
(`routes/trades.py::_authorize_live_trade`'s `SUBMIT_LIVE_TRADE` check,
`routes/deployments.py::approve_strategy_deployment`'s in-handler
`STRATEGY_APPROVE_LIVE_DEPLOYMENT` check) — both already pass this test via
their own base `Depends(...)`, and both already have dedicated route tests
that verify the permission itself. Found on the FIRST run, over the real
app: zero routes with no auth dependency. **It did find one real, separate
discrepancy while the table in `docs/PERMISSION_MATRIX.md` was being
cross-checked against the route source, not from the automated test
itself**: `Permission.SUBMIT_LIVE_TRADE`'s docstring in
`apps/api/app/auth/permissions.py` still read "Reserved, not enforced
anywhere yet" — true before Phase 43 (D058), false since it: it IS enforced
today, in `routes/trades.py::_authorize_live_trade`. Fixed by rewriting the
docstring to state what the code has done since D058, rather than by
touching the (already correct) enforcement code — the simpler side, per
this phase's own instruction.

**(3) The load test is one real request, at the real cap, timed once — not
a concurrency benchmark, and its own docstring says so.**
`tests/backtesting/test_universe_scan_load.py` runs a real universe scan
over exactly `MAX_SCAN_SYMBOLS` (50) real seeded symbols through the real
HTTP API against real Postgres — same risk engine, same portfolio manager,
same paper-broker fill math as every other backtest in this codebase — and
asserts the whole request completes inside 60 seconds, a deliberately
generous bound chosen the same way `apps/api/app/deployments/service.py`'s
`MAX_DEPLOYMENT_SYMBOLS` docstring frames its own sibling cap: fifty
modest-window, no-vendor-I/O backtests is a few seconds of pure in-memory
Decimal arithmetic, and 60 seconds is headroom over that, not a target. A
second test proves the cap this load test depends on is actually enforced
(`MAX_SCAN_SYMBOLS + 1` real symbols → 422 before any work starts), so the
load test's premise — "50 is where this system draws its own synchronous
line" — is not merely asserted by the first test's parameter choice. This
is deliberately **not** a concurrent-request load test: this codebase has
no async task queue or worker pool for a universe scan to run on
(`MAX_SCAN_SYMBOLS`'s own docstring states the design constraint directly),
so there is nothing to load-test concurrently — one worker handles one
request at a time either way. A future job-runner-backed scan, if this
system ever grows one, would need its own, different load test; this one
proves only what the current, deliberately synchronous design can be
proven to do.

**(4) Closing out the Strategy Lab initiative (Phases 53-68, D070-D086).**
This is the last phase of this initiative. `LIVE_TRADING_ENABLED` stays
`false` on every default and every test, and real, unattended live order
placement remains structurally impossible (`StrategyDeploymentRunStatus.
SKIPPED_LIVE_TRADING_DISABLED`, checked before touching market data, the
broker, or the risk engine, and proven by test even with
`TRADING_MODE=live` / `LIVE_TRADING_ENABLED=true` nominally set) — nothing
in this phase changes that, and nothing in this phase claims the platform
is "production ready" or "fully secure" beyond what was actually verified
here. A short retrospective, naming the handful of decisions across the
whole arc that everything after them actually depended on: **D070** (the
persisted historical bar store) is what every backtest, walk-forward run,
robustness check, universe scan, signal evaluation, and deployment cycle
after it reads from — nothing in this initiative would exist without a
real, queryable OHLCV history to replay. **D072** (letting `engine_v2`
coexist permanently alongside `engine.py` as a sibling, never a
replacement) is the reuse seam every later orchestrator (`universe_scan.py`,
`walk_forward.py`, the deployment runner) was built on top of, rather than
each reimplementing the RISK → PORTFOLIO → BROKER sequence its own way.
**D081** (the mandatory per-deployment human-approval state machine — a
`pending_approval` row the runner's own enumeration query cannot see until
an explicit, separately-permissioned `POST .../approve`) is what makes an
unattended scheduler that places real orders acceptable at all under this
project's non-negotiable safety rules. **D082** (the runner's unconditional
refusal of a `live` deployment, checked before consulting `TRADING_MODE` or
`LIVE_TRADING_ENABLED` at all) is this initiative's single most
safety-critical design choice: it is the reason a future config change made
for an unrelated purpose cannot silently arm unattended live trading as a
side effect, and it is the one property this phase's own hardening work
was most careful not to weaken while proving everything around it.

Alternatives considered:
- *Add `hypothesis` for true property-based fuzzing.* Rejected per the
  phase's own instruction — it is not currently a dependency, and adding
  one for a single test file was explicitly out of scope. The hand-crafted
  34-case table found three real bugs without it; a property-based version
  remains a reasonable future addition if this module's surface grows.
- *Have the permission-matrix test re-verify which specific permission each
  route requires, not just that some auth dependency exists.* Rejected —
  that is a claim about authorization LOGIC, which is what each route's own
  dedicated tests already prove, in detail, per-permission. Conflating the
  two would make this test either redundant with dozens of existing tests
  or, worse, a second, weaker copy of what they already check that could
  drift out of agreement with them.
- *Load-test universe scans concurrently, simulating several callers at
  once.* Rejected — see (3). There is no worker pool or task queue for
  concurrent requests to actually exercise differently from one at a time;
  building a concurrency harness would exercise the ASGI server's own
  request handling, not anything specific to this codebase's universe-scan
  orchestrator.
- *Silently patch the three bugs fuzzing found without naming them as
  bugs.* Rejected — a hardening phase whose own fuzz test found real
  crashes and did not say so in the record would undermine the entire
  point of writing the test: proving the property, including proving it
  was previously false.

Files: `apps/api/app/strategies/validation.py` (`_in_vocabulary()` helper;
the three vocabulary-check call sites in `_validate_indicators`,
`_validate_rule`, `_validate_position_sizing` now route through it),
`apps/api/app/strategies/expressions.py` (module docstring addition;
`compute_indicator_series` catches `TypeError` alongside
`InsufficientDataError`; `evaluate_rule`'s instantaneous and crossing
branches both catch `decimal.InvalidOperation` around their comparisons),
`apps/api/app/auth/permissions.py` (`SUBMIT_LIVE_TRADE`'s stale docstring
rewritten to match `routes/trades.py::_authorize_live_trade`'s real,
D058-era enforcement). New tests: `tests/strategies/test_fuzz.py` (new — the
AST guard, 34 adversarial `validate_definition` cases, and the evaluator's
bar-data edge cases), `tests/auth/test_permission_matrix.py` (new — the
route-walk auth-dependency guard, the allowlist's own two-way sanity check,
and a permission-enum-member-is-referenced-somewhere sanity check),
`tests/backtesting/test_universe_scan_load.py` (new — the capped-load test
and its cap-is-enforced companion). Docs: `docs/PERMISSION_MATRIX.md` (new —
all 10 `Permission` values, their routes, and their ADMIN-override status,
linked from `docs/TRADING_SAFETY.md`), `docs/TRADING_SAFETY.md` (link to
the new permission matrix), `docs/IMPLEMENTATION_STATUS.md` (new top
`## Completed` entry, plus the closing note that Phases 53-68 are now
complete).

This phase's own isolated-environment check (ports 55451/56401, freshly
migrated from empty): `alembic upgrade head` confirms no new migration is
needed — the schema is already at `0026` from Phase 66; the three new test
files (69 tests: `test_fuzz.py`, `test_permission_matrix.py`,
`test_universe_scan_load.py`) pass, along with the full targeted run of
every module touched or exercised by this phase's fixes (`tests/strategies`,
`tests/backtesting`, `tests/signals`, `tests/deployments`, `tests/auth`,
plus the relevant `tests/api/test_*` strategy-lab modules — 465 tests, all
passing); `ruff check apps tests migrations` and `mypy apps` (143 source
files, unchanged from Phase 67 — no new source file under `apps/`) both
clean; `bash scripts/secret_scan.sh` clean.

Verification (2026-09-11, isolated Postgres/Redis, freshly migrated from
empty; confirmed at head `0026`, no new migration this phase — the final
full-suite regression for the entire Phase 53-68 initiative):

- **1178 backend tests** (1109 → 1178, +69: `tests/strategies/test_fuzz.py`,
  `tests/auth/test_permission_matrix.py`,
  `tests/backtesting/test_universe_scan_load.py`); `ruff check apps tests
  migrations` clean; `mypy apps` clean, **143 source files** (unchanged from
  Phase 67 — no new `apps/` source file this phase, only fixes to existing
  ones); `bash scripts/secret_scan.sh` clean; full run 23m51s, exit 0.
- Frontend unaffected — **311 tests across 39 files**, unchanged from Phase
  67; no frontend file was touched this phase.

This is the final verification pass of the Strategy Lab initiative
(Phases 53-68, D070-D086): 1178 backend tests and 311 frontend tests, all
green, on a single fresh isolated database, with `LIVE_TRADING_ENABLED`
still `false` and real live order placement still structurally impossible
(D082/D086).

Status: Implemented and verified as above.

---

## D087 — Phase 69: unattended live execution, armed by one dedicated switch and bounded by capital controls

**Supersedes the operative half of D082.** D082's reasoning is preserved
there and is still correct about the problem; what changed is that the
problem now has a solution D082 did not have.

### 1. What D082 actually decided, and why it is being revisited

Phase 64 built the live-deployment data model and permissions, then made
the runner refuse every `mode='live'` cycle **unconditionally** - writing
`SKIPPED_LIVE_TRADING_DISABLED` before touching market data, the broker, or
the risk engine, and deliberately without consulting `TRADING_MODE` /
`LIVE_TRADING_ENABLED`, so flipping those for an unrelated reason could not
arm a robot.

The stated reason was that `docs/TRADING_SAFETY.md` requires explicit,
in-the-moment human approval for every live trade, and a scheduler
structurally cannot supply one. That is true, and D087 does not pretend
otherwise. What D082 got wrong was the conclusion it drew: it treated
"cannot approve per order" as "cannot be approved at all," when the real
gap was that **no switch existed by which an operator could state the
intent unambiguously**, so refusing was the only honest option available.

D087 builds that switch. The approval is still explicit - it is given once,
over a mandate bounded by numbers the operator has to choose, rather than
once per order.

### 2. The third key, and why it is not a reuse of the two that exist

`STRATEGY_LIVE_AUTO_EXECUTION_ENABLED` (default `false`) is a **third**
independent setting, on top of `TRADING_MODE=live` and
`LIVE_TRADING_ENABLED=true`.

Reusing the existing two was the obvious shortcut and is wrong. Those two
arm the *interactive* live path (D058), where a human supplies
`confirm: true` for one specific order. A person who enables them to place
a single live trade by hand must not thereby, silently, also start an
unsupervised robot trading their account on a timer. Those are different
decisions with different risk profiles, and they get different switches.

Same "strictly more demanding" direction D058 chose when it required
`trade:submit:live` **in addition to** `trade:submit:paper`.

A dedicated test holds the line:
`test_a_live_deployment_is_still_skipped_with_interactive_live_trading_on`
turns both D058 keys on, leaves the robot switch off, and asserts the cycle
is still skipped with zero orders and zero market-data reads. That test is
D082's own test, rewritten: its assertions are unchanged, because the
property it protects is the one that still matters most.

### 3. Capital controls, and why the per-trade limit is a cap not an override

The operator supplies two numbers. Neither has a default; there is
deliberately no "unlimited" value for either, and the app refuses to start
if the robot is armed without both.

- `STRATEGY_LIVE_TOTAL_CAPITAL` bounds total deployed cost basis.
- `STRATEGY_LIVE_CAPITAL_PER_TRADE` bounds one entry.

The per-trade number is applied as a **cap on** the strategy's own
`position_sizing`, not a replacement for it: the runner sizes what the
strategy asked for, then takes the smaller of that and the cap. Overriding
outright was considered and rejected - it would mean raising this number
could silently *increase* a conservative strategy's position size, the
wrong direction for a control whose entire job is bounding exposure. The
cap is floored to whole shares with `ROUND_FLOOR` against the real trade
price, because rounding up would commit more than the operator allowed, and
that is the one direction a cap must never fail in.

Both figures are recomputed every cycle from this deployment's **own real
fills** (joined through `Order.deployment_run_id`, the same query shape
D083's monitoring uses), never from a remembered running total and never
from the whole brokerage account - which may hold positions a human opened
by hand or another deployment owns.

**Scope: per deployment, not per account - stated explicitly because the
first draft of the documentation got this wrong.** The capital ceiling and
both loss breakers are evaluated against ONE deployment's own book, so N
active live deployments can commit up to N x `STRATEGY_LIVE_TOTAL_CAPITAL`.
Summing across deployments was considered and is genuine future work; it
was not done here because the clean attribution stops at
`deployment_run_id`, and aggregating would have meant either re-deriving
lots across deployments that may trade the same symbol (where one shared
per-symbol lot walk would mis-attribute entries between them) or reading
the whole brokerage account, which includes positions this system did not
open and must not claim. Documented loudly in `config.py`,
`docs/TRADING_SAFETY.md` and `docs/LIVE_AUTO_TRADING.md` rather than left
for an operator to discover by arithmetic.

### 4. Circuit breakers: pause, never liquidate

`STRATEGY_LIVE_MAX_DAILY_LOSS_PCT` (default 3) and
`STRATEGY_LIVE_MAX_TOTAL_LOSS_PCT` (default 10) halt the runner and
**pause** the deployment through the existing `pause_deployment()` - the
same "reuse the one implementation" choice D084's drift auto-pause made, so
a breaker-paused deployment is indistinguishable in state and in the API
from a human-paused one, and is resumed the same way.

**They never liquidate.** Auto-selling into whatever is happening on the day
a loss breaker fires is how a bad hour gets converted into a realized loss
at the worst price on offer. The positions stay, the robot stops opening new
ones, and a human decides. Re-arming is a deliberate human action, which is
the difference between a breaker and a filter. Asserted by test: after a
halt, zero SELL orders exist.

Unlike D084's drift auto-pause, these default **ON** rather than off. D084's
standing rule that automation stays opt-in applies to automation that
*acts*; these breakers only ever *stop* the robot, and an armed live robot
with its loss limits disabled is not a configuration worth making easy.

### 5. Total loss, not drawdown - named for what it measures

The slower breaker was initially drafted as a drawdown-from-peak limit and
deliberately renamed. A true drawdown breaker needs a stored high-water mark
of equity over time, and this system persists no such series for live
deployments. Computing a peak from whatever history happens to be queryable
would produce a threshold that silently drifts as old rows age out - a
breaker whose trigger point moves is worse than one that measures something
simpler and says so in its name.

### 6. The daily window is conservative, on purpose

`realized_pnl_today` counts round trips closed today; `unrealized_pnl`
counts every open lot regardless of when it was opened. Their sum therefore
charges a multi-day open loss against today's breaker on every day it
persists, so the breaker fires earlier than a strict day-over-day measure
would.

The strict measure needs a start-of-day equity snapshot that does not exist
for live deployments. Between a breaker that fires somewhat early and one
that needs a number this system cannot honestly produce, a control whose job
is stopping losses should err toward firing early - and should say so rather
than implying a precision it does not have.

### 7. An unpriceable position halts but does not pause

If a held symbol has no ingested bar, `_unrealized` returns `None` - never a
partial sum, which would understate the loss in exactly the direction that
keeps the robot trading. The cycle halts.

It does **not** pause the deployment, unlike a loss breach. A missing bar is
an ingestion gap, not a loss event; pausing on it would disguise a stale
data feed as a risk decision and send an operator looking in the wrong
place. Mirrors how the paper path already fails visibly on an unpriceable
held position rather than marking it at cost.

### 8. Alternatives considered

- **Leave D082's refusal in place and decline the request.** Rejected: the
  refusal was a consequence of a missing mechanism, not a standing judgement
  that the mechanism must never exist, and the user owns the platform, the
  account and the capital.
- **Reuse `LIVE_TRADING_ENABLED` alone.** Rejected - §2.
- **Per-cycle confirmation via a notification the operator answers.**
  Rejected for this phase: it reintroduces a human into a loop whose whole
  purpose is running unattended, and a confirmation prompt that is always
  answered "yes" is a worse control than an honest capital bound, because it
  looks like supervision without providing any.
- **Auto-liquidate on a breaker.** Rejected - §4.
- **Override strategy sizing with `capital_per_trade`.** Rejected - §3.
- **A drawdown-from-peak breaker.** Rejected as unimplementable honestly
  without a persisted equity series - §5.

### 9. What this phase did NOT do

It did not enable live trading. `LIVE_TRADING_ENABLED` and
`STRATEGY_LIVE_AUTO_EXECUTION_ENABLED` are both `false` in every default and
every test in this repository, no live credentials were handled, and no live
order was placed at any point during this phase's development or
verification. Arming is an act the operator performs in their own
environment.

It also did not make any strategy profitable. Every control here bounds loss
and speed; none is a view on whether running a given strategy on real money
is wise.

### A note on the risk limits, recorded because it was nearly a silent bug

The first draft swapped the runner's `_risk_limits(settings)` for
`build_risk_limits(settings, live=...)` wholesale. Those two are not
equivalent: `build_risk_limits` honours `risk_require_stop_price` (default
**true**), while `_risk_limits` forces it `false`. A deployed strategy
carries no stop price - its exit rule is its exit (D035) - so the swap would
have made the Risk Engine reject **every** automated entry, paper and live
alike. Caught by an existing paper test before it went anywhere.

`_deployment_risk_limits` now returns `_risk_limits(settings)` byte-for-byte
for paper (so every pre-existing paper test and backtest comparison stays
valid) and D058's tighter `live_risk_*` limits with `require_stop_price`
re-cleared for live.

### Files

- `apps/api/app/deployments/live_guard.py` (new) - `evaluate_live_arming`,
  `evaluate_live_capital`, `live_entry_budget`.
- `apps/api/app/deployments/runner.py` - arming gate replaces the
  unconditional refusal; live broker selection; capital breakers; per-trade
  cap at the sizing site; `_deployment_risk_limits`; `save_paper_broker`
  skipped for live.
- `apps/api/app/deployments/monitoring.py` - `_build_round_trips` now
  returns `OpenLot` (quantity + real entry price/time) instead of bare
  quantities; Phase 65's reported API shape is unchanged.
- `apps/api/app/core/config.py` - six new settings plus
  `_enforce_live_auto_execution_is_fully_configured`.
- `apps/api/app/db/models.py` - `SKIPPED_LIVE_RISK_HALT`; D082 status
  docstring corrected from "unconditional" to "conditional on the switch".
- `migrations/versions/0027_deployment_run_live_risk_halt.py` (new).
- `docs/TRADING_SAFETY.md`, `docs/IMPLEMENTATION_STATUS.md`.
- Tests: `tests/deployments/test_live_guard.py` (new),
  `tests/deployments/test_runner_live.py` (new),
  `tests/core/test_live_auto_execution_settings.py` (new),
  `tests/deployments/test_runner.py` (D082 test rewritten),
  `tests/deployments/test_monitoring.py` (OpenLot assertions),
  `tests/deployments/conftest.py` (live-broker teardown now cleans orders).

### Verification

2026-09-11, isolated Postgres/Redis (timescaledb 2.15.3-pg16 + redis:7),
freshly migrated from empty, confirmed at head `0027` with no drift:

- **1215 backend tests** (1178 → 1215, +37: `tests/deployments/test_live_guard.py`
   +13, `tests/deployments/test_runner_live.py` +6,
  `tests/core/test_live_auto_execution_settings.py` +18), full run 9m11s,
  exit 0.
- **312 frontend tests across 39 files** (311 → 312), `npm run build` clean.
- `ruff check apps tests migrations` clean; `mypy apps` clean, **144 source
  files** (143 → 144, the new `live_guard.py`); `bash scripts/secret_scan.sh`
  clean.
- `LIVE_TRADING_ENABLED` and `STRATEGY_LIVE_AUTO_EXECUTION_ENABLED` both
  `false` in every default and every test in the run above. No live
  credential was present or handled; no live order was placed. The armed
  live path is exercised in tests against an in-memory
  `PaperBrokerAdapter` substituted for the only function that can build a
  real one (`tests/deployments/test_runner_live.py` documents why).

**A note on two failures seen during development, both bogus.** An
intermediate run reported 2 failures in
`tests/api/test_snapshot_scheduler_market_hours.py`, which asserts a GLOBAL
`captured_count == 1` against the shared test database. Cause: an earlier
failing test in this phase's own development aborted
`deployment_world`'s teardown mid-way (the live-broker cleanup did not yet
delete the orders an ARMED live deployment now really places - fixed in
`conftest.py`), orphaning `Broker` rows that the snapshot scheduler then
counted. Reproduced in isolation on the polluted database, and absent on
every fresh one. Not a regression, but a reminder that a global-count
assertion against a shared database reports someone else's leftovers as its
own failure, and that a cross-module failure should be re-checked on a fresh
container before it is believed.

Status: Implemented and verified.

---

## D088 — Phase 70: transaction costs, two more indicators, an intraday bar vocabulary, and a chart of our own bars

The first vertical slice through the BTC/USD strategy work: one change at
each layer — ingestion, indicators, the backtest engine, the API, the UI —
chosen so the whole pipeline is exercised end to end rather than one layer
being built out ahead of the rest.

### 1. Backtests modelled no transaction cost at all, and that was the most important thing wrong with them

Before this phase, a case-insensitive grep for `slippage`, `commission` or
`fee` across the whole `apps/api/app/backtesting/` package returned **zero
matches**. Every backtest figure this platform had ever produced described a
market with no fees, no spread and no slippage — fills at the bar's close,
exactly, on both sides.

That is a mild distortion for a strategy that trades twice a year and a
decisive one for a strategy that trades several times a day, which is
precisely what the BTC/USD spec asks for. More to the point, it distorts in
the **flattering** direction: a frictionless engine will always report a
better result than reality, and a round trip that broke even graded as
neither a win nor a loss when it was in fact a loss.

`apps/api/app/backtesting/costs.py` adds a `CostModel` with two rates in
basis points, and `engine_v2` applies it to every simulated fill.

**Costs are applied to the execution PRICE, not deducted from cash.** A buy
fills above the bar's close and a sell below it. This is not an
approximation — it is an identity: a fee of `bps` on notional is
`quantity * price * bps / 10_000`, and moving the price by
`price * bps / 10_000` moves the notional by exactly the same amount. So
one adjusted price gives the account precisely the cash outcome that
slipping the price and then deducting a fee separately would, while keeping
all the arithmetic inside the existing `PaperBrokerAdapter` fill path
rather than requiring a new way to remove money from a broker.
`tests/backtesting/test_costs.py` asserts that identity over five price
scales, with exact Decimal equality rather than a tolerance.

**Sizing is computed against the costed price.** This is not a refinement,
it is what keeps the feature from silently breaking entries: an `all_in`
entry sized against the raw close would propose a notional larger than the
account's cash once costs land, and the paper broker would refuse the whole
order with `InsufficientFundsError`. A costed backtest would then trade
strictly less often than an uncosted one, for a reason that has nothing to
do with costs.

**Fees and slippage are reported separately** even though they are
interchangeable in the arithmetic, because they are not interchangeable in
what an operator can do about them: a fee is a venue's published schedule,
negotiable or reducible by trading less often, while slippage is a property
of liquidity and order size. Collapsing them into one figure would hide
which one is eating the strategy.

**The defaults are deliberately non-zero** (10 bps fee, 5 bps slippage). A
zero default would let the single most optimistic assumption available be
the one nobody had to choose. They are a plausible retail crypto taker
schedule and a starting point an operator must replace with their venue's
real one — not a figure this system claims is accurate for any broker.
`CostModel.frictionless()` exists and is legitimate to pass; what it is not
is something a caller can reach by forgetting, which is why `cost_model` is
a required keyword argument on `run_strategy_backtest` with no default.

**This is a simplification and says so.** Real slippage scales with order
size against available depth and widens in fast markets; a flat bps figure
models none of that. Funding costs on a perpetual and borrow costs on a
short are not modelled at all, because this engine is long-only and
cash-settled and neither has anything to attach to. What the model buys is
that a backtest is now **pessimistic by default rather than silently
perfect**. It does not make a backtest accurate.

### 2. The four cost columns are nullable, and NULL does not mean zero

Migration 0028 adds `fee_bps`, `slippage_bps`, `total_fees` and
`total_slippage` to `backtest_runs`, all nullable, with no server default
and no data migration.

Runs created before this phase genuinely executed with no cost model.
Writing `0` into their `fee_bps` would assert they were run under a
zero-fee assumption — a claim about a choice nobody made, and exactly the
kind of plausible-looking fabricated figure `docs/TRADING_SAFETY.md`
forbids. NULL means "this run predates cost modelling; its returns are
frictionless and its costs are unknown," which is a different and more
honest statement. The UI renders it as such rather than as `0`.

**The two rate columns are written when the row is created, not on
success.** A FAILED run is still a record of what was attempted, and the
assumptions it was attempted under are part of that. It also means an
operator editing a rate mid-flight cannot change the meaning of a run
already in progress.

**The rates are stored on the row rather than read back from settings.** A
return figure without its cost assumption is not a reproducible result —
the setting behind it can be edited afterwards, silently moving what the
number means. A stored result must stay self-describing.

### 3. EMA and ATR, and why those two rather than any others

The vocabulary was `sma` and `rsi`, which is a real constraint on what a
strategy can express. Two were added:

- **EMA** is the spec's trend core (20/50/200) and the smallest honest step
  past SMA. Seeded from the SMA of the first `period` closes — an EMA
  seeded from a single close depends heavily on where the caller started
  the series, so two callers passing different amounts of history would get
  different "EMA(20)"s for the same bar.
- **ATR** is what makes a **stop-loss possible at all**. The spec wants
  stop-loss, take-profit and volatility-based sizing; the platform has none
  of those, and every deployment runs with `require_stop_price=False`. ATR
  is the prerequisite, not a fourth moving average.

Both are the **simple-average** variants, matching the choice `rsi`'s
docstring already documents, rather than Wilder's smoothing. One convention
across the module, stated rather than inherited from whichever platform a
reader happens to know — these are not claimed to match any particular
vendor's chart.

**ATR is the first indicator that reads `high` and `low`,** and both
columns are nullable. An ATR over a close-only bar raises
`InsufficientDataError` — the same error, not a second one meaning the same
thing — which `expressions.py` already records as `None`, so the rule
simply does not fire. The only alternative is estimating a day's range from
its close, which is fabricating market data. `CLOSE_ONLY_INDICATORS` in
`strategies/models.py` is what makes the dispatch a set membership rather
than an `if` on ATR specifically.

**A test-hygiene finding worth recording.** Four existing tests used `ema`
as their example of an *unknown* indicator type. Implementing EMA made all
four pass for the wrong reason — nothing raised, because nothing was
unknown any more. Each now uses a name nobody is likely to implement, with
a comment saying why: what those tests assert is the **refusal of an
unknown type**, not any particular type's absence, and adding a real
indicator must never silently disarm them again.

### 4. `bar_interval` becomes one shared closed vocabulary, widened past `1d`

Six request schemas each carried their own `Literal["1d"]`. Their stated
reason was sound while `1d` was the only interval anything ingested:
accepting `'1h'` would produce a run that silently found no bars.

That reason no longer holds, because the thing it was a proxy for is now
handled properly: `_load_warmup_and_window` raises a specific
`InsufficientHistoryError` naming the real numbers, and
`run_strategy_backtest` persists that as a FAILED run carrying the message.
A request for an interval with no ingested bars is a **data** question with
a real, auditable answer — strictly more informative than a 422 on the
interval itself.

`BarInterval` (`marketdata/bar_provider.py`) is now the single definition,
imported by all six. It stays CLOSED because `market_data_bars.bar_interval`
is a plain string column (migration 0016, deliberately not a Postgres enum
so adding an interval needs no `ALTER TYPE`) — which by itself would accept
`"1 day"`, `"daily"` and `"1D"` as three intervals that never match each
other on read.

`LongbridgeBarBackfillProvider` maps each interval to the SDK's `Period`
enum by **attribute name resolved through `getattr`**, not by holding real
enum members: `longport.openapi` is imported lazily inside the methods that
need it, never at module level, so that tests and every non-Longbridge path
run without the vendor package installed. An unmappable interval **raises**;
falling back to `Period.Day` would persist daily bars under a
`bar_interval` of `"5m"`, and nothing downstream could ever detect it.

**Verification status, stated plainly.** Only the `Day` mapping has made a
real round trip — it is the one Phase 53 shipped. The five intraday
mappings are correct by direct introspection of the installed SDK's
`Period` enum and are covered by tests against a stub client, which proves
this adapter asks for the right period. It does **not** prove the vendor
returns intraday candles for any particular symbol or entitlement: the
Longbridge token available while this phase was written was expired
(`401103 token is expired`), so no live intraday call was possible. The
first real intraday backfill is what would establish that, and until one
runs this is documented as unverified rather than described as working.

### 5. `lightweight-charts`, and why not TradingView's free widget

The request was for TradingView-style charts with this platform's signals
marked on them. The free TradingView **embed widget** cannot do the second
half: it renders TradingView's own data inside a sealed cross-origin
iframe, with no supported way to hand it a bar series and no way to draw a
marker.

That is not merely an API limitation, it is a correctness problem. A marker
on someone else's bars would sit on a bar the backtest never saw — two
feeds can and do differ on exact OHLC — so the chart would be asserting
something about when the strategy acted that is not quite true.

`lightweight-charts` is the same rendering engine, Apache-2.0 licensed,
published by TradingView, and it takes the caller's series. Every bar it
draws comes from `market_data_bars` through the same `MarketDataStore` the
backtest engine reads, over the run's own `[start_date, end_date]`.

Three honesty rules in the component, each tested:

- **A bar with no high/low is skipped, never completed from its close,** and
  the skipped count is disclosed on the page. Filling those from the close
  would draw a doji asserting the market opened and closed at one price and
  never moved.
- **A signal whose bar is not in the loaded window is dropped, never snapped
  to the nearest bar.** A marker on a bar the trade did not happen on is a
  plausible-looking lie; an absent marker is merely absent.
- **An empty result is a message, not an empty frame** — and it distinguishes
  "no bars are ingested, backfill them" from "these bars are close-only",
  which call for different actions.

`GET /market-data/{symbol}/bars` **never contacts a vendor and never
ingests.** An un-backfilled symbol returns 200 with an empty list, not a
404 and not a silent backfill: reading is a read, and ingestion stays an
explicit ADMIN-gated action, so a chart request can never spend a vendor
quota or write rows. A result above `MAX_BARS` is **refused with a 422
rather than truncated** — a truncated series draws a chart that looks
complete while ending mid-window, and a reader has no way to tell.

### 6. A test-environment finding, recorded because it nearly shipped

The canvas stub `lightweight-charts` needs under jsdom was first added to
the shared `test/setup.ts`, where it broke five unrelated suites: Recharts
measures text through `ctx.measureText(...).width`, and a global stub
answering every method with `undefined` made every Recharts-based chart
test time out. The stub now lives beside the one component that needs it.
An environment stub that changes how *other* components behave under test
is not a stub, it is a mutation of the test environment.

Status: Implemented. Cost model, indicators, vocabulary and chart verified
by test against real Postgres and a real browser DOM; the intraday vendor
round trip is unverified, for the reason stated in section 4.

---

## D089 — Phase 71: a second market-data vendor, and three limits that made BTC unreachable

The BTC/USD brief could not be started, and the reason was not the one
anyone expected. Four separate things had to be true before a single
honest number existed, and each of the first three produced a
plausible-looking wrong answer rather than an error.

### 1. Longbridge has no spot BTC instrument, and no amount of re-authorizing changes that

`BTCUSD.BKKT` answers `301600 invalid symbol`. `.HAS` and `.OSL` crypto
symbols return nothing. Authentication is fine throughout — live `AAPL.US`
quotes and 5-minute candles both work on the same credentials in the same
session.

This matters because the obvious diagnosis was wrong for weeks: an earlier
`401103 token is expired` made it look like a credentials problem, and a
credentials problem is the kind that gets fixed by reconnecting. It is an
ENTITLEMENT fact. The account can price US equities and the Bitcoin
ETFs — IBIT, GBTC, BITO, MSTR, and `BTC.US` (Grayscale Bitcoin Mini Trust)
— and cannot price Bitcoin.

A separate finding worth recording alongside it: **the platform's own
`.env` had all three `LONGPORT_*` lines commented out**, so it had never
ingested anything from Longbridge at any point in its history. The
startup banner said `market_data_vendor: NOT_CONFIGURED` and that was
accurate.

### 2. Coinbase, chosen by measurement rather than reputation

Three candidates, all probed with real requests before picking:

  * **Kraken** — the public OHLC endpoint IGNORES `since` when asked to go
    backwards: it returns the most recent 720 bars whatever is passed.
    Hourly history therefore reaches back about 30 days, and the brief's
    90- and 180-day windows are not obtainable from it at all.
  * **Binance** — exposes a real `BTCUSD` pair, but it is nearly untraded:
    median **0.04 BTC per hour** against BTCUSDT's 560, with daily history
    beginning four days before the probe. Backtesting it would produce
    numbers about an empty order book. Its liquid pair, BTC/USDT, is a
    tether pair rather than a dollar pair.
  * **Coinbase `BTC-USD`** — a genuine, deeply liquid USD market (median
    218 BTC/hour in the sampled window), real `start`/`end` range support,
    and 5-minute candles still available 180 days back.

`CoinbaseBarProvider` needs no credentials, which is why it has no
all-or-nothing gate and no `build_*` returning `None` — a structural
difference from every other provider in that package, and the reason the
router reports WHICH vendors it has rather than a single configured flag.

**The response shape is a trap and is handled explicitly.** Coinbase
returns `[time, low, high, open, close, volume]` — `low` and `high` come
BEFORE `open`, which is not the ordering any other vendor here uses.
Reading it positionally in the conventional order transposes every bar's
open with its low, producing candles that look plausible, validate fine,
and are wrong in a way nothing downstream could detect.

`BarBackfillRouter` dispatches on symbol SHAPE — a dot means Longbridge, a
hyphen without a dot means a Coinbase product — because the two universes
are DISJOINT. That is the important difference from `MarketDataRouter`,
which tries providers in turn for the same symbol: a fallback here would
mean answering "what are AAPL's bars?" with a crypto exchange's silence.
An unroutable symbol is refused with a 422 naming both conventions,
before any job row is created.

### 3. The platform could not trade Bitcoin at all

Sizing floored every quantity to a whole unit. $10,000 of a $70,000 asset
is 0.1428 units, which floors to **zero**. Every entry was skipped, and
every BTC backtest returned **0 trades and a 0.00% return** while
buy-and-hold over the same window made 17.3%.

The dangerous part is not the bug, it is its shape: "no trades" is
indistinguishable from "a strategy that never found a setup". Five
different strategies all reporting 0.00% looked like five strategies that
did not fire, and only comparing against buy-and-hold made it obviously
impossible.

`QUANTITY_PRECISION_FRACTIONAL = 6` for crypto (about seven cents of BTC,
the limit of the `NUMERIC(20,6)` quantity columns), `0` for equities, with
the crypto test delegated to the router so there is one definition of
"crypto symbol" in the codebase. A universe scan decides PER SYMBOL, since
a mixed universe would otherwise either floor every crypto entry to zero
or propose fractional share counts no equity venue accepts.

**A related discovery, not a bug:** the Risk Engine caps any single order
at 10% of equity. Measured precisely — `fixed_fraction` 0.09 fills, 0.10
does not. So `all_in` fills nothing on any symbol, and a strategy sized
above ~9% of equity silently produces an empty backtest. That is the risk
engine doing its job, but it is invisible unless you know to look, so it
is recorded here.

### 4. Two schema limits that only intraday data reaches

Both were written when `1d` was the only interval anything ingested, and
both are hard failures rather than degradations:

  * `backtest_equity_points` was UNIQUE on `(run_id, date)` — one point per
    calendar day. An hourly run produces 24, so the write died on a unique
    violation AFTER the replay had completed. Migration `0029` re-keys it
    on the bar's real timestamp and adds `entry_ts`/`exit_ts` to trades,
    without which two intraday trades on one day are indistinguishable in
    the ledger and collapse onto one another as chart markers.
  * `MarketDataStore.upsert_bars` built one INSERT for the whole backfill.
    At 9 bind parameters per bar, Postgres' 32,767-parameter ceiling caps a
    write at ~3,640 bars. A decade of daily bars fits; 185 days of hourly
    does not. Now chunked at a size DERIVED from the column count, so
    adding a column shrinks the chunk automatically rather than silently
    reintroducing the overflow.

`date` is kept alongside `ts` rather than replaced: the monthly-returns
heatmap groups by day, and re-deriving a calendar day from an instant at
every call site is where timezone bugs live.

### 5. Backtest history as a first-class surface

`GET /backtest-runs` lists every run the caller owns across ALL
strategies. The per-version listing answers "what has this version done",
which is right while iterating on one strategy and wrong afterwards:
comparing a new idea against everything tried before meant knowing each
strategy id in advance. Ownership is enforced in the JOIN rather than by
filtering after loading. FAILED runs are included — a run that could not
complete records what was attempted and why, and hiding them would make
the history a record only of the attempts that happened to work.

### 6. What the real numbers said

Five conventional strategies over 180 days of real hourly BTC/USD, costed:
**every one underperformed buy-and-hold (+8.29%), and three lost money.**
Best was `sma-50-200` at +1.69%. Ranking the five by trade count ranks them
by result, inverted: 11 trades best, 69 trades second-worst.

`rsi-30-70` won **59.3% of its trades and still lost money** — $144.84 of
costs across 54 round trips on a $10,000 account. Before D088's cost model
that strategy would have reported a profit. This is the clearest evidence
so far that the cost model earns its place.

Status: Implemented and verified against real Coinbase data.

---

## D090 — Phase 72: an improvement loop that is built not to fool itself

"Improve the strategy over time" describes, stated plainly, a machine for
overfitting: propose variants, measure them, keep the winner, repeat. Run
that against one window long enough and it will find a definition that
fits that window's noise beautifully and predicts nothing — and it will
report a wonderful number while doing it.

So the design question was never "how do we search" but "how do we stop
the search lying to us". Three rules, all structural rather than
advisory:

**1. Two windows, and the second is not looked at until the first has
already chosen.** `train_*` and `validate_*` are separate NOT NULL columns
with no defaults, so there is no shape of `strategy_improvement_runs` that
means "improve against everything" — a caller cannot express it even by
accident. Candidates are generated and ranked on train alone; the hold-out
is touched exactly once per iteration, for the single candidate that
already won. A candidate that never won on train leaves a step row with
`validate_backtest_run_id IS NULL` — the rule is visible in the data.

**2. Acceptance requires improving on BOTH.** Beating the incumbent on
train earns a candidate the right to be judged; beating it out-of-sample
earns acceptance. Train-only improvement is recorded as `OVERFIT_REJECTED`
with both figures quoted.

**3. The search stops at the first rejection.** It does not go on to the
second-best candidate, the third, and so on. That is precisely how a
hold-out gets consumed: judge enough candidates against it and it stops
being held out. One validation per iteration is the entire budget.

**Nothing here is an LLM.** Variants come from `perturbation.py`'s
deterministic one-factor-at-a-time generator and every number comes from
`engine_v2` replaying real bars through the real Risk Engine and the real
cost model — so the same inputs reproduce the same search, which is what
makes a recorded run re-checkable. Each step keeps the ids of the ordinary
`backtest_runs` behind it, so a search re-opens as normal runs with their
own equity curves and trade ledgers rather than as a private notion of a
result.

**`accepted_reason` is always populated**, on rejection as well as
acceptance. A row recording only numbers leaves a reader reverse-
engineering why the search moved on; the stated reason is the difference
between a record and a log. The objective is deliberately crude — return
only, ignoring drawdown and trade count — and every stored reason carries
that caveat rather than leaving it to be assumed.

**A SUCCEEDED run may have improved nothing.** `best_version_id` is NULL
when no candidate survived validation, and that is a complete answer, not
a failure. Only an ACCEPTED candidate leaves a `StrategyVersion` behind,
so a rejected variant cannot be mistaken for something the search
endorsed.

### What it did on its first real run

Baseline EMA(20/50) on real BTC/USD: **−0.36% train, +1.98% held-out**.
The search tried six one-parameter variants; the best moved EMA(50) →
EMA(55) and improved train to **−0.22%**. On the held-out window that same
variant returned **1.91%** — worse than the baseline's 1.98%.

`OVERFIT_REJECTED`. The search stopped and the baseline stood. A naive
optimiser would have reported that variant as an improvement.

### A latent bug this surfaced, and a wrong fix caught by an existing test

The loop's first real run died on "Object of type Decimal is not JSON
serializable": `perturbation.py` writes `position_sizing.fraction` as a
`Decimal`, and `strategy_versions.definition` is JSONB. It had never
mattered because `robustness.py` uses variants in memory only - this loop
is the first thing that PERSISTS one.

The obvious fix - have `perturbation.py` write a float - was **wrong, and
`test_no_returned_value_is_ever_a_binary_float` caught it.** That module
computes an EXACT Decimal on purpose, so `0.25 * 0.9` is precisely `0.225`
rather than binary float's `0.225000000000000005...`; writing a float
reintroduces the artifact that test exists to forbid. The in-memory
contract was right and the problem was never there.

The conversion therefore happens at the DATABASE BOUNDARY, in the loop's
own `_json_native`, and only there. An integral Decimal becomes an `int`
so a period stays a period rather than becoming `20.0`; a fractional one
becomes `float(str(d))`, which round-trips exactly because every consumer
parses the field back through `Decimal(str(...))` (see
`engine_v2._desired_quantity`). Both halves are pinned by tests.

Status: Implemented, verified against real data, 7 tests covering the
discipline rather than the outcome.

---

## D091 — Phase 73: an intraday engine for TQQQ, and what the data said about the playbook

The brief supplied a 26-section TQQQ intraday reversal playbook and asked
for the best strategies from it, both directions, with real risk
management and testing. Almost none of it was expressible in the existing
platform, and four separate defects had to be found before a single
honest number existed — three of them in code that had been shipping
plausible-looking results for weeks.

### 1. Why this could not be a strategy definition

A `StrategyVersion` is a list of indicators, one entry rule, one exit
rule and a sizing fraction, evaluated long-only by `engine_v2` (whose own
docstring records "every trade row carries side=buy" as a known limit).
The playbook's priority-10 setup needs a swept previous-day low, a
reclaim, a market-structure shift, a retest, an ATR-buffered structural
stop, 1R/2R partials with a trailed runner, a short side, and a 15-minute
regime filter running beside 5-minute execution. None of that is a
comparison between two indicator series.

So `intraday_engine.py` stands beside `engine.py` and `engine_v2.py`
rather than replacing either, reusing `CostModel` and the new session and
structure modules. The two existing engines model *a rule that decides
long or flat*; this one models *a structural event that opens a
risk-defined bracket in either direction, flat by the close*.

### 2. Longbridge was silently returning 7% of every intraday request

`history_candlesticks_by_date` caps a response at 1,000 candles and
serves the most RECENT ones, with the requested `start` having no effect
beyond that. For daily bars the ceiling is about four years and never
bit. For 5-minute bars it is roughly 13 trading days, so the playbook's
minimum 180-day window returned 13 of 124 sessions — with no error, no
truncation flag, and a job row recording SUCCEEDED. Every statistic
computed from it would have been arithmetically correct and about the
wrong period.

Replaced with backward paging via `history_candlesticks_by_offset`,
which turns the same request into 23,808 bars over 124 sessions. Window
filtering afterwards is mandatory rather than tidying: paging backward
overshoots by up to a page.

### 3. The vendor's timestamps are local time, and the container hid it

The SDK converts its epochs to the running process's timezone and returns
a NAIVE datetime. `_as_utc` did `replace(tzinfo=UTC)` — asserting that
whatever the local clock said was UTC.

That is true on a UTC host, which the API container is, so it survived
every test and every production run. On the UTC+8 development machine the
identical code placed the 09:30 ET open at 21:30 UTC. Daily bars absorbed
it (their timestamps are midnight ET, so only the instant moved, not the
date); intraday bars, where the session boundary IS the information, were
corrupted end to end.

Established by measurement, not inference: the only gap in a run of
regular-hours 5-minute bars sits at 21:30 local with a 1,055-minute
span — the 17.5-hour overnight break — and the naive session start shifts
21:30 → 22:30 exactly when US DST ends, which can only happen if the
source zone is fixed-offset rather than the market's own. `.astimezone(UTC)`
reads a naive value as local and undoes the vendor's step on any host.

### 4. Two defects that made the first real results meaningless

Both were found by looking at a result that did not add up, rather than
by a test.

**R was measured from the wrong price.** `risk_per_share` came from the
pre-cost signal price while P&L came from the post-cost fill. The error
scales inversely with stop distance: on a $39 instrument with a $0.05
stop, a 3bp round trip is about half the entire risk budget. 37 of 239
trades therefore lost more than a full stop and one reported **−169R**,
which is not a strategy result at all — it is the denominator being
wrong. R is now measured from the price actually paid.

**There was no stop-quality test.** Section 16 says to reject a trade
whose stop is "so tight that normal noise repeatedly hits it", and the
engine was happily taking setups with a stop distance of 0.00% of price,
where the outcome is decided by the spread. Now floored and capped in ATR
units, which rejected 148 of 264 raw signals.

**And the entry was in the wrong place.** The playbook's sequence ends
*...MSS → RETEST → entry*, and the first implementation entered at the
structure break. That puts the entry at the top of the move with the stop
still at the swept extreme, so the whole advance becomes the risk
distance and a 1R target demands it again. Measured: 15% of trades
reached the first target while 76% were stopped. The asymmetry was an
artefact of entry location, not a fact about the market. With the retest
in place the same setup reaches 26% and stops on 66%.

### 5. What the data actually said

180 calendar days, 124 real TQQQ sessions, 23,808 five-minute bars,
1bp fee + 2bp slippage on every fill, both directions, flat by the bell.

  * **`sweep_mss` — the playbook's own "best single setup" — is the only
    one that did not lose money**: 116 trades, 55.2% win rate, expectancy
    **+0.051R**, profit factor 1.13.
  * **It is not statistically significant.** t = +0.61 against zero. The
    honest reading is "indistinguishable from no edge", not "a small
    edge". Out-of-sample is slightly better than in-sample (+0.099R vs
    +0.023R), which argues against overfitting but does not create
    significance.
  * **The other three setups all lost money**, and the composite of all
    four is significantly NEGATIVE: 321 trades, **t = −3.32**, profit
    factor 0.66, −60.86R. Running every setup together is the one result
    here that clears significance, and it clears it in the wrong
    direction.
  * **The scoring gate makes things worse, monotonically.** Section 14
    proposes trading only at 8/10 and calls the thresholds "starting test
    values, not empirically proven cutoffs". Tested: 8/10 admits ZERO
    trades in six months; 7/10 gives −0.27R at t = −1.95; 6/10 gives
    −0.04R. Every raise of the bar degrades the result. The evidence
    weights are not ranking setups the way the playbook assumes.
  * **The edge, such as it is, is one-sided**: longs +0.127R, shorts
    −0.025R.
  * **Against buy-and-hold it is not close.** TQQQ ran +51.36% over the
    window. `sweep_mss` at 0.5% risk per trade returns +2.96% of equity;
    the composite returns −30.43%.

Nothing here is deployable, and nothing here is being deployed.

### 6. Reusability

No setup references TQQQ. Levels come from the instrument's own sessions
and every distance is in ATR units, so `IntradayRunConfig(symbol=...)`
against any symbol with intraday bars runs the same code. TQQQ is what
these were written FOR, not what they are written AGAINST.

Status: Implemented and verified against real Longbridge data. The
research result is negative and recorded as negative.

---

## D092 — Phase 74: the Markets terminal, and the book that is not there

The brief asked for "live charts view, indicators, watchlist, orderbook,
signals" — a TradingView-shaped surface. Most of it was assembly work over
what Phases 70-73 already built. One part was not, and it is the only part
worth a decision record.

### 1. The vendor answers "no book" with a book

`QuoteContext.depth("TQQQ.US")` returns HTTP 200 and a structurally valid
order book: one bid level, one ask level, each with `price = None` and
`volume = 0`. Measured on this account:

    TQQQ.US   1 bid, 1 ask, top bid None x 0
    AAPL.US   1 bid, 1 ask, top bid None x 0
    700.HK    1 bid, 1 ask, top bid 434.200 x 5700

A positional read of that response produces a ladder of `0.00 x 0` rows and
a spread of `0.00`. It renders beautifully. It is also the most dangerous
reading available, because a zero bid is a PRICE and a zero spread is a
NUMBER — both are things a human or a strategy will act on, and neither
exists.

**Two explanations fit the observation and this codebase deliberately
chooses neither.** The account holds `LV1 Real-time Quotes` for US, and an
LV1 entitlement generally carries no depth ladder; but the measurement was
taken at 01:33 ET with the US market closed and Hong Kong open at 13:33.
One observation cannot separate "this entitlement has no book" from "this
market has no book right now". Asserting either would be a guess wearing
the clothes of a finding, so the error message names both possibilities
and the UI repeats them.

It does not need to be separated, because the handling is identical: **a
level whose price is absent is not a level.** The adapter drops it, and a
response with nothing left becomes `DataUnavailableError` → 404, exactly
like a quote the vendor could not supply. The distinction the endpoint
preserves is between three genuinely different facts — 503 (no vendor
wired), 404 (a vendor answered with nothing), 200 (a real book) — and the
proxy passes all three through verbatim rather than flattening them.

### 2. Session levels are computed here, not fetched

`GET /market-data/{symbol}/session-levels` returns PDH/PDL/PDC, premarket
extremes, the opening range and session VWAP with its 2σ bands, derived
from `market_data_bars` via Phase 73's `sessions.py`. No vendor publishes
these: they are facts about where each bar sits on the exchange's clock.
Computing them server-side also means the endpoint answers when the market
is closed, which is precisely when a trader marks levels for tomorrow.

**The derived figures are quantized to six decimals and the stored ones
are not.** VWAP is a division, so `Decimal` hands back the context's full
28 significant digits — a session VWAP rendered as
`68.26864187763813456219701391`, which is not a price anyone can act on and
claims precision its six-decimal inputs do not have. Levels that come
straight off a bar are passed through untouched, because rounding a stored
price would make the API disagree with the bar it came from.

### 3. Polling, and saying so

There is no websocket anywhere in this platform. The quote and the book
refresh on a five-second interval, every panel carries the timestamp of the
data it is showing, and the page says in as many words that this is polling.
Calling it "real-time streaming" would be a latency claim the system does
not meet.

### 4. Freshness derived, not restored

Four panels needed the same thing: when the symbol changes, stop showing
the previous symbol's data. The obvious `useEffect(() => { setData(null);
load(); }, [symbol])` is what `react-hooks/set-state-in-effect` refuses,
and the rule is right — that reset is a second render pass triggered by the
first, and four of them compound.

`lib/useKeyedFetch.ts` instead TAGS each stored value with the key it was
fetched for and DERIVES freshness during render. Stale data is never shown
because it is never selected, not because something raced to clear it. A
slow response for an abandoned symbol still arrives and is simply not
selected, so there is no torn state where the header says one symbol and
the table shows another.

### 5. A hydration bug worth recording, because the fix is not obvious

The symbol was first read from `?symbol=` with a lazy `useState`
initializer reading `window.location.search`. That looks like the
effect-free way to do it, and it produced a hydration mismatch: the server
has no `window`, so it rendered the default symbol while the browser
rendered the URL's. Observed directly — the page rendered `TQQQ.US`
server-side and `700.HK` client-side.

The fix is to resolve the query in the SERVER component, which already
receives `searchParams`, and pass it down as a prop. Both passes then agree
because the value is known before the first byte.

### What was verified, and how

Both the populated and the absent path, against the live vendor:

  * **TQQQ.US** — 1,152 bars drawn, 10 session levels drawn on the chart,
    quote `67.900 · longbridge`, and the order book showing
    DATA_UNAVAILABLE with its reason.
  * **700.HK** — a real ladder: spread `0.200`, ask `433.800 x 24,800` (49
    orders), bid `433.600 x 17,500` (28 orders) — and, correctly, a chart
    and a levels panel both reporting DATA_UNAVAILABLE, because no HK bars
    have been ingested.

The second half of that matters as much as the first: the states that show
nothing are the ones a fabricating implementation would fill in.

Status: Implemented and verified against live vendor data.

---

## D093 — Phase 75: an options foundation that keeps "modeled" visible

The TQQQ options playbook asks for defined-risk option structures selected
by delta/DTE/liquidity/IV. The platform had no options code at all. The
hard constraint shaping this phase is not the math — it is honesty:

**There is no historical option-chain data.** Longbridge serves live
option quotes and Greeks (with OPRA permission) for forward use, but
nothing supplies PAST option prices. So a backtest of a spread over
historical underlying bars must SYNTHESIZE each leg from the underlying
with a pricing model. This phase builds that model and wraps it so the
synthetic origin can never be mistaken for a fill:

- `options/pricing.py` — Black-Scholes-Merton price + Greeks, every
  function named `theoretical_*`, floats (a float model, not false
  `Decimal` precision), theta per calendar day and vega per vol-point (the
  conventions a screen shows). Verified against the textbook ATM value
  (7.9656), put-call parity, delta bounds and the expiry collapse to
  intrinsic. `implied_delta_strike` inverts the playbook's delta-band
  selection back to a strike by monotone bisection.
- `options/structures.py` — the four verticals (bull call / bear put debit,
  bull put / bear call credit). Money becomes `Decimal` in ONE place, at
  this boundary, because here it is real capital-at-risk. The constructor
  REFUSES any structure whose modeled loss is not bounded and positive —
  the "never naked, always defined-risk" rule enforced in code, not left to
  the caller. `contracts_for_risk` is the playbook's
  floor(risk / max-loss) with max-loss as the exact denominator.
- `options/selection.py` — §2's universal gates as pure predicates
  (liquidity spread caps 10% single / 5% multi, DTE bands, delta bands, IV
  regime), each returning a reason. These run on REAL quotes live; a
  model-priced backtest records that the liquidity gate was not applied
  rather than faking a pass.

Not built this phase, and deliberately: the model-priced spread backtest
that rides the intraday signals, the live option-chain read, and the
multi-leg paper path. Iron condor/calendar stay execution-disabled until
Longbridge multi-leg capability is verified (it natively supports
verticals/straddle/strangle/collar, not condor/calendar).

Status: Implemented and verified — 33 tests, ruff/mypy clean. Pure model
layer; nothing trades.

---

## D094 — Phase 76: candlestick patterns from the ASTA course, by their own rules

Task 6 asked to read the Avadhut Sathe Trading Academy course materials
(SMM/PAPA/GUE/FOME), name and summarize the concepts, and build strategies
following the material's rules. The named/summarized knowledge is the
`asta-trading` book-to-skill skill; the mechanizable strategy code is here.

The cleanly mechanizable subset is the **candlestick patterns** — the
material states them precisely, and imprecision is where a generic library
diverges from what the course teaches. `marketdata/candles.py` encodes them
with the material's exact definitions:

- Piercing is separated from Engulf by the boundary the course draws: a
  close above the prior body's MEDIAN is piercing; above its OPEN is the
  stronger engulf. The two are mutually exclusive here.
- Hammer/Shooting-Star require a wick ≥ 2× body (the course's "2-3 times").
- **The trend-context rule is enforced in code**: "a reversal candle is
  significant only at the end of a trend." Each pattern carries the trend
  it requires, and `is_signal_in_context` is the single place that applies
  it — so shape detection can never be mistaken for a signal on its own.

`detect_at` reports only patterns completing on the current bar, from
backward windows, so it is causal for replay.

Deliberately NOT built, and said plainly in `docs/ASTA_STRATEGIES.md`:
Elliott-wave auto-counting (a research problem, not a checklist — only the
three validity rules and setup checklists are mechanizable), and wiring
these patterns as intraday entries (blocked on the intraday engine being
persisted/exposed first, per `docs/AUDIT.md`). FOME's options mechanics are
already the Phase 75 options layer.

Status: `candles.py` implemented and verified — 12 tests, ruff/mypy clean.
The book-to-skill skill's generated-skill security scan passed.

---

## D095 — Phase 77: candlesticks wired into the engine, and what they did

The ASTA candlestick detectors (D094) were a library with no caller. This
wires them into the intraday engine as a real setup, `candle_reversal`,
and measures them.

**Both of the material's gates are enforced, and each one is what makes
the setup honest rather than a shape-matcher:**

  * **Trend context** — "a reversal candle is significant only at the end
    of a trend; ignore it mid-trend or in a range." The trend is the Dow
    definition the SMM module teaches (higher highs AND higher lows),
    derived from **confirmed** swings only, so it was knowable at that bar.
    Fewer than two confirmed swings of each kind returns SIDEWAYS, which
    makes every reversal pattern fail its gate rather than fire against an
    unknown trend.
  * **Location** — PAPA orders it location first: the pattern must occur AT
    a marked level (PDH/PDL/PDC, premarket extreme, opening range, VWAP)
    within an ATR-scaled tolerance. A hammer in open space is not a setup.
    A `None` level is absent and never read as a price of zero.

A doji never carries a trade: it is indecision, recorded as corroborating
evidence only. A close-only bar is skipped rather than aborting the replay.

### What it did on real data

124 real TQQQ sessions, 5-minute bars, costed, both directions:

**274 trades, 42.3% win rate, expectancy −0.241R, t = −3.45, profit
factor 0.62.** That is significant in the WRONG direction — not noise. For
comparison, on the identical data `sweep_mss` is +0.051R at t = +0.61
(insignificant) and the other three setups are all negative.

This is the second independent negative result on TQQQ intraday. The
engineering is sound and now measurable; the edge is not there. Recording
it because a setup that loses significantly is a finding, and a platform
that only remembers its positive backtests is worthless.

Status: Implemented and verified — 10 wiring tests; result negative and
reported as negative.

## D096 — Option strategy layer: the playbook's decision rules, enforced (Phase 78)

`apps/api/app/options/strategies.py` turns an underlying signal into a
defined-risk option structure, or refuses. It is the decision layer above
Phase 75's pricing/structures/selection foundation, built from
`TQQQ_Longbridge_Auto_Options_Strategies.md`.

**What is enforced rather than advised**

- **§5 score, 0–12**, using the playbook's own weights (sweep +2, MSS +2,
  and eight one-point confirmations). Evidence is kept as named booleans, not
  a bare integer, so a recorded plan shows *which* evidence produced its
  score — a 9 built from a sweep plus a structure shift is a different trade
  from a 9 built from four soft confirmations.
- **§16 hard floor: score < 8 refuses.** "Watchlist" grade (6–7) is
  explicitly not tradeable. This is a typed `TradeRefusal`, never a silent
  `None` — "why did it not trade" is the question an operator actually asks.
- **§10A dual path.** A directional edge routes to a DEBIT vertical; a range
  with IV rank ≥ 0.50 routes to a defined-risk CREDIT vertical. A directional
  edge wins ties, matching the playbook's rule against opening an opposing
  premium position at the same level. **Neither path can produce a naked
  short** — the protective wing is structural, enforced in `build_vertical`.
- **§3 sizing tiers** (A+ 0.50%, normal 0.35%, 0DTE ≤ 0.20%), sized off the
  structure's exact max loss. 0DTE is capped at the speculative tier
  *regardless of grade*: the playbook gives it its own ceiling because
  gamma/theta make it a different risk, not a better trade. When the budget
  cannot buy one contract the answer is a refusal, not a rounded-up contract.

**Two findings worth recording**

1. **A literal 0 DTE cannot be modeled.** With zero time to expiry there is
   no time value, delta becomes a step function, and the modeled spread
   collapses to `max_loss = 0` — which would size an unbounded number of
   contracts. The playbook's "0DTE" means *expiring today*, with hours of the
   session left, so a 0DTE plan is modeled with the hours actually remaining
   (`zero_dte_hours_remaining`, default 4h as a placeholder for a live
   clock). Passing a true zero raises rather than returning a zero-risk plan.
2. **Strike selection is delta-first, then snapped.** The playbook picks legs
   by delta but a chain is quoted by strike, so `implied_delta_strike`
   inverts it and the result is rounded to the strike grid. If rounding
   collapses both legs onto one strike the short leg is pushed one increment
   out — a zero-width "spread" is not a structure and divides by zero in the
   risk math.

**What this does NOT claim.** The option layer converts a signal into a
defined-risk structure; it does not create edge. The underlying TQQQ
intraday signals measured **significantly negative** over 124 sessions of
real 5m data (composite t = −3.32; `candle_reversal` t = −3.45; best single
setup `sweep_mss` t = +0.61, not significant) — see D095. Everything here is
priced by **model** (Black-Scholes), because no historical option-chain data
exists; every plan carries a `priced: MODEL` note and live use must re-price
against real quotes before submitting. Iron condor / calendar / double
calendar from the playbook's Premium Collection path are **not built**: they
are multi-leg structures whose execution depends on Longbridge multi-leg
capability that has not been verified, and building an order path we cannot
submit would be the fabrication this project forbids. 22 tests; ruff/mypy
clean.

## D097 — Owner bootstrap from an environment variable (Phase 79)

`OWNER_BOOTSTRAP_EMAIL` on the API, and `scripts/grant_owner.py`, share
`apps/api/app/auth/bootstrap.py`: widen ONE existing account to an
`owner` role holding every `Permission`, grant it every broker, and (with
`--demote-others` / always at startup) move every other `admin:manage`
holder to a `trader` role.

**Why an env var.** The account that needs widening is by definition the
one that cannot call `/admin/*` yet (D013), and the production operator
has a Railway variables tab but no shell and no local copy of the
database URL. An email is not a secret; setting it is the same act as
setting any other variable, and the API applies it on the next start.

**What it refuses.** It never creates an account: an unknown email is
logged and nothing is written, so a typo — or anyone who can set env
vars — can only widen an account that already exists, which is no more
power than they already hold over the database. It rewrites only the two
roles it owns (`owner`, `trader`), never a custom role. Idempotent.

Live PERMISSION is not live EXECUTION: the three execution gates
(`TRADING_MODE`, `LIVE_TRADING_ENABLED`,
`STRATEGY_LIVE_AUTO_EXECUTION_ENABLED`) are untouched.

## D098 — The Autotrade Bot (Phase 81/82)

An operator-configured intraday robot: symbols (from a watchlist), market
phase, positions open at once, trades per day, capital per trade,
strategy mode, stop-loss / trailing stop, take-profit / trailing take
profit, news blackout. Tables `autotrade_bots`, `autotrade_bot_runs`,
`autotrade_bot_trades` (migration 0031); package `apps/api/app/autotrade/`;
routes `/autotrade/*`; page `/autotrade`.

**Why not a strategy deployment.** A deployment runs one closed-vocabulary
definition on daily bars, long/flat, sized by that definition. The bot
runs the *intraday setup detectors* on 5-minute bars across many symbols,
ranks signals, and manages a bracket per position every cycle. Only the
order path is shared — `submit_trade_and_record`, unchanged. Everything
above it is different, so it is its own subsystem rather than nullable
columns on the deployment tables.

**The scanner reuses the backtest's context verbatim.** Session levels,
swings, VWAP, indicator windows and the detector registry come from
`intraday_engine.py` / `setups.py`, so the live bot cannot fire on a
signal the backtest could not, and D095's measurements apply to it
unchanged. That is the point: those measurements are **negative**
(composite t = −3.32 over 124 real sessions), and the bot is machinery
for running them on paper and reading its own numbers, not a claim that
they pay.

**What "learning" means here, exactly.** `learning.py` computes per-setup
count / win rate / expectancy-R / total-R over the bot's own closed
trades; in `auto` mode a setup with ≥ 20 trades and expectancy < −0.10R
is demoted (not run) until its numbers recover. Nothing tunes a
detector's parameters: a loop that re-fit detectors to the last few dozen
trades would be D090's overfitting machine at a sample size where noise
dominates. Every demotion is visible on the run row (`setups_active`).

**Refusals are typed.** Each cycle writes exactly one run row whose
status says why nothing happened when nothing did: not active, emergency
stop, market phase closed, no vendor (`NOT_CONFIGURED`, never a trade on
stale bars), live broker. A held symbol that cannot be marked fails the
cycle rather than being priced from memory.

**Scope stated, not implied.** Paper brokers only — a live broker gets
`skipped_live_not_supported` before any market data or order path is
touched; D087's separately-armed live path is not reachable from here.
Long only (the paper broker cannot hold a short). Flat by the end of the
traded phase. `Stop` never liquidates (same reasoning as D087's
breakers). The runner defaults ON — a departure from the other loops,
justified by where the gate sits: a bot cannot act without a person
approving it, and with no vendor the loop writes nothing.

**Also in this phase (82).** Emergency stop (D039) and market-data
backfill (D070) gain admin panels; both had existed only as endpoints.
The Markets watchlist rail was reading `payload.items` for a response
that carries `watchlists` / `quotes` and had never rendered — fixed, and
the watchlist now lives in the left rail on every page.

## D099 — The learning loop, second pass (Phase 83)

Three additions to D098's loop, chosen because each turns a number the
bot already produces into something an operator can act on, and none of
them re-fits a detector.

**Adverse excursion.** `autotrade_bot_trades.trough_price` joins
`peak_price`. Together they answer the question a stop-out on its own
cannot: did the trade reach +1R and give it back (a management question)
or go straight to the stop (an entry question)? Backfilled to the entry
price for pre-existing rows — the honest value for a path that was not
recorded.

**Per-symbol demotion.** `active_setups_for_symbol` applies the D098
demotion rule at (setup, symbol) granularity when that pair has reached
the 20-trade floor, else falls back to the setup's overall verdict. A
setup that loses on TQQQ and wins on QQQ is switched off on TQQQ only; a
symbol with no history inherits the overall verdict rather than a blank
slate. Explicit `single`/`multi` modes remain untouched by statistics.

**The journal.** `autotrade_bot_insights` gets one row per bot per
session, written by the engine on the cycle that ends the session (and
for any older session still missing one). It holds the day's aggregate
and `findings`: deterministic sentences, each carrying its sample size,
produced only when a threshold is met — "62% of the 13 losing trades
reached +1R before losing", "score-3 signals average −0.4R over 12
trades while score-4+ average +0.5R over 15", "sweep_mss opened in the
15:00 ET hour averages −0.5R over 6 trades", "58% of exits were
session-end flats". **Findings are recorded, never applied.** The one
automatic consequence in the whole loop is still setup demotion. A loop
that raised `min_score` or tightened stops by itself from a few dozen
trades would be optimising on noise (D090) with real orders as the cost
of being wrong; a person reading the sentence with the numbers in view
is the right decision-maker at these sample sizes.

`GET /health` now reports `market_data: configured|NOT_CONFIGURED` and
the autotrade runner state, so an operator can distinguish "vendor not
wired" from "vendor wired, market closed" without a session.

## D102 — Reading 5-minute candles: three setups, and what they measured (Phase 85)

`scripts/research_5m_structure.py` measures the structure the brief asked
about; `docs/RESEARCH_5M.md` records every figure. Three setups follow from
it — `quiet_pullback`, `volume_climax_reversal`, `gap_fade` — plus
`GET /market-data/{symbol}/signals` and the chart's B/S markers with a
score table (items 6-7).

**The measurement error that shaped everything.** The first run found a
large reversal edge at swing points (−0.81 ATR after a swing high, 66%
reversal). It was look-ahead: a fractal pivot is defined by the three bars
either side of it and is therefore not knowable until three bars later,
which `SwingPoint.confirmed_ts` already records. Measured from the
confirmation bar the edge is −0.02 ATR and 48%. Recorded here because the
same error, left in, would have produced three confident strategies and a
backtest that agreed with them.

**What the data says, over 128 sessions of TQQQ and QQQ 5-minute bars.**
Reversals at swing extremes: 48-50%, mean within ±0.15 ATR. Volume does
not split them. Location (PDH/PDL/premarket/opening range/VWAP±2σ) does
not split them — a level made a reversal slightly LESS likely (44% vs
50%). Gaps ≥ 1 ATR faded 46-49%. The only asymmetry with a consistent sign
on both symbols was a ~0.15 ATR CONTINUATION tilt after a pullback on
falling volume — the opposite of catching a top.

**So the three setups are instrumentation, not a trading plan.** Backtested
through the existing bracket engine with real costs (1bp + 2bps):
`quiet_pullback` −0.099R (t=−1.51) on TQQQ and −0.384R (t=−5.74) on QQQ;
`gap_fade` −0.143R / −0.332R (t=−3.10); `volume_climax_reversal` produced
4 and 11 trades — no sample, and loosening its gates until it traded would
have been fitting, not measuring. Raising the score threshold made results
WORSE (−0.099 → −0.147), the same monotonic degradation D095 found.

`gap_fade` initially produced ZERO trades: it gated on ATR, and ATR(14) is
not computable inside the first 12 bars of a session, which is the only
window it looks at. It now measures the gap in percent of the previous
close. A detector that can never fire is worse than one that fires and
loses — it looks like caution.

**The markers carry their own caveat.** Every `/signals` response includes
a `note` field saying these are measurement rather than advice, and the UI
renders it under the score table. The score box exists for the same
reason: a marker on a chart invites belief, and being able to read WHY it
appeared is the difference between an instrument and an oracle.


## D103 — The chart is created once and only fed; indicators are a plan, not a draw call (Phase 86)
Date: 2026-09-23
Decision: `apps/web/components/PriceChart.tsx` no longer rebuilds the chart
when its props change. The reported bug was "zooming in resets the view in
about a second"; the cause was not a timer. Every prop was in the effect's
dependency array, including `signals = []`, `scoredSignals = []` and
`priceLines = []` — default parameters, so new array identities on EVERY
render — which meant any parent re-render destroyed the canvas, rebuilt it
and called `fitContent()`, discarding the user's viewport. The chart object
is now created once per mount (deps: `height` alone) and afterwards only
fed: `setData` on the existing series, markers replaced in place, price
lines removed and re-added from content KEYS rather than identities.

A hand-set viewport is then held for `VIEW_HOLD_MS` (60s) after the last
wheel, pointerdown or touchstart on the container, with the remaining
seconds shown and explicit Reset/Hold controls. The hold listens to the
CONTAINER, not to `subscribeVisibleLogicalRangeChange`: a range change also
fires when we re-fit, so listening there would make the chart hold a
viewport it set itself and never return to auto-fit.

The Indicators menu (`components/markets/IndicatorsMenu.tsx`) carries the
nine the brief named plus the two the chart already drew unconditionally —
session levels and the scored B/S markers — so ONE menu answers "what is on
this chart". Every series is computed in `apps/web/lib/indicators.ts` from
the same stored bars the candles come from, never from a vendor indicator
endpoint: an indicator drawn from one feed sitting on candles from another
is a picture of two different markets.

Three refusal rules hold throughout and each has a plausible-looking
alternative: a window shorter than the period produces NO point rather than
an average of what is there; `vwap()` declines the whole series if ANY bar
in the window carries no volume, because a VWAP computed from a subset of a
session's prints is not that session's VWAP and looks exactly like one; and
pivots are computed from the PREVIOUS session, never the one still forming.
Reasons are rendered under the chart rather than swallowed.

`planIndicators()` is a pure function returning colour ROLES, run during
render. The drawing effect only translates its plan into library calls.
That split is what lets the "why this is missing" notes reach the JSX
without `setState` inside an effect (the `react-hooks/set-state-in-effect`
rule caught the first version), and it makes every decision about what to
draw directly testable without a canvas jsdom does not implement.
Status: Implemented, tested: `apps/web/test/indicators.test.ts` (19).

## D104 — The Messages client works against Anthropic's own API, not only a gateway (Phase 86)
Date: 2026-09-23
Decision: `AnthropicCompatibleProvider` sent `Authorization: Bearer` only.
`api.anthropic.com` authenticates with `x-api-key` and rejects a request
carrying no `anthropic-version`, so the Agent Trade panel would have
returned an opaque failure even after credentials were set — and that
failure was indistinguishable from a network outage, because every
`httpx.HTTPError` collapsed into "LLM provider request failed". Both
headers are now sent (each endpoint reads the one it knows), an
`HTTPStatusError` names the status and the provider's own message, and
`DEFAULT_ANTHROPIC_BASE_URL` means a key and a model are enough. The
all-or-nothing gate is unchanged: no key, or no model, still returns None
and every caller still renders NOT_CONFIGURED. Guessing a model for a key
would make a misconfiguration look like a working agent right up until it
billed someone.
Status: Implemented, tested: `tests/agents/test_anthropic_compatible.py` (7).
Credentials remain the operator's to set; the assistant does not write them.

## D105 — Extended-hours movement is derived from our own bars, and names its reference (Phase 86)
Date: 2026-09-23
Decision: `GET /market-data/{symbol}/extended-hours` reports pre-market,
regular and after-hours movement for the latest stored session, computed by
`extended_hours_moves()` from `market_data_bars` rather than from a vendor
extended-hours quote — the same reasoning as D092's session levels: which
phase a print belongs to is a fact about the exchange clock, and the bars
this platform trades from are the only ones whose phase it can vouch for.

Each phase carries `reference_label`. "+2.1% pre-market" is meaningless
without knowing whether that is against yesterday's close or the pre-market
open, and the two differ by the overnight gap, so the comparison is named
in the response instead of being left to the reader. Pre-market and regular
are measured against the PREVIOUS session's regular close; after-hours
against THIS session's regular close, because an after-hours move is by
definition a move away from the close.

A phase with no bars is ABSENT from the response, not zero-filled. Nothing
traded and traded unchanged are different facts, and a row of zeros asserts
the second.
Status: Implemented.

## D106 — Both directions, and a separate evidence bar for extended hours (Phase 87, migration 0033)
Date: 2026-09-23
Decision: the setups in `backtesting/setups.py` have ALWAYS emitted short
signals (eleven `Direction.SHORT` sites); `scan_latest_bar`'s
`allow_directions` defaulted to long-only and the bot discarded every one.
`autotrade_bots.allow_short` turns them on, off by default and off for
every existing bot — a human approved a long-only bot and shorting is a
different risk, so enabling it is a fresh, explicit decision.

Making it work meant making four separate things direction-aware, and each
is a place where reusing the long branch yields a number that LOOKS like a
correct one: the bracket (stop above entry, target below), the exit test
(the stop triggers on the bar's HIGH), the trailing ratchet (down from the
lowest low, not up from the highest high), adverse excursion (the highest
high, not the lowest low), and the settlement — the long P&L formula
reports a winning short as a loss of the same size, which reads as a
strategy result rather than as a bug. `brackets.py` writes each comparison
out per direction rather than folding it into a sign multiplier: a
multiplier is compact and unreadable at exactly the moment somebody is
checking whether a live stop is on the correct side of the price.

The paper broker grows OPT-IN shorting (`allow_short`, defaulted False so
every existing caller keeps the adapter it has always had) collateralised
at **100% cash**. It has no margin model, no borrow availability and no
financing cost; rather than invent one it refuses a short the account
cannot cover outright. That is stricter than any real broker, which is the
correct direction for a simulator to be wrong in — a paper account that can
open positions the real one would reject produces a track record the live
account could not have earned.

`extended_hours_min_score` holds a signal fired on a PRE-MARKET or
AFTER-HOURS bar to a higher bar than a regular-session one, defaulting (in
`service.py`, not the DB) to `min_score + 2` for a bot whose `market_type`
admits those phases and NULL for one that cannot reach them — a number
nobody reads is a number somebody eventually trusts. The premium is not a
tuned parameter and the code says so: nothing has measured the right value,
and it is set in the safe direction because extended-hours tape is thin
(roughly 1.9M shares pre-market against 53M regular on TQQQ over the
measured window), so the same score is computed from materially less
evidence. The phase is read from the BAR the signal would fire on, not from
the wall clock, so a cycle running late still judges the bar in front of it.
Status: Implemented, tested: `tests/autotrade/test_shorts.py` (18).

## D107 — The four remaining playbook setups, and a rolling walk-forward that says no (Phase 88)
Date: 2026-09-23
Decision: `rsi_divergence` (strategy 3), `order_block_fvg` (strategy 9),
`fib_confluence` (strategy 10) and `bollinger_confluence` (strategy 6)
bring the registry to twelve. Each was written once to the playbook's
description and measured once — no parameter search, on the principle D095
and D102 already established.

Three implementation decisions are the honest part. Divergence compares
against a CONFIRMED swing and recomputes RSI as of that swing's bar:
comparing against the lowest low in a lookback would use a pivot the market
had not yet revealed (the look-ahead trap `docs/RESEARCH_5M.md` records
being caught in), and reading the current RSI would compare now against
now. A fair value gap must still be UNFILLED at the retest — a gap price
has already traded back through is not an imbalance. Both confluence setups
require an INDEPENDENT second level (a session level or VWAP) rather than
another ratio, because a Fibonacci level agreeing with a Fibonacci level is
one observation counted twice.

Measured on 129 sessions at real costs, all four are negative on both
symbols. `rsi_divergence` is the worst result this study has produced
(−0.279R TQQQ t=−3.68; −0.569R QQQ t=−7.95). `fib_confluence` and
`bollinger_confluence` are small and statistically indistinguishable from
zero on TQQQ with 121 and 66 trades — and were deliberately NOT loosened to
manufacture a sample.

`scripts/walk_forward_5m.py` automates §22's rolling procedure, which this
engine had only ever had one hand-made 60/40 split of. Folds are sliced by
SESSION DATE, never by bar count (half a session is not a sample of a
session); the test window always follows the train window it was selected
on; selection never consults the test window. At 40/20 over the same
sessions the pooled out-of-sample figure is −0.070R on TQQQ and −0.087R on
QQQ, picking a different setup in three folds of four — the signature of
selecting on noise. The windows were NOT retuned after seeing that, which
would be the overfitting the procedure exists to detect.
Status: Implemented, tested: `tests/backtesting/` (161). Findings recorded
in `docs/RESEARCH_5M.md`.

## D108 — Option chains: the ladder says what exists, the quote says what it is worth (Phase 89)
Date: 2026-09-23
Decision: `marketdata/option_chain_provider.py` adds the port and
`LongbridgeOptionChainProvider` the first adapter. It makes BOTH vendor
calls, deliberately: `option_chain_info_by_date` returns the ladder and is
the only source of which contracts exist; `option_quote` returns the market
on named contracts. Building from the ladder alone gives a table of strikes
with no prices; quoting without it means guessing contract symbols from a
naming convention this platform does not get to assume. A full chain runs
to hundreds of contracts, so the ladder is quoted in bounded batches
(`QUOTE_BATCH = 50`) — chosen conservatively, not tuned, because a refusal
on an over-long symbol list would surface as an empty chain.

Every market field is nullable and stays null. A contract the quote call
did not answer for KEEPS its row with an empty market: dropping it would
silently shorten the chain. `OptionQuote.mid` refuses to be computed from
one side — a one-sided mid is that side's price wearing a neutral name, and
it is exactly the number a spread's cost would be taken from. Sizes are the
deliberate exception: a zero VOLUME is a real measurement and survives as
0, while a zero PRICE is the vendor declining to quote, and collapsing the
two loses the distinction a chain is read for.

Greeks are the VENDOR's. This codebase can compute its own
(`apps/api/app/options/pricing.py`, D096) and does not substitute them
here: a delta computed from a different volatility input than the desk is
quoting is a different delta, and mixing the two in one column makes the
column meaningless.

`quoted_contracts` is surfaced in the response and in the desk's header. A
three-hundred-row chain where twelve rows carry a market is a chain nobody
should price a spread from, and that should not have to be inferred from a
screen of dashes. The at-the-money row is marked from the live quote or not
at all — a highlight parked at the middle of the ladder looks identical and
means something else.

NOT built in this phase, and named so nobody assumes otherwise: chain
snapshot persistence, a Greeks dashboard, routes over the D096 decision
layer, an options bot, and option order routing.
Status: Implemented, tested: `tests/marketdata/test_option_chain.py` (8),
`tests/api/test_option_chain_routes.py` (6),
`apps/web/test/OptionChainTerminal.test.tsx` (5).


## D109 — The broker bridge: a provider catalogue, and credentials the database cannot reveal (Phase 90, migration 0034)
Date: 2026-09-23
Decision: `brokers.provider` was always a free string. `execution/registry.py`
makes it a lookup carrying what a venue trades, which order types it takes,
whether it can short or accept a fractional quantity, and which credential
fields it needs. Seven entries: paper, longbridge, ibkr, ig, moomoo,
kraken, binance.

**Every entry carries `adapter_status`.** Six of the seven are
`catalogued`: this platform knows what the venue is and has no adapter for
it. That distinction is load-bearing, not cosmetic — a capability list for
an unreachable venue reads as a promise, and an operator who wires
credentials against one has to be told that no order will flow.
`build_adapter` raises `AdapterNotImplementedError` rather than returning a
stand-in, because a stand-in accepts orders nothing will ever execute.

**`check_supported()` refuses before the order exists.** The difference
between `BROKER_CANNOT: Kraken trades crypto, not option` and
`VENDOR_ERROR: 40004` is the difference between a platform and a wrapper:
in both cases the answer was already knowable, and only one of them says
it. It is wired into bot creation today, so shorting cannot be enabled on
a venue that cannot hold a short — caught at creation, not mid-session on
a bot a human already approved. `opens_short()` is deliberately not
`side is SELL`: selling ten of ten held is an exit, and conflating the two
would block every close on a no-short venue.

**Credentials are one Fernet token per broker** (`execution/credentials.py`,
`broker_credentials`), so a database dump is not a set of live trading
credentials. Four rules, each with a convenient wrong alternative:

* *No key, no write.* With `BROKER_CREDENTIAL_ENCRYPTION_KEY` unset the
  store RAISES. It does not store plaintext, and it does not generate a
  key — a generated key lives in one process's memory and everything
  encrypted under it becomes unreadable at the next restart, silently,
  discovered only when a bot cannot trade.
* *The key is fingerprinted, never stored.* A rotated key then produces
  "re-enter these credentials" instead of an opaque `InvalidToken`.
* *Field NAMES are in the clear; values never are.* The UI must show what
  is set without the key, and "an access token is present" is not a
  secret.
* *Nothing returns a secret.* A field the registry marks PUBLIC (an
  account id, DEMO vs LIVE) IS returned, because an operator has to be
  able to confirm WHICH account is wired and a wrong account is a worse
  failure than a visible one.

`cryptography` is a new dependency. The standard library has no AES, and
hand-rolling authenticated encryption to protect live trading credentials
is not a trade this project should make.

**Router registration order in `main.py` is load-bearing**, and this was
measured rather than reasoned about: with the discovery router first,
`/brokers/providers` was matched by its `/brokers/{broker_id}` and answered
422 on a UUID parse. FastAPI matches in registration order with no
preference for a static segment over a path parameter. The bridge router
is registered first and a test pins it.

Two findings from building it, recorded in `docs/BROKER_INTEGRATION.md`:
IBKR and moomoo are reachable only through a gateway process on the
operator's own machine, which a Railway-hosted API cannot see — so the
build order is now by reachability (Kraken, IG, Binance, then the two that
need a bridge agent). And IBKR's spot FX returns no volume and a
mid-price source, which disqualifies every volume-gated setup and the
session VWAP on currency pairs, and means an FX cost model must be
spread-based.
Status: Implemented, tested: `tests/execution/test_registry.py` (10),
`tests/api/test_broker_bridge.py` (8),
`apps/web/test/BrokerBridgeAdmin.test.tsx` (4). No adapter is implemented;
nothing can trade through a catalogued provider.
