"""Unit tests against a fake LLMProvider - no real network calls, no real
provider credentials needed (D059). Mirrors
tests/agents/test_technical_analyst.py's shape.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.api.app.agents.news_analyst import (
    NewsAnalyst,
    NewsRead,
    build_news_analyst,
    format_headlines,
)
from apps.api.app.agents.provider import LLMProviderError
from apps.api.app.agents.technical_analyst import AnalystOutputError, Stance
from apps.api.app.marketdata.news_provider import NewsHeadline


class FakeProvider:
    name = "fake-llm"

    def __init__(self, response: str | None = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.last_user_prompt: str | None = None
        self.last_system_prompt: str | None = None

    async def complete(self, *, system: str, user: str, max_tokens: int) -> str:
        self.last_system_prompt = system
        self.last_user_prompt = user
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


def _headlines() -> list[NewsHeadline]:
    return [
        NewsHeadline(title="Beats on revenue", published_at=datetime(2026, 8, 25, tzinfo=UTC)),
        NewsHeadline(title="New product line", published_at=datetime(2026, 8, 22, tzinfo=UTC)),
        NewsHeadline(title="Analyst downgrade", published_at=datetime(2026, 8, 19, tzinfo=UTC)),
    ]


@pytest.mark.asyncio
async def test_a_valid_json_response_produces_a_news_read():
    provider = FakeProvider(
        response='{"stance": "bullish", "summary": "three recent headlines, one upbeat", '
        '"confidence": "0.35"}'
    )

    read = await NewsAnalyst(provider).analyze(symbol="AAPL.US", headlines=_headlines())

    assert read.stance is Stance.BULLISH
    assert read.confidence == Decimal("0.35")


def test_the_headline_count_and_date_range_are_computed_not_left_to_the_model():
    rendered = format_headlines("AAPL.US", _headlines())

    assert "this is the exact number of items below): 3" in rendered
    assert "Oldest headline in this set: 2026-08-19T00:00:00+00:00" in rendered
    assert "Newest headline in this set: 2026-08-25T00:00:00+00:00" in rendered


def test_every_real_headline_is_listed_with_its_real_date():
    rendered = format_headlines("AAPL.US", _headlines())

    assert "1. [2026-08-25T00:00:00+00:00] Beats on revenue" in rendered
    assert "2. [2026-08-22T00:00:00+00:00] New product line" in rendered
    assert "3. [2026-08-19T00:00:00+00:00] Analyst downgrade" in rendered


def test_a_single_headline_reports_a_count_of_one_not_a_rounded_up_set():
    rendered = format_headlines("AAPL.US", _headlines()[:1])

    assert "items below): 1" in rendered
    assert "Analyst downgrade" not in rendered


def test_formatting_refuses_an_empty_list_rather_than_inventing_coverage():
    with pytest.raises(ValueError, match="at least one real headline"):
        format_headlines("AAPL.US", [])


@pytest.mark.asyncio
async def test_an_empty_headline_list_is_an_analyst_output_error_not_a_read():
    """No real headlines means no read at all - never a fabricated one,
    and never an LLM call made with an empty list to characterize."""
    provider = FakeProvider(response='{"stance": "neutral", "summary": "x", "confidence": "0"}')

    with pytest.raises(AnalystOutputError, match="at least one real headline"):
        await NewsAnalyst(provider).analyze(symbol="AAPL.US", headlines=[])

    assert provider.last_user_prompt is None


@pytest.mark.asyncio
async def test_the_system_prompt_forbids_citing_anything_outside_the_given_list():
    provider = FakeProvider(
        response='{"stance": "neutral", "summary": "x", "confidence": "0.1"}'
    )
    await NewsAnalyst(provider).analyze(symbol="AAPL.US", headlines=_headlines())

    system = provider.last_system_prompt
    assert system is not None
    assert "Cite only headlines that appear in the list below." in system
    assert "your knowledge of this company from training is not evidence" in system


@pytest.mark.asyncio
async def test_non_json_response_raises_analyst_output_error():
    analyst = NewsAnalyst(FakeProvider(response="I cannot help with that."))

    with pytest.raises(AnalystOutputError, match="not valid JSON"):
        await analyst.analyze(symbol="AAPL.US", headlines=_headlines())


@pytest.mark.asyncio
async def test_json_missing_a_required_field_raises_analyst_output_error():
    analyst = NewsAnalyst(FakeProvider(response='{"stance": "bearish", "summary": "x"}'))

    with pytest.raises(AnalystOutputError, match="schema validation"):
        await analyst.analyze(symbol="AAPL.US", headlines=_headlines())


@pytest.mark.asyncio
async def test_an_invalid_stance_fails_validation():
    analyst = NewsAnalyst(
        FakeProvider(response='{"stance": "very bullish", "summary": "x", "confidence": "0.5"}')
    )

    with pytest.raises(AnalystOutputError, match="schema validation"):
        await analyst.analyze(symbol="AAPL.US", headlines=_headlines())


@pytest.mark.asyncio
async def test_a_provider_error_is_wrapped_as_analyst_output_error():
    analyst = NewsAnalyst(FakeProvider(error=LLMProviderError("rate limited")))

    with pytest.raises(AnalystOutputError, match="LLM provider call failed"):
        await analyst.analyze(symbol="AAPL.US", headlines=_headlines())


def test_build_returns_none_without_a_configured_provider():
    assert build_news_analyst(None) is None
    assert isinstance(build_news_analyst(FakeProvider()), NewsAnalyst)


def test_the_read_has_no_field_that_could_be_mistaken_for_a_trade():
    assert set(NewsRead.model_fields) == {"stance", "summary", "confidence"}
