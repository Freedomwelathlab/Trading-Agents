"""Unit tests against a fake LLMProvider - no real network calls, no real
provider credentials needed. Mirrors tests/marketdata/providers/test_longbridge.py's
shape: exercise the parsing/validation boundary directly, never touch a
real vendor from a test.
"""

from decimal import Decimal

import pytest

from apps.api.app.agents.provider import LLMProviderError
from apps.api.app.agents.trader import AgentOutputError, TraderAgent, stop_price_from_distance
from apps.api.app.risk.models import Side


class FakeProvider:
    name = "fake-llm"

    def __init__(self, response: str | None = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error

    async def complete(self, *, system: str, user: str, max_tokens: int) -> str:
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


@pytest.mark.asyncio
async def test_a_valid_json_response_produces_a_trade_idea():
    provider = FakeProvider(
        response=(
            '{"side": "buy", "quantity": "10", "stop_distance_pct": "2.5", '
            '"rationale": "momentum breakout"}'
        )
    )
    agent = TraderAgent(provider)

    idea = await agent.propose(symbol="AAPL", directive="moderate long")

    assert idea.side is Side.BUY
    assert idea.quantity == Decimal("10")
    assert idea.stop_distance_pct == Decimal("2.5")
    assert idea.rationale == "momentum breakout"


@pytest.mark.asyncio
async def test_json_wrapped_in_prose_is_still_extracted():
    provider = FakeProvider(
        response=(
            'Here is my proposal:\n'
            '{"side": "sell", "quantity": "5", "stop_distance_pct": "3", '
            '"rationale": "overbought"}\n'
            "Let me know if you need anything else."
        )
    )
    agent = TraderAgent(provider)

    idea = await agent.propose(symbol="TSLA", directive="short the pop")

    assert idea.side is Side.SELL
    assert idea.quantity == Decimal("5")


@pytest.mark.asyncio
async def test_non_json_response_raises_agent_output_error():
    provider = FakeProvider(response="I cannot help with that.")
    agent = TraderAgent(provider)

    with pytest.raises(AgentOutputError, match="not valid JSON"):
        await agent.propose(symbol="AAPL", directive="long")


@pytest.mark.asyncio
async def test_json_missing_a_required_field_raises_agent_output_error():
    provider = FakeProvider(response='{"side": "buy", "quantity": "10"}')
    agent = TraderAgent(provider)

    with pytest.raises(AgentOutputError, match="schema validation"):
        await agent.propose(symbol="AAPL", directive="long")


@pytest.mark.asyncio
async def test_a_zero_quantity_fails_validation():
    provider = FakeProvider(
        response=(
            '{"side": "buy", "quantity": "0", "stop_distance_pct": "2", "rationale": "x"}'
        )
    )
    agent = TraderAgent(provider)

    with pytest.raises(AgentOutputError, match="schema validation"):
        await agent.propose(symbol="AAPL", directive="long")


@pytest.mark.asyncio
async def test_a_provider_error_is_wrapped_as_agent_output_error():
    provider = FakeProvider(error=LLMProviderError("rate limited"))
    agent = TraderAgent(provider)

    with pytest.raises(AgentOutputError, match="LLM provider call failed"):
        await agent.propose(symbol="AAPL", directive="long")


def test_stop_price_from_distance_for_a_buy_is_below_price():
    stop = stop_price_from_distance(
        price=Decimal("100"), side=Side.BUY, stop_distance_pct=Decimal("2")
    )
    assert stop == Decimal("98.00")


def test_stop_price_from_distance_for_a_sell_is_above_price():
    stop = stop_price_from_distance(
        price=Decimal("100"), side=Side.SELL, stop_distance_pct=Decimal("2")
    )
    assert stop == Decimal("102.00")
