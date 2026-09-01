"""Integration tests for the optional FundamentalAnalyst and NewsAnalyst
wiring on POST /brokers/{broker_id}/agent-trades (docs/DECISIONS.md D059),
against a real Postgres instance. Reuses tests/api/test_trades.py's
fixtures and tests/api/test_agent_trades.py's fakes rather than
duplicating them.

The property under test throughout is D059's central safety claim: each
analyst is optional, additive context, and no combination of absent
analyst, absent vendor, unsupported symbol, or failed call can block a
trade or put a fabricated read into the trader agent's prompt.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.api.app.agents.fundamental_analyst import FundamentalAnalyst
from apps.api.app.agents.news_analyst import NewsAnalyst
from apps.api.app.agents.trader import TraderAgent
from apps.api.app.api.dependencies import (
    get_fundamental_analyst,
    get_fundamentals_provider,
    get_history_provider,
    get_market_data_router,
    get_news_analyst,
    get_news_provider,
    get_technical_analyst,
    get_trader_agent,
)
from apps.api.app.main import app
from apps.api.app.marketdata.fundamentals_provider import CompanyFundamentals
from apps.api.app.marketdata.news_provider import NewsHeadline
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.router import MarketDataRouter
from tests.api.test_agent_trades import FakeLLMProvider, FakeMarketDataProvider
from tests.api.test_trades import (
    _get_token,
    active_user,
    api_client,
    broker_grant,
    db_session,
    paper_broker_row,
)


class CapturingTraderAgent(TraderAgent):
    """Records every analyst context it was called with, so a test can
    prove which reads actually reached the trader agent's prompt without
    needing a real LLM in the loop."""

    def __init__(self) -> None:
        super().__init__(
            FakeLLMProvider(
                '{"side": "buy", "quantity": "1", "stop_distance_pct": "2", '
                '"rationale": "x"}'
            )
        )
        self.received: dict[str, str | None] = {}

    async def propose(
        self,
        *,
        symbol: str,
        directive: str,
        technical_context: str | None = None,
        fundamental_context: str | None = None,
        news_context: str | None = None,
    ):
        self.received = {
            "technical": technical_context,
            "fundamental": fundamental_context,
            "news": news_context,
        }
        return await super().propose(
            symbol=symbol,
            directive=directive,
            technical_context=technical_context,
            fundamental_context=fundamental_context,
            news_context=news_context,
        )


class FakeAnalystLLM:
    name = "fake-analyst-llm"

    def __init__(self, response: str | None = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error

    async def complete(self, *, system: str, user: str, max_tokens: int) -> str:
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


class FakeFundamentalsProvider:
    name = "fake-fundamentals"

    def __init__(self, *, fundamentals=None, error: Exception | None = None):
        self._fundamentals = fundamentals
        self._error = error

    async def get_fundamentals(self, symbol: str) -> CompanyFundamentals:
        if self._error is not None:
            raise self._error
        assert self._fundamentals is not None
        return self._fundamentals


class FakeNewsProvider:
    name = "fake-news"

    def __init__(self, *, headlines=None, error: Exception | None = None):
        self._headlines = headlines
        self._error = error
        self.requested_limit: int | None = None

    async def get_recent_headlines(self, symbol: str, *, limit: int) -> list[NewsHeadline]:
        self.requested_limit = limit
        if self._error is not None:
            raise self._error
        assert self._headlines is not None
        return self._headlines


def _real_fundamentals() -> CompanyFundamentals:
    return CompanyFundamentals(
        symbol="AAPL",
        source="fake-fundamentals",
        company_name="Apple Inc.",
        pe=Decimal("25"),
        as_of=datetime(2026, 8, 20, tzinfo=UTC),
    )


def _real_headlines() -> list[NewsHeadline]:
    return [
        NewsHeadline(title="Beats on revenue", published_at=datetime(2026, 8, 25, tzinfo=UTC)),
        NewsHeadline(title="New product", published_at=datetime(2026, 8, 22, tzinfo=UTC)),
    ]


async def _post_agent_trade(overrides: dict):
    """Runs one agent-trade request with the given dependency overrides
    layered on top of the always-needed market-data and trader-agent ones.
    Returns (response, capturing_agent)."""
    agent = CapturingTraderAgent()
    base = {
        get_trader_agent: lambda: agent,
        get_market_data_router: lambda: MarketDataRouter([FakeMarketDataProvider()]),
        # This suite is about the two new analysts; the technical one is
        # switched off so its context can't be confused for theirs.
        get_technical_analyst: lambda: None,
        get_history_provider: lambda: None,
    }
    base.update(overrides)

    async with (
        db_session() as session,
        active_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            app.dependency_overrides.update(base)
            try:
                token = await _get_token(client, email)
                response = await client.post(
                    f"/brokers/{broker_id}/agent-trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"symbol": "AAPL", "directive": "moderate long"},
                )
            finally:
                for dependency in base:
                    app.dependency_overrides.pop(dependency, None)

    return response, agent


@pytest.mark.asyncio
async def test_neither_analyst_configured_still_succeeds_with_no_extra_context():
    response, agent = await _post_agent_trade(
        {
            get_fundamental_analyst: lambda: None,
            get_fundamentals_provider: lambda: None,
            get_news_analyst: lambda: None,
            get_news_provider: lambda: None,
        }
    )

    assert response.status_code == 200
    assert response.json()["status"] == "filled"
    assert agent.received == {"technical": None, "fundamental": None, "news": None}


@pytest.mark.asyncio
async def test_both_analysts_reach_the_trader_agents_prompt():
    news_provider = FakeNewsProvider(headlines=_real_headlines())
    response, agent = await _post_agent_trade(
        {
            get_fundamental_analyst: lambda: FundamentalAnalyst(
                FakeAnalystLLM(
                    response='{"stance": "bullish", "summary": "reasonable P/E", '
                    '"confidence": "0.4"}'
                )
            ),
            get_fundamentals_provider: lambda: FakeFundamentalsProvider(
                fundamentals=_real_fundamentals()
            ),
            get_news_analyst: lambda: NewsAnalyst(
                FakeAnalystLLM(
                    response='{"stance": "bearish", "summary": "two recent headlines", '
                    '"confidence": "0.2"}'
                )
            ),
            get_news_provider: lambda: news_provider,
        }
    )

    assert response.status_code == 200
    assert response.json()["status"] == "filled"

    fundamental = agent.received["fundamental"]
    news = agent.received["news"]
    assert fundamental is not None and "bullish" in fundamental
    assert "reasonable P/E" in fundamental
    assert news is not None and "bearish" in news
    assert "two recent headlines" in news
    assert news_provider.requested_limit == 10


@pytest.mark.asyncio
async def test_an_analyst_without_its_vendor_contributes_nothing_rather_than_guessing():
    """A configured LLM analyst but no configured data vendor must produce
    no context at all - there is nothing real for it to narrate."""
    response, agent = await _post_agent_trade(
        {
            get_fundamental_analyst: lambda: FundamentalAnalyst(
                FakeAnalystLLM(response='{"stance": "bullish", "summary": "x", '
                '"confidence": "0.9"}')
            ),
            get_fundamentals_provider: lambda: None,
            get_news_analyst: lambda: NewsAnalyst(
                FakeAnalystLLM(response='{"stance": "bullish", "summary": "x", '
                '"confidence": "0.9"}')
            ),
            get_news_provider: lambda: None,
        }
    )

    assert response.status_code == 200
    assert response.json()["status"] == "filled"
    assert agent.received["fundamental"] is None
    assert agent.received["news"] is None


@pytest.mark.asyncio
async def test_an_unsupported_symbol_omits_the_context_without_failing_the_trade():
    response, agent = await _post_agent_trade(
        {
            get_fundamental_analyst: lambda: FundamentalAnalyst(FakeAnalystLLM(response="{}")),
            get_fundamentals_provider: lambda: FakeFundamentalsProvider(
                error=DataUnavailableError("no coverage for this symbol")
            ),
            get_news_analyst: lambda: NewsAnalyst(FakeAnalystLLM(response="{}")),
            get_news_provider: lambda: FakeNewsProvider(
                error=DataUnavailableError("no news for this symbol")
            ),
        }
    )

    assert response.status_code == 200
    assert response.json()["status"] == "filled"
    assert agent.received["fundamental"] is None
    assert agent.received["news"] is None


@pytest.mark.asyncio
async def test_a_failing_vendor_does_not_block_the_trade():
    response, agent = await _post_agent_trade(
        {
            get_fundamental_analyst: lambda: FundamentalAnalyst(FakeAnalystLLM(response="{}")),
            get_fundamentals_provider: lambda: FakeFundamentalsProvider(
                error=VendorError("vendor down")
            ),
            get_news_analyst: lambda: NewsAnalyst(FakeAnalystLLM(response="{}")),
            get_news_provider: lambda: FakeNewsProvider(error=VendorError("vendor down")),
        }
    )

    assert response.status_code == 200
    assert response.json()["status"] == "filled"
    assert agent.received["fundamental"] is None
    assert agent.received["news"] is None


@pytest.mark.asyncio
async def test_a_failing_analyst_llm_does_not_block_the_trade():
    from apps.api.app.agents.provider import LLMProviderError

    response, agent = await _post_agent_trade(
        {
            get_fundamental_analyst: lambda: FundamentalAnalyst(
                FakeAnalystLLM(error=LLMProviderError("analyst vendor down"))
            ),
            get_fundamentals_provider: lambda: FakeFundamentalsProvider(
                fundamentals=_real_fundamentals()
            ),
            get_news_analyst: lambda: NewsAnalyst(
                FakeAnalystLLM(error=LLMProviderError("analyst vendor down"))
            ),
            get_news_provider: lambda: FakeNewsProvider(headlines=_real_headlines()),
        }
    )

    assert response.status_code == 200
    assert response.json()["status"] == "filled"
    assert agent.received["fundamental"] is None
    assert agent.received["news"] is None


@pytest.mark.asyncio
async def test_one_analyst_failing_does_not_suppress_the_other():
    """The three analysts run concurrently and are independently
    failure-isolated - a dead news vendor must not cost the trade its
    perfectly good fundamental read."""
    response, agent = await _post_agent_trade(
        {
            get_fundamental_analyst: lambda: FundamentalAnalyst(
                FakeAnalystLLM(
                    response='{"stance": "neutral", "summary": "survived", '
                    '"confidence": "0.5"}'
                )
            ),
            get_fundamentals_provider: lambda: FakeFundamentalsProvider(
                fundamentals=_real_fundamentals()
            ),
            get_news_analyst: lambda: NewsAnalyst(FakeAnalystLLM(response="{}")),
            get_news_provider: lambda: FakeNewsProvider(error=VendorError("news vendor down")),
        }
    )

    assert response.status_code == 200
    fundamental = agent.received["fundamental"]
    assert fundamental is not None and "survived" in fundamental
    assert agent.received["news"] is None
