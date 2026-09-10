"""Integration tests for the two Phase-62 market-risk constraints (D079) on
the trade-path Portfolio Manager, over real HTTP against a real Postgres
instance.

Same harness rationale as tests/api/test_trades_portfolio_manager.py
(httpx.AsyncClient + ASGITransport). What these add: real `market_data_bars`
rows seeded through `MarketDataStore.upsert_bars` exactly as a backfill
would, so the route's `load_market_risk_inputs` read has genuine daily
series to compute an annualized volatility and a pairwise correlation from -
and the three outcomes D079 cares about are exercised end to end:

  * a BUY that pushes projected book volatility over the ceiling is sized
    DOWN (MODIFY, binding `portfolio_volatility`);
  * opening a position too tightly correlated with something already held
    is REJECTED (binding `position_correlation`);
  * a submission whose symbols have NO ingested bars falls through to the
    three base D029 checks, audited, never blocked for a market-risk reason
    (fail-open).

Price series are chosen so the action/binding/relative-quantity outcome is
robust; no exact volatility or correlation decimal is asserted.
"""

import contextlib
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from apps.api.app.auth.permissions import Permission
from apps.api.app.auth.security import hash_password
from apps.api.app.db.base import get_session
from apps.api.app.db.models import (
    Broker,
    BrokerAccount,
    BrokerGrant,
    BrokerKind,
    BrokerPosition,
    MarketDataBar,
    Role,
    User,
)
from apps.api.app.db.models import Fill as FillRow
from apps.api.app.db.models import Order as OrderRow
from apps.api.app.main import app
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.store import MarketDataStore
from apps.api.app.portfolio_manager.manager import decide
from apps.api.app.portfolio_manager.models import PortfolioLimits, PortfolioState
from apps.api.app.risk.models import Side, TradeProposal

TEST_PASSWORD = "correct-horse-battery-staple"

# A year of trailing weekdays, all comfortably inside the default 365-day
# lookback and far more than the 60-observation floor.
_BAR_START = date(2026, 1, 2)
_BAR_END = date(2026, 9, 4)


@contextlib.asynccontextmanager
async def db_session():
    gen = get_session()
    session = await anext(gen)
    try:
        yield session
    finally:
        await gen.aclose()


@contextlib.asynccontextmanager
async def api_client():
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@contextlib.asynccontextmanager
async def trading_user(session):
    user_id = uuid.uuid4()
    role_id = uuid.uuid4()
    email = f"{uuid.uuid4()}@example.com"
    session.add(
        Role(
            id=role_id,
            name=f"test-role-{role_id}",
            permissions=[Permission.SUBMIT_PAPER_TRADE.value],
        )
    )
    session.add(
        User(
            id=user_id,
            email=email,
            hashed_password=hash_password(TEST_PASSWORD),
            is_active=True,
            role_id=role_id,
        )
    )
    await session.commit()
    try:
        yield user_id, email
    finally:
        await session.execute(delete(BrokerGrant).where(BrokerGrant.user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.execute(delete(Role).where(Role.id == role_id))
        await session.commit()


@contextlib.asynccontextmanager
async def paper_broker_row(session):
    broker_id = uuid.uuid4()
    session.add(Broker(id=broker_id, name="Test Broker", kind=BrokerKind.PAPER, provider="paper"))
    await session.commit()
    try:
        yield broker_id
    finally:
        await session.execute(
            delete(FillRow).where(
                FillRow.order_id.in_(select(OrderRow.id).where(OrderRow.broker_id == broker_id))
            )
        )
        await session.execute(delete(OrderRow).where(OrderRow.broker_id == broker_id))
        await session.execute(delete(BrokerGrant).where(BrokerGrant.broker_id == broker_id))
        await session.execute(delete(BrokerPosition).where(BrokerPosition.broker_id == broker_id))
        await session.execute(delete(BrokerAccount).where(BrokerAccount.broker_id == broker_id))
        await session.execute(delete(Broker).where(Broker.id == broker_id))
        await session.commit()


@contextlib.asynccontextmanager
async def broker_grant(session, *, user_id, broker_id):
    session.add(BrokerGrant(user_id=user_id, broker_id=broker_id))
    await session.commit()
    try:
        yield
    finally:
        await session.execute(
            delete(BrokerGrant).where(
                BrokerGrant.user_id == user_id, BrokerGrant.broker_id == broker_id
            )
        )
        await session.commit()


def _weekdays(start: date, end: date) -> list[date]:
    days, cursor = [], start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


async def _seed_series(session, symbol: str, closes: list[int]) -> None:
    """Persist one symbol's real daily bars: `closes[i % len(closes)]` over
    every trailing weekday in the window, written through MarketDataStore
    exactly as a backfill would."""
    days = _weekdays(_BAR_START, _BAR_END)
    bars = [
        Bar(
            symbol=symbol,
            bar_interval="1d",
            ts=datetime(day.year, day.month, day.day, 21, 0, tzinfo=UTC),
            close=Decimal(closes[index % len(closes)]),
            source="test-market-risk",
        )
        for index, day in enumerate(days)
    ]
    await MarketDataStore(session).upsert_bars(bars)
    await session.commit()


async def _seed_holding(
    session, broker_id, *, cash: Decimal, positions: dict[str, Decimal]
) -> None:
    session.add(BrokerAccount(broker_id=broker_id, cash=cash))
    for symbol, quantity in positions.items():
        session.add(BrokerPosition(broker_id=broker_id, symbol=symbol, quantity=quantity))
    await session.commit()


@contextlib.asynccontextmanager
async def seeded_symbols(session, *symbols: str):
    try:
        yield symbols
    finally:
        await session.execute(delete(MarketDataBar).where(MarketDataBar.symbol.in_(symbols)))
        await session.commit()


async def _token(client: AsyncClient, email: str) -> str:
    response = await client.post(
        "/auth/login", data={"username": email, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


async def _trade(client, broker_id, token, **body):
    return await client.post(
        f"/brokers/{broker_id}/trades",
        headers={"Authorization": f"Bearer {token}"},
        json={"side": "buy", **body},
    )


@pytest.mark.asyncio
async def test_a_buy_that_lifts_projected_book_volatility_is_sized_down():
    """Hold a low-volatility name plus a small slice of a very
    high-volatility one; a full-size BUY of the volatile name would take
    projected book volatility well over the 40% ceiling, so it is sized
    DOWN rather than filled in full."""
    low = f"MRL{uuid.uuid4().hex[:5].upper()}"
    high = f"MRH{uuid.uuid4().hex[:5].upper()}"
    async with (
        db_session() as session,
        trading_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
        seeded_symbols(session, low, high),
    ):
        await _seed_series(session, low, [100, 101])
        await _seed_series(session, high, [100, 140, 140, 100])
        # Equity 100k: 20k in the calm name, 5k in the volatile one, 75k cash.
        await _seed_holding(
            session,
            broker_id,
            cash=Decimal(75_000),
            positions={low: Decimal(200), high: Decimal(50)},
        )

        async with api_client() as client:
            token = await _token(client, email)
            response = await _trade(
                client,
                broker_id,
                token,
                symbol=high,
                quantity="100",
                estimated_price="100",
                stop_price="95",
                marks={low: "100"},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["approved"] is True  # the risk engine passed it
        assert body["portfolio_action"] == "modify"
        assert body["portfolio_binding_constraint"] == "portfolio_volatility"
        assert body["portfolio_requested_quantity"] == "100"
        approved = Decimal(body["fill_quantity"])
        assert Decimal(0) < approved < Decimal(100)


@pytest.mark.asyncio
async def test_opening_a_position_correlated_with_a_holding_is_rejected():
    """Two symbols whose daily moves are identical up to a scale factor
    (correlation 1.0). Hold one; opening the other - a NEW position - blows
    past the 0.80 correlation ceiling, and correlation does not depend on
    size, so the trade is REJECTED, not resized."""
    base = f"MCA{uuid.uuid4().hex[:5].upper()}"
    twin = f"MCB{uuid.uuid4().hex[:5].upper()}"
    async with (
        db_session() as session,
        trading_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
        seeded_symbols(session, base, twin),
    ):
        await _seed_series(session, base, [100, 101])
        await _seed_series(session, twin, [200, 202])  # 2x base, identical returns
        await _seed_holding(
            session, broker_id, cash=Decimal(90_000), positions={base: Decimal(100)}
        )

        async with api_client() as client:
            token = await _token(client, email)
            response = await _trade(
                client,
                broker_id,
                token,
                symbol=twin,
                quantity="50",
                estimated_price="200",
                stop_price="195",
                marks={base: "100"},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "rejected"
        assert body["approved"] is True  # risk passed; the portfolio didn't
        assert body["block_reason"] is None
        assert body["portfolio_action"] == "reject"
        assert body["portfolio_binding_constraint"] == "position_correlation"
        assert body["fill_quantity"] is None

        positions = (
            await session.execute(
                select(BrokerPosition).where(BrokerPosition.broker_id == broker_id)
            )
        ).scalars().all()
        assert {p.symbol for p in positions} == {base}


@pytest.mark.asyncio
async def test_thin_bar_history_fails_open_and_is_not_a_market_risk_block():
    """A held symbol and a proposed symbol both with NO ingested bars: the
    two Phase-62 checks skip (audited, non-binding) and the trade is decided
    on the three base D029 checks exactly as before Phase 62."""
    held = f"TNA{uuid.uuid4().hex[:5].upper()}"
    fresh = f"TNB{uuid.uuid4().hex[:5].upper()}"
    async with (
        db_session() as session,
        trading_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        await _seed_holding(
            session, broker_id, cash=Decimal(90_000), positions={held: Decimal(100)}
        )

        async with api_client() as client:
            token = await _token(client, email)
            response = await _trade(
                client,
                broker_id,
                token,
                symbol=fresh,
                quantity="10",
                estimated_price="100",
                stop_price="95",
                marks={held: "100"},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["portfolio_binding_constraint"] not in (
            "portfolio_volatility",
            "position_correlation",
        )
        assert body["portfolio_action"] in ("approve", "modify", "reject")
        # With everything else uncontested this is a clean fill.
        assert body["portfolio_action"] == "approve"
        assert body["status"] == "filled"
        assert body["fill_quantity"] == "10"


def test_decide_without_market_risk_keeps_exactly_the_three_pre_phase_62_checks():
    """The backtest path: every backtest engine calls decide() with
    market_risk=None, and must keep producing exactly the pre-Phase-62
    audit record - the three D029 checks, no market-risk checks, byte for
    byte the same outcome. The full backtesting suite proves results are
    unchanged; this pins the check list itself."""
    proposal = TradeProposal(
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal(10),
        estimated_price=Decimal(100),
        stop_price=Decimal(95),
        market_data_as_of=datetime(2026, 9, 4, 15, 0, tzinfo=UTC),
    )
    portfolio = PortfolioState(cash=Decimal(100_000), holdings=[])
    limits = PortfolioLimits(
        max_symbol_pct_of_equity=Decimal("0.25"),
        min_cash_reserve_pct_of_equity=Decimal("0.05"),
        max_open_positions=20,
        # Even with both ceilings configured, no MarketRiskInputs means
        # neither check runs.
        max_portfolio_volatility_pct=Decimal("0.40"),
        max_position_correlation=Decimal("0.80"),
    )

    decision = decide(proposal, portfolio, limits, market_risk=None)

    assert len(decision.checks) == 3
    assert {c.constraint.value for c in decision.checks} == {
        "symbol_concentration",
        "cash_reserve",
        "max_open_positions",
    }
