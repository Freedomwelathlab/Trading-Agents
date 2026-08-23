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
