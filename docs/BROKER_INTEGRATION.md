# Connecting other brokers — the recommended design

Status: **proposal, not built.** Written 2026-09-23 in answer to "upgrade
the platform so other brokers can be connected via their APIs to trade
intraday, spot and options — propose the best approach."

Nothing in this document is implemented. It names the code that already
exists, the two things that are genuinely missing, and the order to build
them in. It ends with what I would *not* build, which is the part that
usually costs the most.

---

## 1. What already exists (do not redesign it)

The hard part of multi-broker support is already done, and it was done
before any second broker existed — which is the right order.

| Piece | File | What it gives you |
|---|---|---|
| Execution port | `apps/api/app/execution/broker.py` | `OrderRequest` / `Fill` / `BrokerAdapter` Protocol. Market **and** limit orders, cancel, and an `OrderWouldRestError` for a venue that cannot hold a resting order. |
| Two adapters | `paper_broker.py`, `live_broker.py` | Proof the Protocol is not shaped around one vendor: one is an in-memory simulator, the other a real SDK client. |
| Broker registry | `brokers` / `broker_grants` tables | Brokers are **rows**, not enum members. `kind` (paper/live), `provider` (a free string), and a per-user grant already gate who may trade which. |
| The one order path | `apps/api/app/oms/service.py::submit_trade` | RISK → PORTFOLIO → BROKER → persist. Every caller — human, agent, strategy deployment, intraday bot — goes through it. |
| Reconciliation | `apps/api/app/execution/reconciliation.py` | Resolves an order the venue accepted but has not confirmed. Broker-agnostic already. |

**The consequence:** adding a broker is adding one adapter class plus one
credential record. It is not a change to the risk engine, the OMS, the
bot, or any route. Any proposal that touches those is the wrong proposal.

## 2. The two things that are actually missing

### 2a. Per-broker credentials, stored and encrypted

Today Longbridge's credentials come from process environment variables
(`LONGPORT_APP_KEY` and friends). That works for exactly one broker per
deployment and cannot express "this user's IBKR account and that user's
Alpaca account".

**Recommended:** a `broker_credentials` table — `broker_id`, `provider`,
an encrypted `secrets` JSONB blob, `created_by_user_id`, `rotated_at` —
with encryption at the application layer via a single key held in the
environment (envelope encryption, not a per-secret key). Read only inside
an adapter factory; never returned by any route, never logged, and
redacted in every error path per `docs/TRADING_SAFETY.md`.

Two rules that are worth stating now because they are cheap now and
expensive later:

* **A credential is written by its owner, through a dedicated route, and
  is never readable back.** The UI shows "set / not set / rotated on
  DATE", never a value or a prefix of one.
* **The environment variables stay supported** as the deployment-level
  default for the platform's own broker. Removing them would break the
  running system for no gain.

### 2b. An adapter registry keyed on `provider`

`brokers.provider` is already a free string. What is missing is a map from
that string to a factory:

```python
ADAPTERS: dict[str, BrokerAdapterFactory] = {
    "paper":      build_paper_adapter,
    "longbridge": build_longbridge_adapter,
    "alpaca":     build_alpaca_adapter,      # new
    "ibkr":       build_ibkr_adapter,        # new
}
```

with a `capabilities` declaration per adapter — which asset classes,
which order types, whether it supports extended hours, whether it can
short. The OMS consults that declaration **before** building an order, so
"this broker cannot short" is a specific refusal at the proposal stage
rather than a vendor error after the fact. This matters more than it
sounds: the difference between "rejected: broker has no options
entitlement" and "VENDOR_ERROR: 40004" is the difference between a
platform and a wrapper.

## 3. Which brokers, in which order

Judged on API quality, cost, and whether this platform's existing
discipline survives contact with them.

| Broker | Asset classes | Why / why not |
|---|---|---|
| **Alpaca** — build first | US equities, options, crypto | Clean REST + websockets, free paper accounts with the *same* API as live, no desktop gateway, generous docs. The paper/live symmetry is the decisive property: it is the only way to test a live adapter without risking money. |
| **Interactive Brokers** — build second | Almost everything, globally | The widest coverage by far, and the reason serious users ask for it. Costs: it needs a running TWS/IB Gateway process (or the Client Portal Web API), the session expires daily, and its order model is the most complex of the three. Budget real time for reconciliation. |
| **Tradier** — optional third | US equities, options | Simple REST, genuinely good options chain data, cheap. Narrower than IBKR but far less operational overhead. |
| **Longbridge** — already wired | US/HK equities, options | Keep as-is. |

**Start with Alpaca even if IBKR is the eventual goal.** Building the
second adapter is what proves the Protocol generalises, and doing that
against the *easiest* API means any friction you hit is a real design
problem rather than an IB Gateway problem.

## 4. Order of work

1. **`broker_credentials` + encryption + the write-only route.** No new
   broker yet. Migrate Longbridge onto it, keeping env vars as the
   fallback, so the mechanism is proven against a broker that already
   works.
2. **The adapter registry + `capabilities`.** Still no new broker. Wire
   `paper` and `longbridge` through it and add the pre-flight capability
   check to the OMS.
3. **Alpaca adapter, paper only.** Same Protocol, same tests, its own
   stub-client test file. Approve it on a paper broker row first.
4. **Alpaca live** — behind the existing `LIVE_TRADING_ENABLED` gate and
   the per-broker approval. Fresh explicit approval required, per
   `CLAUDE.md`.
5. **IBKR**, once steps 1–4 have proven the shape.

Steps 1 and 2 are the whole architecture. Everything after is repetition.

## 5. What I would deliberately NOT build

* **A unified symbol namespace.** `TQQQ.US` at Longbridge and `TQQQ` at
  Alpaca are the same instrument, and a translation layer that silently
  maps between them will eventually map two different instruments onto
  one. Store the broker's own symbol on the order; translate explicitly
  at the edges, where a failure is visible.
* **Cross-broker net positions.** Aggregating "your TQQQ position" across
  two brokers produces a number you cannot trade against: you can only
  close a position at the broker holding it. Report per broker; sum only
  for display, and label it as display.
* **A generic "any broker" adapter driven by config.** Every broker's
  order lifecycle differs in exactly the places that matter (partial
  fills, resting orders, extended-hours flags, options multipliers). A
  config-driven adapter would hide those differences behind a schema and
  surface them as fill discrepancies.
* **Automatic failover between brokers.** If a broker is down, the honest
  answer is that the order did not go. Rerouting it elsewhere changes
  where the position lives without telling anyone.

## 6. Effort, honestly

Steps 1–2 are the bulk of the design work and are largely mechanical once
decided: a migration, an encryption helper, a registry, a capability
check, and their tests. Step 3 is one adapter and its stub tests. Step 5
(IBKR) is the one to schedule generously — the gateway session, the
contract-resolution model and its order states are each their own problem.

Nothing here requires touching the risk engine, the portfolio manager, the
OMS's decision sequence, or any strategy. That is the point, and it is why
the answer to "can we add other brokers" is yes without a rewrite.


---

## Update, 2026-09-23 — steps 1 and 2 are built, and the order changed

Phase 90 (D109) implemented the credential store and the adapter registry
described above. What follows revises section 3 with two things learned
while building it, both verified rather than reasoned about.

### The deciding constraint is not API quality. It is reachability.

Two of the six brokers do not expose a public REST endpoint at all. They
expose a **gateway process you run on your own machine**:

* **Interactive Brokers** — the Client Portal API talks to a local gateway
  at (typically) `https://localhost:5000/v1/api`, whose session expires
  daily and must be re-authenticated by a human.
* **moomoo / Futu** — the API talks to **OpenD**, likewise a local
  process, which also has to be unlocked per session with a trade
  password.

Trading OS's API runs on Railway. It cannot reach `localhost` on your
desktop. This is not an adapter problem and no adapter solves it; it is a
topology problem with exactly three answers:

1. **A bridge agent.** A small process on the machine running the gateway,
   which holds an outbound connection to Trading OS and relays orders. It
   is the only option that keeps the platform hosted AND reaches these two
   venues, and it is a component that does not exist yet.
2. **Self-host the API** next to the gateway. Simplest, and it gives up
   the hosted deployment.
3. **Use these two venues manually** and let the platform trade the
   REST-reachable ones.

**So the revised build order is by reachability, not by API quality:**

| Order | Broker | Why here |
|---|---|---|
| 1 | **Kraken** | Plain REST, key + secret, no gateway, 24/7 — the shortest path from "bridge exists" to "bridge places a real order". |
| 2 | **IG Markets** | Plain REST, and the only REST-reachable venue in the list that quotes FX. |
| 3 | **Binance** | Plain REST. Jurisdiction decides the host, so the adapter must be told which one rather than assume. |
| 4 | **IBKR** | Needs the bridge agent (or a self-hosted API) first. Widest coverage by far, and the only venue here with spot FX *and* equities *and* options *and* futures. |
| 5 | **moomoo** | Needs the bridge agent, same as IBKR. |

Alpaca was recommended above as the first adapter and is not in the user's
list; the reasoning that made it first — an identical paper and live API —
applies to Kraken's sandbox too, which is why Kraken takes that slot.

### Verified against the live IBKR connector, 2026-09-23

Spot FX is real and reachable: `EUR` on **IDEALPRO**, contract id
`12087792`, security type `CASH`. A five-minute history request returns
genuine OHLC.

It also returns **no volume field**, and `source: "MidPoint"`. That is not
a gap in the response; spot FX has no central tape to have a volume on.
The consequence for this platform is concrete and has to be designed for
rather than discovered later:

* **Every volume-gated setup cannot run on FX.** `quiet_pullback` compares
  impulse volume to pullback volume; `volume_climax_reversal` is entirely
  a volume test. Neither has an input on a currency pair.
* **Session VWAP cannot be computed on FX**, and the chart already refuses
  it correctly — `apps/web/lib/indicators.ts::vwap` declines the whole
  series when any bar carries no volume rather than treating "not
  reported" as zero. That rule was written for equities and pays for
  itself here.
* **The bars are mid-price, not traded prints.** A backtest filling at the
  mid is assuming half the spread it would actually pay, and on FX the
  spread is the entire transaction cost. Any FX cost model has to be
  spread-based, not the equity fee-plus-slippage model
  `backtesting/costs.py` uses today.

None of that blocks a Forex build. All of it changes what an honest one
looks like, and it is cheaper to know now than after a strategy has been
measured on a VWAP that was never there.
