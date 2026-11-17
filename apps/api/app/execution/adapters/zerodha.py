"""Zerodha (Kite Connect v3) adapter - Indian cash equities (Phase 110, D137).

The first India venue in `execution/registry.py`. Same `BrokerAdapter`
Protocol as every other adapter, so the OMS, the risk gate and the
Portfolio Manager treat a Zerodha order like any other.

READ THIS BEFORE TOUCHING ANYTHING HERE.

**Written from Zerodha's published documentation and tested against a
stub. It has never been run against a real Kite account** - no key was
available when it was built. The connection check (an account and holdings
read, no order) is the first thing to run with a real key, and its
positions must be compared with Kite's own screen before `live_orders` is
ever set.

**There is no Kite sandbox.** What exists instead is `/margins/orders`,
which prices an order's margin and charges without placing it. That is
this adapter's `validate_only` mode, and it is the default.

**The access token dies every day.** Kite invalidates it at 06:00 IST
(a regulatory requirement, per Zerodha's docs). A new one needs a browser
login by the account holder; `scripts/zerodha_login.py` does the exchange.
An expired token comes back as `TokenException` and is reported in those
words - it is never retried and never treated as an empty account.

**Symbols are `EXCHANGE:TRADINGSYMBOL`, verbatim** - `NSE:INFY`,
`BSE:SBIN`, Kite's own key form. A bare `INFY` is refused rather than
given a default exchange: the same name on NSE and BSE is two order books
(D109, no unified symbol namespace).

**Delivery equity only.** Orders are sent with product `CNC`. An account
holding an intraday (`MIS`) or derivative (`NRML`) position is refused on
read, because this adapter does not model margin and would report an
equity figure that is wrong.

**What "held" means here (an assumption, stated so it can be checked).**
Kite splits one holding across two reads: `/portfolio/holdings` carries
settled and T+1 stock, and a delivery buy made today appears only in
`/portfolio/positions` until tomorrow. Held = holdings `quantity` +
`t1_quantity` + today's positive net `CNC` position. Selling a holding
today reduces the holdings quantity itself, so a negative net position is
not subtracted a second time. If Zerodha's behaviour differs from that
reading, the connection check will show a quantity that disagrees with
Kite - which is why it must be compared before arming.

**Nothing is fabricated.** A partial fill reports the quantity Kite
executed; an order accepted and not yet executed raises
`OrderNotConfirmedError` carrying Kite's order id; a missing balance is an
error, never a zero.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from apps.api.app.execution.broker import (
    Fill,
    OrderNotConfirmedError,
    OrderRequest,
)
from apps.api.app.risk.models import AccountState, Side

KITE_API_URL = "https://api.kite.trade"
KITE_LOGIN_URL = "https://kite.zerodha.com/connect/login"
KITE_VERSION = "3"
DELIVERY_PRODUCT = "CNC"
EXCHANGES = frozenset({"NSE", "BSE"})
"""Cash-equity exchanges only. NFO/BFO/CDS/MCX are derivatives and
commodities, which this adapter does not trade."""

_TERMINAL_UNFILLED = frozenset({"REJECTED", "CANCELLED"})


class ZerodhaError(Exception):
    """Kite refused, or the response could not be read."""


class ZerodhaOrderNotConfirmedError(ZerodhaError, OrderNotConfirmedError):
    """Kite accepted an order and reports no execution yet. Subclasses the
    port's `OrderNotConfirmedError` so the OMS attaches its decision
    context and the reconciliation path resolves it by order id."""

    def __init__(self, message: str, *, order_id: str) -> None:
        OrderNotConfirmedError.__init__(self, message, order_id=order_id)


class KiteHttpClient(Protocol):
    """The one network seam. Each call returns `(http_status, decoded_body)`;
    reading Kite's envelope is the adapter's job, not the client's."""

    def get(self, path: str) -> tuple[int, Any]: ...

    def post_form(self, path: str, data: Mapping[str, str]) -> tuple[int, Any]: ...

    def post_json(self, path: str, payload: Any) -> tuple[int, Any]: ...

    def delete(self, path: str) -> tuple[int, Any]: ...


def session_checksum(api_key: str, request_token: str, api_secret: str) -> str:
    """Kite's token-exchange checksum: SHA-256 of the three values joined
    in exactly this order, hex-encoded."""
    return hashlib.sha256(f"{api_key}{request_token}{api_secret}".encode()).hexdigest()


def split_symbol(symbol: str) -> tuple[str, str]:
    """`NSE:INFY` -> (`NSE`, `INFY`). Anything else is refused."""
    exchange, sep, tradingsymbol = symbol.partition(":")
    if not sep or not tradingsymbol or exchange not in EXCHANGES:
        raise ZerodhaError(
            f"Zerodha symbols are EXCHANGE:TRADINGSYMBOL with exchange NSE or BSE "
            f"(for example NSE:INFY); got {symbol!r}. No default exchange is assumed."
        )
    return exchange, tradingsymbol


def _decimal(raw: Any, *, field: str) -> Decimal:
    """Kite sends JSON floats. They are read through `str` so 352.95 does
    not become its binary expansion, and a value that will not parse is an
    error, never a zero."""
    if raw is None or isinstance(raw, bool):
        raise ZerodhaError(f"Kite returned no usable {field}: {raw!r}.")
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ZerodhaError(f"Kite returned an unreadable {field}: {raw!r}.") from exc


def _check(status: int, body: Any, *, what: str) -> Any:
    """Read Kite's envelope: `{"status": "success", "data": ...}` or
    `{"status": "error", "message": ..., "error_type": ...}`. Both the
    HTTP status and the envelope must say success."""
    if not isinstance(body, Mapping):
        raise ZerodhaError(f"Kite returned a non-object body for {what} (HTTP {status}).")
    if body.get("status") != "success" or status >= 400:
        kind = body.get("error_type") or f"HTTP {status}"
        message = body.get("message") or "no message"
        if kind == "TokenException":
            raise ZerodhaError(
                f"TOKEN_EXPIRED: Kite refused {what}: {message}. The access token is "
                f"invalidated daily at 06:00 IST; log in again (scripts/zerodha_login.py) "
                f"and store the new token."
            )
        raise ZerodhaError(f"Kite refused {what} ({kind}): {message}")
    if "data" not in body:
        raise ZerodhaError(f"Kite returned no data for {what}.")
    return body["data"]


class ZerodhaAdapter:
    """Indian delivery equity at Zerodha, through the standard port.
    Synchronous, like every other adapter here."""

    name = "zerodha"

    def __init__(self, client: KiteHttpClient, *, validate_only: bool = True) -> None:
        self._client = client
        self._validate_only = validate_only
        """Defaults to TRUE: a freshly built adapter cannot place an order."""
        self._cash: Decimal | None = None
        self._positions: dict[str, Decimal] | None = None

    # --- account ----------------------------------------------------------

    def _load(self) -> None:
        if self._cash is not None and self._positions is not None:
            return

        margins = _check(*self._client.get("/user/margins/equity"), what="a funds read")
        if not isinstance(margins, Mapping) or "net" not in margins:
            raise ZerodhaError(
                "Kite reported no net equity balance on this account. That is an absent "
                "balance, not a zero one."
            )
        cash = _decimal(margins["net"], field="net equity balance")

        held: dict[str, Decimal] = {}
        holdings = _check(*self._client.get("/portfolio/holdings"), what="a holdings read")
        if not isinstance(holdings, Sequence) or isinstance(holdings, (str, bytes)):
            raise ZerodhaError("Kite returned holdings that are not a list.")
        for row in holdings:
            key = self._row_key(row, what="holding")
            quantity = _decimal(row.get("quantity"), field=f"holding quantity for {key}")
            pending = _decimal(row.get("t1_quantity", 0), field=f"T+1 quantity for {key}")
            held[key] = held.get(key, Decimal(0)) + quantity + pending

        positions = _check(*self._client.get("/portfolio/positions"), what="a positions read")
        net = positions.get("net") if isinstance(positions, Mapping) else None
        if not isinstance(net, Sequence) or isinstance(net, (str, bytes)):
            raise ZerodhaError("Kite returned no net position list.")
        for row in net:
            key = self._row_key(row, what="position")
            quantity = _decimal(row.get("quantity"), field=f"position quantity for {key}")
            if quantity == 0:
                continue
            product = row.get("product")
            if product != DELIVERY_PRODUCT:
                raise ZerodhaError(
                    f"This account holds a {product} position in {key} ({quantity}). This "
                    f"adapter trades delivery equity ({DELIVERY_PRODUCT}) only and does not "
                    f"model margin, so it will not report an account it cannot value."
                )
            if quantity > 0:
                # Bought for delivery today; not in holdings until tomorrow.
                held[key] = held.get(key, Decimal(0)) + quantity

        self._cash = cash
        self._positions = {k: v for k, v in held.items() if v != 0}

    @staticmethod
    def _row_key(row: Any, *, what: str) -> str:
        if not isinstance(row, Mapping):
            raise ZerodhaError(f"Kite returned a {what} row that is not an object.")
        exchange, tradingsymbol = row.get("exchange"), row.get("tradingsymbol")
        if not exchange or not tradingsymbol:
            raise ZerodhaError(f"Kite returned a {what} with no exchange or tradingsymbol.")
        return f"{exchange}:{tradingsymbol}"

    @property
    def cash(self) -> Decimal:
        self._load()
        assert self._cash is not None  # nosec - _load sets it or raises
        return self._cash

    @property
    def positions(self) -> dict[str, Decimal]:
        self._load()
        assert self._positions is not None  # nosec - _load sets it or raises
        return dict(self._positions)

    def get_account_state(self, *, marks: dict[str, Decimal]) -> AccountState:
        """`marks` must carry a price for every held symbol - a missing
        mark is a data problem the caller resolves."""
        self._load()
        assert self._cash is not None and self._positions is not None  # nosec
        exposure = Decimal(0)
        value = Decimal(0)
        for symbol, quantity in self._positions.items():
            if symbol not in marks:
                raise ZerodhaError(
                    f"No mark supplied for held symbol {symbol}; cannot value the account."
                )
            exposure += abs(quantity) * marks[symbol]
            value += quantity * marks[symbol]
        return AccountState(equity=self._cash + value, cash=self._cash, current_exposure=exposure)

    # --- orders -----------------------------------------------------------

    def _order_fields(self, order: OrderRequest) -> dict[str, str]:
        if order.quantity <= 0:
            raise ZerodhaError(f"Refusing to submit a non-positive quantity: {order.quantity}.")
        if order.quantity != order.quantity.to_integral_value():
            raise ZerodhaError(
                f"Indian equities trade in whole shares; refusing quantity {order.quantity}."
            )
        if order.order_type == "limit" and order.limit_price is None:
            raise ZerodhaError("A limit order needs a limit price.")
        exchange, tradingsymbol = split_symbol(order.symbol)
        fields = {
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "transaction_type": "BUY" if order.side is Side.BUY else "SELL",
            "order_type": "LIMIT" if order.order_type == "limit" else "MARKET",
            "quantity": str(int(order.quantity)),
            "product": DELIVERY_PRODUCT,
        }
        if order.order_type == "limit":
            assert order.limit_price is not None  # nosec - checked above
            fields["price"] = format(order.limit_price, "f")
        return fields

    def submit_order(self, order: OrderRequest, *, market_price: Decimal) -> Fill:
        """Place one delivery order and report only what Kite says happened.
        `market_price` is the caller's reference and is not sent to Kite."""
        fields = self._order_fields(order)

        if self._validate_only:
            payload = [{**fields, "variety": "regular", "quantity": int(order.quantity),
                        "price": float(fields.get("price", 0)), "trigger_price": 0}]
            data = _check(
                *self._client.post_json("/margins/orders", payload),
                what=f"a margin check for {order.symbol}",
            )
            total = data[0].get("total") if isinstance(data, Sequence) and data else None
            # Kite priced it and placed nothing. Reporting a Fill here would
            # be fabricating one.
            raise ZerodhaError(
                f"VALIDATE_ONLY: Kite priced this order (margin required: {total}) and did "
                f"NOT place it. Set live_orders to trade for real."
            )

        fields["validity"] = "DAY"
        if fields["order_type"] == "MARKET":
            # Kite's own automatic price band for market orders.
            fields["market_protection"] = "-1"
        data = _check(
            *self._client.post_form("/orders/regular", fields),
            what=f"an order for {order.symbol}",
        )
        order_id = data.get("order_id") if isinstance(data, Mapping) else None
        if not order_id:
            raise ZerodhaError(f"Kite accepted the order but returned no order_id: {data!r}")
        return self._fill_from(str(order_id), order)

    def _fill_from(self, order_id: str, order: OrderRequest) -> Fill:
        """Kite returns an id on placement, never an execution. The order's
        history is read back and its LAST entry is its current state."""
        history = _check(
            *self._client.get(f"/orders/{order_id}"), what=f"a status read of order {order_id}"
        )
        if not isinstance(history, Sequence) or isinstance(history, (str, bytes)) or not history:
            raise ZerodhaOrderNotConfirmedError(
                f"Kite accepted order {order_id} but returned no history for it yet.",
                order_id=order_id,
            )
        latest = history[-1]
        if not isinstance(latest, Mapping):
            raise ZerodhaError(f"Kite returned an unreadable state for order {order_id}.")
        status = str(latest.get("status", "unknown"))
        executed = _decimal(latest.get("filled_quantity", 0), field="filled quantity")

        if executed <= 0:
            if status in _TERMINAL_UNFILLED:
                raise ZerodhaError(
                    f"Kite {status.lower()} order {order_id} with nothing executed: "
                    f"{latest.get('status_message') or 'no reason given'}"
                )
            raise ZerodhaOrderNotConfirmedError(
                f"Kite accepted order {order_id} (status {status!r}) and has executed "
                f"nothing yet. No fill is reported; it is resolved by order id.",
                order_id=order_id,
            )

        price = _decimal(latest.get("average_price"), field="average fill price")
        if price <= 0:
            raise ZerodhaError(
                f"Kite reported {executed} executed on order {order_id} at a non-positive "
                f"average price {price}; refusing to record that fill."
            )
        return Fill(
            symbol=order.symbol,
            side=order.side,
            # The EXECUTED quantity, not the requested one.
            quantity=executed,
            fill_price=price,
            filled_at=datetime.now(UTC),
        )

    def cancel_order(self, order_id: str) -> bool:
        """True when Kite accepts the cancel. An order that is already
        complete is refused by Kite, and that refusal is surfaced."""
        data = _check(
            *self._client.delete(f"/orders/regular/{order_id}"),
            what=f"a cancel of order {order_id}",
        )
        return isinstance(data, Mapping) and bool(data.get("order_id"))


class HttpxKiteClient:
    """The real network client. The token travels in the `Authorization`
    header and is never placed in an error, a log line or a URL."""

    def __init__(self, *, api_key: str, access_token: str, base_url: str = KITE_API_URL) -> None:
        self._headers = {
            "X-Kite-Version": KITE_VERSION,
            "Authorization": f"token {api_key}:{access_token}",
        }
        self._base = base_url.rstrip("/")

    def _send(self, method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
        import httpx

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.request(
                    method, self._base + path, headers=self._headers, **kwargs
                )
        except Exception as exc:  # noqa: BLE001 - re-raised as our own type
            raise ZerodhaError(
                f"Kite request to {path} failed: {type(exc).__name__}"
            ) from exc
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, None

    def get(self, path: str) -> tuple[int, Any]:
        return self._send("GET", path)

    def post_form(self, path: str, data: Mapping[str, str]) -> tuple[int, Any]:
        return self._send("POST", path, data=dict(data))

    def post_json(self, path: str, payload: Any) -> tuple[int, Any]:
        return self._send("POST", path, json=payload)

    def delete(self, path: str) -> tuple[int, Any]:
        return self._send("DELETE", path)


def exchange_request_token(
    *, api_key: str, api_secret: str, request_token: str, base_url: str = KITE_API_URL
) -> str:
    """Trade the one-time `request_token` from a browser login for the
    day's `access_token`. Used by `scripts/zerodha_login.py`; nothing on
    the order path calls it."""
    import httpx

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                base_url.rstrip("/") + "/session/token",
                headers={"X-Kite-Version": KITE_VERSION},
                data={
                    "api_key": api_key,
                    "request_token": request_token,
                    "checksum": session_checksum(api_key, request_token, api_secret),
                },
            )
            body = response.json()
    except Exception as exc:  # noqa: BLE001 - re-raised as our own type
        raise ZerodhaError(f"Kite token exchange failed: {type(exc).__name__}") from exc
    data = _check(response.status_code, body, what="a token exchange")
    token = data.get("access_token") if isinstance(data, Mapping) else None
    if not token:
        raise ZerodhaError("Kite completed the token exchange but returned no access_token.")
    return str(token)


def build_zerodha_adapter(credentials: Mapping[str, str]) -> ZerodhaAdapter:
    """Registry factory. All-or-nothing on credentials (D015), and
    validate-only unless `live_orders` says otherwise."""
    key = credentials.get("api_key")
    token = credentials.get("access_token")
    if not key or not token:
        raise ZerodhaError(
            "NOT_CONFIGURED: Zerodha needs both api_key and access_token; refusing to build "
            "an adapter that could not authenticate."
        )
    live = str(credentials.get("live_orders", "")).strip().lower() in {"true", "yes", "1"}
    return ZerodhaAdapter(
        HttpxKiteClient(api_key=key, access_token=token), validate_only=not live
    )
