# API

## `X-Request-ID` — correlation header (all endpoints)

Applies to **every** endpoint below, including error responses, 401s, 404s,
and validation failures (Phase 42, D056).

**Response.** Every response carries `X-Request-ID`. Its value is the
correlation ID that was bound into structlog for the whole of that
request's handling, so every structured log line the server emitted while
serving it carries the same value under the `request_id` key. Given an
`X-Request-ID` from a failed response, `request_id="<value>"` retrieves
that request's complete server-side log trail.

**Request (optional).** A caller may supply `X-Request-ID` to propagate an
ID it already assigned — a load balancer, an ingress, or the frontend's
route handlers. It is reused verbatim **only if well-formed**:

```
^[A-Za-z0-9._-]{8,128}$
```

A supplied value that does not match — too short, too long, containing
whitespace, control characters, or CR/LF — is **discarded and replaced with
a freshly generated UUID4. The request is not rejected** and its status
code is unaffected; the substitution is recorded once as a
`request_id_header_rejected` warning (the offending value itself is not
logged). When no header is supplied, a UUID4 is generated.

This value is a **correlation label only**. It is never an authentication
or authorization input, is never compared against anything, and grants no
access — do not treat it as a token. The narrow charset exists so the value
can be safely echoed into a response header (no CR/LF injection) and so a
hostile caller cannot write unbounded text into every log line of a
request. The `request_id` log key is deliberately outside the secret
redaction patterns in `apps/api/app/core/logging.py`, so correlation IDs
reach the log sink intact while credentials are still redacted.

## `GET /health` — liveness

Returns:

```json
{"status": "ok",
 "trading_mode": "<research|paper|live>",
 "live_trading_enabled": false,
 "live_order_reconciler": "DISABLED"}
```

No auth. Reflects the actual configured `Settings`, never a fabricated value.

`live_order_reconciler` (Phase 49, D066) is `"DISABLED"` or
`"enabled:<n>s"`, where `<n>` is `LIVE_ORDER_RECONCILER_INTERVAL_SECONDS`.
It reports only whether the background reconciliation **loop is running** —
never that anything can actually be reconciled. With
`live_trading_enabled: false` (the committed default) every cycle
short-circuits with `NOT_CONFIGURED` before touching the database or a
broker, so the two fields must be read together. Added as a key because the
reconciler is the only background job that reaches a real trading venue,
and whether it runs was otherwise visible only in a startup log line.

Every field is a plain read of in-process `Settings` — this endpoint still
performs no I/O and checks no dependency (see below). Keys are additive
across phases; consumers should read the keys they need and ignore the
rest.

This is a **liveness** probe (Phase 41, D054): it answers "is this process
alive", performs no I/O, checks no dependency, and therefore stays 200 even
while the database is unreachable. That is deliberate — an orchestrator
restarts a container whose liveness probe fails, and restarting the process
does not fix a down database. Do not use this endpoint to decide whether the
instance can serve traffic; use `/health/ready` for that.

## `GET /health/ready` — readiness

No auth. Runs a real `SELECT 1` against Postgres through the application's
own SQLAlchemy engine (so it exercises the same connection pool real
requests draw from), bounded by `HEALTH_READINESS_TIMEOUT_SECONDS`
(default `3.0`).

`200` when the database answered:

```json
{"status": "ready", "checks": {"database": {"status": "ok"}}}
```

`503` when it did not — same body shape, never FastAPI's `{"detail": ...}`,
so a consumer parses one schema in both cases:

```json
{"status": "not_ready",
 "checks": {"database": {"status": "error", "reason": "connection_failed",
                         "error_type": "ConnectionRefusedError"}}}
```

```json
{"status": "not_ready",
 "checks": {"database": {"status": "error", "reason": "timeout",
                         "timeout_seconds": 3.0}}}
```

`reason` is a fixed vocabulary: `connection_failed` or `timeout`. On
`connection_failed` the exception's **type name** is reported and its
message never is — a driver error message can carry the DSN, and the DSN
carries the database password (spec §38, `docs/TRADING_SAFETY.md`).

**No Redis check.** Redis is provisioned in `docker-compose.yml` and
`REDIS_URL` exists, but no Python code in this repository opens a Redis
connection yet — the `redis` package is an unused declared dependency.
Checking a connection the app never makes would be fabricated signal and
could fail the service over a dependency no request path needs. The check
belongs here when the first real Redis client lands, and not before.

## `POST /auth/login`

OAuth2 password flow (form-encoded, not JSON) — `username` carries the
email. No public registration endpoint exists; users are created via
`POST /admin/users` (D013) or, for the first admin, direct DB insert
(D010).

Request (form fields): `username=<email>&password=<password>`

Response (`TokenResponse`):
```json
{"access_token": "<jwt>", "token_type": "bearer"}
```

401 for an unknown email, wrong password, or inactive user — the response
doesn't distinguish which, and the route deliberately takes the same time
either way (see `login.py`'s `_DUMMY_HASH`) to avoid leaking which emails
are registered.

**423 for a locked-out account** (Phase 39, D049). After
`AUTH_MAX_FAILED_LOGIN_ATTEMPTS` consecutive failed password attempts
(default 5), the account is locked for `AUTH_LOCKOUT_DURATION_MINUTES`
(default 15) and even the correct password is refused:

```json
{"detail": "Account temporarily locked after repeated failed login attempts. Try again later."}
```

Details that matter to a client:

- **423 is only ever returned to a caller who supplied the CORRECT
  password.** A wrong password against a locked account still returns the
  generic 401, and an unknown email always returns 401 — so 423 can never
  be used to discover whether an email is registered. A client must not
  treat a 401 as proof that an account is *not* locked.
- The response does **not** say how much time is left, deliberately. There
  is no `Retry-After` header. "Later" is all a client can truthfully
  relay.
- A wrong password while locked does **not** extend the lock, so a third
  party cannot hold a user out indefinitely by guessing.
- A successful login resets the counter to zero; the lock expires on its
  own. There is still no unlock endpoint. Since Phase 46 (D063) a
  **successful password reset** also clears the lock, which is the closest
  thing to a recovery path — someone who forgot their password very likely
  locked themselves out guessing at it first, and leaving the lock in force
  would strand them behind a fresh, correct password.
- Set `AUTH_MAX_FAILED_LOGIN_ATTEMPTS=0` to disable the lockout entirely,
  in which case 423 is never returned.

## `POST /auth/password-reset/request`

Public and unauthenticated (Phase 46, D063) — it has to be, since a caller
who could authenticate would not need it.

Request: `{"email": "someone@example.com"}`

**Always 200, always with this exact body**, whatever happened:

```json
{"detail": "If that email address is registered, a password reset link has been issued for it. The link can be used once and expires shortly. If no email arrives, contact an administrator - this deployment may not have email delivery configured."}
```

Five branches converge on it: address not registered, account deactivated,
per-account throttle tripped, no email provider configured, and email send
failed. A client must not attempt to distinguish them and must render this
message rather than substituting one of its own — a UI that turned this
into "check your inbox" would reintroduce a claim the API deliberately does
not make, and one that is false on any deployment with no email provider.

Note the wording: it says a link was *issued*, not *sent*. That is the only
sentence true in all five cases.

- **429** when the per-IP throttle
  (`AUTH_PASSWORD_RESET_MAX_REQUESTS_PER_IP_PER_HOUR`, default 20) is
  exceeded. Keyed on the client address, never the email, so it reveals
  nothing about which addresses exist. It is an in-process, per-worker
  counter — a real rate limit belongs at the reverse proxy.
- The per-**account** limit
  (`AUTH_PASSWORD_RESET_MAX_REQUESTS_PER_HOUR`, default 5) never produces a
  429; it silently stops issuing tokens and returns the same 200, because a
  status that only fired for registered addresses would be the enumeration
  oracle this endpoint exists to close.
- No row is written for an unknown or inactive address, so storage cannot
  be used as an oracle either.

## `POST /auth/password-reset/confirm`

Public and unauthenticated. Redeems a token from a reset link.

Request: `{"token": "<from the link>", "new_password": "<at least 8 chars>"}`

Response (200): `{"detail": "Password updated. Sign in with your new password."}`

A successful reset also clears any active login lockout (D049) for that
account, and marks **every** other outstanding unused token for that user
as consumed — an older link in an older email stops working the moment a
newer one is used.

**400 for every failure, with one sentinel and no elaboration:**

```json
{"detail": "INVALID_OR_EXPIRED_TOKEN: this reset link is not valid. Reset links can be used once and expire; request a new one."}
```

Unknown token, expired token, already-used token, and a token whose user
has since been deactivated are indistinguishable on purpose — saying "this
expired" would confirm that a real reset was requested for a real account.
There is never a stack trace, and never a 404-vs-410 distinction a caller
could probe with.

422 (not 400) for a `new_password` under 8 characters or a missing field —
that is the request schema rejecting the body before any token is looked
at, so the token survives and can still be redeemed with a valid password.

This endpoint issues **no session**. A user signs in afterwards with the
password they just set.

## `POST /admin/users/{user_id}/password-reset`

Requires `admin:manage`. Issues a reset link for one user on an admin's
behalf (Phase 46, D063). Empty body.

Unlike the public endpoint, this one is specific: the caller already holds
`admin:manage` and named a real id, so there is nothing left to enumerate.

Response (201) with **no** email provider configured — the committed
default:

```json
{
  "user_id": "...",
  "expires_at": "2026-09-02T12:30:00Z",
  "delivery": "NOT_CONFIGURED_returned_directly",
  "reset_link": "http://localhost:3000/reset-password?token=..."
}
```

`reset_link` is a **live, single-use credential**, not a display string.
This is the whole point of the endpoint: on a self-hosted deployment with
no email vendor it is the only way a reset link reaches anybody.

Response (201) with an email provider configured:

```json
{"user_id": "...", "expires_at": "...", "delivery": "SENT", "reset_link": null}
```

`reset_link` is withheld once email works — otherwise configuring a
provider would not have changed who can obtain a link. `"SENT"` means the
vendor **accepted** the message; nothing in this system can observe an
inbox, and no field claims delivery.

- **404** for an unknown `user_id`.
- **400 `USER_INACTIVE:`** for a deactivated account — it cannot log in, so
  a working password for it would be theatre.
- **502 `EMAIL_DELIVERY_FAILED:`** when a configured provider refuses the
  message. There is deliberately no third `delivery` value meaning "we
  tried and it failed": the just-issued token is invalidated before the 502
  is raised, so a failed attempt never leaves a live capability behind a
  response nobody acted on.

There is deliberately **no** endpoint that lets an admin set a user's
password directly — an admin who could type it would know it, which is
strictly worse than handing over a single-use link the user redeems
themselves. `PATCH /admin/users/{user_id}` still has no password field.

## CSRF posture

The frontend's auth cookie is `httpOnly` with `SameSite=Lax` (D020), and
that is the app's **only** anti-CSRF defence — there is no anti-CSRF
token, deliberately (D050). `SameSite=Lax` withholds the cookie from every
cross-site request except a top-level GET navigation, and **no route in
this API or in `apps/web/` changes state on a GET** — every mutation is a
POST, PATCH, or DELETE. Adding a state-changing GET route would silently
break this and require a real CSRF token; don't. The backend itself is
authenticated by `Authorization: Bearer`, never by a cookie, so it is not
CSRF-reachable at all.

## `GET /auth/session`

Requires `Authorization: Bearer <token>` — any active user, no special
permission. Reports on the presented token; it never returns, renews, or
extends it. Added in D032 because the frontend keeps the JWT in an
httpOnly cookie (D020) that browser JS cannot read, so the UI has no
other truthful source for a session's remaining time.

Response (200, `SessionResponse`):
```json
{
  "user_id": "...",
  "email": "...",
  "issued_at": "2026-08-29T02:47:01Z",
  "expires_at": "2026-08-29T03:17:01Z",
  "expires_in_seconds": 1798
}
```

`expires_in_seconds` is computed server-side against the server's clock
and floored at 0, so a client with a skewed clock can't disagree with
what the API will actually accept.

401 for a missing, malformed, or already-expired token, and for a token
whose user has since been deactivated — the same `get_current_user`
fail-closed path every other authenticated route uses (D016), so this
endpoint never reports time remaining on a session the API would reject.
There is no refresh/renewal endpoint; a token's lifetime is fixed at
issue (D010).

## `POST /brokers/{broker_id}/trades`

Requires `Authorization: Bearer <token>` from `/auth/login` — 401 if
missing, invalid, expired, or the user is inactive (D010). Additionally
requires the user's role to grant the `trade:submit:paper` permission —
403 (`Missing required permission: ...`) if not (D011). Additionally
requires an explicit `BrokerGrant` for this specific `broker_id` — 403
(`No access grant for broker ...`) if the user hasn't been granted access
to it, even if they hold the permission (D012). Broker existence (404) is
checked before the grant, so an unknown `broker_id` reliably 404s rather
than 403ing.

**Paper vs live routing (D058).** This is the ONLY trade-submission
endpoint, and the broker row's `kind` — never anything in the request body
— decides which execution path it takes: `kind=paper` routes to the
in-process `PaperBrokerAdapter`, `kind=live` routes to the real
`LiveBrokerAdapter`. Flip a broker between the two with `PATCH
/admin/brokers/{broker_id}/mode` (see below); that is the per-broker
trading-mode toggle. A `kind=live` broker is never quietly served by the
simulator — if no live path is configured, the request is refused.

A `kind=live` broker additionally requires ALL of:

1. `"confirm": true` in this request's body — 400
   `LIVE_CONFIRMATION_REQUIRED:` if missing or false. Checked first, so an
   unconfirmed request is refused for that reason regardless of server
   configuration.
2. A configured live execution path: `TRADING_MODE=live`,
   `LIVE_TRADING_ENABLED=true`, and all three `LONGPORT_LIVE_*`
   credentials set together — 400 `NOT_CONFIGURED:` otherwise. **This is
   false on the committed defaults**, so a live-broker request is refused
   here out of the box.
3. The `trade:submit:live` permission **in addition to**
   `trade:submit:paper` — 403 otherwise.

Everything after those three gates is the same code a paper trade runs:
the emergency stop, the duplicate-order check, the deterministic Risk
Engine, and the Portfolio Manager all gate a live order identically. The
only other difference is that live trades are evaluated against the
tighter `LIVE_RISK_*` limits (5% max position, 20% max exposure, 1% risk
per trade) instead of the `RISK_*` ones (10%/50%/1%).

Request body (`TradeSubmissionRequest`):
```json
{
  "symbol": "AAPL",
  "side": "buy",
  "quantity": "10",
  "estimated_price": "100",
  "stop_price": "95",
  "market_data_as_of": null,
  "marks": {},
  "confirm": false
}
```
`confirm` (D058) defaults to `false`. It is REQUIRED for a `kind=live`
broker and IGNORED entirely for a `kind=paper` one — sending it against a
paper broker changes nothing. It gates a live trade; it does not select
one, so it cannot make a paper broker execute live or vice versa.
`estimated_price` is optional (D017). Supplied: it's authoritative and no
vendor is consulted; `market_data_as_of` defaults to the server's current
time if also omitted. Omitted: the server fetches a live quote from the
configured market data vendor and uses its price AND timestamp together
(never a caller-supplied `market_data_as_of` mixed with a vendor price) —
400 `NOT_CONFIGURED:` if no vendor is wired, 400 `NO_DATA_AVAILABLE:` if
the vendor has no data for the symbol, matching `GET /market-data/{symbol}/quote`'s
own error semantics.
`marks` must supply the current price for every OTHER open position on
this broker (not the traded symbol, which uses `estimated_price`) — a
missing mark for a held position is a 400 `DATA_UNAVAILABLE:`, never a
guessed price.

Response (`TradeSubmissionResponse`, always 200 unless the request itself
was malformed/broker not found):
```json
{
  "order_id": "...",
  "status": "filled",
  "approved": true,
  "block_reason": null,
  "detail": null,
  "portfolio_action": "approve",
  "portfolio_binding_constraint": null,
  "portfolio_detail": "No portfolio-level constraint is breached by this trade.",
  "portfolio_requested_quantity": "10",
  "fill_quantity": "10",
  "fill_price": "100"
}
```
A risk-rejected trade returns the same 200 shape with `status:
"rejected"`, `approved: false`, `block_reason` set to one of
`risk/models.py`'s `BlockReason` values, both fill fields `null`, and
every `portfolio_*` field `null` — the Portfolio Manager never ran,
because the Risk Engine stopped the proposal first (D029). A null
`portfolio_action` therefore means "never ran", never "approved".

The four `portfolio_*` fields (D029) report the trade-path Portfolio
Manager's own verdict, which is separate from `approved`:

- `portfolio_action`: `"approve"`, `"modify"`, or `"reject"`
  (`PortfolioAction` also declares `"request_more_research"` for spec §18
  fidelity, but this deterministic implementation never emits it).
- `portfolio_binding_constraint`: `"symbol_concentration"`,
  `"cash_reserve"`, or `"max_open_positions"` on a modify/reject; null on
  approve.
- `portfolio_requested_quantity`: the quantity the Portfolio Manager was
  given. On `"modify"` it is larger than `fill_quantity` — the trade was
  shrunk to fit a portfolio-level limit, and the resized quantity was
  re-checked by the Risk Engine before it reached the broker.

Because of this, `approved: true` with `status: "rejected"` is a real and
meaningful combination: the Risk Engine passed the trade and the
Portfolio Manager stopped it. Read `portfolio_action` (not `approved`) to
tell the two apart.

Error responses: 401 if unauthenticated/token invalid; 403 if authenticated
but missing the `trade:submit:paper` permission (or, on a live broker,
`trade:submit:live`); 404 if `broker_id` doesn't exist; 400 with a
`DATA_UNAVAILABLE:`-prefixed detail if a mark is missing for an existing
position.

Live-broker-only errors (D058): 400 `LIVE_CONFIRMATION_REQUIRED:` when
`confirm` is absent or false; 400 `NOT_CONFIGURED:` when no live execution
path is configured (the default); 502 `DATA_UNAVAILABLE:` when the live
account's cash/positions cannot be read (fail closed — the trade is never
evaluated against an unknown book); 502 `LIVE_BROKER_ERROR:` on a broker
connection failure; and 502 `LIVE_ORDER_UNCONFIRMED:` when the broker
ACCEPTED a real order but has reported no execution. That last one names
the real broker order id so the position can be reconciled by hand — no
fill is recorded and none is assumed (spec §57).

## `POST /brokers/{broker_id}/agent-trades`

Same auth/permission/broker-grant requirements as
`POST /brokers/{broker_id}/trades` (D018). Request (`AgentTradeRequest`):
```json
{"symbol": "AAPL.US", "directive": "moderate momentum long, tight stop", "marks": {}}
```
No price field — the `TraderAgent` proposes `side`/`quantity`/a stop
distance (percent of price); the price itself always comes from the same
live-quote path `POST /brokers/{broker_id}/trades` uses when
`estimated_price` is omitted (D017), never from the agent. The resulting
proposal goes through the identical Risk Engine path a human-submitted
trade uses — an oversized or otherwise invalid agent-proposed trade is
rejected the same way a human-submitted one would be, not silently
allowed through.

Response (200, `AgentTradeResponse` — `TradeSubmissionResponse` plus
`side`, `quantity`, `rationale`):
```json
{
  "order_id": "...", "status": "filled", "approved": true,
  "block_reason": null, "detail": null,
  "portfolio_action": "approve", "portfolio_binding_constraint": null,
  "portfolio_detail": "No portfolio-level constraint is breached by this trade.",
  "portfolio_requested_quantity": "1",
  "fill_quantity": "1", "fill_price": "123.45",
  "side": "buy", "quantity": "1", "rationale": "clean breakout above resistance"
}
```
This route shares `_execute_trade()` with the human-submitted route, so
the Portfolio Manager (D029) applies identically here. Note that
`quantity` is what the agent *proposed*: on `portfolio_action: "modify"`
the Portfolio Manager shrank it, and `fill_quantity` is what actually
traded.
`rationale` is the agent's one-sentence explanation — informational only,
never itself validated or acted on. If a `TechnicalAnalyst` is configured
(D019) it reads the same live quote and, if a `HistoryProvider` is also
configured (D021), real computed `SMA(20)`/`RSI(14)` values — its read
becomes optional context in the `TraderAgent`'s prompt; absence or
failure of either never blocks the request, and `rationale`/the response
shape are unaffected either way (there's no field exposing the
analyst's read directly in this response).

Error responses: same 401/403/404 as the human-submitted route, plus 400
`NOT_CONFIGURED:` if no LLM provider is wired
(`LLM_PROVIDER_BASE_URL`/`_API_KEY`/`_MODEL` not all set) or if no market
data vendor is wired; 502 `AGENT_OUTPUT_INVALID:` if the provider's
response isn't parseable/valid JSON matching the expected schema — never
a fabricated trade in either failure case.

Request/response shape unchanged since D018. As of Phase 16 (D019), when
a `TechnicalAnalyst` is configured (same `LLM_PROVIDER_*` connection as
the trader agent — no separate config), its read of the same live quote
is appended to the `TraderAgent`'s prompt as informational-only context,
invisible to the caller — no new request or response field. If no
analyst is configured, or its read fails/doesn't parse, the trade
proceeds exactly as before Phase 16 with no context appended — this never
produces a new error response, since the analyst is optional.

As of Phase 44 (D059) two more analysts join on exactly the same footing,
still with **no new request or response field** and no new error response:

- `FundamentalAnalyst` — narrates real company fundamentals (company name,
  vendor category, P/E, P/B, P/S, dividend yield, and an earnings yield
  derived deterministically as `100/PE`) fetched through a
  `FundamentalsProvider`.
- `NewsAnalyst` — narrates up to 10 real recent headlines (title +
  publication date) fetched through a `NewsProvider`, with the headline
  count and date range computed in code, not by the model.

Both are wired to the same already-credentialed Longbridge relationship as
the quote and history providers (`LONGPORT_APP_KEY`/`_APP_SECRET`/
`_ACCESS_TOKEN`, all three required together). Each analyst needs **both**
its LLM analyst and its data vendor configured to contribute anything: with
no vendor data there is nothing real to narrate, and narrating without data
is exactly what spec §57 forbids.

All three analysts now run concurrently and are independently
failure-isolated. Any of these — no analyst configured, no vendor
configured, a symbol the vendor doesn't cover (`DataUnavailableError`), a
vendor failure, or an unparseable LLM response — silently omits that one
analyst's paragraph of context. None of them can block the trade, change
the response shape, or supply a price, side, or quantity. A
`SentimentAnalyst` is deliberately **not** implemented — see D059.

## `GET /brokers/{broker_id}/portfolio`

Read-only (D022). Requires `Permission.VIEW_PORTFOLIO` (a new, weaker
permission than `SUBMIT_PAPER_TRADE` — a viewer doesn't need trade
rights) plus the usual `require_broker_access` grant for this specific
broker. Current marks travel as a JSON body on the GET
(`PortfolioMarksRequest`), same `dict[symbol, price]` shape as
`TradeSubmissionRequest.marks`:
```json
{"marks": {"AAPL": "120.00"}}
```
`marks` may be omitted entirely (defaults to `{}`) if the broker
currently holds no open positions.

Response (200, `PortfolioSnapshot`):
```json
{
  "broker_id": "...",
  "cash": "99000.00000000",
  "positions": [
    {
      "symbol": "AAPL",
      "quantity": "10.00000000",
      "avg_cost": "100.00000000",
      "current_value": "1200.00000000",
      "unrealized_pnl": "200.0000000000000000",
      "realized_pnl": "0"
    }
  ],
  "total_equity": "100200.00000000",
  "total_unrealized_pnl": "200.0000000000000000",
  "total_realized_pnl": "0"
}
```
`avg_cost`/`realized_pnl` are replayed from that symbol's `orders`/`fills`
history; `quantity` itself is the execution layer's current
`broker_positions` row, not a replay total. `total_realized_pnl` includes
every symbol ever traded on this broker, even ones with no position open
right now (a closed-out symbol's realized P&L isn't lost).

Optional query parameter `cost_basis_method` (D041) selects how those two
figures — and `unrealized_pnl`, which derives from the basis — are
computed:

| value | method |
| --- | --- |
| `average` (default) | Average-cost basis, the D022 behaviour: one running weighted-average cost per symbol, never moved by a sell. |
| `fifo` | Lot tracking, oldest open lot consumed first. |
| `lifo` | Lot tracking, newest open lot consumed first. |

Omitting the parameter returns exactly the D022 response, unchanged. An
unrecognised value is a **422**, never a silent fallback to `average`.

The same fill history gives three different `realized_pnl` figures — that
is correct, not an inconsistency. For buy 10 @ 100, buy 10 @ 110, sell
15 @ 120: `average` realizes 225 (basis 105), `fifo` 250 (basis 110),
`lifo` 200 (basis 100). `cash`, `quantity`, `current_value` and
`total_equity` are identical under all three; only the basis-derived
figures move. Under `fifo`/`lifo`, `avg_cost` is the weighted-average
price of the lots still open, so it does change as sells consume lots, and
is `0` once the position is fully closed (no held quantity, no basis) —
whereas `average` carries its last running average forward.

On this endpoint the method travels as a **query parameter**. The write
path takes it as a **body field** instead — see
`POST .../portfolio/snapshots` below. D041 originally scoped the choice to
this read-only endpoint because `portfolio_snapshots` recorded no method
column; Phase 36/D044 added that column, so the write path now offers it
too and every persisted row names the method that produced it.

Error responses: 401 (no/invalid token), 403 (missing `VIEW_PORTFOLIO` or
no `BrokerGrant` for this broker), 404 (unknown `broker_id`), 422
(unrecognised `cost_basis_method`), 400 `DATA_UNAVAILABLE:` if a
currently-held symbol's mark is missing from the request body — never a
guessed or stale price.

## `POST /backtests`

Runs the one hard-coded SMA(20)-crossover strategy (D025) against real
historical daily closes through the real Risk Engine, the real trade-path
Portfolio Manager (D035) and a fresh in-memory paper broker. Never touches
any broker's persisted state.
Requires `Authorization: Bearer <token>` from any authenticated, active
user — no `broker_id`, no `Permission`, no `BrokerGrant` (see
docs/DECISIONS.md D025 for why this endpoint is deliberately not scoped
to a broker or gated by a specific permission).

Request body (`BacktestRequest`):
```json
{
  "symbol": "AAPL.US",
  "start_date": "2026-08-24",
  "end_date": "2026-08-28",
  "starting_cash": "100000"
}
```
`end_date` must equal today (UTC) — `HistoryProvider` (D021) only exposes
the most recent N daily closes as of now, so an arbitrary past window
can't be honestly served (see D025's "Consequences" section). `start_date`
and `end_date` together determine how many trading days of history are
requested, on top of a fixed 20-day SMA warmup buffer.

Response (200, `BacktestResult`):
```json
{
  "symbol": "AAPL.US",
  "start_date": "2026-08-24",
  "end_date": "2026-08-28",
  "starting_cash": "100000",
  "final_equity": "99966.830",
  "total_return_pct": "-0.0331700",
  "num_trades": 1,
  "win_rate_pct": "0",
  "max_drawdown_pct": "0.1714300",
  "equity_curve": [
    {"date": "2026-08-24", "equity": "100000"},
    {"date": "2026-08-25", "equity": "100000"},
    {"date": "2026-08-26", "equity": "99863.600"},
    {"date": "2026-08-27", "equity": "99863.600"},
    {"date": "2026-08-28", "equity": "99966.830"}
  ],
  "portfolio_modified_trades": 0,
  "portfolio_modify_risk_blocked_trades": 0,
  "portfolio_rejected_trades": 0
}
```
`num_trades`/`win_rate_pct` are computed over completed round trips (a
BUY that opens a flat position, followed by the SELL that fully closes
it), not raw fill count. Every trade proposal in the run was gated by the
same Risk Engine a real paper trade uses — an all-cash BUY proposal that
exceeds `max_position_pct_of_equity` is sized down and retried once, same
as any other risk-gated trade in this system.

The three `portfolio_*` counters (added in D035) report what the trade-path
Portfolio Manager did during the run, using the same `PORTFOLIO_*` settings
the live trade path uses — no backtest-specific limits:
- `portfolio_modified_trades` — risk-approved signals it resized. Counts
  MODIFY decisions, not fills.
- `portfolio_modify_risk_blocked_trades` — the subset of those whose resized
  quantity was then rejected by the Risk Engine's mandatory re-evaluation,
  so nothing filled. Reported separately so a modify that filled and a
  modify that was blocked afterwards are never conflated.
- `portfolio_rejected_trades` — risk-approved signals it rejected outright.
Risk Engine rejections are not counted by any of these — the two gates stay
distinguishable in the audit trail (D029).

Error responses: 401 (no/invalid token), 400 `NOT_CONFIGURED:` (no
`HistoryProvider` configured — see D021/D015's Longbridge credential
gate), 400 `UNSUPPORTED_DATE_RANGE:` (`end_date` isn't today), 400
`DATA_UNAVAILABLE:` (the vendor has fewer closes than the requested
window plus warmup requires — never a shorter, silently-truncated
backtest), 502 `DATA_UNAVAILABLE:` (the vendor itself failed), 422
(`end_date <= start_date` or any other request-shape validation error).

## `POST /brokers/{broker_id}/portfolio/snapshots`

Persists a portfolio snapshot (D027). Computes a snapshot exactly as
`GET /brokers/{broker_id}/portfolio` does — same
`compute_portfolio_snapshot()` call, same `{"marks": {...}}` request
body, same `DATA_UNAVAILABLE:` discipline on a missing mark — and, only
if that computation succeeds, persists the result as a new append-only
row (never a fabricated, interpolated, or scheduled one; see D027 —
there is no cron/background job, a snapshot exists only because a caller
explicitly POSTed here). Gated by the same `Permission.VIEW_PORTFOLIO` +
`require_broker_access` as the read endpoint — see D027 for why creating
a persisted record of the current state is still a "view" capability,
not a stronger one.

Request body (`PortfolioSnapshotCaptureRequest`): the GET's `marks` plus an
optional `cost_basis_method` (D044):
```json
{
  "marks": {"AAPL": "130"},
  "cost_basis_method": "fifo"
}
```
`cost_basis_method` takes the same `average` (default) / `fifo` / `lifo`
values as the GET's query parameter, computes the snapshot under that
method **and records it on the persisted row**, so
`GET .../portfolio/history` can never present a FIFO row's realized P&L
as though it were average-cost. Omitting it persists exactly the
average-cost row this endpoint has always persisted. An unrecognised value
is a **422** and nothing is written.

Note it is a **body field here, not a query parameter** (the GET's is a
query parameter). A `?cost_basis_method=` on this POST's query string is
ignored — but the response echoes the method actually used, so the mistake
is visible rather than silently mislabelling stored history.

Response (201, `PortfolioSnapshotHistoryEntry`):
```json
{
  "id": "...",
  "broker_id": "...",
  "captured_at": "2026-08-28T13:35:16.948447Z",
  "cash": "99000.00000000",
  "positions": [
    {
      "symbol": "AAPL",
      "quantity": "10.00000000",
      "avg_cost": "100.00000000",
      "current_value": "1200.00000000",
      "unrealized_pnl": "200.00000000",
      "realized_pnl": "0"
    }
  ],
  "total_equity": "100200.00000000",
  "total_unrealized_pnl": "200.00000000",
  "total_realized_pnl": "0",
  "cost_basis_method": "average"
}
```

Error responses: 401, 403 (same as the GET), 404 (unknown `broker_id`),
400 `DATA_UNAVAILABLE:` on a missing mark for a held position — and, on
that 400, nothing is persisted (the DB write happens only after
`compute_portfolio_snapshot()` returns successfully).

## `GET /brokers/{broker_id}/portfolio/history`

Returns persisted snapshots for a broker (D027), oldest-to-newest by
`captured_at`. Gated the same way as the two routes above.

Query params: `limit` (default 50, max 500), `offset` (default 0).

Response (200, `PortfolioSnapshotHistoryResponse`):
```json
{
  "snapshots": [ /* list of PortfolioSnapshotHistoryEntry, oldest first */ ],
  "limit": 50,
  "offset": 0
}
```

Every entry carries the `cost_basis_method` it was captured under (D044).
**A client must group or filter by this field before treating the series
as one equity curve** — an `average` row and a `fifo` row off the same
fill history carry incomparable `realized_pnl`/`avg_cost` figures. Rows
written before D044's migration read `average`, which is what they are:
the write path was average-only until that phase, so this is a recorded
fact, not a default standing in for an unknown.

Error responses: 401, 403 (same as the GET above), 404 (unknown
`broker_id`). An empty `snapshots` list (200, not an error) means no
snapshot has ever been POSTed for this broker.

## `POST /admin/users`

Requires `Authorization: Bearer <token>` from a user whose role grants
`admin:manage` — 403 otherwise (D013). No public registration; this is
the only way to create a user via HTTP.

Request (`CreateUserRequest`): `{"email": "...", "password": "...",
"is_active": true, "role_id": null}` — `role_id` optional, 404 if it
doesn't reference an existing role.

Response (201, `CreateUserResponse`): `{"id": "...", "email": "...",
"is_active": true, "role_id": null}` — never includes the password or its
hash. 409 for a duplicate email.

## `PATCH /admin/users/{user_id}`

Requires `admin:manage` (D016). Request (`UpdateUserRequest`), all fields
optional: `{"is_active": false, "role_id": null}`. Only keys actually
present in the JSON body are applied — omit a key to leave it untouched;
send `"role_id": null` to unassign the role. `"is_active": null` is 422
(the field can be omitted or a real boolean, never null). An unknown
`role_id` is 404. Unknown `user_id` is 404.

This is how a user is deactivated — there is no separate delete/deactivate
endpoint (deleting the row would orphan `orders.submitted_by_user_id`).
Takes effect immediately: `get_current_user` re-checks `is_active` on
every request rather than trusting the JWT, so an already-issued token
for a just-deactivated user gets 401 on its very next request, with no
wait for token expiry. Response (200, `CreateUserResponse`).

## `POST /admin/roles`

Requires `admin:manage`. Request (`CreateRoleRequest`): `{"name": "...",
"description": null, "permissions": ["trade:submit:paper"]}`. Response
(201, `CreateRoleResponse`) echoes back the created row. 409 for a
duplicate name.

## `PATCH /admin/roles/{role_id}`

Requires `admin:manage` (D016). Request (`UpdateRoleRequest`), all fields
optional: `{"description": "...", "permissions": ["trade:submit:paper"]}`.
Same `model_fields_set` convention as the user PATCH. `name` is
deliberately not updatable here. Unknown `role_id` is 404. Response (200,
`CreateRoleResponse`).

Replacing `permissions` takes effect for every holder of the role on
their very next request — nothing caches a permission set, so an
already-issued token immediately reflects the new grant (or its absence).

## `POST /admin/brokers`

Requires `admin:manage`. Creates a broker row with an explicit `kind` —
the per-broker paper/live execution switch (D058). Before this endpoint,
broker rows could only be created by direct SQL insert.

```json
{
  "name": "Longbridge Live",
  "kind": "live",
  "provider": "longbridge",
  "is_active": false,
  "confirm_live": true
}
```

`confirm_live` is REQUIRED when `kind` is `"live"` and ignored otherwise —
400 `LIVE_KIND_CONFIRMATION_REQUIRED:` without it, and no row is created.
Creating a live-kind broker does NOT enable live trading: an order against
it still requires `TRADING_MODE=live`, `LIVE_TRADING_ENABLED=true`, the
`LONGPORT_LIVE_*` credentials, the `trade:submit:live` permission, and
`"confirm": true` on that individual trade request.

Returns 201 with the same `BrokerResponse` shape `GET /brokers/{id}`
returns.

## `PATCH /admin/brokers/{broker_id}/mode`

Requires `admin:manage`. **The per-broker paper/live trading-mode toggle
(D058).** Flips one broker between execution modes; `POST
/brokers/{broker_id}/trades` reads the result on every submission and
routes to `PaperBrokerAdapter` or `LiveBrokerAdapter` accordingly.

```json
{ "kind": "live", "confirm_live": true }
```

- `confirm_live` is required to flip TO live (400
  `LIVE_KIND_CONFIRMATION_REQUIRED:` without it) and ignored flipping to
  paper. Only the direction toward real money is made deliberately
  awkward; live → paper can only make the system safer.
- **409 if the broker has any recorded order.** `orders`/`fills` are
  append-only and record no per-order kind, so flipping a traded broker
  would make its simulated and real history indistinguishable. Create a
  new broker instead.
- **409 if the broker has a simulated cash/position book**
  (`broker_accounts`/`broker_positions` rows), so a simulated balance can
  never become the identity of a real account.
- 404 for an unknown `broker_id`. A no-op flip (the broker already has the
  requested kind) returns 200 without applying those checks.

The change is logged at WARNING with the actor, previous kind, and new
kind. Read a broker's current mode back with `GET /brokers/{broker_id}`.

## `POST /admin/broker-grants`

Requires `admin:manage`. Request (`CreateBrokerGrantRequest`):
`{"user_id": "...", "broker_id": "..."}`. Response (201,
`BrokerGrantResponse`): `{"id": "...", "user_id": "...", "broker_id":
"..."}`. 404 if the user or broker doesn't exist; 409 if the grant already
exists.

## `DELETE /admin/broker-grants/{grant_id}`

Requires `admin:manage`. 204 on success, 404 if the grant doesn't exist.
Revoking takes effect immediately — a subsequent trade attempt on that
broker by that user gets 403.

## `GET /admin/users`

Requires `admin:manage` (D031). Query params: `limit` (default 50, max
500 — 422 outside that range), `offset` (default 0, 422 if negative).
Ordered by `email` ascending, which makes `offset` paging deterministic
(`users` has no created_at column).

Response (200, `ListUsersResponse`):
```json
{
  "users": [ /* CreateUserResponse rows */ ],
  "limit": 50,
  "offset": 0
}
```

Each row is exactly the `CreateUserResponse` shape the create/update
routes return — never a password or password hash.

## `GET /admin/roles`

Requires `admin:manage` (D031). Same `limit`/`offset` contract as above,
ordered by `name` ascending. Response (200, `ListRolesResponse`):
`{"roles": [ /* CreateRoleResponse rows */ ], "limit": 50, "offset": 0}`.
`permissions` is returned in full — this is the only way to see what a
role currently grants without a direct DB query.

## `GET /admin/broker-grants`

Requires `admin:manage` (D031). Same `limit`/`offset` contract, ordered
by `(user_id, broker_id)` — that pair is unique, so the sort is total.
Response (200, `ListBrokerGrantsResponse`): `{"grants": [ /*
BrokerGrantResponse rows */ ], "limit": 50, "offset": 0}`. Each row's
`id` is the handle `DELETE /admin/broker-grants/{grant_id}` needs.

All three listings return an empty list (200, not an error) when the page
is past the end of the data. None of them take filter or search
parameters — a caller pages, it does not query (D031).

No delete endpoints for users/roles (D016 — would orphan FKs, deactivate/
clear-permissions instead). D013's original "no listing endpoints
anywhere" scope cut was closed by D031 above; D034's `GET /brokers`
(below) covers brokers, but only those the caller holds a grant for —
there is still no admin-wide broker listing, and none for orders or
fills.

## `POST /admin/emergency-stop`

Requires `admin:manage` (D039). Halts all trading platform-wide. Request
(`EmergencyStopRequest`): `{"reason": "..."}` — required, 1–500 chars and
non-blank after stripping (422 otherwise); there is no way to flip this
control without recording why.

Response (200, `EmergencyStopStatusResponse`):
```json
{
  "active": true,
  "source": "database",
  "reason": "vendor feed went stale",
  "actor_user_id": "…",
  "changed_at": "2026-08-29T11:37:22.042596Z"
}
```

Takes effect on the very next trade submission in every worker process —
no restart, no `.env` edit, no redeploy. Every trade then returns
`status: "rejected"` with `block_reason: "emergency_stop_active"`, exactly
as the `EMERGENCY_STOP_ACTIVE` block reason has always behaved; only the
source of the flag changed (D039). Each call appends a row to the
append-only `emergency_stop_events` table, including a repeat activation —
the table is a log of attempts to change the control, not a state cell.

## `POST /admin/emergency-stop/deactivate`

Requires `admin:manage` (D039). Resumes trading. Same request and response
shapes as above; `reason` is required here too. A separate URL rather than
a boolean on the activate route, so disabling a safety control can never
be the accidental result of a defaulted body.

## `GET /admin/emergency-stop`

Requires `Authorization: Bearer <token>` — any active user, no special
permission (D039, same scoping argument as D034's `GET /brokers`: knowing
you are blocked is not privileged information, only flipping the switch
is). Returns the same `EmergencyStopStatusResponse` shape, read from the
database, so it always reflects what the next trade will be evaluated
against.

`source` is `"database"` once any flip has ever been persisted, and
`"settings_default"` while the table is still empty and
`Settings.emergency_stop_active` (the `EMERGENCY_STOP_ACTIVE` env var) is
acting as the bootstrap default. In the `settings_default` case `reason`,
`actor_user_id`, and `changed_at` are `null` — not invented. Once a row
exists, the env var is never consulted again.

## `GET /brokers`

Requires `Authorization: Bearer <token>` — any active user, no special
permission (D034). Returns exactly the brokers the calling user holds a
`BrokerGrant` for; a user with no grants gets an empty list (200, not
403). Query params: `limit` (default 50, max 500 — 422 outside that
range), `offset` (default 0, 422 if negative) — same convention as D027/
D031. Ordered by `(name, id)`; `name` is not unique on `brokers`, so the
id is the tiebreaker that makes `offset` paging deterministic.

Response (200, `ListBrokersResponse`):
```json
{
  "brokers": [
    {
      "id": "…uuid…",
      "name": "Live Check Alpha",
      "kind": "paper",
      "provider": "paper-sim",
      "is_active": true
    }
  ],
  "limit": 50,
  "offset": 0
}
```

Those five fields are an explicit allow-list — no credential-shaped field
is ever returned. `is_active` is reported as stored, never filtered on:
an inactive broker the user holds a grant for is still a real fact about
their access.

## `GET /brokers/{broker_id}`

Same auth as above. 200 with a single `BrokerResponse` (the row shape
above) when the caller holds a grant for it; **404** if no broker with
that id exists; **403** if it exists but the caller holds no grant — the
same order and codes `require_broker_access` uses, so this route and the
trade/portfolio routes never disagree about a given `broker_id`.

## `GET /market-data/{symbol}/quote`

Requires `Authorization: Bearer <token>` — any active user, no special
permission (read-only, not a trading action). Example: `symbol=AAPL.US`
(Longbridge's symbol format — market suffix required, e.g. `.US`/`.HK`).

Response (200, `QuoteResponse`): `{"symbol": "AAPL.US", "price": "...",
"as_of": "...", "source": "longbridge"}`.

503 with a `NOT_CONFIGURED:`-prefixed detail if no market data vendor is
wired (`LONGPORT_APP_KEY`/`LONGPORT_APP_SECRET`/`LONGPORT_ACCESS_TOKEN`
not all set — D015). 404 with a `NO_DATA_AVAILABLE:`-prefixed detail if
the vendor has no quote for the symbol. Connected to trade submission as
of D017 — `POST /brokers/{broker_id}/trades` uses this same router when
`estimated_price` is omitted from the trade request; this endpoint lets a
caller preview the price such a submission would use.

## `GET /brokers/{broker_id}/orders`

Read-only order history (D065). Requires `Authorization: Bearer <token>`
from a user whose role grants `portfolio:view` **and** who holds a
`BrokerGrant` for this broker — the same `require_broker_access` gate as
`GET /brokers/{broker_id}/portfolio`, not a new scheme. Nothing on this
route writes.

Query params: `limit` (default 50, max 500 — 422 outside that range),
`offset` (default 0, 422 if negative) — same convention as D027/D031/D034.
Ordered **newest first** by `(submitted_at DESC, id DESC)`; the id
tiebreaker makes `offset` paging deterministic when two orders share a
server-side timestamp. Note this is the opposite direction from
`.../portfolio/history`, which is oldest-first because it is an equity
curve.

Response (200, `ListOrdersResponse`):
```json
{
  "orders": [
    {
      "id": "…uuid…",
      "broker_id": "…uuid…",
      "broker_kind": "paper",
      "symbol": "AAPL",
      "side": "buy",
      "quantity": "10.00000000",
      "estimated_price": "100.00000000",
      "stop_price": "95.00000000",
      "status": "filled",
      "risk_block_reason": null,
      "risk_detail": null,
      "portfolio_action": "approve",
      "portfolio_binding_constraint": null,
      "portfolio_detail": null,
      "portfolio_requested_quantity": "10.00000000",
      "submitted_by_user_id": "…uuid…",
      "submitted_at": "2026-09-02T23:49:04.315262Z",
      "fills": [
        {
          "id": "…uuid…",
          "order_id": "…uuid…",
          "quantity": "10.00000000",
          "fill_price": "100.00000000",
          "filled_at": "2026-09-02T23:49:04.315262Z"
        }
      ]
    }
  ],
  "limit": 50,
  "offset": 0
}
```

Things this shape does and does not claim:

- **`broker_kind` is joined from `brokers.kind`, not stored on the order.**
  `orders` has no paper/live column — `Broker.kind` is the one
  discriminator (spec §51, D058) — so this is the real broker row's kind
  for `broker_id`, never an inference about which adapter ran. It is
  repeated on every row so a blotter can label each line honestly.
- **There is no order-type field**, because there is no order-type column.
  `orders` records `side`/`quantity`/`estimated_price` and an optional
  `stop_price`; emitting a constant `"market"` would look like a recorded
  fact and is deliberately not done (D065).
- **`estimated_price` is the requested price; the executed price is on the
  fill.** They are equal under the paper broker by construction.
- **Rejected orders are included.** `status` is `filled` or `rejected` and
  never pending — a row is written once, after the decision, and never
  updated (§17). A rejection carries `risk_block_reason`/`risk_detail`
  when the Risk Engine stopped it and `portfolio_action: "reject"` when the
  Portfolio Manager did; a null `portfolio_action` means the Portfolio
  Manager never ran, never that it approved (D029).
- **`quantity` vs `portfolio_requested_quantity`** differ exactly when
  `portfolio_action` is `modify` — that is what makes a D029 resize
  visible instead of silently rewritten.
- `fills` is `[]` for a rejected order and has one entry for a filled one.
  It is a list rather than a single object because `fills` is a separate
  table precisely so partial fills are a later, schema-compatible addition.

Error responses: 401 (no/invalid token), 403 (missing `portfolio:view`, or
no `BrokerGrant` for this broker), 404 (unknown `broker_id`). An empty
`orders` list is a 200, not an error.

## `GET /brokers/{broker_id}/orders/{order_id}`

One order and its fills (D065) — the same `OrderResponse` object the
listing returns, rendered through the same code, so the two can never
disagree about a given order. Same auth, same 401/403/404 as above.

**404 when the order id is unknown *or* belongs to a different broker.**
The lookup is filtered on `broker_id` as well as `order_id`, so a caller
holding a grant on one broker cannot use this route to probe for order ids
on another. The two cases are deliberately indistinguishable.

## `GET /brokers/{broker_id}/fills`

Flat execution blotter (D065) — one row per actual fill, newest first by
`(filled_at DESC, id DESC)`. Same auth, same pagination, same error codes
as the orders listing.

`fills` carries no `broker_id` of its own, so scoping is a join through
`orders.broker_id`, enforced in the query. Each row repeats the parent
order's `symbol`/`side` (and the same joined `broker_kind`) so a blotter
line is readable without a second request, and keeps `order_id` so it can
always be traced back to the decision that produced it.

```json
{
  "fills": [
    {
      "id": "…uuid…",
      "order_id": "…uuid…",
      "quantity": "10.00000000",
      "fill_price": "100.00000000",
      "filled_at": "2026-09-02T23:49:04.315262Z",
      "broker_id": "…uuid…",
      "broker_kind": "paper",
      "symbol": "AAPL",
      "side": "buy"
    }
  ],
  "limit": 50,
  "offset": 0
}
```

This is **not** the orders listing with rejections filtered out — it is a
different grain. A rejected order contributes no row here and exactly one
row there.

## `POST /watchlists` — create

Requires `Authorization: Bearer <token>` — any active user, no special
permission and **no broker grant** (Phase 50). A watchlist is user-scoped
research state, not a broker-scoped resource.

Request: `{"name": "Semis"}`. `name` is 1–64 chars after trimming; blank
or over-length is a **422**. Names are not unique — two watchlists may
share one.

Response (**201**, `WatchlistResponse`):
```json
{"id": "…uuid…", "name": "Semis", "created_at": "…", "symbols": []}
```

There is no auto-created default watchlist. A user who has created none
has none, and `GET /watchlists` says so with an empty list — a read
endpoint never writes a row.

## `GET /watchlists` — list your own

Same auth. Returns exactly the calling user's own watchlists, each with
its stored symbols (no prices — see the quotes endpoint). Query params:
`limit` (default 50, max 500 — 422 outside that range), `offset`
(default 0, 422 if negative), same convention as D027/D031/D034. Ordered
by `(created_at, id)`; the id is the tiebreaker that makes `offset`
paging deterministic.

Response (200, `ListWatchlistsResponse`): `{"watchlists": [...], "limit":
50, "offset": 0}`. Another user's watchlist never appears.

## `DELETE /watchlists/{watchlist_id}`

Same auth, owner-only. **204** on success — the list's items go with it
(`ON DELETE CASCADE`, migration `0015`). **404** if no watchlist has that
id; **403** if one does but the caller does not own it — the same order
and codes `require_broker_access` uses, so "not there" and "not yours"
never answer alike. This is a real delete, not a soft one: a watchlist is
working state, not an audit trail.

## `POST /watchlists/{watchlist_id}/items` — add a symbol

Same auth, owner-only. Request: `{"symbol": "AAPL.US"}`. The symbol is
trimmed and upper-cased before storage, so `"aapl.us"` and `"AAPL.US"`
are the same entry.

**201** with the updated `WatchlistResponse`. **409** if the symbol is
already on this list (the database's unique constraint, not a
check-then-insert). **409** if the list already holds 200 symbols — the
per-list cap that bounds the quotes endpoint's vendor fan-out. **422**
for a blank symbol. **403/404** as above.

No vendor validation: a symbol is accepted whether or not any configured
provider knows it. An unpriceable symbol surfaces honestly at the quotes
endpoint rather than being refused here.

## `DELETE /watchlists/{watchlist_id}/items/{symbol}`

Same auth, owner-only. The path segment is normalized the same way, so
`.../items/aapl.us` removes `AAPL.US`. **204** on success, **404** if
that symbol is not on the list, **403/404** for the watchlist itself as
above.

## `GET /watchlists/{watchlist_id}/quotes`

Same auth, owner-only. Every symbol on the list with its live quote,
resolved through the **same** path as
`GET /market-data/{symbol}/quote` (`apps/api/app/marketdata/resolution.py`
— one function, two callers, so the two endpoints cannot drift apart).

Response (200, `WatchlistQuotesResponse`):
```json
{
  "watchlist_id": "…uuid…",
  "name": "Semis",
  "market_data_configured": true,
  "quotes": [
    {"symbol": "AAPL.US", "price": "195.25", "as_of": "…",
     "source": "longbridge", "unavailable": null},
    {"symbol": "NOSUCH.US", "price": null, "as_of": null, "source": null,
     "unavailable": "DATA_UNAVAILABLE: NO_DATA_AVAILABLE: no configured provider returned data for 'NOSUCH.US'. …"}
  ]
}
```

The response is **always exactly as long as the watchlist**. A symbol the
vendor cannot price keeps its row with `price`/`as_of`/`source` all null
and a `DATA_UNAVAILABLE:`-prefixed `unavailable` carrying the real
underlying cause — never a dropped row and never a stand-in price.

With **no market-data vendor wired at all** (D008/D015) this is still a
**200**: `market_data_configured` is `false` and every row is unavailable
with `DATA_UNAVAILABLE: NOT_CONFIGURED: …`. That deliberately differs
from `GET /market-data/{symbol}/quote`, which 503s for the same
condition — there the quote *is* the whole response, here it is one
column of a list the user is still entitled to see.

Symbols are resolved sequentially, not concurrently: the vendors behind
`MarketDataRouter` are rate-limited third parties, and the 200-symbol cap
plus sequential resolution keeps one page refresh from becoming a burst
of upstream requests.

Nothing else is implemented. Do not document endpoints that don't exist yet
— add them here as they ship, not in advance.
