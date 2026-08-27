"""Integration tests for the optional TechnicalAnalyst wiring on
POST /brokers/{broker_id}/agent-trades (docs/DECISIONS.md D019), against a
real Postgres instance. Reuses tests/api/test_trades.py's fixtures and
tests/api/test_agent_trades.py's fakes rather than duplicating them.
"""

import pytest

from apps.api.app.agents.provider import LLMProviderError
from apps.api.app.agents.technical_analyst import TechnicalAnalyst
from apps.api.app.agents.trader import TraderAgent
from apps.api.app.api.dependencies import (
    get_market_data_router,
    get_technical_analyst,
    get_trader_agent,
)
from apps.api.app.main import app
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
    """Records the technical_context it was called with, so a test can
    prove the analyst's read actually reached the trader agent's prompt
    without needing a real LLM in the loop."""

    def __init__(self, response: str) -> None:
        super().__init__(FakeLLMProvider(response))
        self.received_technical_context: str | None = "UNSET"

    async def propose(self, *, symbol: str, directive: str, technical_context: str | None = None):
        self.received_technical_context = technical_context
        return await super().propose(
            symbol=symbol, directive=directive, technical_context=technical_context
        )


class FakeAnalystProvider:
    name = "fake-analyst-llm"

    def __init__(self, response: str | None = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error

    async def complete(self, *, system: str, user: str, max_tokens: int) -> str:
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


@pytest.mark.asyncio
async def test_no_technical_analyst_configured_still_succeeds_without_context():
    """Absence of a TechnicalAnalyst must never block agent-trades - it's
    optional context, not a hard dependency (unlike get_trader_agent)."""
    fake_agent = CapturingTraderAgent(
        '{"side": "buy", "quantity": "1", "stop_distance_pct": "2", "rationale": "x"}'
    )
    fake_market_data = MarketDataRouter([FakeMarketDataProvider()])

    async with (
        db_session() as session,
        active_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            app.dependency_overrides[get_trader_agent] = lambda: fake_agent
            app.dependency_overrides[get_market_data_router] = lambda: fake_market_data
            app.dependency_overrides[get_technical_analyst] = lambda: None
            try:
                token = await _get_token(client, email)
                response = await client.post(
                    f"/brokers/{broker_id}/agent-trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"symbol": "AAPL", "directive": "moderate long"},
                )
            finally:
                del app.dependency_overrides[get_trader_agent]
                del app.dependency_overrides[get_market_data_router]
                del app.dependency_overrides[get_technical_analyst]

        assert response.status_code == 200
        assert response.json()["status"] == "filled"
        assert fake_agent.received_technical_context is None


@pytest.mark.asyncio
async def test_a_configured_technical_analyst_reaches_the_trader_agents_prompt():
    fake_agent = CapturingTraderAgent(
        '{"side": "buy", "quantity": "1", "stop_distance_pct": "2", "rationale": "x"}'
    )
    fake_analyst = TechnicalAnalyst(
        FakeAnalystProvider(
            response='{"stance": "bullish", "summary": "near a round number", "confidence": "0.3"}'
        )
    )

    fake_market_data = MarketDataRouter([FakeMarketDataProvider()])

    async with (
        db_session() as session,
        active_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            app.dependency_overrides[get_trader_agent] = lambda: fake_agent
            app.dependency_overrides[get_market_data_router] = lambda: fake_market_data
            app.dependency_overrides[get_technical_analyst] = lambda: fake_analyst
            try:
                token = await _get_token(client, email)
                response = await client.post(
                    f"/brokers/{broker_id}/agent-trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"symbol": "AAPL", "directive": "moderate long"},
                )
            finally:
                del app.dependency_overrides[get_trader_agent]
                del app.dependency_overrides[get_market_data_router]
                del app.dependency_overrides[get_technical_analyst]

        assert response.status_code == 200
        assert response.json()["status"] == "filled"
        assert fake_agent.received_technical_context is not None
        assert "bullish" in fake_agent.received_technical_context
        assert "near a round number" in fake_agent.received_technical_context


@pytest.mark.asyncio
async def test_a_failing_technical_analyst_does_not_block_the_trade():
    """The analyst is informational only (D019) - a provider error from it
    must never fabricate a read and must never fail the whole request;
    the trade proceeds with technical_context omitted."""
    fake_agent = CapturingTraderAgent(
        '{"side": "buy", "quantity": "1", "stop_distance_pct": "2", "rationale": "x"}'
    )
    fake_analyst = TechnicalAnalyst(
        FakeAnalystProvider(error=LLMProviderError("analyst vendor down"))
    )

    fake_market_data = MarketDataRouter([FakeMarketDataProvider()])

    async with (
        db_session() as session,
        active_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            app.dependency_overrides[get_trader_agent] = lambda: fake_agent
            app.dependency_overrides[get_market_data_router] = lambda: fake_market_data
            app.dependency_overrides[get_technical_analyst] = lambda: fake_analyst
            try:
                token = await _get_token(client, email)
                response = await client.post(
                    f"/brokers/{broker_id}/agent-trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"symbol": "AAPL", "directive": "moderate long"},
                )
            finally:
                del app.dependency_overrides[get_trader_agent]
                del app.dependency_overrides[get_market_data_router]
                del app.dependency_overrides[get_technical_analyst]

        assert response.status_code == 200
        assert response.json()["status"] == "filled"
        assert fake_agent.received_technical_context is None
