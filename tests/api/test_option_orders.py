"""Paper option routing (Phase 102, D122) against the real database and the
real app: the open -> mark -> close round trip, settlement at expiry, and
every refusal - live broker, naked short, no two-sided quote, stale quote,
per-trade risk.

The chain provider is swapped on `app.state` for a deterministic stub
(tests/options/chain_fixtures.py) - never a network call, never real
market data presented as such.
"""

import contextlib
import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select

from apps.api.app.auth.permissions import Permission
from apps.api.app.core.config import get_settings
from apps.api.app.db.models import (
    Broker,
    BrokerAccount,
    BrokerGrant,
    BrokerKind,
    BrokerPosition,
    MarketDataBar,
    OptionFill,
    OptionOrder,
    OptionsBot,
    OptionsBotRun,
    OptionsBotTrade,
    OptionStructure,
)
from apps.api.app.main import app
from apps.api.app.options.paper_book import (
    OpenOrderRequest,
    settle_expired,
    submit_open,
)
from apps.api.app.options.paper_orders import LegSide, LegSpec, OptionStructureType
from apps.api.app.options.pricing import OptionRight
from tests.api.test_admin import _get_token, api_client, db_session
from tests.api.test_deployments import _h, _role_user
from tests.options.chain_fixtures import FakeProvider, make_chain

NY = ZoneInfo("America/New_York")
D = Decimal


def ny_today():
    return datetime.now(NY).date()


@contextlib.asynccontextmanager
async def options_user(session):
    async with _role_user(
        session,
        permissions=[
            Permission.SUBMIT_PAPER_TRADE.value,
            Permission.VIEW_PORTFOLIO.value,
            Permission.STRATEGY_MANAGE.value,
            Permission.STRATEGY_DEPLOY.value,
            Permission.STRATEGY_APPROVE_DEPLOYMENT.value,
        ],
        label="options",
    ) as pair:
        yield pair


async def purge_broker(session, broker_id):
    await session.rollback()
    bot_ids = (
        await session.execute(select(OptionsBot.id).where(OptionsBot.broker_id == broker_id))
    ).scalars().all()
    for bid in bot_ids:
        await session.execute(delete(OptionsBotTrade).where(OptionsBotTrade.bot_id == bid))
    order_ids = select(OptionOrder.id).where(OptionOrder.broker_id == broker_id)
    await session.execute(delete(OptionFill).where(OptionFill.order_id.in_(order_ids)))
    await session.execute(delete(OptionOrder).where(OptionOrder.broker_id == broker_id))
    await session.execute(delete(OptionStructure).where(OptionStructure.broker_id == broker_id))
    for bid in bot_ids:
        await session.execute(delete(OptionsBotRun).where(OptionsBotRun.bot_id == bid))
    await session.execute(delete(OptionsBot).where(OptionsBot.broker_id == broker_id))
    await session.execute(delete(BrokerPosition).where(BrokerPosition.broker_id == broker_id))
    await session.execute(delete(BrokerAccount).where(BrokerAccount.broker_id == broker_id))
    await session.execute(delete(BrokerGrant).where(BrokerGrant.broker_id == broker_id))
    await session.execute(delete(Broker).where(Broker.id == broker_id))
    await session.commit()


@contextlib.asynccontextmanager
async def granted_broker(session, user_id, *, kind=BrokerKind.PAPER):
    broker_id = uuid.uuid4()
    session.add(Broker(id=broker_id, name="opt-pb", kind=kind, provider="paper"))
    session.add(BrokerGrant(id=uuid.uuid4(), user_id=user_id, broker_id=broker_id))
    await session.commit()
    try:
        yield broker_id
    finally:
        await purge_broker(session, broker_id)


@contextlib.asynccontextmanager
async def provider(value):
    previous = getattr(app.state, "option_chain_provider", None)
    app.state.option_chain_provider = value
    try:
        yield
    finally:
        app.state.option_chain_provider = previous


def fresh_chain(**kw):
    expiry = ny_today() + timedelta(days=10)
    return make_chain(expiry, datetime.now(UTC) - timedelta(seconds=30), **kw)


def bull_call(expiry, quantity=2):
    return {
        "action": "open", "underlying": "tqqq.us", "expiry": expiry.isoformat(),
        "structure": "bull_call", "quantity": quantity,
        "legs": [
            {"right": "call", "strike": "50", "side": "buy"},
            {"right": "call", "strike": "52", "side": "sell"},
        ],
    }


async def _cash(session, broker_id):
    await session.rollback()
    row = await session.get(BrokerAccount, broker_id, populate_existing=True)
    return row.cash if row else None


@pytest.mark.asyncio
async def test_open_mark_close_round_trip_moves_cash_by_exactly_the_pnl():
    start = get_settings().paper_broker_starting_cash
    chain = fresh_chain()
    async with db_session() as session, options_user(session) as (uid, email), \
            granted_broker(session, uid) as broker_id, api_client() as client, \
            provider(FakeProvider(chain)):
        token = await _get_token(client, email)
        r = await client.post(f"/brokers/{broker_id}/option-orders",
                              json=bull_call(chain.expiry), headers=_h(token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "filled" and body["approved"] is True
        assert D(body["net_price"]) == D("0.22")
        assert D(body["max_loss"]) == D("44") and D(body["cash_change"]) == D("-44")
        assert body["quote_source"] == "cboe-delayed" and body["quote_delayed"] is True
        assert "MODELLED" in body["pricing_basis"]
        assert [D(f["fill_price"]) for f in body["fills"]] == [D("2.01"), D("1.79")]
        assert all(f["quote_source"] == "cboe-delayed" for f in body["fills"])
        assert await _cash(session, broker_id) == start - D(44)

        r = await client.get(f"/brokers/{broker_id}/option-positions", headers=_h(token))
        assert r.status_code == 200, r.text
        book = r.json()
        assert D(book["capital_reserved"]) == D("44")
        assert D(book["equity_at_cost"]) == start
        [s] = book["structures"]
        assert D(s["mid_value"]) == D("0.20")  # 2.00 - 1.80
        assert D(s["unrealized_pnl_mid"]) == D("-4.00")
        assert s["mark_delayed"] is True
        positions = {p["contract_symbol"]: p["quantity"] for p in book["positions"]}
        assert sorted(positions.values()) == [-2, 2]
        assert all(sym.startswith("TQQQ") for sym in positions)

        # The market moves: the 50 call is now worth 3.00, the 52 call 2.00.
        moved = make_chain(chain.expiry, datetime.now(UTC) - timedelta(seconds=30), spot=D("51"))
        async with provider(FakeProvider(moved)):
            r = await client.post(
                f"/brokers/{broker_id}/option-orders",
                json={"action": "close", "structure_id": s["id"]}, headers=_h(token),
            )
        assert r.status_code == 200, r.text
        closed = r.json()
        assert closed["status"] == "filled" and closed["action"] == "close"
        # sell 50C: mid 2.9 (1 + 1.9) -> 2.89; buy 52C: mid 1.9 -> 1.91; exit 0.98
        assert D(closed["net_price"]) == D("-0.98")
        assert D(closed["realized_pnl"]) == D("152.00")  # (0.98 - 0.22) x 100 x 2
        assert await _cash(session, broker_id) == start + D("152")

        r = await client.get(f"/brokers/{broker_id}/option-orders", headers=_h(token))
        assert [o["action"] for o in r.json()["orders"]] == ["close", "open"]


@pytest.mark.asyncio
async def test_refusals_write_nothing_before_the_risk_engine_and_a_row_after():
    start = get_settings().paper_broker_starting_cash
    async with db_session() as session, options_user(session) as (uid, email), \
            granted_broker(session, uid) as broker_id, api_client() as client:
        token = await _get_token(client, email)
        url = f"/brokers/{broker_id}/option-orders"
        chain = fresh_chain()

        # No two-sided market on a leg: DATA_UNAVAILABLE, nothing recorded.
        async with provider(FakeProvider(fresh_chain(
                overrides={(OptionRight.CALL, D(52)): {"bid": None}}))):
            r = await client.post(url, json=bull_call(chain.expiry), headers=_h(token))
        assert r.status_code == 409 and r.json()["detail"].startswith("DATA_UNAVAILABLE")

        # A naked short call, whatever it is called.
        async with provider(FakeProvider(chain)):
            naked = bull_call(chain.expiry)
            naked["legs"] = [{"right": "call", "strike": "55", "side": "sell"}]
            naked["structure"] = "long_call"
            r = await client.post(url, json=naked, headers=_h(token))
        assert r.status_code == 400 and r.json()["detail"].startswith("NAKED_SHORT_REFUSED")

        # No chain provider at all.
        async with provider(None):
            r = await client.post(url, json=bull_call(chain.expiry), headers=_h(token))
        assert r.status_code == 503 and r.json()["detail"].startswith("NOT_CONFIGURED")

        rows = (await session.execute(
            select(OptionOrder).where(OptionOrder.broker_id == broker_id))).scalars().all()
        assert rows == []

        # Stale quote: the Risk Engine refuses, and that decision IS recorded.
        stale = make_chain(chain.expiry, datetime.now(UTC) - timedelta(hours=2))
        async with provider(FakeProvider(stale)):
            r = await client.post(url, json=bull_call(chain.expiry), headers=_h(token))
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"
        assert r.json()["block_reason"] == "market_data_stale"

        # Per-trade risk: 50 x 22 = 1,100 against 1% of 100,000.
        async with provider(FakeProvider(chain)):
            r = await client.post(url, json=bull_call(chain.expiry, quantity=50),
                                  headers=_h(token))
        assert r.json()["status"] == "rejected"
        assert r.json()["block_reason"] == "exceeds_per_trade_risk"
        assert r.json()["fills"] == []

        await session.rollback()
        rows = (await session.execute(
            select(OptionOrder).where(OptionOrder.broker_id == broker_id))).scalars().all()
        assert sorted(r.status.value for r in rows) == ["rejected", "rejected"]
        structures = (await session.execute(
            select(OptionStructure).where(OptionStructure.broker_id == broker_id)
        )).scalars().all()
        assert structures == []
        assert await _cash(session, broker_id) == start


@pytest.mark.asyncio
async def test_a_live_broker_is_refused_before_anything_is_priced():
    async with db_session() as session, options_user(session) as (uid, email), \
            granted_broker(session, uid, kind=BrokerKind.LIVE) as broker_id, \
            api_client() as client, provider(FakeProvider(fresh_chain())):
        token = await _get_token(client, email)
        r = await client.post(f"/brokers/{broker_id}/option-orders",
                              json=bull_call(ny_today() + timedelta(days=10)),
                              headers=_h(token))
        assert r.status_code == 400
        assert r.json()["detail"].startswith("LIVE_NOT_SUPPORTED")
        r = await client.post(f"/brokers/{broker_id}/option-positions/settle",
                              headers=_h(token))
        assert r.status_code == 400
        rows = (await session.execute(
            select(OptionOrder).where(OptionOrder.broker_id == broker_id))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_a_user_without_a_grant_cannot_route_or_read():
    async with db_session() as session, options_user(session) as (uid, email), \
            options_user(session) as (_other, other_email), \
            granted_broker(session, uid) as broker_id, api_client() as client:
        other = await _get_token(client, other_email)
        r = await client.get(f"/brokers/{broker_id}/option-positions", headers=_h(other))
        assert r.status_code == 403
        r = await client.post(f"/brokers/{broker_id}/option-orders",
                              json=bull_call(ny_today()), headers=_h(other))
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_expiry_settles_at_intrinsic_from_the_stored_close_and_waits_without_one():
    settings = get_settings()
    start = settings.paper_broker_starting_cash
    underlying = f"ZZ{uuid.uuid4().hex[:6].upper()}.US"
    t0 = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)
    expiry = t0.date() + timedelta(days=4)  # Friday 2026-10-09
    chain = make_chain(expiry, t0 - timedelta(seconds=60), underlying=underlying)
    async with db_session() as session, options_user(session) as (uid, _email), \
            granted_broker(session, uid) as broker_id:
        broker = await session.get(Broker, broker_id)
        out = await submit_open(
            session, broker=broker,
            request=OpenOrderRequest(
                underlying=underlying, expiry=expiry,
                structure_type=OptionStructureType.BULL_CALL,
                legs=(LegSpec(OptionRight.CALL, D(50), LegSide.BUY),
                      LegSpec(OptionRight.CALL, D(52), LegSide.SELL)),
                quantity=3,
            ),
            provider=FakeProvider(chain), settings=settings, now=t0,
            submitted_by_user_id=None, chain=chain,
        )
        assert out.filled
        await session.commit()

        # Expiry day, before the session is final: nothing happens.
        noon = datetime.combine(expiry, time(12, 0), tzinfo=NY)
        assert await settle_expired(session, broker_id=broker_id, settings=settings,
                                    now=noon) == []

        # After expiry with no stored close: pending, not guessed.
        after = datetime.combine(expiry + timedelta(days=1), time(10, 0), tzinfo=NY)
        [pending] = await settle_expired(session, broker_id=broker_id, settings=settings,
                                         now=after)
        assert not pending.settled and pending.detail.startswith("SETTLEMENT_PENDING")

        session.add(MarketDataBar(
            symbol=underlying, bar_interval="1d",
            ts=datetime.combine(expiry, time(9, 30), tzinfo=NY), open=D(54), high=D(56),
            low=D(53), close=D(55), volume=1, source="test-fixture",
        ))
        await session.commit()
        try:
            [done] = await settle_expired(session, broker_id=broker_id, settings=settings,
                                          now=after)
            await session.commit()
            assert done.settled
            s = done.structure
            # both calls ITM at 55: worth the full 2.00 width; paid 0.22
            assert s.exit_net_price == D(2)
            assert s.realized_pnl == D("534.00")  # (2 - 0.22) x 100 x 3
            assert s.close_reason == "expiry"
            assert await _cash(session, broker_id) == start + D("534")
        finally:
            await session.execute(delete(MarketDataBar).where(MarketDataBar.symbol == underlying))
            await session.commit()
