# API

## `GET /health`

Returns `{"status": "ok", "trading_mode": "<research|paper|live>", "live_trading_enabled": <bool>}`.
No auth yet (no auth exists in the codebase). Reflects the actual configured
`Settings`, never a fabricated value.

## `POST /brokers/{broker_id}/trades`

No auth (anyone who can reach the API can submit a paper trade on any
`broker_id` — see `docs/DECISIONS.md` D009). Only works against a broker
row with `kind=paper`; a `kind=live` broker returns 400 (no live execution
path exists).

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
`market_data_as_of` defaults to the server's current time if omitted.
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

Error responses: 404 if `broker_id` doesn't exist; 400 with a
`NOT_CONFIGURED:`-prefixed detail if the broker isn't a paper broker; 400
with a `DATA_UNAVAILABLE:`-prefixed detail if a mark is missing for an
existing position.

Nothing else is implemented. Do not document endpoints that don't exist yet
— add them here as they ship, not in advance.
