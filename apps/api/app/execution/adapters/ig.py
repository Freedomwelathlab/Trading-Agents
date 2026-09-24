"""IG Markets adapter — the bridge's route to Forex (Phase 93, D112).

Second implemented venue, and the first that quotes FX from a host a
cloud-deployed API can actually reach (D109's reachability finding).

READ THIS FIRST, BECAUSE IT CHANGES WHAT A POSITION MEANS HERE.

**Nothing at IG is the underlying.** Every instrument is a CFD or a spread
bet *over* the underlying: leveraged, carrying overnight financing, never
delivered. "Buy EUR/USD at IG" and "buy EUR/USD at IBKR" produce different
exposures with different costs, and a strategy measured on one does not
transfer to the other unchanged. Nothing in this adapter hides that; the
platform's own cost model does not yet price financing, which is recorded
as an open gap rather than silently absorbed.

**IG has a real demo environment** (`demo-api.ig.com`, verified reachable
2026-09-23) — unlike Kraken, where no spot sandbox exists at all (D111).
That makes IG the venue where a live adapter can genuinely be rehearsed,
and the `account_type` credential picks the host. Demo and live are
DIFFERENT HOSTS with different credentials, so a mix-up trades the wrong
account rather than failing; the adapter therefore refuses an
unrecognised `account_type` instead of defaulting to either one.

**A deal is not an order, and a position is not a quantity.** IG accepts a
deal and answers with a `dealReference`; whether it was actually accepted,
and at what level, is only knowable from a follow-up confirm. That maps
directly onto the port's `OrderNotConfirmedError`. And IG holds individual
DEALS, each with its own `dealId` — not a net quantity per instrument.
`positions` aggregates signed size per epic because the Risk Engine and
Portfolio Manager need a book-shaped view, and `open_deals()` exists
beside it because **closing requires a dealId that the aggregate cannot
carry**. Anything that tries to close a position from the aggregate alone
is working from a number that does not identify what it would close.
"""

from __future__ import annotations

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

IG_LIVE_URL = "https://api.ig.com/gateway/deal"
IG_DEMO_URL = "https://demo-api.ig.com/gateway/deal"

ACCOUNT_TYPES = {"LIVE": IG_LIVE_URL, "DEMO": IG_DEMO_URL}


class IgError(Exception):
    """IG refused, or the response could not be read. IG answers with a
    structured `errorCode` (e.g. `error.security.api-key-missing`), which
    is surfaced verbatim — it is about the request, never the secret."""


class IgOrderNotConfirmedError(IgError, OrderNotConfirmedError):
    """IG accepted a deal reference and has not confirmed the deal.

    Subclasses the port's own type so the existing reconciler resolves it
    with no IG-specific branch, exactly as the Kraken and Longbridge
    adapters do.
    """

    def __init__(self, message: str, *, order_id: str) -> None:
        OrderNotConfirmedError.__init__(self, message, order_id=order_id)


class IgHttpClient(Protocol):
    """The one network seam, so every rule below is testable without a
    login. Returns IG's decoded body plus its response headers, because
    the session tokens arrive in headers rather than in the body."""

    def request(
        self,
        method: str,
        path: str,
        *,
        version: str,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, Mapping[str, str], Mapping[str, Any]]: ...


def _decimal(raw: Any, *, field: str) -> Decimal:
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise IgError(f"IG returned an unreadable {field}: {raw!r}.") from exc


# What IG's login error codes mean, for an operator about to fix them.
#
# Measured against IG's own hosts on 2026-09-24 rather than taken from
# memory: a fabricated API key gets HTTP 403 `error.security.api-key-invalid`
# from BOTH demo-api.ig.com and api.ig.com. So `invalid-details` - which is a
# different status (401) and a different code - is IG accepting the key and
# rejecting the username/password paired with it. That distinction is the
# whole value of this table: "check your credentials" sends an operator to
# re-paste a key that was never the problem.
LOGIN_ERRORS = {
    "error.security.api-key-invalid": (
        "IG does not recognise this API key on the {host} host. Keys are issued per "
        "environment - a live key is refused by the demo host and the other way round - so "
        "check IG_ACCOUNT_TYPE matches the environment the key was created in."
    ),
    "error.security.invalid-details": (
        "IG ACCEPTED the API key and rejected the username/password for the {host} host. "
        "IG demo and live are separate logins with separate usernames and passwords, so "
        "the likeliest cause is a {host} key paired with the other environment's login. "
        "IG_USERNAME must be the IG username, not an email address. IG can lock an account "
        "after repeated failed logins, so correct the credentials before testing again "
        "rather than retrying."
    ),
}


def _check(status: int, body: Mapping[str, Any], *, what: str) -> Mapping[str, Any]:
    """IG uses real HTTP status codes AND an `errorCode` body. Both are
    read: a 200 carrying an errorCode is still a refusal."""
    code = body.get("errorCode")
    if status >= 400 or code:
        raise IgError(f"IG refused {what}: HTTP {status} {code or body!r}")
    return body


class IgAdapter:
    """CFD / spread-bet trading at IG, through the standard port.

    Synchronous like every other adapter here, so it stays structurally
    interchangeable with the paper one.
    """

    name = "ig"

    def __init__(
        self,
        client: IgHttpClient,
        *,
        api_key: str,
        username: str,
        password: str,
        account_type: str,
        deal_currency: str = "USD",
    ) -> None:
        normalised = (account_type or "").strip().upper()
        if normalised not in ACCOUNT_TYPES:
            # Never default. DEMO and LIVE are different hosts with
            # different money in them, and guessing picks one of them.
            raise IgError(
                f"IG account_type must be one of {', '.join(sorted(ACCOUNT_TYPES))}; "
                f"got {account_type!r}. There is no default: demo and live are separate "
                f"accounts and choosing for you would trade the wrong one."
            )
        self._client = client
        self._api_key = api_key
        self._username = username
        self._password = password
        self.account_type = normalised
        self.base_url = ACCOUNT_TYPES[normalised]
        self._deal_currency = deal_currency
        self._auth: dict[str, str] | None = None
        self._cash: Decimal | None = None
        self._positions: dict[str, Decimal] | None = None
        self._deals: list[dict[str, Any]] | None = None

    # --- session ----------------------------------------------------------

    def _headers(self, *, authed: bool = True) -> dict[str, str]:
        headers = {
            "X-IG-API-KEY": self._api_key,
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json; charset=UTF-8",
        }
        if authed:
            headers.update(self._login())
        return headers

    def _login(self) -> dict[str, str]:
        """Log in once per adapter instance and cache the two tokens.

        IG returns `CST` and `X-SECURITY-TOKEN` as response HEADERS, not
        in the body — a client that only reads JSON authenticates once and
        then 401s on everything after, which looks like a bad password.
        """
        if self._auth is not None:
            return dict(self._auth)
        status, headers, body = self._client.request(
            "POST",
            "/session",
            version="2",
            body={"identifier": self._username, "password": self._password},
            headers={
                "X-IG-API-KEY": self._api_key,
                "Content-Type": "application/json; charset=UTF-8",
                "Accept": "application/json; charset=UTF-8",
            },
        )
        code = body.get("errorCode")
        if code in LOGIN_ERRORS:
            # The venue's own code stays in the message: the translation
            # is guidance, and an operator searching IG's docs needs the
            # string IG actually sent.
            raise IgError(
                f"IG refused a login: HTTP {status} {code!r}. "
                + LOGIN_ERRORS[code].format(host=self.account_type)
            )
        _check(status, body, what="a login")
        lowered = {k.lower(): v for k, v in headers.items()}
        cst = lowered.get("cst")
        security = lowered.get("x-security-token")
        if not cst or not security:
            raise IgError(
                "IG accepted the login but returned no CST / X-SECURITY-TOKEN header, so "
                "no authenticated request can be made. Nothing is retried with an "
                "unauthenticated request."
            )
        self._auth = {"CST": cst, "X-SECURITY-TOKEN": security}
        return dict(self._auth)

    # --- account ----------------------------------------------------------

    def _load(self) -> None:
        if self._cash is not None and self._positions is not None:
            return
        status, _, body = self._client.request(
            "GET", "/accounts", version="1", headers=self._headers()
        )
        accounts = _check(status, body, what="an account read").get("accounts")
        if not isinstance(accounts, Sequence) or not accounts:
            raise IgError("IG returned no accounts; refusing to treat that as an empty one.")

        # The account IG itself marks preferred, else the first. Never a
        # sum across accounts: they are separate pots of money and an
        # order goes to exactly one of them.
        chosen = next(
            (a for a in accounts if isinstance(a, Mapping) and a.get("preferred")), accounts[0]
        )
        if not isinstance(chosen, Mapping):
            raise IgError(f"IG returned an unreadable account entry: {chosen!r}")
        balance = chosen.get("balance")
        if not isinstance(balance, Mapping) or "available" not in balance:
            raise IgError(
                f"IG account {chosen.get('accountId')!r} carries no available balance; "
                f"that is an absent figure, not zero cash."
            )
        # AVAILABLE, not `balance`: `balance` includes margin already
        # committed to open deals, and sizing against it would double-count
        # money the account cannot actually deploy.
        self._cash = _decimal(balance["available"], field="available balance")

        self._load_positions()

    def _load_positions(self) -> None:
        status, _, body = self._client.request(
            "GET", "/positions", version="2", headers=self._headers()
        )
        rows = _check(status, body, what="a position read").get("positions") or []
        aggregate: dict[str, Decimal] = {}
        deals: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            position = row.get("position")
            market = row.get("market")
            if not isinstance(position, Mapping) or not isinstance(market, Mapping):
                continue
            epic = market.get("epic")
            if not isinstance(epic, str):
                continue
            size = _decimal(position.get("size", "0"), field=f"position size for {epic}")
            signed = size if str(position.get("direction", "")).upper() == "BUY" else -size
            aggregate[epic] = aggregate.get(epic, Decimal(0)) + signed
            deals.append(
                {
                    "deal_id": position.get("dealId"),
                    "epic": epic,
                    "direction": position.get("direction"),
                    "size": size,
                    "level": position.get("level"),
                }
            )
        self._positions = {k: v for k, v in aggregate.items() if v != 0}
        self._deals = deals

    @property
    def cash(self) -> Decimal:
        self._load()
        assert self._cash is not None  # nosec - _load sets it or raises
        return self._cash

    @property
    def positions(self) -> dict[str, Decimal]:
        """Signed size per epic, for the Risk Engine's book-shaped view.

        This CANNOT close anything: IG closes a `dealId`, and the
        aggregate does not carry one. Use `open_deals()` for that.
        """
        self._load()
        assert self._positions is not None  # nosec
        return dict(self._positions)

    def open_deals(self) -> list[dict[str, Any]]:
        """Each open deal with its `dealId` — the only handle IG accepts
        to close one. Kept separate from `positions` so nothing can close
        a position from an aggregate that does not identify it."""
        self._load()
        return list(self._deals or [])

    def get_account_state(self, *, marks: dict[str, Decimal]) -> AccountState:
        self._load()
        assert self._cash is not None and self._positions is not None  # nosec
        exposure = Decimal(0)
        value = Decimal(0)
        for epic, quantity in self._positions.items():
            if quantity == 0:
                continue
            if epic not in marks:
                raise IgError(f"No mark supplied for open position {epic}; cannot value it.")
            exposure += abs(quantity) * marks[epic]
            value += quantity * marks[epic]
        return AccountState(
            equity=self._cash + value, cash=self._cash, current_exposure=exposure
        )

    # --- dealing ----------------------------------------------------------

    def submit_order(self, order: OrderRequest, *, market_price: Decimal) -> Fill:
        """Open one OTC position and report only what IG's confirm says.

        `market_price` is the caller's reference and is NOT sent: IG fills
        at its own level, and reporting the caller's price as a fill would
        record a number the venue never quoted.
        """
        if order.quantity <= 0:
            raise IgError(f"Refusing to deal a non-positive size: {order.quantity}.")
        if order.order_type == "limit" and order.limit_price is None:
            raise IgError("A limit deal needs a limit price.")

        payload: dict[str, Any] = {
            # The epic passes through verbatim, per D109 — IG's own
            # identifiers (CS.D.EURUSD.MINI.IP) are yet another namespace
            # and translating between namespaces is how two instruments
            # eventually become one.
            "epic": order.symbol,
            "direction": "BUY" if order.side is Side.BUY else "SELL",
            "size": format(order.quantity, "f"),
            "orderType": "MARKET" if order.order_type == "market" else "LIMIT",
            "currencyCode": self._deal_currency,
            "expiry": "-",
            "guaranteedStop": False,
            "forceOpen": True,
            "timeInForce": (
                "FILL_OR_KILL" if order.order_type == "limit" else "EXECUTE_AND_ELIMINATE"
            ),
        }
        if order.order_type == "limit":
            assert order.limit_price is not None  # nosec - checked above
            payload["level"] = format(order.limit_price, "f")

        status, _, body = self._client.request(
            "POST", "/positions/otc", version="2", body=payload, headers=self._headers()
        )
        result = _check(status, body, what=f"a deal on {order.symbol}")
        reference = result.get("dealReference")
        if not isinstance(reference, str) or not reference:
            raise IgError(f"IG accepted the request but returned no dealReference: {result!r}")

        return self._fill_from(reference, order)

    def _fill_from(self, deal_reference: str, order: OrderRequest) -> Fill:
        """Read the confirm. A dealReference is a receipt for a REQUEST —
        acceptance and the dealt level are only knowable from here."""
        status, _, body = self._client.request(
            "GET", f"/confirms/{deal_reference}", version="1", headers=self._headers()
        )
        confirm = _check(status, body, what=f"a confirm of {deal_reference}")

        deal_status = str(confirm.get("dealStatus", "")).upper()
        if deal_status == "REJECTED":
            # A rejection is a definite answer, not an unconfirmed order:
            # raising the reconcilable type here would leave the platform
            # waiting forever for a deal IG has already declined.
            raise IgError(
                f"IG REJECTED the deal on {order.symbol}: "
                f"{confirm.get('reason') or 'no reason given'} (ref {deal_reference})."
            )
        if deal_status != "ACCEPTED":
            raise IgOrderNotConfirmedError(
                f"IG has not confirmed deal {deal_reference} (dealStatus "
                f"{deal_status or 'absent'!r}). No fill is reported; the reconciler "
                f"resolves it by reference.",
                order_id=deal_reference,
            )

        level = confirm.get("level")
        size = confirm.get("size")
        if level is None or size is None:
            raise IgOrderNotConfirmedError(
                f"IG accepted deal {deal_reference} but reported no level and/or size, so "
                f"no honest fill can be recorded yet.",
                order_id=confirm.get("dealId") or deal_reference,
            )

        dealt_level = _decimal(level, field="dealt level")
        dealt_size = _decimal(size, field="dealt size")
        if dealt_level <= 0 or dealt_size <= 0:
            raise IgError(
                f"IG confirmed deal {deal_reference} at size {dealt_size} and level "
                f"{dealt_level}; refusing to record that as a fill."
            )

        return Fill(
            symbol=order.symbol,
            side=order.side,
            quantity=dealt_size,
            fill_price=dealt_level,
            filled_at=datetime.now(UTC),
        )

    def close_deal(self, deal_id: str, *, direction: str, size: Decimal) -> str:
        """Close one DEAL by its id, returning the new dealReference.

        Takes a `dealId` rather than an epic on purpose: IG holds separate
        deals on the same instrument, and closing "the EUR/USD position"
        is not a thing IG can be asked to do. `direction` is the CLOSING
        side — the opposite of the one that opened it — and is required
        from the caller rather than inferred, because inferring it from a
        stale local view is how a close becomes a doubling.
        """
        if size <= 0:
            raise IgError(f"Refusing to close a non-positive size: {size}.")
        closing = direction.strip().upper()
        if closing not in {"BUY", "SELL"}:
            raise IgError(f"Close direction must be BUY or SELL; got {direction!r}.")

        status, _, body = self._client.request(
            "POST",
            "/positions/otc",
            version="1",
            body={
                "dealId": deal_id,
                "direction": closing,
                "size": format(size, "f"),
                "orderType": "MARKET",
            },
            # IG tunnels the delete through POST with this header; sending
            # a real DELETE with a body is rejected.
            headers={**self._headers(), "_method": "DELETE"},
        )
        result = _check(status, body, what=f"a close of deal {deal_id}")
        reference = result.get("dealReference")
        if not isinstance(reference, str) or not reference:
            raise IgError(f"IG accepted the close but returned no dealReference: {result!r}")
        return reference


class HttpxIgClient:
    """The real network client. The password is used to log in and is not
    stored anywhere a log line, an error or a response model can reach."""

    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        *,
        version: str,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, Mapping[str, str], Mapping[str, Any]]:
        import httpx

        sent = {**(headers or {}), "Version": version}
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.request(
                    method, self._base + path, json=dict(body) if body is not None else None,
                    headers=sent,
                )
                decoded = response.json() if response.content else {}
        except Exception as exc:  # noqa: BLE001 - re-raised as our own type
            raise IgError(f"IG request to {path} failed: {exc}") from exc
        if not isinstance(decoded, Mapping):
            raise IgError(f"IG returned a non-object body from {path}: {decoded!r}")
        return response.status_code, dict(response.headers), decoded


def build_ig_adapter(credentials: Mapping[str, str]) -> IgAdapter:
    """Registry factory. All-or-nothing on credentials (D015), and no
    default account type — see `IgAdapter.__init__` for why guessing
    between demo and live is not an option."""
    missing = [
        name
        for name in ("api_key", "username", "password", "account_type")
        if not credentials.get(name)
    ]
    if missing:
        raise IgError(
            f"NOT_CONFIGURED: IG needs {', '.join(missing)}; refusing to build an adapter "
            f"that could not log in."
        )
    account_type = credentials["account_type"].strip().upper()
    return IgAdapter(
        HttpxIgClient(ACCOUNT_TYPES.get(account_type, IG_DEMO_URL)),
        api_key=credentials["api_key"],
        username=credentials["username"],
        password=credentials["password"],
        account_type=account_type,
        deal_currency=credentials.get("deal_currency") or "USD",
    )
