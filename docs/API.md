# API

## `GET /health`

Returns `{"status": "ok", "trading_mode": "<research|paper|live>", "live_trading_enabled": <bool>}`.
No auth yet (no auth exists in the codebase). Reflects the actual configured
`Settings`, never a fabricated value.

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
than 403ing. Only works against a broker row with `kind=paper`; a
`kind=live` broker returns 400 (no live execution path exists).

Request body (`TradeSubmissionRequest`):
```json
{
  "symbol": "AAPL",
  "side": "buy",
  "quantity": "10",
  "estimated_price": "100",
  "stop_price": "95",
  "market_data_as_of": null,
  "marks": {}
}
```
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
but missing the `trade:submit:paper` permission; 404 if `broker_id` doesn't
exist; 400 with a `NOT_CONFIGURED:`-prefixed detail if the broker isn't a
paper broker; 400 with a `DATA_UNAVAILABLE:`-prefixed detail if a mark is
missing for an existing position.

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
`avg_cost`/`realized_pnl` use average-cost basis (not FIFO/LIFO —
see docs/DECISIONS.md D022), replayed from that symbol's `orders`/`fills`
history; `quantity` itself is the execution layer's current
`broker_positions` row, not a replay total. `total_realized_pnl` includes
every symbol ever traded on this broker, even ones with no position open
right now (a closed-out symbol's realized P&L isn't lost).

Error responses: 401 (no/invalid token), 403 (missing `VIEW_PORTFOLIO` or
no `BrokerGrant` for this broker), 404 (unknown `broker_id`), 400
`DATA_UNAVAILABLE:` if a currently-held symbol's mark is missing from the
request body — never a guessed or stale price.

## `POST /backtests`

Runs the one hard-coded SMA(20)-crossover strategy (D025) against real
historical daily closes through the real Risk Engine and a fresh
in-memory paper broker. Never touches any broker's persisted state.
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
  ]
}
```
`num_trades`/`win_rate_pct` are computed over completed round trips (a
BUY that opens a flat position, followed by the SELL that fully closes
it), not raw fill count. Every trade proposal in the run was gated by the
same Risk Engine a real paper trade uses — an all-cash BUY proposal that
exceeds `max_position_pct_of_equity` is sized down and retried once, same
as any other risk-gated trade in this system.

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

Request body: identical `PortfolioMarksRequest` shape as the GET above.

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
  "total_realized_pnl": "0"
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

Nothing else is implemented. Do not document endpoints that don't exist yet
— add them here as they ship, not in advance.
