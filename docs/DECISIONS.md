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
