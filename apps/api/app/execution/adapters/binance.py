"""Binance spot adapter (Phase 97, D116).

Same `BrokerAdapter` Protocol as the paper, Longbridge, Kraken and IG
adapters, so an order routed here passes every gate an order routed
anywhere else does.

READ THIS BEFORE TOUCHING ANYTHING HERE.

**Binance is three venues, and the adapter is told which.** `binance.com`,
`binance.us` and the spot TESTNET are different hosts with different keys
and, for the first two, different legal entities. A key from one is simply
refused by another. So `environment` is a required credential with no
default - `LIVE`, `US` or `TESTNET` - and an unrecognised value is refused
rather than guessed, for the same reason IG's `account_type` is: a guess
picks one of several accounts, and a mix-up trades the wrong one instead of
failing.

**Binance HAS a spot testnet** (`testnet.binance.vision`, verified
reachable 2026-09-24) - unlike Kraken, which has none. Testnet keys are
issued separately (testnet.binance.vision, "Generate HMAC_SHA256 Key") and
the money in it is not real, so on TESTNET orders are placed for real by
default: that is the rehearsal. On LIVE and US the adapter defaults to
Binance's own `POST /api/v3/order/test`, which validates an order and places
nothing - the same posture as Kraken's `validate=true` - and arming it is
the explicit `live_orders` credential field.

**Location matters.** `api.binance.com` refuses requests from restricted
jurisdictions with HTTP 451, and the United States is one of them. This
platform's API runs on Railway in a US region, so a LIVE key is expected to
be refused there by LOCATION, not by credential. That refusal is translated
into those words rather than surfaced as a bare status code, because the
operator's natural response to "HTTP 451" is to regenerate a key that was
never the problem.

**Symbols are passed through verbatim** (`BTCUSDT`), per D109's
no-unified-symbol-namespace rule. Binance's own "Invalid symbol" is
surfaced as given.

**Nothing is fabricated.** A fill reports Binance's own `executedQty` and
the average price derived from Binance's own `cummulativeQuoteQty`; an
accepted-but-unexecuted order raises `OrderNotConfirmedError` carrying
`SYMBOL:orderId`, which is what a later status read or cancel needs -
Binance addresses an order by symbol AND id, never by id alone.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from apps.api.app.execution.broker import Fill, OrderNotConfirmedError, OrderRequest
from apps.api.app.risk.models import AccountState, Side

HOSTS = {
    "LIVE": "https://api.binance.com",
    "US": "https://api.binance.us",
    "TESTNET": "https://testnet.binance.vision",
}
DEFAULT_QUOTE = {"LIVE": "USDT", "US": "USD", "TESTNET": "USDT"}
RECV_WINDOW_MS = "5000"


class BinanceError(Exception):
    """Binance refused, or the response could not be read."""


class BinanceOrderNotConfirmedError(BinanceError, OrderNotConfirmedError):
    """Accepted, nothing executed yet. Subclasses the port's
    `OrderNotConfirmedError` so the OMS and the reconciler treat it like
    every other venue's."""

    def __init__(self, message: str, *, order_id: str) -> None:
        OrderNotConfirmedError.__init__(self, message, order_id=order_id)


# Binance's own error codes that an operator is about to act on, translated
# into what to change. The code and Binance's message always stay in the
# final text as well: the translation is guidance, and IG/Kraken/Binance
# docs are searched by the string the venue actually sent.
ERROR_ADVICE = {
    -2014: "The API key is malformed - check it was pasted whole, with no spaces.",
    -2015: (
        "Binance rejected the key, the source IP, or the key's permissions. Keys are "
        "issued per environment (binance.com, binance.us and the testnet are separate), "
        "so check BINANCE_ENVIRONMENT matches where the key was created; if the key has "
        "an IP allow-list, this server's IP must be on it; and a key used for trading "
        "needs 'Enable Spot & Margin Trading'."
    ),
    -1021: "This server's clock is outside Binance's receive window; the request was refused.",
    -1022: "Binance rejected the signature - the secret does not match the API key.",
}


class BinanceHttpClient(Protocol):
    """The one network seam. `signed` adds timestamp + signature and the key
    header; `public` sends nothing secret. Both return `(status, body)`."""

    def signed(
        self, method: str, path: str, params: Mapping[str, str]
    ) -> tuple[int, Any]: ...

    def public(self, path: str, params: Mapping[str, str]) -> tuple[int, Any]: ...


def binance_signature(query: str, secret: str) -> str:
    """HMAC-SHA256 of the exact query string, hex. Verified against the
    worked example in Binance's own API documentation (see the test)."""
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def _decimal(raw: Any, *, field: str) -> Decimal:
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BinanceError(f"Binance returned an unreadable {field}: {raw!r}.") from exc


def _check(status: int, body: Any, *, what: str, environment: str) -> Any:
    """Binance uses real HTTP status codes and a `{code, msg}` body."""
    if status == 451:
        raise BinanceError(
            f"Binance refused {what}: HTTP 451 - requests from this server's LOCATION are "
            f"restricted on the {environment} host (Binance blocks the United States, where "
            f"this API is deployed). This is not a credential problem; a US-based server needs "
            f"BINANCE_ENVIRONMENT=US with a Binance.US key, or the service must run elsewhere."
        )
    code = body.get("code") if isinstance(body, Mapping) else None
    refused = isinstance(code, int) and code < 0 and isinstance(body, Mapping) and "msg" in body
    if status >= 400 or refused:
        msg = body.get("msg") if isinstance(body, Mapping) else body
        advice = ERROR_ADVICE.get(int(code)) if isinstance(code, int) else None
        text = f"Binance refused {what}: HTTP {status} code {code} {msg!r}"
        raise BinanceError(f"{text}. {advice}" if advice else text)
    return body


class BinanceAdapter:
    """Spot trading at Binance through the standard port. Synchronous, like
    every adapter here."""

    name = "binance"

    def __init__(
        self,
        client: BinanceHttpClient,
        *,
        environment: str,
        quote_asset: str | None = None,
        validate_only: bool,
    ) -> None:
        if environment not in HOSTS:
            raise BinanceError(
                f"Unknown Binance environment {environment!r}; expected one of "
                f"{', '.join(HOSTS)}. Refusing to guess which venue to trade."
            )
        self._client = client
        self.environment = environment
        self._quote = quote_asset or DEFAULT_QUOTE[environment]
        self._validate_only = validate_only
        self._cash: Decimal | None = None
        self._positions: dict[str, Decimal] | None = None

    # --- account ----------------------------------------------------------

    def _load(self) -> None:
        """One `GET /api/v3/account` per adapter instance.

        Cash is the quote asset's FREE balance: `locked` is already
        committed to resting orders, and sizing against it double-counts
        money the account cannot deploy (the same reasoning IG's
        `available` follows). Every other asset with a non-zero total is a
        position, at free + locked, because a locked coin is still held.
        """
        if self._cash is not None and self._positions is not None:
            return
        status, body = self._client.signed("GET", "/api/v3/account", {})
        body = _check(status, body, what="an account read", environment=self.environment)
        balances = body.get("balances") if isinstance(body, Mapping) else None
        if not isinstance(balances, list):
            raise BinanceError(f"Binance returned no balances: {body!r}")
        cash: Decimal | None = None
        positions: dict[str, Decimal] = {}
        for row in balances:
            asset = str(row.get("asset"))
            free = _decimal(row.get("free", "0"), field=f"free balance for {asset}")
            locked = _decimal(row.get("locked", "0"), field=f"locked balance for {asset}")
            if asset == self._quote:
                cash = free
                continue
            if free + locked != 0:
                positions[asset] = free + locked
        if cash is None:
            # Binance lists every asset the account has EVER been able to
            # hold, so a missing quote asset means the configured quote is
            # wrong for this venue, not that the balance is zero.
            raise BinanceError(
                f"Binance reported no {self._quote} balance row on this account. That is an "
                f"absent balance, not a zero one; check BINANCE_QUOTE_ASSET."
            )
        self._cash = cash
        self._positions = positions

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
        """Needs a mark for every held asset, and refuses rather than
        inventing one - the Kraken rule, for the Kraken reason."""
        self._load()
        assert self._cash is not None and self._positions is not None  # nosec
        exposure = Decimal(0)
        value = Decimal(0)
        for asset, quantity in self._positions.items():
            if asset not in marks:
                raise BinanceError(
                    f"No mark supplied for held asset {asset}; cannot value the account."
                )
            exposure += abs(quantity) * marks[asset]
            value += quantity * marks[asset]
        return AccountState(equity=self._cash + value, cash=self._cash, current_exposure=exposure)

    # --- orders -----------------------------------------------------------

    def submit_order(self, order: OrderRequest, *, market_price: Decimal) -> Fill:
        """`market_price` is never sent and never becomes a fill price."""
        if order.quantity <= 0:
            raise BinanceError(f"Refusing to submit a non-positive quantity: {order.quantity}.")
        params: dict[str, str] = {
            "symbol": order.symbol,
            "side": "BUY" if order.side is Side.BUY else "SELL",
            "type": order.order_type.upper(),
            "quantity": format(order.quantity, "f"),
            "newOrderRespType": "FULL",
        }
        if order.order_type == "limit":
            if order.limit_price is None:
                raise BinanceError("A limit order needs a limit price.")
            params["price"] = format(order.limit_price, "f")
            params["timeInForce"] = "GTC"

        if self._validate_only:
            status, body = self._client.signed("POST", "/api/v3/order/test", params)
            _check(status, body, what=f"an order for {order.symbol}", environment=self.environment)
            raise BinanceError(
                f"VALIDATE_ONLY: Binance ({self.environment}) accepted this order as valid and "
                f"did NOT place it. Set BINANCE_LIVE_ORDERS=true to trade for real."
            )

        status, body = self._client.signed("POST", "/api/v3/order", params)
        body = _check(
            status, body, what=f"an order for {order.symbol}", environment=self.environment
        )
        return self._fill_from(body, order)

    def _fill_from(self, body: Mapping[str, Any], order: OrderRequest) -> Fill:
        raw_id = body.get("orderId")
        if raw_id is None:
            raise BinanceError(f"Binance accepted the order but returned no orderId: {body!r}")
        order_id = f"{order.symbol}:{raw_id}"
        executed = _decimal(body.get("executedQty", "0"), field="executed quantity")
        if executed <= 0:
            raise BinanceOrderNotConfirmedError(
                f"Binance accepted order {order_id} (status {body.get('status', 'unknown')!r}) "
                f"and has executed nothing yet. No fill is reported.",
                order_id=order_id,
            )
        quote_spent = _decimal(body.get("cummulativeQuoteQty", "0"), field="quote quantity")
        price = quote_spent / executed
        if price <= 0:
            raise BinanceError(
                f"Binance reported {executed} executed on {order_id} for a non-positive quote "
                f"amount {quote_spent}; refusing to record that fill."
            )
        return Fill(
            symbol=order.symbol,
            side=order.side,
            quantity=executed,
            fill_price=price,
            filled_at=datetime.now(UTC),
        )

    def cancel_order(self, order_id: str) -> bool:
        """`order_id` is `SYMBOL:orderId`, as this adapter issues it."""
        symbol, sep, raw_id = order_id.partition(":")
        if not sep or not symbol or not raw_id:
            raise BinanceError(
                f"Binance order ids here are SYMBOL:orderId; got {order_id!r}. Binance cannot "
                f"address an order by id alone."
            )
        status, body = self._client.signed(
            "DELETE", "/api/v3/order", {"symbol": symbol, "orderId": raw_id}
        )
        if isinstance(body, Mapping) and body.get("code") == -2011:
            return False  # Unknown order: already filled or gone. A real answer.
        body = _check(status, body, what=f"a cancel of {order_id}", environment=self.environment)
        return str(body.get("status", "")).upper() == "CANCELED"


class HttpxBinanceClient:
    """The real network client. The secret signs and is never stored where
    a log line, error or response model can reach it."""

    def __init__(self, *, api_key: str, api_secret: str, base_url: str) -> None:
        self._key = api_key
        self._secret = api_secret
        self._base = base_url.rstrip("/")

    def signed(self, method: str, path: str, params: Mapping[str, str]) -> tuple[int, Any]:
        import httpx

        query_params = {
            **params,
            "recvWindow": RECV_WINDOW_MS,
            "timestamp": str(int(time.time() * 1000)),
        }
        query = urllib.parse.urlencode(query_params)
        query += "&signature=" + binance_signature(query, self._secret)
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.request(
                    method,
                    f"{self._base}{path}?{query}",
                    headers={"X-MBX-APIKEY": self._key},
                )
        except Exception as exc:  # noqa: BLE001 - re-raised as our own type
            raise BinanceError(f"Binance request to {path} failed: {exc}") from exc
        return response.status_code, _json(response)

    def public(self, path: str, params: Mapping[str, str]) -> tuple[int, Any]:
        import httpx

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(self._base + path, params=dict(params))
        except Exception as exc:  # noqa: BLE001 - re-raised as our own type
            raise BinanceError(f"Binance request to {path} failed: {exc}") from exc
        return response.status_code, _json(response)


def _json(response: Any) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001 - a non-JSON body is reported as text
        return response.text[:300]


def build_binance_adapter(credentials: Mapping[str, str]) -> BinanceAdapter:
    """Registry factory. All-or-nothing on credentials (D015).

    Orders are PLACED by default only on TESTNET, where the money is not
    real; on LIVE and US the adapter validates and places nothing until
    `live_orders` is set - the Kraken posture, for venues with real money.
    """
    key = credentials.get("api_key")
    secret = credentials.get("api_secret")
    environment = str(credentials.get("environment", "")).strip().upper()
    if not key or not secret:
        raise BinanceError(
            "NOT_CONFIGURED: Binance needs both api_key and api_secret; refusing to build an "
            "adapter that could not sign a request."
        )
    if environment not in HOSTS:
        raise BinanceError(
            f"BINANCE_ENVIRONMENT must be one of {', '.join(HOSTS)}; got {environment!r}. "
            f"There is no default, because each is a different venue with different keys."
        )
    armed = str(credentials.get("live_orders", "")).strip().lower() in {"true", "yes", "1"}
    return BinanceAdapter(
        HttpxBinanceClient(api_key=key, api_secret=secret, base_url=HOSTS[environment]),
        environment=environment,
        quote_asset=credentials.get("quote_asset") or None,
        validate_only=not (armed or environment == "TESTNET"),
    )
