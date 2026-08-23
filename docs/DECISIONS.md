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
