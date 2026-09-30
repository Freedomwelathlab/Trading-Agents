"""IBKR and moomoo, reached through the bridge agent (Phase 104, D124).

Both venues expose their API only through a gateway process on the
operator's own PC - IBKR's Client Portal Gateway, moomoo's OpenD - and this
API runs on Railway, which cannot reach that machine's localhost (D109).
`BridgedAdapter` implements the same `BrokerAdapter` Protocol as every
other venue, but every call becomes a job in `bridge_jobs` that the agent
on the PC pulls, executes against the local gateway and answers.

READ THIS BEFORE TOUCHING ANYTHING HERE.

**Orders default to VALIDATE-ONLY**, exactly like Kraken (D111). With no
`live_orders` credential field set, `submit_order` asks the agent for
IBKR's what-if preview (or moomoo's trading-info pre-check), places
NOTHING, and RAISES - returning a `Fill` for an order that was never
placed would fabricate the one thing this module exists not to fabricate.

**A timeout is never an answer.** The channel's errors say exactly what
happened (never queued / never claimed / claimed and silent / refused);
none of them becomes a zero balance or an assumed fill. An order the agent
claimed and did not report is `BRIDGE_OUTCOME_UNKNOWN`, which tells the
operator to check the venue before retrying.

**A fill is written only from the venue's own terminal answer.** An order
the venue reports still working raises `OrderNotConfirmedError` carrying
the venue's order id, so the existing reconciliation path owns it - the
same port every other adapter uses. A partially filled order that is still
working is NOT reported as a partial fill: the safety rules record partial
fills only once the venue reports the order finished.

**Symbols pass through verbatim** (D109's no-unified-symbol rule). At IBKR
an order is addressed by CONTRACT ID (`conid`), so a symbol must be a conid
(digits, or `conid:NNN`); this adapter does not map tickers to contracts,
because a helpful mapping is exactly how two instruments become one. IBKR
positions are therefore keyed by conid too, so a sell addresses the same
key the position was reported under. moomoo codes (`US.AAPL`, `HK.00700`)
pass through as moomoo writes them.

**No secret is ever in a job.** The platform holds only PUBLIC fields -
which account, which environment, whether orders are armed. The IBKR login
and the moomoo trade password stay on the PC.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from apps.api.app.bridge.channel import (
    BridgeChannel,
    BridgeNotConfiguredError,
    DatabaseBridgeChannel,
)
from apps.api.app.bridge.store import BRIDGED_PROVIDERS, JobKind
from apps.api.app.execution.broker import Fill, OrderNotConfirmedError, OrderRequest
from apps.api.app.risk.models import AccountState

MOOMOO_TRADE_ENVS = ("SIMULATE", "REAL")

_CONID = re.compile(r"^(?:conid:)?(\d{1,12})$", re.IGNORECASE)
_MOOMOO_CODE = re.compile(r"^[A-Z]{2}\.[A-Za-z0-9.\-]{1,24}$")

_FILLED_STATES = frozenset({"filled", "cancelled"})
"""Terminal states that can carry an executed quantity. A cancelled order
that executed part of its size before being cancelled is a finished
partial fill, and reporting that executed part is the truthful answer."""


class BridgedVenueError(Exception):
    """The venue (through the agent) answered, and the answer is not a
    usable result: a validate-only order, a refused order, a malformed
    reply."""


def _decimal(raw: Any, *, what: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise BridgedVenueError(f"The bridge agent returned an unreadable {what}: {raw!r}") from exc
    if not value.is_finite():
        raise BridgedVenueError(f"The bridge agent returned a non-finite {what}: {raw!r}")
    return value


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"true", "yes", "1"}


class BridgedAdapter:
    def __init__(
        self,
        provider: str,
        *,
        channel: BridgeChannel,
        account_id: str,
        currency: str = "USD",
        live_orders: bool = False,
        broker_id: uuid.UUID | None = None,
        context: Mapping[str, str] | None = None,
    ) -> None:
        if provider not in BRIDGED_PROVIDERS:
            raise ValueError(f"{provider!r} is not a bridged venue.")
        if not account_id.strip():
            raise ValueError(f"NOT_CONFIGURED: {provider} needs an account_id.")
        self._provider = provider
        self._channel = channel
        self._account_id = account_id.strip()
        self._currency = currency.strip().upper() or "USD"
        self._validate_only = not live_orders
        self._broker_id = broker_id
        self._context = dict(context or {})
        self._cash: Decimal | None = None
        self._positions: dict[str, Decimal] | None = None

    # --- plumbing -----------------------------------------------------------

    def _call(self, kind: JobKind, **fields: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"account_id": self._account_id, **self._context, **fields}
        return self._channel.call(
            provider=self._provider, kind=kind, payload=payload, broker_id=self._broker_id
        )

    # --- account ------------------------------------------------------------

    @property
    def cash(self) -> Decimal:
        if self._cash is None:
            result = self._call(JobKind.ACCOUNT_READ, currency=self._currency)
            balances = result.get("cash_by_currency")
            if not isinstance(balances, Mapping) or not balances:
                raise BridgedVenueError(
                    f"The {self._provider} account read returned no cash balances: {result!r}"
                )
            if self._currency not in balances:
                raise BridgedVenueError(
                    f"The {self._provider} account reports no {self._currency} cash row "
                    f"(it reports {', '.join(sorted(map(str, balances)))}). Set the "
                    f"account_currency credential field to one of those; this adapter will "
                    f"not substitute another currency's cash."
                )
            self._cash = _decimal(balances[self._currency], what=f"{self._currency} cash")
        return self._cash

    @property
    def positions(self) -> dict[str, Decimal]:
        if self._positions is None:
            result = self._call(JobKind.POSITIONS_READ)
            rows = result.get("positions")
            if not isinstance(rows, list):
                raise BridgedVenueError(
                    f"The {self._provider} positions read returned no position list: {result!r}"
                )
            held: dict[str, Decimal] = {}
            for row in rows:
                if not isinstance(row, Mapping) or "symbol" not in row:
                    raise BridgedVenueError(f"Unreadable position row from the agent: {row!r}")
                quantity = _decimal(row.get("quantity"), what=f"quantity of {row['symbol']}")
                if quantity != 0:
                    held[str(row["symbol"])] = held.get(str(row["symbol"]), Decimal(0)) + quantity
            self._positions = held
        return dict(self._positions)

    def get_account_state(self, *, marks: dict[str, Decimal]) -> AccountState:
        """`marks` must price every held symbol; a missing mark is a data
        problem the caller resolves, never one this fills in."""
        cash = self.cash
        exposure = Decimal(0)
        value = Decimal(0)
        for symbol, quantity in self.positions.items():
            if symbol not in marks:
                raise BridgedVenueError(
                    f"No mark supplied for held {self._provider} position {symbol}; cannot "
                    f"value the account."
                )
            exposure += abs(quantity) * marks[symbol]
            value += quantity * marks[symbol]
        return AccountState(equity=cash + value, cash=cash, current_exposure=exposure)

    # --- orders -------------------------------------------------------------

    def _venue_symbol(self, symbol: str) -> str:
        if self._provider == "ibkr":
            match = _CONID.match(symbol.strip())
            if match is None:
                raise BridgedVenueError(
                    f"IBKR orders are addressed by contract id (conid), not {symbol!r}. "
                    f"Resolve the contract explicitly (IBKR's /iserver/secdef/search) and pass "
                    f"its conid; this adapter does not map tickers to contracts (D109)."
                )
            return match.group(1)
        if _MOOMOO_CODE.match(symbol.strip()) is None:
            raise BridgedVenueError(
                f"moomoo codes carry their market prefix (US.AAPL, HK.00700), not {symbol!r}."
            )
        return symbol.strip()

    def submit_order(self, order: OrderRequest, *, market_price: Decimal) -> Fill:
        """`market_price` is the caller's reference and is never sent: a
        market order has no price, and a fill is recorded only at the price
        the venue reports."""
        if order.quantity <= 0:
            raise BridgedVenueError(
                f"Refusing to submit a non-positive quantity: {order.quantity}."
            )
        if order.order_type == "limit" and (order.limit_price is None or order.limit_price <= 0):
            raise BridgedVenueError("A limit order needs a positive limit price.")
        fields: dict[str, Any] = {
            "symbol": self._venue_symbol(order.symbol),
            "side": order.side.value,
            "quantity": format(order.quantity, "f"),
            "order_type": order.order_type,
        }
        if order.limit_price is not None and order.order_type == "limit":
            fields["limit_price"] = format(order.limit_price, "f")

        if self._validate_only:
            preview = self._call(JobKind.ORDER_VALIDATE, **fields)
            raise BridgedVenueError(
                f"VALIDATE_ONLY: {self._provider} checked this order through the bridge and "
                f"NOTHING was placed ({preview.get('summary') or preview!r}). Set the "
                f"live_orders credential field to `true` only when you mean to trade."
            )

        result = self._call(JobKind.ORDER_SUBMIT, **fields)
        return self._fill_from(result, order)

    def _fill_from(self, result: Mapping[str, Any], order: OrderRequest) -> Fill:
        order_id = str(result.get("order_id") or "").strip()
        if not order_id:
            raise BridgedVenueError(
                f"{self._provider} returned no order id for a submitted order: {result!r}. "
                f"Check the venue's order list - it may or may not have been placed."
            )
        state = str(result.get("state") or "unknown").lower()
        venue_status = result.get("venue_status")
        if state == "rejected":
            raise BridgedVenueError(
                f"{self._provider} REJECTED order {order_id} ({venue_status!r}): "
                f"{result.get('detail') or ''}".strip()
            )
        filled = _decimal(result.get("filled_quantity") or "0", what="filled quantity")
        if state in _FILLED_STATES and filled > 0:
            price = _decimal(result.get("avg_price") or "0", what="average fill price")
            if price <= 0:
                raise BridgedVenueError(
                    f"{self._provider} reported {filled} executed on order {order_id} at a "
                    f"non-positive average price {price}; refusing to record that fill."
                )
            return Fill(
                symbol=order.symbol,
                side=order.side,
                quantity=filled,
                fill_price=price,
                filled_at=datetime.now(UTC),
            )
        if state == "cancelled":
            raise BridgedVenueError(
                f"{self._provider} closed order {order_id} unfilled ({venue_status!r})."
            )
        raise OrderNotConfirmedError(
            f"{self._provider} accepted order {order_id} (status {venue_status!r}) and has not "
            f"reported it finished. No fill is reported; reconcile it by the venue's id.",
            order_id=order_id,
        )

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        """The venue's own answer, normalised by the agent. Read-only."""
        return self._call(JobKind.ORDER_STATUS, order_id=order_id)

    def cancel_order(self, order_id: str) -> bool:
        """True when the venue reports a cancel accepted. Allowed in
        validate-only mode too: cancelling can only reduce exposure."""
        result = self._call(JobKind.CANCEL, order_id=order_id)
        return bool(result.get("cancelled"))


def build_bridged_adapter(provider: str, credentials: Mapping[str, str]) -> BridgedAdapter:
    """Registry factory for `ibkr` and `moomoo`.

    Must be called from async code (the probe does): the channel captures
    the running loop so the adapter's synchronous calls can be scheduled
    back onto it from a worker thread.
    """
    from apps.api.app.core.config import BRIDGE_AGENT_TOKEN_MIN_LENGTH, get_settings
    from apps.api.app.db.base import get_session_factory

    settings = get_settings()
    if not settings.bridge_enabled:
        raise BridgeNotConfiguredError(
            f"NOT_CONFIGURED: the bridge agent is disabled - set BRIDGE_AGENT_TOKEN (at least "
            f"{BRIDGE_AGENT_TOKEN_MIN_LENGTH} characters) on the platform and the same value in "
            f"the agent's .env on the PC that runs the {provider} gateway."
        )
    account_id = str(credentials.get("account_id") or "").strip()
    if not account_id:
        raise ValueError(f"NOT_CONFIGURED: {provider} needs account_id.")
    context: dict[str, str] = {}
    if provider == "moomoo":
        trade_env = str(credentials.get("trade_env") or "").strip().upper()
        if trade_env not in MOOMOO_TRADE_ENVS:
            raise ValueError(
                f"moomoo trade_env must be SIMULATE or REAL, not {trade_env!r}. They are "
                f"different accounts, so there is no default."
            )
        context["trade_env"] = trade_env
        context["market"] = str(credentials.get("market") or "US").strip().upper()
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    return BridgedAdapter(
        provider,
        channel=DatabaseBridgeChannel(
            loop=loop, session_factory=get_session_factory(), settings=settings
        ),
        account_id=account_id,
        currency=str(credentials.get("account_currency") or "USD"),
        live_orders=_truthy(credentials.get("live_orders")),
        context=context,
    )
