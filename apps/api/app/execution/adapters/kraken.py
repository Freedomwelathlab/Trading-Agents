"""Kraken spot adapter — the bridge's first real venue (Phase 92, D111).

The first provider in `execution/registry.py` to move from `catalogued` to
`implemented`. Same `BrokerAdapter` Protocol as the paper and Longbridge
adapters, so the OMS, the risk gate and the Portfolio Manager treat a
Kraken order exactly like any other and no check can be skipped by routing
around them.

READ THIS BEFORE TOUCHING ANYTHING HERE.

**There is no Kraken SPOT sandbox.** Verified 2026-09-23 by probing the
hosts: `api.demo.kraken.com` does not resolve, and `demo-futures.kraken.com`
is Kraken FUTURES — a different product with a different API. An earlier
note in `docs/BROKER_INTEGRATION.md` recommended Kraken first partly
because "its sandbox mirrors live"; that was wrong and is corrected there.

What exists instead is **`validate=true` on AddOrder**, which runs
Kraken's own order validation and returns what it WOULD have done without
placing anything. That is the only honest rehearsal available, so this
adapter makes it a first-class mode (`validate_only`) rather than a
footnote: a live Kraken broker should be exercised through it before it is
ever armed for real.

**Symbols are passed through verbatim.** Kraken names one market three
ways — the key `XXBTZUSD`, the `altname` `XBTUSD` and the `wsname`
`XBT/USD` — and accepts more than one of them on input while returning the
key form. This adapter translates NONE of them, per D109's
no-unified-symbol-namespace rule: the caller's symbol is sent as given and
Kraken's own "Unknown asset pair" error is surfaced. A helpful mapping
here is exactly how two different instruments eventually become one.

**Nothing is fabricated.** A partial fill reports the quantity Kraken
actually executed; an accepted-but-unexecuted order raises
`OrderNotConfirmedError` carrying Kraken's own txid so the existing
reconciler can resolve it later; a missing balance is an error, never a
zero.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
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

KRAKEN_API_URL = "https://api.kraken.com"
API_VERSION = "0"


class KrakenError(Exception):
    """Kraken refused, or the response could not be read. Kraken returns
    HTTP 200 with a non-empty `error` array for most failures, so the
    status code alone is not a success signal and this adapter never
    treats it as one."""


class KrakenOrderNotConfirmedError(KrakenError, OrderNotConfirmedError):
    """Kraken accepted an order and reported no execution yet.

    Subclasses the port's `OrderNotConfirmedError` for the same reason
    `LiveOrderNotFilledError` does: the OMS attaches the risk/portfolio
    decision context on the way out and the existing reconciliation path
    resolves it, with no per-adapter special case.
    """

    def __init__(self, message: str, *, order_id: str) -> None:
        OrderNotConfirmedError.__init__(self, message, order_id=order_id)


class KrakenHttpClient(Protocol):
    """The one network seam, so every rule below is testable without a key.

    `private` posts a signed request; `public` gets an unsigned one. Both
    return Kraken's decoded JSON body — including its `error` array, which
    this adapter is responsible for reading.
    """

    def private(self, path: str, data: Mapping[str, str]) -> Mapping[str, Any]: ...

    def public(self, path: str, params: Mapping[str, str]) -> Mapping[str, Any]: ...


def kraken_signature(path: str, data: Mapping[str, str], secret: str) -> str:
    """Kraken's `API-Sign`: HMAC-SHA512 over `path + SHA256(nonce + body)`,
    keyed with the base64-DECODED private key.

    Written out here rather than left to a client library because every
    part of it is a place a signature silently comes out wrong and the
    venue answers `EAPI:Invalid key` — which looks identical to a wrong
    key. The nonce must be the same value in the hashed body and in the
    posted body, which is why the caller builds `data` once and passes the
    same mapping to both.
    """
    post = urllib.parse.urlencode(data)
    encoded = (data["nonce"] + post).encode()
    message = path.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def _decimal(raw: Any, *, field: str) -> Decimal:
    """Kraken sends numbers as strings. A value that will not parse is an
    error, never a zero: a zero fill price is a number the P&L would be
    computed from."""
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise KrakenError(f"Kraken returned an unreadable {field}: {raw!r}.") from exc


def _check(body: Mapping[str, Any], *, what: str) -> Mapping[str, Any]:
    """Read Kraken's envelope. HTTP 200 with a non-empty `error` array is
    the ORDINARY failure shape, so this is the only place a Kraken call is
    allowed to be called successful."""
    errors = body.get("error") or []
    if errors:
        raise KrakenError(f"Kraken refused {what}: {'; '.join(str(e) for e in errors)}")
    result = body.get("result")
    if not isinstance(result, Mapping):
        raise KrakenError(f"Kraken returned no result for {what}: {body!r}")
    return result


class KrakenAdapter:
    """Spot trading at Kraken, through the standard `BrokerAdapter` port.

    Synchronous, like every other adapter here, so it stays structurally
    interchangeable with the paper one rather than forcing the OMS to
    become async for one venue (the same trade-off `live_broker.py`
    records).
    """

    name = "kraken"

    def __init__(
        self,
        client: KrakenHttpClient,
        *,
        quote_currency: str = "USD",
        validate_only: bool = True,
    ) -> None:
        self._client = client
        self._quote = quote_currency
        self._validate_only = validate_only
        """Defaults to TRUE. Kraken has no spot sandbox, so the safe state
        for a freshly built adapter is the one that cannot place an order;
        arming it is a deliberate act by the code that constructs it, not
        the default that arrives with a copy-paste."""
        self._cash: Decimal | None = None
        self._positions: dict[str, Decimal] | None = None

    # --- account ----------------------------------------------------------

    def _load(self) -> None:
        """Read balances once per adapter instance.

        Kraken's `Balance` returns every asset the account holds, with no
        distinction between "cash" and "position" — they are all balances.
        This treats the configured quote currency as cash and every other
        asset as a position, which is what a spot account actually is.
        """
        if self._cash is not None and self._positions is not None:
            return
        body = self._client.private(f"/{API_VERSION}/private/Balance", self._nonce_data())
        result = _check(body, what="a balance read")

        cash: Decimal | None = None
        positions: dict[str, Decimal] = {}
        for asset, raw in result.items():
            amount = _decimal(raw, field=f"balance for {asset}")
            # Kraken prefixes some legacy asset codes (ZUSD, XXBT). Match
            # on the suffix so both `USD` and `ZUSD` resolve, without
            # inventing a general asset-name translation.
            if asset == self._quote or asset == f"Z{self._quote}":
                cash = amount
                continue
            if amount != 0:
                positions[asset] = amount

        if cash is None:
            raise KrakenError(
                f"Kraken reported no {self._quote} balance on this account. That is an "
                f"absent balance, not a zero one, and this adapter will not treat it as "
                f"cash it has not seen."
            )
        self._cash = cash
        self._positions = positions

    @staticmethod
    def _nonce_data(**extra: str) -> dict[str, str]:
        """A fresh nonce per request, monotonically increasing.

        Kraken rejects a nonce lower than the last one this key used, and
        a millisecond clock can repeat under two requests in the same
        millisecond. Nanoseconds do not.
        """
        return {"nonce": str(time.time_ns()), **extra}

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
        """`marks` must carry a price for every held asset — a missing mark
        is a data problem the caller resolves, never one this fills in."""
        self._load()
        assert self._cash is not None and self._positions is not None  # nosec
        exposure = Decimal(0)
        value = Decimal(0)
        for asset, quantity in self._positions.items():
            if quantity == 0:
                continue
            if asset not in marks:
                raise KrakenError(
                    f"No mark supplied for held asset {asset}; cannot value the account."
                )
            exposure += abs(quantity) * marks[asset]
            value += quantity * marks[asset]
        return AccountState(
            equity=self._cash + value, cash=self._cash, current_exposure=exposure
        )

    # --- orders -----------------------------------------------------------

    def submit_order(self, order: OrderRequest, *, market_price: Decimal) -> Fill:
        """Place one spot order and report only what Kraken says happened.

        `market_price` is the caller's own reference price and is NOT sent
        to Kraken — a market order has no price field, and a limit order
        carries the caller's `limit_price`. It is used for nothing here,
        which is deliberate: this adapter must never report a fill at a
        price the venue did not give it.
        """
        if order.quantity <= 0:
            raise KrakenError(f"Refusing to submit a non-positive quantity: {order.quantity}.")
        if order.order_type == "limit" and order.limit_price is None:
            raise KrakenError("A limit order needs a limit price.")

        data = self._nonce_data(
            pair=order.symbol,
            type="buy" if order.side is Side.BUY else "sell",
            ordertype=order.order_type,
            volume=format(order.quantity, "f"),
        )
        if order.order_type == "limit":
            assert order.limit_price is not None  # nosec - checked above
            data["price"] = format(order.limit_price, "f")
        if self._validate_only:
            data["validate"] = "true"

        body = self._client.private(f"/{API_VERSION}/private/AddOrder", data)
        result = _check(body, what=f"an order for {order.symbol}")

        if self._validate_only:
            # Kraken validated and placed nothing. Reporting a Fill here
            # would be fabricating the one thing this whole module exists
            # not to fabricate.
            raise KrakenError(
                f"VALIDATE_ONLY: Kraken accepted this order as valid and did NOT place it "
                f"({result.get('descr', {})}). Construct the adapter with "
                f"validate_only=False to trade for real."
            )

        txids = result.get("txid") or []
        if not isinstance(txids, Sequence) or not txids:
            raise KrakenError(f"Kraken accepted the order but returned no txid: {result!r}")
        order_id = str(txids[0])

        # AddOrder returns an id, never an execution. The fill has to be
        # read back, exactly as the Longbridge adapter does.
        return self._fill_from(order_id, order)

    def _fill_from(self, order_id: str, order: OrderRequest) -> Fill:
        body = self._client.private(
            f"/{API_VERSION}/private/QueryOrders", self._nonce_data(txid=order_id)
        )
        result = _check(body, what=f"a status read of order {order_id}")
        detail = result.get(order_id)
        if not isinstance(detail, Mapping):
            raise KrakenOrderNotConfirmedError(
                f"Kraken accepted order {order_id} but returned no detail for it yet.",
                order_id=order_id,
            )

        executed = _decimal(detail.get("vol_exec", "0"), field="executed volume")
        if executed <= 0:
            raise KrakenOrderNotConfirmedError(
                f"Kraken accepted order {order_id} (status "
                f"{detail.get('status', 'unknown')!r}) and has executed nothing yet. "
                f"No fill is reported; the reconciler resolves it by txid.",
                order_id=order_id,
            )

        price = _decimal(detail.get("price", "0"), field="average fill price")
        if price <= 0:
            raise KrakenError(
                f"Kraken reported {executed} executed on order {order_id} at a "
                f"non-positive average price {price}; refusing to record that fill."
            )

        return Fill(
            symbol=order.symbol,
            side=order.side,
            # The EXECUTED quantity, not the requested one. A partial fill
            # recorded at full size is a position the account does not hold.
            quantity=executed,
            fill_price=price,
            filled_at=datetime.now(UTC),
        )

    def cancel_order(self, order_id: str) -> bool:
        """True when Kraken reports it cancelled something.

        `count: 0` means there was nothing to cancel — already filled, or
        already gone — which is a real answer and not an error.
        """
        body = self._client.private(
            f"/{API_VERSION}/private/CancelOrder", self._nonce_data(txid=order_id)
        )
        result = _check(body, what=f"a cancel of order {order_id}")
        try:
            return int(result.get("count", 0)) > 0
        except (TypeError, ValueError) as exc:
            raise KrakenError(f"Kraken returned an unreadable cancel count: {result!r}") from exc


class HttpxKrakenClient:
    """The real network client. One place that touches the wire, so every
    rule in `KrakenAdapter` is tested against a stub instead of a key.

    The key travels in the `API-Key` header and the secret NEVER leaves
    this object — it is used to sign and is not stored anywhere that a log
    line, an error, or a response model can reach. Kraken's own error text
    is surfaced because it is about the request, not the credential.
    """

    def __init__(self, *, api_key: str, api_secret: str, base_url: str = KRAKEN_API_URL) -> None:
        self._key = api_key
        self._secret = api_secret
        self._base = base_url.rstrip("/")

    def private(self, path: str, data: Mapping[str, str]) -> Mapping[str, Any]:
        import httpx

        payload = dict(data)
        headers = {
            "API-Key": self._key,
            "API-Sign": kraken_signature(path, payload, self._secret),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(self._base + path, data=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
        except Exception as exc:  # noqa: BLE001 - re-raised as our own type
            raise KrakenError(f"Kraken request to {path} failed: {exc}") from exc
        if not isinstance(body, Mapping):
            raise KrakenError(f"Kraken returned a non-object body from {path}: {body!r}")
        return body

    def public(self, path: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        import httpx

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(self._base + path, params=dict(params))
                response.raise_for_status()
                body = response.json()
        except Exception as exc:  # noqa: BLE001 - re-raised as our own type
            raise KrakenError(f"Kraken request to {path} failed: {exc}") from exc
        if not isinstance(body, Mapping):
            raise KrakenError(f"Kraken returned a non-object body from {path}: {body!r}")
        return body


def build_kraken_adapter(credentials: Mapping[str, str]) -> KrakenAdapter:
    """Registry factory. All-or-nothing on credentials, like every other
    builder in this codebase (D015).

    **`validate_only` defaults to ON and is opt-out via an explicit
    credential field.** Kraken has no spot sandbox, so the state a
    freshly-wired broker arrives in is the one that cannot place an order.
    Turning it off is a deliberate act recorded in the credential set,
    which means it is visible in the admin UI rather than buried in a
    constructor somewhere.
    """
    key = credentials.get("api_key")
    secret = credentials.get("api_secret")
    if not key or not secret:
        raise KrakenError(
            "NOT_CONFIGURED: Kraken needs both api_key and api_secret; refusing to build "
            "an adapter that could not sign a request."
        )
    live = str(credentials.get("live_orders", "")).strip().lower() in {"true", "yes", "1"}
    return KrakenAdapter(
        HttpxKrakenClient(api_key=key, api_secret=secret),
        quote_currency=credentials.get("quote_currency") or "USD",
        validate_only=not live,
    )
