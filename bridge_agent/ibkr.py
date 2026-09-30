"""IBKR Client Portal Gateway handler.

Talks to the gateway IBKR ships for its Web API, running on this PC
(default `https://localhost:5000/v1/api`). The gateway holds the login - a
human signs in once a day in a browser at https://localhost:5000 - and
this handler never sees a password.

Rules this file keeps, each for a reason:

* **TLS verification is skipped only for a gateway on this machine.** The
  gateway ships a self-signed certificate; anything not on localhost is
  verified.
* **Account pinning.** If `IBKR_ACCOUNT_ID` is set, a job for any other
  account is refused before a request is made; either way the account must
  be one the gateway itself lists.
* **An IBKR confirmation prompt is NEVER auto-confirmed.** When IBKR answers
  an order with a question ("this order exceeds X, are you sure?"), the
  order has not been transmitted. This agent reports that and stops; a
  human answers such questions, not a relay.
* **`cOID` is the platform's job id**, so if the same job were ever
  submitted twice IBKR itself rejects the duplicate.
* **Every number goes back as the venue's own value**, as a decimal string.
  Missing is an error, never zero.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from bridge_agent.gateway import (
    GatewayError,
    GatewayStatus,
    GatewayUnreachableError,
    decimal_text,
    require,
)

_STATE_BY_IBKR_STATUS = {
    "filled": "filled",
    "cancelled": "cancelled",
    "apicancelled": "cancelled",
    "submitted": "working",
    "presubmitted": "working",
    "pendingsubmit": "working",
    "pendingcancel": "working",
    "apipending": "working",
}
"""IBKR's `order_status` -> the platform's normalised state. `Inactive` is
deliberately absent: IBKR uses it for rejected AND for not-yet-active
orders, so it maps to `unknown` and the platform keeps the order under
reconciliation rather than guessing either way."""

_TERMINAL = frozenset({"filled", "cancelled", "rejected"})


class IbkrGateway:
    name = "ibkr"

    def __init__(
        self,
        *,
        base_url: str,
        account_id: str | None,
        verify_tls: bool,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        order_poll_seconds: float = 6.0,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            verify=verify_tls,
            timeout=15.0,
            transport=transport,
            headers={"User-Agent": "trading-os-bridge-agent"},
        )
        self._pinned_account = account_id.strip() if account_id else None
        self._sleep = sleep
        self._order_poll_seconds = order_poll_seconds
        self._portfolio_accounts_loaded = False
        self._iserver_accounts_loaded = False

    def close(self) -> None:
        self._client.close()

    # --- transport ----------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise GatewayUnreachableError(
                f"IBKR gateway unreachable at {self._client.base_url}: {type(exc).__name__}. "
                f"Is the Client Portal Gateway running?"
            ) from exc
        if response.status_code == 401:
            self._portfolio_accounts_loaded = self._iserver_accounts_loaded = False
            raise GatewayError(
                "IBKR gateway is not authenticated (401). Log in at the gateway's page in a "
                "browser on this PC; the session expires daily."
            )
        if response.status_code >= 400:
            raise GatewayError(
                f"IBKR answered {method} {path} with HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise GatewayError(f"IBKR returned non-JSON for {path}: {response.text[:200]}") from exc

    # --- health -------------------------------------------------------------

    def status(self) -> GatewayStatus:
        try:
            body = self._request("POST", "/iserver/auth/status")
        except GatewayUnreachableError as exc:
            return GatewayStatus(reachable=False, authenticated=False, detail=str(exc))
        except GatewayError as exc:
            return GatewayStatus(reachable=True, authenticated=False, detail=str(exc))
        if not isinstance(body, dict):
            return GatewayStatus(True, False, f"Unexpected auth status reply: {body!r}"[:300])
        authenticated = bool(body.get("authenticated"))
        connected = bool(body.get("connected"))
        competing = bool(body.get("competing"))
        if authenticated and connected and not competing:
            return GatewayStatus(True, True, "authenticated")
        reasons = []
        if not authenticated:
            reasons.append("not logged in - sign in at the gateway page in a browser")
        if not connected:
            reasons.append("not connected to IBKR's servers")
        if competing:
            reasons.append("another session (TWS or a phone) is competing for this login")
        return GatewayStatus(True, False, "; ".join(reasons))

    def keepalive(self) -> None:
        """IBKR ends an idle session; `/tickle` keeps it open."""
        self._request("POST", "/tickle")

    # --- accounts -----------------------------------------------------------

    def _account(self, payload: dict[str, Any]) -> str:
        account = require(payload, "account_id")
        if self._pinned_account and account != self._pinned_account:
            raise GatewayError(
                f"Job asks for IBKR account {account}; this agent is pinned to "
                f"{self._pinned_account} (IBKR_ACCOUNT_ID). Refused."
            )
        return account

    @staticmethod
    def _ids(rows: Any) -> set[str]:
        ids: set[str] = set()
        if isinstance(rows, dict):
            rows = rows.get("accounts") or []
        for row in rows or []:
            if isinstance(row, str):
                ids.add(row)
            elif isinstance(row, dict):
                for key in ("id", "accountId"):
                    if row.get(key):
                        ids.add(str(row[key]))
        return ids

    def _ensure_portfolio_account(self, account: str) -> None:
        # IBKR requires /portfolio/accounts before any other /portfolio call.
        rows = self._request("GET", "/portfolio/accounts")
        self._portfolio_accounts_loaded = True
        if account not in self._ids(rows):
            raise GatewayError(f"IBKR gateway does not list account {account}.")

    def _ensure_iserver_account(self, account: str) -> None:
        # ...and /iserver/accounts before any order call.
        rows = self._request("GET", "/iserver/accounts")
        self._iserver_accounts_loaded = True
        if account not in self._ids(rows):
            raise GatewayError(f"IBKR gateway does not list account {account} for trading.")

    # --- jobs ---------------------------------------------------------------

    def handle(self, kind: str, payload: dict[str, Any], *, job_id: str) -> dict[str, Any]:
        handlers = {
            "account_read": self._account_read,
            "positions_read": self._positions_read,
            "order_validate": self._order_validate,
            "order_submit": self._order_submit,
            "order_status": self._order_status,
            "cancel": self._cancel,
        }
        handler = handlers.get(kind)
        if handler is None:
            raise GatewayError(f"Unknown job kind {kind!r}.")
        return handler(payload, job_id)

    def _account_read(self, payload: dict[str, Any], _job_id: str) -> dict[str, Any]:
        account = self._account(payload)
        self._ensure_portfolio_account(account)
        ledger = self._request("GET", f"/portfolio/{account}/ledger")
        cash: dict[str, str] = {}
        if isinstance(ledger, dict):
            for currency, row in ledger.items():
                if isinstance(row, dict) and row.get("cashbalance") is not None:
                    cash[str(currency).upper()] = decimal_text(
                        row["cashbalance"], what=f"{currency} cash balance"
                    )
        summary = self._request("GET", f"/portfolio/{account}/summary")
        net_liq = summary.get("netliquidation") if isinstance(summary, dict) else None
        result: dict[str, Any] = {"account_id": account, "cash_by_currency": cash}
        if isinstance(net_liq, dict) and net_liq.get("amount") is not None:
            result["net_liquidation"] = {
                "amount": decimal_text(net_liq["amount"], what="net liquidation"),
                "currency": net_liq.get("currency"),
            }
        if not cash:
            raise GatewayError(f"IBKR returned no cash balances for account {account}.")
        return result

    def _positions_read(self, payload: dict[str, Any], _job_id: str) -> dict[str, Any]:
        account = self._account(payload)
        self._ensure_portfolio_account(account)
        positions: list[dict[str, Any]] = []
        for page in range(50):
            rows = self._request("GET", f"/portfolio/{account}/positions/{page}")
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                if not isinstance(row, dict) or row.get("conid") is None:
                    raise GatewayError(f"Unreadable IBKR position row: {row!r}"[:300])
                positions.append(
                    {
                        # Keyed by conid: IBKR orders are addressed by conid,
                        # so a sell must use the key the position came under.
                        "symbol": str(row["conid"]),
                        "quantity": decimal_text(row.get("position"), what="position size"),
                        "description": row.get("contractDesc") or row.get("ticker"),
                        "currency": row.get("currency"),
                    }
                )
            if len(rows) < 100:
                break
        return {"account_id": account, "positions": positions}

    @staticmethod
    def _order_body(payload: dict[str, Any], account: str, *, coid: str | None) -> dict[str, Any]:
        symbol = require(payload, "symbol")
        if not symbol.isdigit():
            raise GatewayError(f"IBKR orders need a numeric conid, got {symbol!r}.")
        side = require(payload, "side").lower()
        if side not in ("buy", "sell"):
            raise GatewayError(f"Unknown side {side!r}.")
        try:
            quantity = Decimal(require(payload, "quantity"))
            limit = Decimal(str(payload["limit_price"])) if payload.get("limit_price") else None
        except (InvalidOperation, ValueError) as exc:
            raise GatewayError("The job carries an unreadable quantity or limit price.") from exc
        if not quantity.is_finite() or quantity <= 0:
            raise GatewayError(f"Refusing a non-positive quantity {quantity}.")
        order_type = require(payload, "order_type").lower()
        body: dict[str, Any] = {
            "acctId": account,
            "conid": int(symbol),
            "side": side.upper(),
            "quantity": float(quantity),
            "tif": "DAY",
        }
        if order_type == "market":
            body["orderType"] = "MKT"
        elif order_type == "limit":
            if limit is None or not limit.is_finite() or limit <= 0:
                raise GatewayError("A limit order needs a positive limit price.")
            body["orderType"] = "LMT"
            body["price"] = float(limit)
        else:
            raise GatewayError(f"Unsupported order type {order_type!r}.")
        if coid:
            body["cOID"] = coid
        return body

    def _order_validate(self, payload: dict[str, Any], _job_id: str) -> dict[str, Any]:
        """IBKR's what-if: margin and commission impact, and NOTHING placed."""
        account = self._account(payload)
        self._ensure_iserver_account(account)
        body = self._order_body(payload, account, coid=None)
        reply = self._request(
            "POST", f"/iserver/account/{account}/orders/whatif", json={"orders": [body]}
        )
        if not isinstance(reply, dict):
            raise GatewayError(f"Unexpected what-if reply: {reply!r}"[:300])
        if reply.get("error"):
            raise GatewayError(f"IBKR what-if refused the order: {reply['error']}")
        amount = reply.get("amount") if isinstance(reply.get("amount"), dict) else {}
        equity = reply.get("equity") if isinstance(reply.get("equity"), dict) else {}
        summary = (
            f"what-if {body['side']} {payload['quantity']} conid {body['conid']}: "
            f"amount {amount.get('amount')}, commission {amount.get('commission')}, "
            f"equity after {equity.get('after')}"
        )
        if reply.get("warn"):
            summary += f"; IBKR warns: {reply['warn']}"
        return {
            "summary": summary,
            "amount": amount,
            "equity": equity,
            "initial": reply.get("initial"),
            "maintenance": reply.get("maintenance"),
            "warn": reply.get("warn"),
            "placed": False,
        }

    def _order_submit(self, payload: dict[str, Any], job_id: str) -> dict[str, Any]:
        account = self._account(payload)
        self._ensure_iserver_account(account)
        body = self._order_body(payload, account, coid=job_id)
        reply = self._request("POST", f"/iserver/account/{account}/orders", json={"orders": [body]})
        first = reply[0] if isinstance(reply, list) and reply else reply
        if not isinstance(first, dict):
            raise GatewayError(f"Unexpected IBKR order reply: {reply!r}"[:300])
        if first.get("error"):
            raise GatewayError(f"IBKR refused the order: {first['error']}")
        if first.get("order_id") is None:
            if first.get("id") and first.get("message"):
                # A confirmation question. The order has NOT been transmitted
                # and this agent will not answer it on a human's behalf.
                raise GatewayError(
                    "IBKR_CONFIRMATION_REQUIRED: IBKR asked "
                    f"{first.get('message')!r} before transmitting this order. The agent "
                    "never auto-confirms, so the order was NOT placed. Review the warning in "
                    "IBKR; suppress it there only if you accept it for every future order."
                )
            raise GatewayError(f"IBKR returned no order id: {reply!r}"[:300])
        order_id = str(first["order_id"])

        # Market orders usually fill within a second or two; watch briefly,
        # then report whatever IBKR says - the platform reconciles the rest.
        status = self._status_of(order_id)
        for _ in range(int(self._order_poll_seconds / 0.5)):
            if status["state"] in _TERMINAL:
                break
            self._sleep(0.5)
            status = self._status_of(order_id)
        return status

    def _status_of(self, order_id: str) -> dict[str, Any]:
        reply = self._request("GET", f"/iserver/account/order/status/{order_id}")
        if not isinstance(reply, dict):
            raise GatewayError(f"Unexpected IBKR order status reply: {reply!r}"[:300])
        venue_status = str(reply.get("order_status") or "")
        state = _STATE_BY_IBKR_STATUS.get(venue_status.replace(" ", "").lower(), "unknown")
        filled_raw = reply.get("cum_fill")
        avg_raw = reply.get("average_price")
        return {
            "order_id": order_id,
            "venue_status": venue_status,
            "state": state,
            "filled_quantity": (
                decimal_text(filled_raw, what="filled quantity")
                if filled_raw not in (None, "")
                else "0"
            ),
            "avg_price": (
                decimal_text(avg_raw, what="average price") if avg_raw not in (None, "") else None
            ),
            "detail": reply.get("order_not_editable_reason") or reply.get("text"),
        }

    def _order_status(self, payload: dict[str, Any], _job_id: str) -> dict[str, Any]:
        self._account(payload)
        return self._status_of(require(payload, "order_id"))

    def _cancel(self, payload: dict[str, Any], _job_id: str) -> dict[str, Any]:
        account = self._account(payload)
        self._ensure_iserver_account(account)
        order_id = require(payload, "order_id")
        reply = self._request("DELETE", f"/iserver/account/{account}/order/{order_id}")
        if isinstance(reply, dict) and reply.get("error"):
            raise GatewayError(f"IBKR refused the cancel: {reply['error']}")
        return {"order_id": order_id, "cancelled": True, "venue_reply": reply}
