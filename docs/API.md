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
  "fill_quantity": "10",
  "fill_price": "100"
}
```
A rejected trade returns the same 200 shape with `status: "rejected"`,
`approved: false`, `block_reason` set to one of `risk/models.py`'s
`BlockReason` values, and both fill fields `null`.

Error responses: 401 if unauthenticated/token invalid; 403 if authenticated
but missing the `trade:submit:paper` permission; 404 if `broker_id` doesn't
exist; 400 with a `NOT_CONFIGURED:`-prefixed detail if the broker isn't a
paper broker; 400 with a `DATA_UNAVAILABLE:`-prefixed detail if a mark is
missing for an existing position.

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

No delete endpoints for users/roles (D016 — would orphan FKs, deactivate/
clear-permissions instead), and no listing endpoints anywhere (D013 —
deliberate scope cut, not an oversight).

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
