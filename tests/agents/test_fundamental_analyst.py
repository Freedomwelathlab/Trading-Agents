"""Unit tests against a fake LLMProvider - no real network calls, no real
provider credentials needed (D059). Mirrors
tests/agents/test_technical_analyst.py's shape.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.api.app.agents.fundamental_analyst import (
    FundamentalAnalyst,
    build_fundamental_analyst,
    format_fundamentals,
)
from apps.api.app.agents.provider import LLMProviderError
from apps.api.app.agents.technical_analyst import AnalystOutputError, Stance
from apps.api.app.marketdata.fundamentals_provider import CompanyFundamentals


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


def _fundamentals(**overrides) -> CompanyFundamentals:
    defaults = dict(
        symbol="AAPL.US",
        source="longbridge",
        company_name="Apple Inc.",
        category="Technology",
        pe=Decimal("25"),
        pb=Decimal("8"),
        ps=Decimal("6"),
        dividend_yield=Decimal("0.5"),
        as_of=datetime(2026, 8, 20, tzinfo=UTC),
    )
    defaults.update(overrides)
    return CompanyFundamentals(**defaults)


@pytest.mark.asyncio
async def test_a_valid_json_response_produces_a_fundamental_read():
    provider = FakeProvider(
        response='{"stance": "bullish", "summary": "P/E of 25 with a real dividend", '
        '"confidence": "0.4"}'
    )
    analyst = FundamentalAnalyst(provider)

    read = await analyst.analyze(fundamentals=_fundamentals())

    assert read.stance is Stance.BULLISH
    assert read.summary == "P/E of 25 with a real dividend"
    assert read.confidence == Decimal("0.4")


@pytest.mark.asyncio
async def test_real_figures_reach_the_prompt_verbatim():
    provider = FakeProvider(
        response='{"stance": "neutral", "summary": "x", "confidence": "0.1"}'
    )
    analyst = FundamentalAnalyst(provider)

    await analyst.analyze(fundamentals=_fundamentals())

    prompt = provider.last_user_prompt
    assert prompt is not None
    assert "P/E: 25" in prompt
    assert "P/B: 8" in prompt
    assert "Apple Inc." in prompt
    assert "2026-08-20T00:00:00+00:00" in prompt


def test_missing_metrics_are_spelled_out_as_absent_never_defaulted_to_zero():
    rendered = format_fundamentals(
        _fundamentals(pe=None, pb=None, ps=None, dividend_yield=None, as_of=None)
    )

    assert "P/E: not reported by the vendor" in rendered
    assert "P/S: not reported by the vendor" in rendered
    assert "Valuation figures as of: not reported by the vendor" in rendered
    # With no P/E there is nothing to derive, so no earnings-yield line.
    assert "Earnings yield" not in rendered


def test_earnings_yield_is_computed_deterministically_and_labelled_as_such():
    rendered = format_fundamentals(_fundamentals(pe=Decimal("25")))

    assert "Earnings yield % (computed deterministically as 100/PE): 4" in rendered


def test_a_non_positive_pe_produces_no_earnings_yield_line():
    rendered = format_fundamentals(_fundamentals(pe=Decimal("-3")))

    assert "P/E: -3" in rendered
    assert "Earnings yield" not in rendered


@pytest.mark.asyncio
async def test_the_system_prompt_forbids_the_model_calculating_or_inventing_figures():
    """The no-fabrication instruction is part of this analyst's contract,
    not incidental prose - a refactor that drops it must fail a test."""
    provider = FakeProvider(
        response='{"stance": "neutral", "summary": "x", "confidence": "0.1"}'
    )
    await FundamentalAnalyst(provider).analyze(fundamentals=_fundamentals())

    system = provider.last_system_prompt
    assert system is not None
    assert "Never calculate" in system
    assert "never state a numeric value that was not given to you" in system


@pytest.mark.asyncio
async def test_non_json_response_raises_analyst_output_error():
    analyst = FundamentalAnalyst(FakeProvider(response="I cannot help with that."))

    with pytest.raises(AnalystOutputError, match="not valid JSON"):
        await analyst.analyze(fundamentals=_fundamentals())


@pytest.mark.asyncio
async def test_json_missing_a_required_field_raises_analyst_output_error():
    analyst = FundamentalAnalyst(FakeProvider(response='{"stance": "bullish", "summary": "x"}'))

    with pytest.raises(AnalystOutputError, match="schema validation"):
        await analyst.analyze(fundamentals=_fundamentals())


@pytest.mark.asyncio
async def test_confidence_out_of_range_fails_validation():
    analyst = FundamentalAnalyst(
        FakeProvider(response='{"stance": "bullish", "summary": "x", "confidence": "1.5"}')
    )

    with pytest.raises(AnalystOutputError, match="schema validation"):
        await analyst.analyze(fundamentals=_fundamentals())


@pytest.mark.asyncio
async def test_a_provider_error_is_wrapped_as_analyst_output_error():
    analyst = FundamentalAnalyst(FakeProvider(error=LLMProviderError("rate limited")))

    with pytest.raises(AnalystOutputError, match="LLM provider call failed"):
        await analyst.analyze(fundamentals=_fundamentals())


def test_build_returns_none_without_a_configured_provider():
    assert build_fundamental_analyst(None) is None
    assert isinstance(build_fundamental_analyst(FakeProvider()), FundamentalAnalyst)


def test_the_read_has_no_field_that_could_be_mistaken_for_a_trade():
    from apps.api.app.agents.fundamental_analyst import FundamentalRead

    assert set(FundamentalRead.model_fields) == {"stance", "summary", "confidence"}
