"""Unit tests against a fake LLMProvider - no real network calls, no real
provider credentials needed. Mirrors tests/agents/test_trader.py's shape.
"""

from decimal import Decimal

import pytest

from apps.api.app.agents.provider import LLMProviderError
from apps.api.app.agents.technical_analyst import (
    AnalystOutputError,
    Stance,
    TechnicalAnalyst,
)


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
async def test_a_valid_json_response_produces_a_technical_read():
    provider = FakeProvider(
        response=(
            '{"stance": "bullish", "summary": "price is near a round number", '
            '"confidence": "0.4"}'
        )
    )
    analyst = TechnicalAnalyst(provider)

    read = await analyst.analyze(
        symbol="AAPL", price=Decimal("200.00"), as_of="2026-08-27T00:00:00Z"
    )

    assert read.stance is Stance.BULLISH
    assert read.summary == "price is near a round number"
    assert read.confidence == Decimal("0.4")


@pytest.mark.asyncio
async def test_json_wrapped_in_prose_is_still_extracted():
    provider = FakeProvider(
        response=(
            "Here's my read:\n"
            '{"stance": "neutral", "summary": "no strong signal from one quote", '
            '"confidence": "0.2"}\n'
            "Hope that helps."
        )
    )
    analyst = TechnicalAnalyst(provider)

    read = await analyst.analyze(symbol="TSLA", price=Decimal("250"), as_of="2026-08-27T00:00:00Z")

    assert read.stance is Stance.NEUTRAL
    assert read.confidence == Decimal("0.2")


@pytest.mark.asyncio
async def test_non_json_response_raises_analyst_output_error():
    provider = FakeProvider(response="I cannot help with that.")
    analyst = TechnicalAnalyst(provider)

    with pytest.raises(AnalystOutputError, match="not valid JSON"):
        await analyst.analyze(symbol="AAPL", price=Decimal("100"), as_of="2026-08-27T00:00:00Z")


@pytest.mark.asyncio
async def test_json_missing_a_required_field_raises_analyst_output_error():
    provider = FakeProvider(response='{"stance": "bullish", "summary": "x"}')
    analyst = TechnicalAnalyst(provider)

    with pytest.raises(AnalystOutputError, match="schema validation"):
        await analyst.analyze(symbol="AAPL", price=Decimal("100"), as_of="2026-08-27T00:00:00Z")


@pytest.mark.asyncio
async def test_confidence_out_of_range_fails_validation():
    provider = FakeProvider(
        response='{"stance": "bullish", "summary": "x", "confidence": "1.5"}'
    )
    analyst = TechnicalAnalyst(provider)

    with pytest.raises(AnalystOutputError, match="schema validation"):
        await analyst.analyze(symbol="AAPL", price=Decimal("100"), as_of="2026-08-27T00:00:00Z")


@pytest.mark.asyncio
async def test_an_invalid_stance_fails_validation():
    provider = FakeProvider(
        response='{"stance": "very bullish", "summary": "x", "confidence": "0.5"}'
    )
    analyst = TechnicalAnalyst(provider)

    with pytest.raises(AnalystOutputError, match="schema validation"):
        await analyst.analyze(symbol="AAPL", price=Decimal("100"), as_of="2026-08-27T00:00:00Z")


@pytest.mark.asyncio
async def test_a_provider_error_is_wrapped_as_analyst_output_error():
    provider = FakeProvider(error=LLMProviderError("rate limited"))
    analyst = TechnicalAnalyst(provider)

    with pytest.raises(AnalystOutputError, match="LLM provider call failed"):
        await analyst.analyze(symbol="AAPL", price=Decimal("100"), as_of="2026-08-27T00:00:00Z")
