"""Integration tests for the optional HistoryProvider -> real-indicator
wiring on POST /brokers/{broker_id}/agent-trades (docs/DECISIONS.md D021),
against a real Postgres instance. Reuses existing fixtures/fakes rather
than duplicating them.
"""

from decimal import Decimal

import pytest

from apps.api.app.agents.technical_analyst import TechnicalAnalyst, TechnicalRead
from apps.api.app.agents.trader import TraderAgent
from apps.api.app.api.dependencies import (
    get_history_provider,
    get_market_data_router,
    get_technical_analyst,
    get_trader_agent,
)
from apps.api.app.main import app
from apps.api.app.marketdata.provider import VendorError
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


class CapturingTechnicalAnalyst(TechnicalAnalyst):
    """Records the indicator_context it was called with, so a test can
    prove computed indicators actually reached the analyst without a
    real LLM in the loop."""

    def __init__(self) -> None:
        super().__init__(provider=object())  # never actually called
        self.received_indicator_context: str | None = "UNSET"

    async def analyze(self, *, symbol, price, as_of, indicator_context=None) -> TechnicalRead:
        self.received_indicator_context = indicator_context
        return TechnicalRead(stance="neutral", summary="captured", confidence=Decimal("0.5"))


class FakeHistoryProvider:
    name = "fake-history"

    def __init__(self, *, closes: list[Decimal] | None = None, error: Exception | None = None):
        self._closes = closes
        self._error = error

    async def get_daily_closes(self, symbol: str, *, count: int) -> list[Decimal]:
        if self._error is not None:
            raise self._error
        assert self._closes is not None
        return self._closes


def _agent_response():
    return FakeLLMProvider(
        '{"side": "buy", "quantity": "1", "stop_distance_pct": "2", "rationale": "x"}'
    )


@pytest.mark.asyncio
async def test_no_history_provider_configured_still_succeeds_with_no_indicator_context():
    fake_agent = TraderAgent(_agent_response())
    fake_analyst = CapturingTechnicalAnalyst()
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
            app.dependency_overrides[get_history_provider] = lambda: None
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
                del app.dependency_overrides[get_history_provider]

        assert response.status_code == 200
        assert response.json()["status"] == "filled"
        assert fake_analyst.received_indicator_context is None


@pytest.mark.asyncio
async def test_a_configured_history_provider_produces_real_computed_indicators():
    fake_agent = TraderAgent(_agent_response())
    fake_analyst = CapturingTechnicalAnalyst()
    fake_market_data = MarketDataRouter([FakeMarketDataProvider()])
    # 21 closes: enough for SMA(20) and RSI(14).
    closes = [Decimal(100 + i) for i in range(21)]
    fake_history = FakeHistoryProvider(closes=closes)

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
            app.dependency_overrides[get_history_provider] = lambda: fake_history
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
                del app.dependency_overrides[get_history_provider]

        assert response.status_code == 200
        assert response.json()["status"] == "filled"
        assert fake_analyst.received_indicator_context is not None
        assert "SMA(20)=" in fake_analyst.received_indicator_context
        assert "RSI(14)=" in fake_analyst.received_indicator_context
        # Strictly increasing closes -> RSI must be 100 (all gains, no losses).
        assert "RSI(14)=100" in fake_analyst.received_indicator_context


@pytest.mark.asyncio
async def test_too_short_a_history_omits_the_indicator_context_without_failing():
    fake_agent = TraderAgent(_agent_response())
    fake_analyst = CapturingTechnicalAnalyst()
    fake_market_data = MarketDataRouter([FakeMarketDataProvider()])
    # Only 3 closes - not enough for SMA(20) or RSI(14).
    fake_history = FakeHistoryProvider(closes=[Decimal("100"), Decimal("101"), Decimal("102")])

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
            app.dependency_overrides[get_history_provider] = lambda: fake_history
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
                del app.dependency_overrides[get_history_provider]

        assert response.status_code == 200
        assert response.json()["status"] == "filled"
        assert fake_analyst.received_indicator_context is None


@pytest.mark.asyncio
async def test_a_failing_history_provider_does_not_block_the_trade():
    fake_agent = TraderAgent(_agent_response())
    fake_analyst = CapturingTechnicalAnalyst()
    fake_market_data = MarketDataRouter([FakeMarketDataProvider()])
    fake_history = FakeHistoryProvider(error=VendorError("vendor down"))

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
            app.dependency_overrides[get_history_provider] = lambda: fake_history
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
                del app.dependency_overrides[get_history_provider]

        assert response.status_code == 200
        assert response.json()["status"] == "filled"
        assert fake_analyst.received_indicator_context is None
