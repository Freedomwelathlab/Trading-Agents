"""Unit tests against a fake LLMProvider - no real network calls, no real
provider credentials needed. Mirrors tests/agents/test_technical_analyst.py's
shape.
"""

import json

import pytest

from apps.api.app.agents.provider import LLMProviderError
from apps.api.app.agents.strategy_research_assistant import (
    StrategyResearchAssistant,
    build_strategy_research_assistant,
)
from apps.api.app.agents.technical_analyst import AnalystOutputError

VALID_DEFINITION = {
    "indicators": [{"id": "rsi_14", "type": "rsi", "period": 14}],
    "entry_rule": {"op": "crosses_below", "left": "rsi_14", "right": "close"},
    "exit_rule": {"op": "crosses_above", "left": "rsi_14", "right": "close"},
    "position_sizing": {"type": "fixed_fraction", "fraction": 0.5},
}

INVALID_DEFINITION = {
    # "macd" is not a member of IndicatorType (only sma/rsi exist).
    "indicators": [{"id": "macd_1", "type": "macd", "period": 12}],
    "entry_rule": {"op": "crosses_below", "left": "macd_1", "right": "close"},
    "exit_rule": {"op": "crosses_above", "left": "macd_1", "right": "close"},
    # fixed_fraction requires "fraction" - missing here.
    "position_sizing": {"type": "fixed_fraction"},
}


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


def _response(
    *,
    name="Mean reversion via RSI",
    definition,
    rationale="Buys oversold, sells overbought.",
):
    return json.dumps({"name": name, "definition": definition, "rationale": rationale})


@pytest.mark.asyncio
async def test_a_vocabulary_valid_definition_produces_no_validation_errors():
    provider = FakeProvider(response=_response(definition=VALID_DEFINITION))
    assistant = StrategyResearchAssistant(provider)

    proposal = await assistant.propose(brief="a mean-reversion idea using RSI")

    assert proposal.validation_errors == []
    assert proposal.is_valid is True
    assert proposal.definition == VALID_DEFINITION
    assert proposal.name == "Mean reversion via RSI"


@pytest.mark.asyncio
async def test_a_vocabulary_violating_definition_returns_real_errors_without_raising():
    provider = FakeProvider(response=_response(definition=INVALID_DEFINITION))
    assistant = StrategyResearchAssistant(provider)

    proposal = await assistant.propose(brief="a mean-reversion idea using MACD")

    assert proposal.is_valid is False
    assert proposal.validation_errors  # non-empty
    # Itemized, real errors from validate_definition - not a generic message.
    joined = " ".join(proposal.validation_errors)
    assert "macd" in joined
    assert "fraction" in joined


@pytest.mark.asyncio
async def test_non_json_response_raises_analyst_output_error():
    provider = FakeProvider(response="I cannot help with that.")
    assistant = StrategyResearchAssistant(provider)

    with pytest.raises(AnalystOutputError, match="not valid JSON"):
        await assistant.propose(brief="a trend-following idea")


@pytest.mark.asyncio
async def test_json_missing_a_required_top_level_field_raises_analyst_output_error():
    # "rationale" is missing entirely - a schema failure on the raw response
    # model, before `definition` is ever handed to validate_definition.
    provider = FakeProvider(
        response=json.dumps({"name": "x", "definition": VALID_DEFINITION})
    )
    assistant = StrategyResearchAssistant(provider)

    with pytest.raises(AnalystOutputError, match="schema validation"):
        await assistant.propose(brief="anything")


@pytest.mark.asyncio
async def test_a_provider_error_is_wrapped_as_analyst_output_error():
    provider = FakeProvider(error=LLMProviderError("rate limited"))
    assistant = StrategyResearchAssistant(provider)

    with pytest.raises(AnalystOutputError, match="LLM provider call failed"):
        await assistant.propose(brief="anything")


def test_build_with_no_provider_returns_none():
    assert build_strategy_research_assistant(None) is None


def test_build_with_a_provider_returns_an_assistant():
    assistant = build_strategy_research_assistant(FakeProvider(response="{}"))
    assert isinstance(assistant, StrategyResearchAssistant)
