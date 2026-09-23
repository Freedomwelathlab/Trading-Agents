"""Autotrade Bot routes (Phase 81, D098): lifecycle over HTTP, the human
gate, ownership, and the input guardrails."""

import contextlib
import uuid

import pytest
from sqlalchemy import delete, select

from apps.api.app.auth.permissions import Permission
from apps.api.app.db.models import (
    AutotradeBot,
    AutotradeBotRun,
    AutotradeBotTrade,
    Broker,
    BrokerAccount,
    BrokerGrant,
    BrokerKind,
    BrokerPosition,
    Fill,
    Order,
)
from tests.api.test_admin import _get_token, api_client, db_session
from tests.api.test_deployments import _h, _role_user, deploy_only_user, deploy_user


@contextlib.asynccontextmanager
async def granted_paper_broker(session, user_id):
    broker_id = uuid.uuid4()
    session.add(Broker(id=broker_id, name="bot-pb", kind=BrokerKind.PAPER, provider="paper"))
    session.add(BrokerGrant(id=uuid.uuid4(), user_id=user_id, broker_id=broker_id))
    await session.commit()
    try:
        yield broker_id
    finally:
        await session.rollback()
        bot_ids = [
            b.id for b in (await session.execute(
                AutotradeBot.__table__.select().where(AutotradeBot.broker_id == broker_id)
            )).all()
        ]
        for bid in bot_ids:
            await session.execute(delete(AutotradeBotTrade).where(AutotradeBotTrade.bot_id == bid))
            await session.execute(delete(AutotradeBotRun).where(AutotradeBotRun.bot_id == bid))
        await session.execute(delete(AutotradeBot).where(AutotradeBot.broker_id == broker_id))
        # `run-now` runs the REAL engine against REAL market data, so
        # whether it places a paper order depends on whether a setup fired
        # at that moment. On a quiet tape nothing is written and this
        # teardown is a no-op; on a day one fires, an orders row references
        # the broker and deleting it hits orders_broker_id_fkey. The same
        # trap tests/deployments/conftest.py documents, and the reason this
        # test failed only intermittently.
        await session.execute(
            delete(Fill).where(
                Fill.order_id.in_(select(Order.id).where(Order.broker_id == broker_id))
            )
        )
        await session.execute(delete(Order).where(Order.broker_id == broker_id))
        await session.execute(delete(BrokerPosition).where(BrokerPosition.broker_id == broker_id))
        await session.execute(delete(BrokerAccount).where(BrokerAccount.broker_id == broker_id))
        await session.execute(delete(BrokerGrant).where(BrokerGrant.broker_id == broker_id))
        await session.execute(delete(Broker).where(Broker.id == broker_id))
        await session.commit()


def body(broker_id, **over):
    base = dict(
        name="TQQQ intraday", broker_id=str(broker_id), symbols=["tqqq.us", "QQQ.US"],
        market_type="regular", max_trades_per_session=2, max_trades_per_day=4,
        capital_per_trade="2500", strategy_mode="auto", stop_loss_mode="max",
        stop_loss_max_pct="1.5", trailing_stop_pct="1", take_profit_mode="auto",
        trailing_take_profit_pct="0.5", news_blackout_minutes=30,
    )
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_create_is_pending_until_the_separately_permissioned_approval():
    async with db_session() as session, deploy_only_user(session) as (uid, email), \
            granted_paper_broker(session, uid) as broker_id, api_client() as client:
        token = await _get_token(client, email)
        r = await client.post("/autotrade/bots", json=body(broker_id), headers=_h(token))
        assert r.status_code == 201, r.text
        bot = r.json()
        assert bot["status"] == "pending_approval"
        assert bot["symbols"] == ["TQQQ.US", "QQQ.US"]  # normalised
        assert bot["strategy_mode"] == "auto" and len(bot["setups"]) >= 5  # every registered setup

        # deploy-only cannot approve.
        r = await client.post(f"/autotrade/bots/{bot['id']}/approve", headers=_h(token))
        assert r.status_code == 403

        r = await client.get("/autotrade/bots", headers=_h(token))
        assert [b["id"] for b in r.json()["bots"]] == [bot["id"]]


@pytest.mark.asyncio
async def test_full_lifecycle_and_run_now_writes_a_run_row():
    async with db_session() as session, deploy_user(session) as (uid, email), \
            granted_paper_broker(session, uid) as broker_id, api_client() as client:
        token = await _get_token(client, email)
        bot = (await client.post("/autotrade/bots", json=body(broker_id), headers=_h(token))).json()
        bid = bot["id"]

        r = await client.post(f"/autotrade/bots/{bid}/approve", headers=_h(token))
        assert r.status_code == 200 and r.json()["status"] == "active"
        r = await client.post(f"/autotrade/bots/{bid}/pause", json={"reason": "lunch"},
                              headers=_h(token))
        assert r.json()["status"] == "paused" and r.json()["paused_reason"] == "lunch"
        r = await client.post(f"/autotrade/bots/{bid}/resume", headers=_h(token))
        assert r.json()["status"] == "active"
        # Resuming an active bot is a 409, not a silent no-op.
        r = await client.post(f"/autotrade/bots/{bid}/resume", headers=_h(token))
        assert r.status_code == 409

        # "Run now" on this test host: the runner may or may not be wired
        # and the vendor may or may not be configured. Either way the
        # answer is a real status, never a fabricated trade.
        r = await client.post(f"/autotrade/bots/{bid}/run", headers=_h(token))
        assert r.status_code == 200, r.text
        assert r.json()["status"] in ("ran", "skipped_not_configured", "skipped_lock_held")

        r = await client.get(f"/autotrade/bots/{bid}/runs", headers=_h(token))
        assert r.status_code == 200
        # Assert the INVARIANTS, not emptiness. `run-now` above ran the
        # real engine against the real tape, so whether a setup fired is a
        # property of the market at this minute and not of this code — an
        # assertion of `trades == []` passes on a quiet afternoon and fails
        # the day `order_block_fvg` triggers on QQQ, which is what it did.
        # What must hold either way: every trade belongs to this bot's
        # symbols, a trade with no exit is not counted as closed, and the
        # two endpoints agree with each other.
        r = await client.get(f"/autotrade/bots/{bid}/trades", headers=_h(token))
        assert r.status_code == 200
        trades = r.json()["trades"]
        assert all(t["symbol"] in ("TQQQ.US", "QQQ.US") for t in trades)
        assert all(t["closed_at"] is None or t["exit_price"] is not None for t in trades)
        closed = [t for t in trades if t["closed_at"] is not None]

        r = await client.get(f"/autotrade/bots/{bid}/stats", headers=_h(token))
        assert r.status_code == 200
        assert r.json()["closed_trades"] == len(closed)

        r = await client.post(f"/autotrade/bots/{bid}/stop", headers=_h(token))
        assert r.json()["status"] == "stopped"
        r = await client.post(f"/autotrade/bots/{bid}/stop", headers=_h(token))
        assert r.status_code == 409


@pytest.mark.asyncio
async def test_guardrails_are_specific_4xx():
    async with db_session() as session, deploy_user(session) as (uid, email), \
            granted_paper_broker(session, uid) as broker_id, api_client() as client:
        token = await _get_token(client, email)

        async def post(**over):
            return await client.post("/autotrade/bots", json=body(broker_id, **over),
                                     headers=_h(token))

        r = await post(max_trades_per_session=5, max_trades_per_day=2)
        assert r.status_code == 400 and "BAD_LIMITS" in r.json()["detail"]
        r = await post(stop_loss_mode="max", stop_loss_max_pct=None)
        assert r.status_code == 400 and "STOP_MAX_REQUIRED" in r.json()["detail"]
        r = await post(strategy_mode="single", setups=["sweep_mss", "ema_reversal"])
        assert r.status_code == 400 and "SINGLE_MEANS_ONE" in r.json()["detail"]
        r = await post(strategy_mode="multi", setups=["not_a_setup"])
        assert r.status_code == 400 and "UNKNOWN_SETUP" in r.json()["detail"]
        r = await post(symbols=[])
        assert r.status_code == 400 and "NO_SYMBOLS" in r.json()["detail"]
        r = await client.post(
            "/autotrade/bots", json=body(uuid.uuid4()), headers=_h(token)
        )
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_a_broker_without_a_grant_is_refused_and_other_users_bots_are_hidden():
    async with db_session() as session, deploy_user(session) as (uid, email), \
            granted_paper_broker(session, uid) as broker_id, \
            _role_user(session, permissions=[Permission.STRATEGY_DEPLOY.value],
                       label="other") as (_oid, other_email), api_client() as client:
        token = await _get_token(client, email)
        other = await _get_token(client, other_email)

        # The other user holds no grant on this broker.
        r = await client.post("/autotrade/bots", json=body(broker_id), headers=_h(other))
        assert r.status_code == 403 and "NO_BROKER_GRANT" in r.json()["detail"]

        bot = (await client.post("/autotrade/bots", json=body(broker_id), headers=_h(token))).json()
        r = await client.get(f"/autotrade/bots/{bot['id']}", headers=_h(other))
        assert r.status_code == 403
        r = await client.get("/autotrade/bots", headers=_h(other))
        assert r.json()["bots"] == []
        r = await client.get(f"/autotrade/bots/{uuid.uuid4()}", headers=_h(token))
        assert r.status_code == 404
