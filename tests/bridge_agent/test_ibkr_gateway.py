"""The agent's IBKR handler against a stubbed Client Portal Gateway
(httpx.MockTransport). No request leaves this process."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from bridge_agent.gateway import GatewayError
from bridge_agent.ibkr import IbkrGateway

ACCOUNT = "DU1234567"


class StubGateway:
    """Routes (method, path) to canned replies and records every request."""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/api")
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))
        reply = self.routes.get((request.method, path))
        if reply is None:
            return httpx.Response(404, text=f"no stub for {request.method} {path}")
        if callable(reply):
            reply = reply()
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply)

    def paths(self) -> list[str]:
        return [f"{m} {p}" for m, p, _ in self.requests]


def gateway(stub: StubGateway, *, pinned: str | None = ACCOUNT) -> IbkrGateway:
    return IbkrGateway(
        base_url="https://localhost:5000/v1/api",
        account_id=pinned,
        verify_tls=False,
        transport=httpx.MockTransport(stub),
        sleep=lambda _s: None,
        order_poll_seconds=2.0,
    )


ACCOUNTS = {
    ("GET", "/portfolio/accounts"): [{"id": ACCOUNT}],
    ("GET", "/iserver/accounts"): {"accounts": [ACCOUNT]},
}


def test_status_authenticated_and_logged_out():
    ok = StubGateway({("POST", "/iserver/auth/status"): {"authenticated": True, "connected": True}})
    status = gateway(ok).status()
    assert status.reachable and status.authenticated

    out = StubGateway(
        {("POST", "/iserver/auth/status"): {"authenticated": False, "connected": True}}
    )
    status = gateway(out).status()
    assert status.reachable and not status.authenticated
    assert "not logged in" in status.detail

    competing = StubGateway(
        {
            ("POST", "/iserver/auth/status"): {
                "authenticated": True,
                "connected": True,
                "competing": True,
            }
        }
    )
    assert "competing" in gateway(competing).status().detail


def test_status_when_the_gateway_is_not_running():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    down = IbkrGateway(
        base_url="https://localhost:5000/v1/api",
        account_id=None,
        verify_tls=False,
        transport=httpx.MockTransport(refuse),
    )
    status = down.status()
    assert not status.reachable and not status.authenticated
    assert "Client Portal Gateway" in status.detail


def test_a_401_means_log_in_again():
    stub = StubGateway({("POST", "/iserver/auth/status"): httpx.Response(401)})
    status = gateway(stub).status()
    assert status.reachable and not status.authenticated
    assert "session expires daily" in status.detail


def test_account_read_returns_ledger_cash_per_currency_as_strings():
    stub = StubGateway(
        {
            **ACCOUNTS,
            ("GET", f"/portfolio/{ACCOUNT}/ledger"): {
                "USD": {"cashbalance": 1234.5, "currency": "USD"},
                "BASE": {"cashbalance": 1234.5, "currency": "BASE"},
            },
            ("GET", f"/portfolio/{ACCOUNT}/summary"): {
                "netliquidation": {"amount": 2000.25, "currency": "USD"}
            },
        }
    )
    result = gateway(stub).handle("account_read", {"account_id": ACCOUNT}, job_id="j")
    assert result["cash_by_currency"] == {"USD": "1234.5", "BASE": "1234.5"}
    assert result["net_liquidation"]["amount"] == "2000.25"
    # IBKR requires /portfolio/accounts first.
    assert stub.paths()[0] == "GET /portfolio/accounts"


def test_a_job_for_another_account_is_refused_before_any_request():
    stub = StubGateway(ACCOUNTS)
    with pytest.raises(GatewayError, match="pinned"):
        gateway(stub).handle("account_read", {"account_id": "U999"}, job_id="j")
    assert stub.requests == []


def test_an_account_the_gateway_does_not_list_is_refused():
    stub = StubGateway({("GET", "/portfolio/accounts"): [{"id": "DU0000000"}]})
    with pytest.raises(GatewayError, match="does not list"):
        gateway(stub, pinned=None).handle("account_read", {"account_id": ACCOUNT}, job_id="j")


def test_positions_are_keyed_by_conid():
    stub = StubGateway(
        {
            **ACCOUNTS,
            ("GET", f"/portfolio/{ACCOUNT}/positions/0"): [
                {"conid": 265598, "position": 10, "contractDesc": "AAPL", "currency": "USD"}
            ],
        }
    )
    result = gateway(stub).handle("positions_read", {"account_id": ACCOUNT}, job_id="j")
    assert result["positions"] == [
        {"symbol": "265598", "quantity": "10", "description": "AAPL", "currency": "USD"}
    ]


ORDER = {
    "account_id": ACCOUNT,
    "symbol": "265598",
    "side": "buy",
    "quantity": "2",
    "order_type": "market",
}


def test_validate_calls_whatif_and_never_the_order_endpoint():
    stub = StubGateway(
        {
            **ACCOUNTS,
            ("POST", f"/iserver/account/{ACCOUNT}/orders/whatif"): {
                "amount": {"amount": "350.00 USD", "commission": "1.00 USD"},
                "equity": {"after": "9650"},
                "warn": None,
            },
        }
    )
    result = gateway(stub).handle("order_validate", ORDER, job_id="job-1")
    assert result["placed"] is False
    assert "what-if BUY 2 conid 265598" in result["summary"]
    posted = [p for p in stub.paths() if p.startswith("POST /iserver/account")]
    assert posted == [f"POST /iserver/account/{ACCOUNT}/orders/whatif"]
    body = stub.requests[-1][2]["orders"][0]
    assert body == {
        "acctId": ACCOUNT,
        "conid": 265598,
        "side": "BUY",
        "quantity": 2.0,
        "tif": "DAY",
        "orderType": "MKT",
    }


def test_a_whatif_error_fails_the_job():
    stub = StubGateway(
        {
            **ACCOUNTS,
            ("POST", f"/iserver/account/{ACCOUNT}/orders/whatif"): {"error": "no permissions"},
        }
    )
    with pytest.raises(GatewayError, match="no permissions"):
        gateway(stub).handle("order_validate", ORDER, job_id="j")


def test_submit_uses_the_job_id_as_coid_and_reports_the_terminal_fill():
    statuses = iter(
        [
            {"order_status": "Submitted", "cum_fill": 0},
            {"order_status": "Filled", "cum_fill": 2, "average_price": "175.5"},
        ]
    )
    stub = StubGateway(
        {
            **ACCOUNTS,
            ("POST", f"/iserver/account/{ACCOUNT}/orders"): [
                {"order_id": "111", "order_status": "Submitted"}
            ],
            ("GET", "/iserver/account/order/status/111"): lambda: next(statuses),
        }
    )
    result = gateway(stub).handle("order_submit", ORDER, job_id="job-42")
    sent = next(b for m, p, b in stub.requests if p == f"/iserver/account/{ACCOUNT}/orders")
    assert sent["orders"][0]["cOID"] == "job-42"
    assert result == {
        "order_id": "111",
        "venue_status": "Filled",
        "state": "filled",
        "filled_quantity": "2",
        "avg_price": "175.5",
        "detail": None,
    }


def test_a_confirmation_prompt_is_never_answered():
    stub = StubGateway(
        {
            **ACCOUNTS,
            ("POST", f"/iserver/account/{ACCOUNT}/orders"): [
                {"id": "reply-1", "message": ["This order exceeds your size limit."]}
            ],
        }
    )
    with pytest.raises(GatewayError, match="IBKR_CONFIRMATION_REQUIRED") as err:
        gateway(stub).handle("order_submit", ORDER, job_id="j")
    assert "NOT placed" in str(err.value)
    assert not any("/iserver/reply" in p for p in stub.paths())


def test_inactive_is_unknown_not_rejected_and_polling_is_bounded():
    stub = StubGateway(
        {
            **ACCOUNTS,
            ("POST", f"/iserver/account/{ACCOUNT}/orders"): [{"order_id": "5"}],
            ("GET", "/iserver/account/order/status/5"): {"order_status": "Inactive"},
        }
    )
    result = gateway(stub).handle("order_submit", ORDER, job_id="j")
    assert result["state"] == "unknown"
    polls = [p for p in stub.paths() if p.endswith("/status/5")]
    assert len(polls) == 5  # one read + four half-second polls in 2 s


def test_a_ticker_symbol_is_refused():
    stub = StubGateway(ACCOUNTS)
    with pytest.raises(GatewayError, match="conid"):
        gateway(stub).handle("order_validate", {**ORDER, "symbol": "AAPL"}, job_id="j")


def test_cancel():
    stub = StubGateway(
        {
            **ACCOUNTS,
            ("DELETE", f"/iserver/account/{ACCOUNT}/order/111"): {"msg": "Request was submitted"},
        }
    )
    result = gateway(stub).handle("cancel", {"account_id": ACCOUNT, "order_id": "111"}, job_id="j")
    assert result["cancelled"] is True


def test_keepalive_tickles():
    stub = StubGateway({("POST", "/tickle"): {"session": "x"}})
    gateway(stub).keepalive()
    assert stub.paths() == ["POST /tickle"]
