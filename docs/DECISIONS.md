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
