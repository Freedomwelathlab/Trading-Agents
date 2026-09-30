"""The agent's moomoo handler against a stand-in `futu` module. futu-api is
not installed in this environment; the stand-in mimics the documented
`OpenSecTradeContext` calls and returns lists instead of DataFrames."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from bridge_agent.gateway import GatewayError
from bridge_agent.moomoo import MoomooGateway

RET_OK, RET_ERROR = 0, -1


class FakeContext:
    def __init__(self, log: list[tuple[str, dict[str, Any]]], replies: dict[str, Any]):
        self.log = log
        self.replies = replies
        self.closed = False

    def __getattr__(self, name: str):
        def call(*args: Any, **kwargs: Any):
            self.log.append((name, {"args": args, **kwargs}))
            reply = self.replies.get(name, (RET_OK, []))
            return reply() if callable(reply) else reply

        return call

    def close(self) -> None:
        self.closed = True


def fake_futu(replies: dict[str, Any]) -> tuple[SimpleNamespace, list]:
    log: list[tuple[str, dict[str, Any]]] = []
    contexts: list[FakeContext] = []

    def open_ctx(**kwargs: Any) -> FakeContext:
        ctx = FakeContext(log, replies)
        contexts.append(ctx)
        return ctx

    module = SimpleNamespace(
        RET_OK=RET_OK,
        OpenSecTradeContext=open_ctx,
        TrdMarket=SimpleNamespace(US="US", HK="HK"),
        SecurityFirm=SimpleNamespace(FUTUINC="FUTUINC"),
        TrdEnv=SimpleNamespace(SIMULATE="SIMULATE", REAL="REAL"),
        Currency=SimpleNamespace(USD="USD", HKD="HKD"),
        TrdSide=SimpleNamespace(BUY="BUY", SELL="SELL"),
        OrderType=SimpleNamespace(MARKET="MARKET", NORMAL="NORMAL"),
        ModifyOrderOp=SimpleNamespace(CANCEL="CANCEL"),
    )
    module.contexts = contexts
    return module, log


def gw(module, **kw) -> MoomooGateway:
    return MoomooGateway(
        host="127.0.0.1",
        port=11111,
        account_id=kw.pop("account_id", "123"),
        trade_password=kw.pop("trade_password", None),
        futu_module=module,
        sleep=lambda _s: None,
        order_poll_seconds=1.0,
    )


BASE = {"account_id": "123", "trade_env": "SIMULATE", "market": "US"}


def test_account_read_and_context_is_closed():
    module, log = fake_futu({"accinfo_query": (RET_OK, [{"cash": 500.25, "total_assets": 900}])})
    result = gw(module).handle("account_read", {**BASE, "currency": "USD"}, job_id="j")
    assert result["cash_by_currency"] == {"USD": "500.25"}
    assert log[0][0] == "accinfo_query" and log[0][1]["trd_env"] == "SIMULATE"
    assert all(c.closed for c in module.contexts)


def test_a_refusal_carries_moomoos_words():
    module, _ = fake_futu({"accinfo_query": (RET_ERROR, "unlock needed")})
    with pytest.raises(GatewayError, match="unlock needed"):
        gw(module).handle("account_read", BASE, job_id="j")


def test_positions():
    module, _ = fake_futu(
        {"position_list_query": (RET_OK, [{"code": "US.AAPL", "qty": 3, "stock_name": "Apple"}])}
    )
    result = gw(module).handle("positions_read", BASE, job_id="j")
    assert result["positions"] == [{"symbol": "US.AAPL", "quantity": "3", "description": "Apple"}]


def test_pinned_account_and_unknown_env_are_refused():
    module, log = fake_futu({})
    with pytest.raises(GatewayError, match="pinned"):
        gw(module).handle("account_read", {**BASE, "account_id": "999"}, job_id="j")
    with pytest.raises(GatewayError, match="trade_env"):
        gw(module).handle("account_read", {**BASE, "trade_env": "LIVE"}, job_id="j")
    assert log == []


ORDER = {**BASE, "symbol": "US.AAPL", "side": "buy", "quantity": "5", "order_type": "market"}


def test_validate_is_a_precheck_and_never_places():
    module, log = fake_futu(
        {
            "acctradinginfo_query": (
                RET_OK,
                [{"max_cash_and_margin_buy": 10, "max_position_sell": 0}],
            )
        }
    )
    result = gw(module).handle("order_validate", ORDER, job_id="j")
    assert result["placed"] is False
    assert [name for name, _ in log] == ["acctradinginfo_query"]

    too_big = {**ORDER, "quantity": "50"}
    with pytest.raises(GatewayError, match="exceeds"):
        gw(module).handle("order_validate", too_big, job_id="j")


def test_real_orders_need_the_local_trade_password():
    module, log = fake_futu({})
    with pytest.raises(GatewayError, match="MOOMOO_TRADE_PASSWORD"):
        gw(module).handle("order_submit", {**ORDER, "trade_env": "REAL"}, job_id="j")
    assert [name for name, _ in log] == []


def test_submit_reports_the_dealt_fill():
    module, log = fake_futu(
        {
            "place_order": (RET_OK, [{"order_id": "77"}]),
            "order_list_query": (
                RET_OK,
                [{"order_status": "FILLED_ALL", "dealt_qty": 5, "dealt_avg_price": 190.1}],
            ),
        }
    )
    result = gw(module).handle("order_submit", ORDER, job_id="job-9")
    assert result["state"] == "filled"
    assert result["filled_quantity"] == "5" and result["avg_price"] == "190.1"
    placed = dict(log)["place_order"]
    assert placed["remark"] == "job-9" and placed["trd_env"] == "SIMULATE"


def test_missing_futu_is_reported_not_crashed(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_futu(name, *args, **kwargs):
        if name == "futu":
            raise ImportError("no futu")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_futu)
    gateway = MoomooGateway(host="127.0.0.1", port=11111, account_id=None, trade_password=None)
    with pytest.raises(GatewayError, match="pip install futu-api"):
        gateway.handle("account_read", BASE, job_id="j")
