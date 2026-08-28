"""Integration tests for POST /brokers/{broker_id}/agent-trades against a
real Postgres instance. Reuses tests/api/test_trades.py's fixtures/helpers
rather than duplicating them - same DB/auth/broker-grant setup, just a
different endpoint and a fake TraderAgent instead of (or alongside) a
fake MarketDataRouter.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.api.app.agents.provider import LLMProviderError
from apps.api.app.agents.trader import TraderAgent
from apps.api.app.api.dependencies import get_market_data_router, get_trader_agent
from apps.api.app.main import app
from tests.api.test_trades import (
    _get_token,
    active_user,
    api_client,
    broker_grant,
    db_session,
    paper_broker_row,
)


class FakeLLMProvider:
    name = "fake-llm"

    def __init__(self, response: str) -> None:
        self._response = response

    async def complete(self, *, system: str, user: str, max_tokens: int) -> str:
        return self._response


class ExplodingLLMProvider:
    name = "fake-llm"

    async def complete(self, *, system: str, user: str, max_tokens: int) -> str:
        raise LLMProviderError("should not be called")


class FakeMarketDataProvider:
    name = "fake-vendor"

    async def get_snapshot(self, symbol: str):
        from apps.api.app.marketdata.models import MarketSnapshot

        return MarketSnapshot(
            symbol=symbol, price=Decimal("100"), as_of=datetime.now(UTC), source=self.name
        )


@pytest.mark.asyncio
async def test_omitting_a_trader_agent_is_400_not_configured():
    async with (
        db_session() as session,
        active_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.post(
                f"/brokers/{broker_id}/agent-trades",
                headers={"Authorization": f"Bearer {token}"},
                json={"symbol": "AAPL", "directive": "moderate long"},
            )

        assert response.status_code == 400
        assert "NOT_CONFIGURED" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_valid_agent_idea_is_submitted_through_the_normal_risk_path():
    from apps.api.app.marketdata.router import MarketDataRouter

    fake_agent = TraderAgent(
        FakeLLMProvider(
            '{"side": "buy", "quantity": "1", "stop_distance_pct": "2", '
            '"rationale": "clean breakout"}'
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

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "filled"
        assert body["side"] == "buy"
        assert body["quantity"] == "1"
        assert body["rationale"] == "clean breakout"
        assert body["fill_price"] == "100"


@pytest.mark.asyncio
async def test_an_oversized_agent_idea_is_rejected_by_the_risk_engine_not_the_agent():
    """The agent can propose anything; the same Risk Engine a human trade
    goes through must still be the one that blocks an oversized order -
    proves this route doesn't bypass it (docs/AGENT_POLICY.md)."""
    from apps.api.app.marketdata.router import MarketDataRouter

    fake_agent = TraderAgent(
        FakeLLMProvider(
            '{"side": "buy", "quantity": "20000", "stop_distance_pct": "2", '
            '"rationale": "go big"}'
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
            try:
                token = await _get_token(client, email)
                response = await client.post(
                    f"/brokers/{broker_id}/agent-trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"symbol": "AAPL", "directive": "go big"},
                )
            finally:
                del app.dependency_overrides[get_trader_agent]
                del app.dependency_overrides[get_market_data_router]

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "rejected"
        assert body["approved"] is False
        assert body["block_reason"] is not None


@pytest.mark.asyncio
async def test_malformed_agent_output_is_a_502_not_a_fabricated_trade():
    from apps.api.app.marketdata.router import MarketDataRouter

    fake_agent = TraderAgent(FakeLLMProvider("not json at all"))
    # D019 resolves the live quote before calling the trader agent (so an
    # optional TechnicalAnalyst can share the same quote) - a market data
    # router must be stubbed here too, or this test would hit the
    # NOT_CONFIGURED quote path before ever reaching the agent at all.
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
            try:
                token = await _get_token(client, email)
                response = await client.post(
                    f"/brokers/{broker_id}/agent-trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"symbol": "AAPL", "directive": "long"},
                )
            finally:
                del app.dependency_overrides[get_trader_agent]
                del app.dependency_overrides[get_market_data_router]

        assert response.status_code == 502
        assert "AGENT_OUTPUT_INVALID" in response.json()["detail"]


@pytest.mark.asyncio
async def test_missing_broker_grant_is_403_before_the_agent_is_ever_called():
    """Authorization must be checked before spending any tokens on an LLM
    call - proven with a provider that raises if invoked at all."""
    fake_agent = TraderAgent(ExplodingLLMProvider())

    async with (
        db_session() as session,
        active_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        # deliberately no broker_grant
    ):
        async with api_client() as client:
            app.dependency_overrides[get_trader_agent] = lambda: fake_agent
            try:
                token = await _get_token(client, email)
                response = await client.post(
                    f"/brokers/{broker_id}/agent-trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"symbol": "AAPL", "directive": "long"},
                )
            finally:
                del app.dependency_overrides[get_trader_agent]

        assert response.status_code == 403


@pytest.mark.asyncio
async def test_an_identical_agent_trade_submitted_twice_is_blocked_as_a_duplicate():
    """docs/DECISIONS.md D024: an LLM-originated duplicate is just as real
    a risk as a human-submitted one, so the agent-trades route must go
    through the exact same duplicate check as POST .../trades."""
    from apps.api.app.marketdata.router import MarketDataRouter

    fake_agent = TraderAgent(
        FakeLLMProvider(
            '{"side": "buy", "quantity": "1", "stop_distance_pct": "2", '
            '"rationale": "clean breakout"}'
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
            try:
                token = await _get_token(client, email)
                headers = {"Authorization": f"Bearer {token}"}
                payload = {"symbol": "AAPL", "directive": "moderate long"}
                first = await client.post(
                    f"/brokers/{broker_id}/agent-trades", headers=headers, json=payload
                )
                second = await client.post(
                    f"/brokers/{broker_id}/agent-trades", headers=headers, json=payload
                )
            finally:
                del app.dependency_overrides[get_trader_agent]
                del app.dependency_overrides[get_market_data_router]

        assert first.status_code == 200
        assert first.json()["status"] == "filled"

        assert second.status_code == 200
        second_body = second.json()
        assert second_body["status"] == "rejected"
        assert second_body["block_reason"] == "duplicate_order"
