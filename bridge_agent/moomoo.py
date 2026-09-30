"""moomoo / Futu OpenD handler.

Talks to OpenD on this PC through the `futu-api` package, imported LAZILY:
an agent configured for IBKR only never needs it installed.

What stays on this PC: the OpenD address, and the trade-unlock password
(`MOOMOO_TRADE_PASSWORD`), which is used only to unlock REAL trading right
before a real order and is never sent to the platform.

**Not verified against a live OpenD.** Written against futu-api's
documented `OpenSecTradeContext` interface and tested with a stand-in
module; the first real run should be a probe (account read), then a
validate, in SIMULATE.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from bridge_agent.gateway import GatewayError, GatewayStatus, decimal_text, require

_STATE_BY_STATUS = {
    "FILLED_ALL": "filled",
    "CANCELLED_ALL": "cancelled",
    "CANCELLED_PART": "cancelled",
    "DELETED": "cancelled",
    "FAILED": "rejected",
    "SUBMIT_FAILED": "rejected",
    "DISABLED": "rejected",
}
_TERMINAL = frozenset({"filled", "cancelled", "rejected"})


def _records(data: Any) -> list[dict[str, Any]]:
    """futu returns pandas DataFrames; a stand-in may return a list."""
    if hasattr(data, "to_dict"):
        return list(data.to_dict("records"))
    if isinstance(data, list):
        return [dict(row) for row in data]
    raise GatewayError(f"moomoo returned an unreadable table: {data!r}"[:300])


class MoomooGateway:
    name = "moomoo"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        account_id: str | None,
        trade_password: str | None,
        security_firm: str = "FUTUINC",
        futu_module: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        order_poll_seconds: float = 6.0,
        status_cache_seconds: float = 60.0,
    ) -> None:
        self._host = host
        self._port = port
        self._pinned_account = account_id.strip() if account_id else None
        self._trade_password = trade_password
        self._security_firm = security_firm
        self._futu = futu_module
        self._sleep = sleep
        self._order_poll_seconds = order_poll_seconds
        self._status_cache_seconds = status_cache_seconds
        self._cached_status: tuple[float, GatewayStatus] | None = None

    def close(self) -> None:
        return None

    def _module(self) -> Any:
        if self._futu is None:
            try:
                import futu  # type: ignore[import-not-found]
            except ImportError as exc:
                raise GatewayError(
                    "futu-api is not installed on this PC: pip install futu-api"
                ) from exc
            self._futu = futu
        return self._futu

    def _context(self, market: str) -> Any:
        futu = self._module()
        try:
            trd_market = getattr(futu.TrdMarket, market)
            firm = getattr(futu.SecurityFirm, self._security_firm)
        except AttributeError as exc:
            raise GatewayError(f"Unknown moomoo market or security firm: {exc}") from exc
        return futu.OpenSecTradeContext(
            filter_trdmarket=trd_market,
            host=self._host,
            port=self._port,
            security_firm=firm,
        )

    def _ok(self, ret_data: tuple[Any, Any], what: str) -> Any:
        ret, data = ret_data
        if ret != self._module().RET_OK:
            raise GatewayError(f"moomoo refused {what}: {data}")
        return data

    # --- health -------------------------------------------------------------

    def status(self) -> GatewayStatus:
        now = time.monotonic()
        if self._cached_status and now - self._cached_status[0] < self._status_cache_seconds:
            return self._cached_status[1]
        try:
            with socket.create_connection((self._host, self._port), timeout=3):
                pass
        except OSError as exc:
            result = GatewayStatus(
                False, False, f"OpenD not reachable at {self._host}:{self._port}: {exc}"
            )
        else:
            try:
                ctx = self._context("US")
                try:
                    self._ok(ctx.get_acc_list(), "the account list")
                finally:
                    ctx.close()
                result = GatewayStatus(True, True, "OpenD logged in")
            except GatewayError as exc:
                result = GatewayStatus(True, False, str(exc))
        self._cached_status = (now, result)
        return result

    def keepalive(self) -> None:
        return None

    # --- jobs ---------------------------------------------------------------

    def _job_context(self, payload: dict[str, Any]) -> tuple[Any, Any, int]:
        account = require(payload, "account_id")
        if self._pinned_account and account != self._pinned_account:
            raise GatewayError(
                f"Job asks for moomoo account {account}; this agent is pinned to "
                f"{self._pinned_account} (MOOMOO_ACCOUNT_ID). Refused."
            )
        env_name = require(payload, "trade_env").upper()
        if env_name not in ("SIMULATE", "REAL"):
            raise GatewayError(f"Unknown moomoo trade_env {env_name!r}.")
        try:
            acc_id = int(account)
        except ValueError as exc:
            raise GatewayError(f"moomoo account ids are numeric, not {account!r}.") from exc
        futu = self._module()
        ctx = self._context(str(payload.get("market") or "US").upper())
        return ctx, getattr(futu.TrdEnv, env_name), acc_id

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
        ctx, trd_env, acc_id = self._job_context(payload)
        try:
            return handler(ctx, trd_env, acc_id, payload, job_id)
        finally:
            ctx.close()

    def _account_read(
        self, ctx: Any, trd_env: Any, acc_id: int, payload: dict[str, Any], _job_id: str
    ) -> dict[str, Any]:
        futu = self._module()
        currency = str(payload.get("currency") or "USD").upper()
        try:
            futu_currency = getattr(futu.Currency, currency)
        except AttributeError as exc:
            raise GatewayError(f"moomoo has no currency {currency!r}.") from exc
        rows = _records(
            self._ok(
                ctx.accinfo_query(trd_env=trd_env, acc_id=acc_id, currency=futu_currency),
                "the account read",
            )
        )
        if not rows or rows[0].get("cash") is None:
            raise GatewayError("moomoo returned no cash figure for this account.")
        result: dict[str, Any] = {
            "account_id": str(acc_id),
            "cash_by_currency": {currency: decimal_text(rows[0]["cash"], what="cash")},
        }
        if rows[0].get("total_assets") is not None:
            result["net_liquidation"] = {
                "amount": decimal_text(rows[0]["total_assets"], what="total assets"),
                "currency": currency,
            }
        return result

    def _positions_read(
        self, ctx: Any, trd_env: Any, acc_id: int, _payload: dict[str, Any], _job_id: str
    ) -> dict[str, Any]:
        rows = _records(
            self._ok(ctx.position_list_query(trd_env=trd_env, acc_id=acc_id), "the positions")
        )
        return {
            "account_id": str(acc_id),
            "positions": [
                {
                    "symbol": str(row["code"]),
                    "quantity": decimal_text(row.get("qty"), what=f"quantity of {row['code']}"),
                    "description": row.get("stock_name"),
                }
                for row in rows
                if row.get("code")
            ],
        }

    def _order_args(self, payload: dict[str, Any]) -> tuple[str, Any, Decimal, Any, float]:
        futu = self._module()
        code = require(payload, "symbol")
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
        if order_type == "market":
            # OpenD ignores the price of a market order but the call requires one.
            return code, futu.OrderType.MARKET, quantity, _side(futu, side), 0.0
        if order_type == "limit" and limit is not None and limit.is_finite() and limit > 0:
            return code, futu.OrderType.NORMAL, quantity, _side(futu, side), float(limit)
        raise GatewayError(f"Unsupported order type {order_type!r} or missing limit price.")

    def _order_validate(
        self, ctx: Any, trd_env: Any, acc_id: int, payload: dict[str, Any], _job_id: str
    ) -> dict[str, Any]:
        """moomoo has no what-if; its trading-info query says how much this
        account could buy or sell right now. NOTHING is placed."""
        futu = self._module()
        code, order_type, quantity, side, price = self._order_args(payload)
        rows = _records(
            self._ok(
                ctx.acctradinginfo_query(
                    order_type=order_type,
                    code=code,
                    price=price,
                    trd_env=trd_env,
                    acc_id=acc_id,
                ),
                "the order pre-check",
            )
        )
        if not rows:
            raise GatewayError("moomoo returned no trading info for this order.")
        info = rows[0]
        if side == futu.TrdSide.BUY:
            limit_key = "max_cash_and_margin_buy"
        else:
            limit_key = "max_position_sell"
        allowed = Decimal(decimal_text(info.get(limit_key), what=limit_key))
        if quantity > allowed:
            raise GatewayError(
                f"moomoo pre-check: {quantity} {code} exceeds {limit_key} = {allowed}."
            )
        return {
            "summary": f"moomoo pre-check passed: {quantity} {code} within {limit_key} {allowed}",
            limit_key: format(allowed, "f"),
            "placed": False,
        }

    def _order_submit(
        self, ctx: Any, trd_env: Any, acc_id: int, payload: dict[str, Any], job_id: str
    ) -> dict[str, Any]:
        futu = self._module()
        code, order_type, quantity, side, price = self._order_args(payload)
        if trd_env == futu.TrdEnv.REAL:
            if not self._trade_password:
                raise GatewayError(
                    "MOOMOO_TRADE_PASSWORD is not set on this PC, so REAL trading cannot be "
                    "unlocked. The order was NOT placed."
                )
            self._ok(ctx.unlock_trade(self._trade_password), "the trade unlock")
        rows = _records(
            self._ok(
                ctx.place_order(
                    price=price,
                    qty=float(quantity),
                    code=code,
                    trd_side=side,
                    order_type=order_type,
                    trd_env=trd_env,
                    acc_id=acc_id,
                    remark=job_id[:60],
                ),
                "the order",
            )
        )
        if not rows or rows[0].get("order_id") in (None, ""):
            raise GatewayError("moomoo returned no order id; check the order list in the app.")
        order_id = str(rows[0]["order_id"])
        status = self._status_of(ctx, trd_env, acc_id, order_id)
        for _ in range(int(self._order_poll_seconds / 0.5)):
            if status["state"] in _TERMINAL:
                break
            self._sleep(0.5)
            status = self._status_of(ctx, trd_env, acc_id, order_id)
        return status

    def _status_of(self, ctx: Any, trd_env: Any, acc_id: int, order_id: str) -> dict[str, Any]:
        rows = _records(
            self._ok(
                ctx.order_list_query(order_id=order_id, trd_env=trd_env, acc_id=acc_id),
                f"the status of order {order_id}",
            )
        )
        if not rows:
            return {
                "order_id": order_id,
                "venue_status": None,
                "state": "unknown",
                "filled_quantity": "0",
                "avg_price": None,
            }
        row = rows[0]
        venue_status = str(row.get("order_status") or "")
        dealt = row.get("dealt_qty")
        avg = row.get("dealt_avg_price")
        return {
            "order_id": order_id,
            "venue_status": venue_status,
            "state": _STATE_BY_STATUS.get(
                venue_status.upper(), "working" if venue_status else "unknown"
            ),
            "filled_quantity": decimal_text(dealt, what="dealt quantity")
            if dealt not in (None, "")
            else "0",
            "avg_price": decimal_text(avg, what="dealt average price")
            if avg not in (None, "")
            else None,
        }

    def _order_status(
        self, ctx: Any, trd_env: Any, acc_id: int, payload: dict[str, Any], _job_id: str
    ) -> dict[str, Any]:
        return self._status_of(ctx, trd_env, acc_id, require(payload, "order_id"))

    def _cancel(
        self, ctx: Any, trd_env: Any, acc_id: int, payload: dict[str, Any], _job_id: str
    ) -> dict[str, Any]:
        futu = self._module()
        order_id = require(payload, "order_id")
        self._ok(
            ctx.modify_order(
                futu.ModifyOrderOp.CANCEL, order_id, 0, 0, trd_env=trd_env, acc_id=acc_id
            ),
            f"the cancel of order {order_id}",
        )
        return {"order_id": order_id, "cancelled": True}


def _side(futu: Any, side: str) -> Any:
    return futu.TrdSide.BUY if side == "buy" else futu.TrdSide.SELL
