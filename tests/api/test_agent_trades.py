"""Integration tests for POST /brokers/{broker_id}/agent-trades against a
real Postgres instance. Reuses tests/api/test_trades.py's fixtures/helpers
rather than duplicating them - same DB/auth/broker-grant setup, just a
different endpoint and a fake TraderAgent instead of (or alongside) a
fake MarketDataRouter.

Every test in this module is HERMETIC with respect to the LLM provider.
The `_no_real_analysts` autouse fixture below pins all three of D059's
optional analysts (and their three market-data providers) to "not
configured", so no test here can reach a real `LLM_PROVIDER_BASE_URL`
even when one is set in `.env`. Tests that want a real analyst read
override that explicitly, with a fake provider, via `analysts()`.

That fixture is what closed the duplicate-detection flake documented in
docs/IMPLEMENTATION_STATUS.md up to Phase 52: `get_trader_agent` was
overridden but `get_technical_analyst` was not, so the route still made a
real network call, and a slow endpoint stretched the wall-clock gap
between the two identical submissions past D024's duplicate window.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apps.api.app.agents.fundamental_analyst import FundamentalAnalyst
from apps.api.app.agents.news_analyst import NewsAnalyst
from apps.api.app.agents.provider import LLMProviderError
from apps.api.app.agents.technical_analyst import TechnicalAnalyst
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


# --- Phase 52 fakes: real-shaped inputs for the three analysts ----------
#
# These stand in for the vendor side only. Every value an analyst READ
# carries still comes from the analyst itself; nothing below fabricates a
# stance, a summary or a confidence.


class FakeHistoryProvider:
    """30 real-shaped daily closes - enough for both SMA(20) and RSI(14),
    and alternating so RSI has both gains and losses to work with."""

    name = "fake-history"

    async def get_daily_closes(self, symbol: str, *, count: int) -> list[Decimal]:
        closes = []
        price = Decimal("100")
        for index in range(30):
            price += Decimal("1") if index % 2 == 0 else Decimal("-0.5")
            closes.append(price)
        return closes[:count]


FUNDAMENTALS_AS_OF = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


class FakeFundamentalsProvider:
    name = "fake-fundamentals"

    async def get_fundamentals(self, symbol: str) -> CompanyFundamentals:
        return CompanyFundamentals(
            symbol=symbol,
            source=self.name,
            company_name="Fake Company Inc",
            category="Technology",
            pe=Decimal("25"),
            pb=Decimal("8"),
            as_of=FUNDAMENTALS_AS_OF,
        )


class UnavailableFundamentalsProvider:
    name = "fake-fundamentals"

    async def get_fundamentals(self, symbol: str) -> CompanyFundamentals:
        raise DataUnavailableError(f"DATA_UNAVAILABLE: no fundamentals for {symbol}")


FAKE_HEADLINE_COUNT = 3


class FakeNewsProvider:
    name = "fake-news"

    async def get_recent_headlines(self, symbol: str, *, limit: int) -> list[NewsHeadline]:
        now = datetime.now(UTC)
        return [
            NewsHeadline(
                title=f"{symbol} headline {index}",
                published_at=now - timedelta(hours=index),
            )
            for index in range(FAKE_HEADLINE_COUNT)
        ][:limit]


class ExplodingNewsProvider:
    name = "fake-news"

    async def get_recent_headlines(self, symbol: str, *, limit: int) -> list[NewsHeadline]:
        raise VendorError("news vendor is down")


ANALYST_JSON = (
    '{{"stance": "{stance}", "summary": "{summary}", "confidence": "{confidence}"}}'
)


def analyst_response(*, stance: str, summary: str, confidence: str) -> str:
    return ANALYST_JSON.format(stance=stance, summary=summary, confidence=confidence)


@pytest.fixture(autouse=True)
def _no_real_analysts() -> Iterator[None]:
    """Pins every optional analyst and every optional market-data provider
    this route consults to "not configured", for every test in this module.

    Without this, a test that overrides only `get_trader_agent` still lets
    the route build a real TechnicalAnalyst from whatever
    `LLM_PROVIDER_BASE_URL` happens to be in `.env` and make a real network
    call - which is exactly how the duplicate-detection test below became
    environmentally flaky. Overriding the three ANALYSTS alone would be
    enough (a None analyst means its provider is never consulted), but the
    providers are pinned too so no test can reach a real vendor either.
    """
    deps = (
        get_technical_analyst,
        get_fundamental_analyst,
        get_news_analyst,
        get_history_provider,
        get_fundamentals_provider,
        get_news_provider,
    )
    for dep in deps:
        app.dependency_overrides[dep] = lambda: None
    try:
        yield
    finally:
        for dep in deps:
            # pop, not del: a test may have removed its own override first.
            app.dependency_overrides.pop(dep, None)


@contextmanager
def analysts(
    *,
    technical: object | None = None,
    history: object | None = None,
    fundamental: object | None = None,
    fundamentals: object | None = None,
    news: object | None = None,
    headlines: object | None = None,
) -> Iterator[None]:
    """Opt back in to specific analysts on top of the hermetic default.
    Anything not named stays "not configured", which is the whole point:
    the three analysts are independently optional, so a test must be able
    to configure any subset of them."""
    overrides = {
        get_technical_analyst: technical,
        get_history_provider: history,
        get_fundamental_analyst: fundamental,
        get_fundamentals_provider: fundamentals,
        get_news_analyst: news,
        get_news_provider: headlines,
    }
    for dep, value in overrides.items():
        # Default-arg binding, not a closure: all six lambdas must capture
        # their OWN value, not the last one the loop saw.
        app.dependency_overrides[dep] = lambda v=value: v
    try:
        yield
    finally:
        for dep in overrides:
            app.dependency_overrides.pop(dep, None)


@pytest.mark.asyncio
async def test_omitting_a_trader_agent_is_400_not_configured():
    """The absent agent and vendor are stated EXPLICITLY (Phase 73, D091).

    Previously this leaned on the test host having no credentials of any
    kind. Adding real Longbridge credentials to `.env` wired a market-data
    vendor and the endpoint stopped reporting NOT_CONFIGURED - a failure
    caused entirely by the environment rather than by the code under test.
    """
    from apps.api.app.api.dependencies import get_market_data_router

    async with (
        db_session() as session,
        active_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            app.dependency_overrides[get_market_data_router] = lambda: None
            try:
                token = await _get_token(client, email)
                response = await client.post(
                    f"/brokers/{broker_id}/agent-trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"symbol": "AAPL", "directive": "moderate long"},
                )
            finally:
                del app.dependency_overrides[get_market_data_router]

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
    through the exact same duplicate check as POST .../trades.

    Made hermetic in Phase 52. The `_no_real_analysts` autouse fixture
    pins the three analysts to "not configured", so neither submission can
    make a real LLM call. That is the whole fix for the flake this test
    was carrying: D024's duplicate window is wall-clock, and a slow real
    provider used to stretch the gap between the two POSTs past it, making
    the second come back `filled` instead of `rejected`."""
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


# --- Phase 52: the per-analyst reads the response now carries -----------


def _trader_and_market_data():
    from apps.api.app.marketdata.router import MarketDataRouter

    return (
        TraderAgent(
            FakeLLMProvider(
                '{"side": "buy", "quantity": "1", "stop_distance_pct": "2", '
                '"rationale": "clean breakout"}'
            )
        ),
        MarketDataRouter([FakeMarketDataProvider()]),
    )


@pytest.mark.asyncio
async def test_all_three_analyst_reads_are_returned_when_all_three_are_configured():
    """The gap D062 recorded, closed: the response now says which analysts
    ran and what each one actually concluded. Every asserted value below
    traces to a fake analyst's real output or a deterministically-computed
    input - none of it is defaulted or synthesised by the route."""
    fake_agent, fake_market_data = _trader_and_market_data()

    technical = TechnicalAnalyst(
        FakeLLMProvider(
            analyst_response(
                stance="bullish", summary="Trading above its 20-day average.", confidence="0.7"
            )
        )
    )
    fundamental = FundamentalAnalyst(
        FakeLLMProvider(
            analyst_response(
                stance="neutral", summary="A P/E of 25 with no growth figures.", confidence="0.4"
            )
        )
    )
    news = NewsAnalyst(
        FakeLLMProvider(
            analyst_response(
                stance="bearish", summary="Three recent headlines, none positive.", confidence="0.6"
            )
        )
    )

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
                with analysts(
                    technical=technical,
                    history=FakeHistoryProvider(),
                    fundamental=fundamental,
                    fundamentals=FakeFundamentalsProvider(),
                    news=news,
                    headlines=FakeNewsProvider(),
                ):
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

        # The trade decision is untouched by this phase.
        assert body["status"] == "filled"
        assert body["side"] == "buy"
        assert body["quantity"] == "1"
        assert body["rationale"] == "clean breakout"

        technical_read = body["technical_analyst"]
        assert technical_read["stance"] == "bullish"
        assert technical_read["summary"] == "Trading above its 20-day average."
        assert technical_read["confidence"] == "0.7"
        # D021's real, deterministically-computed values - the exact string
        # the analyst was handed, not a re-derivation.
        assert "SMA(20)=" in technical_read["indicator_context"]
        assert "RSI(14)=" in technical_read["indicator_context"]

        fundamental_read = body["fundamental_analyst"]
        assert fundamental_read["stance"] == "neutral"
        assert fundamental_read["summary"] == "A P/E of 25 with no growth figures."
        assert fundamental_read["confidence"] == "0.4"
        assert fundamental_read["data_source"] == "fake-fundamentals"
        assert fundamental_read["fundamentals_as_of"] is not None

        news_read = body["news_analyst"]
        assert news_read["stance"] == "bearish"
        assert news_read["summary"] == "Three recent headlines, none positive."
        assert news_read["confidence"] == "0.6"
        # Counted in code from the list actually shown to the analyst.
        assert news_read["headline_count"] == FAKE_HEADLINE_COUNT


@pytest.mark.asyncio
async def test_no_analyst_configured_returns_three_nulls_and_the_trade_still_succeeds():
    """The three fields are present and explicitly null - never absent,
    never a synthesised "no signal" read. And the trade goes through
    exactly as it did before any analyst existed."""
    fake_agent, fake_market_data = _trader_and_market_data()

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
        assert body["fill_price"] == "100"

        for field in ("technical_analyst", "fundamental_analyst", "news_analyst"):
            assert field in body, f"{field} must be present even when the analyst didn't run"
            assert body[field] is None


@pytest.mark.asyncio
async def test_each_analyst_field_is_null_independently_of_the_other_two():
    """The nullability is PER ANALYST, which is the whole reason D062 said
    the widened shape must not "imply all three always ran". Configuring
    only the news analyst must populate only the news field."""
    fake_agent, fake_market_data = _trader_and_market_data()

    news = NewsAnalyst(
        FakeLLMProvider(
            analyst_response(stance="neutral", summary="Quiet news flow.", confidence="0.2")
        )
    )

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
                with analysts(news=news, headlines=FakeNewsProvider()):
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

        assert body["technical_analyst"] is None
        assert body["fundamental_analyst"] is None
        assert body["news_analyst"] is not None
        assert body["news_analyst"]["stance"] == "neutral"
        assert body["news_analyst"]["headline_count"] == FAKE_HEADLINE_COUNT


@pytest.mark.asyncio
async def test_a_configured_analyst_that_fails_is_null_and_never_blocks_the_trade():
    """Failure isolation, now visible in the response rather than only in
    the server log. Three different real failure modes at once:

    - the technical analyst's LLM call raises (LLMProviderError),
    - the fundamentals vendor has nothing for the symbol
      (DataUnavailableError),
    - the news vendor itself fails (VendorError).

    All three must produce null - not a partial read, not a neutral
    stance - and the trade must still fill.
    """
    fake_agent, fake_market_data = _trader_and_market_data()

    exploding_technical = TechnicalAnalyst(ExplodingLLMProvider())
    fundamental = FundamentalAnalyst(
        FakeLLMProvider(
            analyst_response(stance="bullish", summary="Should never be reached.", confidence="0.9")
        )
    )
    news = NewsAnalyst(
        FakeLLMProvider(
            analyst_response(stance="bullish", summary="Should never be reached.", confidence="0.9")
        )
    )

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
                with analysts(
                    technical=exploding_technical,
                    history=FakeHistoryProvider(),
                    fundamental=fundamental,
                    fundamentals=UnavailableFundamentalsProvider(),
                    news=news,
                    headlines=ExplodingNewsProvider(),
                ):
                    token = await _get_token(client, email)
                    response = await client.post(
                        f"/brokers/{broker_id}/agent-trades",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"symbol": "AAPL", "directive": "moderate long"},
                    )
            finally:
                del app.dependency_overrides[get_trader_agent]
                del app.dependency_overrides[get_market_data_router]

        # An analyst failure is never an error response.
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "filled"
        assert body["fill_price"] == "100"

        assert body["technical_analyst"] is None
        assert body["fundamental_analyst"] is None
        assert body["news_analyst"] is None


@pytest.mark.asyncio
async def test_an_unparseable_analyst_response_is_null_not_a_partial_read():
    """An analyst whose provider answers with something that isn't a valid
    read must be reported as no read at all. Salvaging a stance out of
    unparseable output would be exactly the fabrication spec Sec57
    forbids."""
    fake_agent, fake_market_data = _trader_and_market_data()

    garbage_technical = TechnicalAnalyst(FakeLLMProvider("I think it looks bullish, honestly"))

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
                with analysts(technical=garbage_technical):
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
        assert body["technical_analyst"] is None


@pytest.mark.asyncio
async def test_a_technical_read_without_history_reports_a_null_indicator_context():
    """No HistoryProvider means no real SMA/RSI was computed, so the field
    that would report them is null - the analyst still produced a genuine
    qualitative read of the single live quote (D019's original case), and
    that read is returned in full."""
    fake_agent, fake_market_data = _trader_and_market_data()

    technical = TechnicalAnalyst(
        FakeLLMProvider(
            analyst_response(
                stance="neutral", summary="One quote, near a round number.", confidence="0.3"
            )
        )
    )

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
                # history deliberately left unconfigured
                with analysts(technical=technical):
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
        technical_read = response.json()["technical_analyst"]
        assert technical_read is not None
        assert technical_read["stance"] == "neutral"
        assert technical_read["summary"] == "One quote, near a round number."
        assert technical_read["indicator_context"] is None


@pytest.mark.asyncio
async def test_an_analyst_read_carries_no_field_that_could_be_read_as_a_trade():
    """AnalystReadOut is read-only by construction on the wire too: no
    side, quantity, price or stop field exists on it, so nothing in an
    analyst's read can be mistaken by a consumer for a proposal
    (docs/AGENT_POLICY.md, docs/TRADING_SAFETY.md spec Sec62)."""
    fake_agent, fake_market_data = _trader_and_market_data()

    technical = TechnicalAnalyst(
        FakeLLMProvider(
            analyst_response(stance="bullish", summary="Momentum intact.", confidence="0.55")
        )
    )

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
                with analysts(technical=technical, history=FakeHistoryProvider()):
                    token = await _get_token(client, email)
                    response = await client.post(
                        f"/brokers/{broker_id}/agent-trades",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"symbol": "AAPL", "directive": "moderate long"},
                    )
            finally:
                del app.dependency_overrides[get_trader_agent]
                del app.dependency_overrides[get_market_data_router]

        technical_read = response.json()["technical_analyst"]
        assert set(technical_read) == {"stance", "summary", "confidence", "indicator_context"}
        for forbidden in ("side", "quantity", "price", "stop_price", "order_id"):
            assert forbidden not in technical_read
