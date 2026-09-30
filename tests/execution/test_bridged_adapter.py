"""`BridgedAdapter` against a fake agent (Phase 104, D124).

The fake channel stands in for the whole queue + agent: it records every
job and answers from a script. What is pinned here is what the adapter
DOES with an answer - above all, that it never turns a preview, a timeout
or a still-working order into a fill.
"""

from __future__ import annotations

from decimal import Decimal as D
from typing import Any

import pytest

from apps.api.app.bridge.channel import BridgeOutcomeUnknownError, BridgeTimeoutError
from apps.api.app.bridge.store import JobKind
from apps.api.app.execution.adapters.bridged import (
    BridgedAdapter,
    BridgedVenueError,
    build_bridged_adapter,
)
from apps.api.app.execution.broker import OrderNotConfirmedError, OrderRequest
from apps.api.app.risk.models import Side


class FakeAgent:
    def __init__(self, answers: dict[JobKind, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, JobKind, dict[str, Any]]] = []

    def call(self, *, provider, kind, payload, broker_id=None):
        self.calls.append((provider, kind, dict(payload)))
        answer = self.answers[kind]
        if isinstance(answer, Exception):
            raise answer
        return answer


def adapter(answers, **kw) -> tuple[BridgedAdapter, FakeAgent]:
    agent = FakeAgent(answers)
    provider = kw.pop("provider", "ibkr")
    return BridgedAdapter(provider, channel=agent, account_id="DU1", **kw), agent


def order(**over) -> OrderRequest:
    base: dict[str, Any] = dict(symbol="265598", side=Side.BUY, quantity=D("2"))
    base.update(over)
    return OrderRequest(**base)


def test_cash_reads_the_configured_currency_and_never_substitutes_another():
    a, agent = adapter({JobKind.ACCOUNT_READ: {"cash_by_currency": {"USD": "10.5", "EUR": "3"}}})
    assert a.cash == D("10.5")
    assert agent.calls == [("ibkr", JobKind.ACCOUNT_READ, {"account_id": "DU1", "currency": "USD"})]

    hkd, _ = adapter({JobKind.ACCOUNT_READ: {"cash_by_currency": {"USD": "10.5"}}}, currency="HKD")
    with pytest.raises(BridgedVenueError, match="no HKD cash row"):
        _ = hkd.cash


def test_positions_are_keyed_as_the_agent_reports_and_zero_rows_dropped():
    a, _ = adapter(
        {
            JobKind.POSITIONS_READ: {
                "positions": [
                    {"symbol": "265598", "quantity": "10"},
                    {"symbol": "8314", "quantity": "0"},
                ]
            }
        }
    )
    assert a.positions == {"265598": D("10")}


def test_account_state_needs_a_mark_for_every_holding():
    a, _ = adapter(
        {
            JobKind.ACCOUNT_READ: {"cash_by_currency": {"USD": "100"}},
            JobKind.POSITIONS_READ: {"positions": [{"symbol": "265598", "quantity": "2"}]},
        }
    )
    with pytest.raises(BridgedVenueError, match="No mark"):
        a.get_account_state(marks={})
    state = a.get_account_state(marks={"265598": D("50")})
    assert state.equity == D("200") and state.cash == D("100")
    assert state.current_exposure == D("100")


def test_orders_are_validate_only_by_default_and_never_return_a_fill():
    a, agent = adapter({JobKind.ORDER_VALIDATE: {"summary": "what-if ok", "placed": False}})
    with pytest.raises(BridgedVenueError, match="VALIDATE_ONLY") as err:
        a.submit_order(order(), market_price=D("100"))
    assert "NOTHING was placed" in str(err.value)
    kinds = [k for _, k, _ in agent.calls]
    assert kinds == [JobKind.ORDER_VALIDATE], "validate-only must never send order_submit"
    payload = agent.calls[0][2]
    assert payload["symbol"] == "265598" and payload["quantity"] == "2"
    assert "market_price" not in payload, "the caller's reference price is never sent"


def test_ibkr_orders_need_a_conid_not_a_ticker():
    a, agent = adapter({})
    with pytest.raises(BridgedVenueError, match="conid"):
        a.submit_order(order(symbol="AAPL"), market_price=D("1"))
    assert agent.calls == []
    b, agent_b = adapter({JobKind.ORDER_VALIDATE: {"summary": "ok"}})
    with pytest.raises(BridgedVenueError, match="VALIDATE_ONLY"):
        b.submit_order(order(symbol="conid:265598"), market_price=D("1"))
    assert agent_b.calls[0][2]["symbol"] == "265598"


def test_an_armed_order_reports_only_the_venues_terminal_fill():
    filled = {
        "order_id": "987",
        "state": "filled",
        "venue_status": "Filled",
        "filled_quantity": "2",
        "avg_price": "101.25",
    }
    a, agent = adapter({JobKind.ORDER_SUBMIT: filled}, live_orders=True)
    fill = a.submit_order(order(), market_price=D("100"))
    assert fill.quantity == D("2") and fill.fill_price == D("101.25")
    assert [k for _, k, _ in agent.calls] == [JobKind.ORDER_SUBMIT]


def test_a_working_order_is_not_confirmed_not_a_partial_fill():
    working = {
        "order_id": "987",
        "state": "working",
        "venue_status": "Submitted",
        "filled_quantity": "1",
        "avg_price": "101",
    }
    a, _ = adapter({JobKind.ORDER_SUBMIT: working}, live_orders=True)
    with pytest.raises(OrderNotConfirmedError) as err:
        a.submit_order(order(), market_price=D("100"))
    assert err.value.order_id == "987"


def test_rejected_and_unfilled_cancelled_orders_raise():
    for answer, match in (
        ({"order_id": "1", "state": "rejected", "venue_status": "Rejected"}, "REJECTED"),
        ({"order_id": "1", "state": "cancelled", "filled_quantity": "0"}, "unfilled"),
        ({"state": "filled"}, "no order id"),
        (
            {"order_id": "1", "state": "filled", "filled_quantity": "2", "avg_price": "0"},
            "non-positive",
        ),
    ):
        a, _ = adapter({JobKind.ORDER_SUBMIT: answer}, live_orders=True)
        with pytest.raises(BridgedVenueError, match=match):
            a.submit_order(order(), market_price=D("1"))


def test_bridge_failures_propagate_and_are_never_a_result():
    timeout = BridgeTimeoutError("BRIDGE_TIMEOUT: nothing was sent")
    a, _ = adapter({JobKind.ACCOUNT_READ: timeout})
    with pytest.raises(BridgeTimeoutError):
        _ = a.cash
    unknown = BridgeOutcomeUnknownError("BRIDGE_OUTCOME_UNKNOWN")
    b, _ = adapter({JobKind.ORDER_SUBMIT: unknown}, live_orders=True)
    with pytest.raises(BridgeOutcomeUnknownError):
        b.submit_order(order(), market_price=D("1"))


def test_moomoo_context_travels_with_every_job_and_codes_keep_their_prefix():
    a, agent = adapter(
        {JobKind.ORDER_VALIDATE: {"summary": "ok"}},
        provider="moomoo",
        context={"trade_env": "SIMULATE", "market": "US"},
    )
    with pytest.raises(BridgedVenueError, match="market prefix"):
        a.submit_order(order(symbol="AAPL"), market_price=D("1"))
    with pytest.raises(BridgedVenueError, match="VALIDATE_ONLY"):
        a.submit_order(order(symbol="US.AAPL"), market_price=D("1"))
    assert agent.calls[0][2]["trade_env"] == "SIMULATE"
    assert agent.calls[0][2]["symbol"] == "US.AAPL"


def test_the_factory_refuses_without_a_bridge_token():
    from apps.api.app.bridge.channel import BridgeNotConfiguredError
    from apps.api.app.core.config import get_settings

    settings = get_settings()
    previous = settings.bridge_agent_token
    settings.bridge_agent_token = None
    try:
        with pytest.raises(BridgeNotConfiguredError, match="BRIDGE_AGENT_TOKEN"):
            build_bridged_adapter("ibkr", {"account_id": "DU1"})
    finally:
        settings.bridge_agent_token = previous


def test_the_factory_defaults_to_validate_only_and_requires_a_moomoo_env():
    from apps.api.app.core.config import get_settings

    settings = get_settings()
    previous = settings.bridge_agent_token
    settings.bridge_agent_token = "k" * 40
    try:
        built = build_bridged_adapter("ibkr", {"account_id": "DU1"})
        assert built._validate_only is True
        armed = build_bridged_adapter("ibkr", {"account_id": "DU1", "live_orders": "true"})
        assert armed._validate_only is False
        with pytest.raises(ValueError, match="SIMULATE or REAL"):
            build_bridged_adapter("moomoo", {"account_id": "1"})
        with pytest.raises(ValueError, match="account_id"):
            build_bridged_adapter("ibkr", {})
    finally:
        settings.bridge_agent_token = previous
