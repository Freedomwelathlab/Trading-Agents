"""Options paper bot (Phase 102, D122): the human gate over HTTP, the input
guardrails, and full engine cycles against the real database with a
deterministic chain stub and an injected clock."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from apps.api.app.core.config import get_settings
from apps.api.app.db.base import get_session_factory
from apps.api.app.db.models import (
    BrokerAccount,
    BrokerKind,
    OptionOrder,
    OptionsBot,
    OptionsBotRun,
    OptionsBotRunStatus,
    OptionsBotStatus,
    OptionsBotTrade,
    OptionStructure,
)
from apps.api.app.options.pricing import OptionRight
from apps.api.app.options_bot.runner import run_options_bot_cycle_all
from apps.api.app.safety.emergency_stop import set_emergency_stop
from tests.api.test_admin import _get_token, api_client, db_session
from tests.api.test_deployments import _h, deploy_only_user
from tests.api.test_option_orders import granted_broker, options_user
from tests.options.chain_fixtures import FakeProvider, make_chain

D = Decimal
MONDAY_11_ET = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)
EXPIRY = date(2026, 10, 12)  # 7 DTE from the Monday


def body(broker_id, **over):
    base = dict(
        name="TQQQ put spreads", broker_id=str(broker_id), underlying="tqqq.us",
        structure_type="bull_put", target_delta="0.30", dte_min=3, dte_max=14,
        spread_width="2", profit_target_pct="50", stop_pct="100",
        max_concurrent_positions=2, capital_per_trade="500",
    )
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_lifecycle_needs_the_separate_approval_permission():
    async with db_session() as session, deploy_only_user(session) as (uid, email), \
            granted_broker(session, uid) as broker_id, api_client() as client:
        token = await _get_token(client, email)
        r = await client.post("/options-bots", json=body(broker_id), headers=_h(token))
        assert r.status_code == 201, r.text
        bot = r.json()
        assert bot["status"] == "pending_approval" and bot["underlying"] == "TQQQ.US"
        r = await client.post(f"/options-bots/{bot['id']}/approve", headers=_h(token))
        assert r.status_code == 403
        r = await client.get("/options-bots/structures", headers=_h(token))
        assert "iron_condor" in r.json()["structures"]
        assert "cash_secured_put" not in r.json()["structures"]


@pytest.mark.asyncio
async def test_full_lifecycle_and_guardrails():
    async with db_session() as session, options_user(session) as (uid, email), \
            granted_broker(session, uid) as broker_id, api_client() as client:
        token = await _get_token(client, email)
        bot = (await client.post("/options-bots", json=body(broker_id),
                                 headers=_h(token))).json()
        bid = bot["id"]
        r = await client.post(f"/options-bots/{bid}/approve", headers=_h(token))
        assert r.json()["status"] == "active"
        r = await client.post(f"/options-bots/{bid}/pause", json={"reason": "earnings"},
                              headers=_h(token))
        assert r.json()["status"] == "paused" and r.json()["paused_reason"] == "earnings"
        r = await client.post(f"/options-bots/{bid}/resume", headers=_h(token))
        assert r.json()["status"] == "active"
        r = await client.post(f"/options-bots/{bid}/resume", headers=_h(token))
        assert r.status_code == 409
        r = await client.post(f"/options-bots/{bid}/stop", headers=_h(token))
        assert r.json()["status"] == "stopped"
        # A stopped bot's "run now" records why it did nothing.
        r = await client.post(f"/options-bots/{bid}/run", headers=_h(token))
        assert r.status_code == 200 and r.json()["run_status"] == "skipped_not_active"

        async def post(**over):
            return await client.post("/options-bots", json=body(broker_id, **over),
                                     headers=_h(token))

        r = await post(structure_type="bull_call", spread_width=None)
        assert r.status_code == 400 and "WIDTH_REQUIRED" in r.json()["detail"]
        r = await post(structure_type="long_call", spread_width="2")
        assert "BAD_WIDTH" in r.json()["detail"]
        r = await post(structure_type="cash_secured_put", spread_width=None)
        assert "BAD_STRUCTURE" in r.json()["detail"]
        r = await post(profit_target_pct="100")
        assert "BAD_PERCENT" in r.json()["detail"]
        r = await post(dte_min=10, dte_max=3)
        assert "BAD_DTE_BAND" in r.json()["detail"]
        r = await post(target_delta="1.2")
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_a_bot_cannot_be_created_on_a_live_broker():
    async with db_session() as session, options_user(session) as (uid, email), \
            granted_broker(session, uid, kind=BrokerKind.LIVE) as broker_id, \
            api_client() as client:
        token = await _get_token(client, email)
        r = await client.post("/options-bots", json=body(broker_id), headers=_h(token))
        assert r.status_code == 400 and "LIVE_NOT_SUPPORTED" in r.json()["detail"]


async def _make_active_bot(session, broker_id, uid, **over) -> uuid.UUID:
    spec = dict(
        name="bot", owner_user_id=uid, broker_id=broker_id,
        status=OptionsBotStatus.ACTIVE, underlying="TQQQ.US", structure_type="bull_put",
        target_delta=D("0.30"), dte_min=3, dte_max=14, spread_width=D(2),
        profit_target_pct=D(50), stop_pct=D(100), max_concurrent_positions=2,
        capital_per_trade=D(500), requested_by_user_id=uid,
    )
    spec.update(over)
    bot = OptionsBot(id=uuid.uuid4(), **spec)
    session.add(bot)
    await session.commit()
    return bot.id


async def _stop_off(session):
    """These tests assert what the bot does with the stop OFF. The stop's
    state is a global audit table other tests flip, so state it explicitly
    (an appended "off" row, exactly as those tests' own teardowns do)."""
    await set_emergency_stop(session, active=False, reason="options bot test", actor_user_id=None)
    await session.commit()


async def _cycle(bot_id, clock_at, provider):
    return await run_options_bot_cycle_all(
        get_session_factory(), settings=get_settings(), provider=provider,
        clock=lambda: clock_at, only_bot_id=bot_id,
    )


@pytest.mark.asyncio
async def test_engine_opens_by_delta_manages_to_target_and_learns():
    start = get_settings().paper_broker_starting_cash
    chain = make_chain(EXPIRY, MONDAY_11_ET - timedelta(seconds=60))
    stub = FakeProvider(chain, expiries=[date(2026, 10, 6), EXPIRY, date(2026, 11, 20)])
    async with db_session() as session, options_user(session) as (uid, _email), \
            granted_broker(session, uid) as broker_id:
        bot_id = await _make_active_bot(session, broker_id, uid)
        await _stop_off(session)

        # Cycle 1: opens a 46/44 bull put (the 0.30-delta put), 2 contracts.
        result = await _cycle(bot_id, MONDAY_11_ET, stub)
        [out] = result.outcomes
        assert out.status is OptionsBotRunStatus.SUCCEEDED, out.detail
        assert out.trades_opened == 1
        await session.rollback()
        [trade] = (await session.execute(
            select(OptionsBotTrade).where(OptionsBotTrade.bot_id == bot_id))).scalars().all()
        trade_id = trade.id
        assert trade.structure_type == "bull_put" and trade.quantity == 2
        assert trade.expiry == EXPIRY and trade.entry_net_price == D("-0.18")
        assert abs(trade.entry_delta) == D("0.3")
        structure = await session.get(OptionStructure, trade.structure_id)
        assert [leg["strike"] for leg in structure.legs] == ["46", "44"]
        assert structure.capital_reserved == D("364")
        account = await session.get(BrokerAccount, broker_id, populate_existing=True)
        assert account.cash == start - D("364")

        # Cycle 2, same day, market unchanged: no second entry, no exit.
        later = MONDAY_11_ET + timedelta(minutes=5)
        stub.chain = make_chain(EXPIRY, later - timedelta(seconds=60))
        [out] = (await _cycle(bot_id, later, stub)).outcomes
        assert out.trades_opened == 0 and out.trades_closed == 0
        assert "one new structure per session day" in (out.detail or "")

        # Cycle 3: the puts have decayed - closing now captures > 50% of the credit.
        cheap = {
            (OptionRight.PUT, D(46)): {"bid": D("0.59"), "ask": D("0.61")},
            (OptionRight.PUT, D(44)): {"bid": D("0.54"), "ask": D("0.56")},
        }
        later = MONDAY_11_ET + timedelta(minutes=10)
        stub.chain = make_chain(EXPIRY, later - timedelta(seconds=60), overrides=cheap)
        [out] = (await _cycle(bot_id, later, stub)).outcomes
        assert out.trades_closed == 1, out.detail
        await session.rollback()
        trade = await session.get(OptionsBotTrade, trade_id, populate_existing=True)
        # buy back 46P at 0.61, sell 44P at 0.54: exit -0.07; (0.18 - 0.07) x 200 = 22
        assert trade.exit_reason == "take_profit"
        assert trade.realized_pnl == D("22.00")
        assert trade.return_on_risk == (D(22) / D(364)).quantize(D("0.000001"))
        assert trade.peak_pnl_pct is not None and trade.peak_pnl_pct >= D(50)
        account = await session.get(BrokerAccount, broker_id, populate_existing=True)
        assert account.cash == start + D(22)

        orders = (await session.execute(
            select(OptionOrder).where(OptionOrder.broker_id == broker_id))).scalars().all()
        assert sorted(o.action for o in orders) == ["close", "open"]
        assert all(o.options_bot_run_id is not None for o in orders)


@pytest.mark.asyncio
async def test_engine_refusals_each_leave_a_terminal_run_row():
    chain = make_chain(EXPIRY, MONDAY_11_ET - timedelta(seconds=60))
    async with db_session() as session, options_user(session) as (uid, _e), \
            granted_broker(session, uid) as broker_id, \
            granted_broker(session, uid, kind=BrokerKind.LIVE) as live_id:
        bot_id = await _make_active_bot(session, broker_id, uid)
        await _stop_off(session)

        [out] = (await _cycle(bot_id, MONDAY_11_ET, None)).outcomes
        assert out.status is OptionsBotRunStatus.SKIPPED_NOT_CONFIGURED

        sunday = datetime(2026, 10, 4, 15, 0, tzinfo=UTC)
        [out] = (await _cycle(bot_id, sunday, FakeProvider(chain))).outcomes
        assert out.status is OptionsBotRunStatus.SKIPPED_MARKET_CLOSED

        # A bot row pointed at a live broker (not creatable through the
        # route) is still refused by the engine before any quote is read.
        live_bot = await _make_active_bot(session, live_id, uid)
        [out] = (await _cycle(live_bot, MONDAY_11_ET, FakeProvider(chain))).outcomes
        assert out.status is OptionsBotRunStatus.SKIPPED_LIVE_NOT_SUPPORTED

        # No expiry inside the band: a succeeded run that says why it did nothing.
        far = FakeProvider(chain, expiries=[date(2027, 1, 15)])
        [out] = (await _cycle(bot_id, MONDAY_11_ET, far)).outcomes
        assert out.status is OptionsBotRunStatus.SUCCEEDED and out.trades_opened == 0
        assert "DTE" in (out.detail or "")

        # Capital below one structure's max loss: no entry, said why.
        poor = await _make_active_bot(session, broker_id, uid, capital_per_trade=D(100))
        [out] = (await _cycle(poor, MONDAY_11_ET, FakeProvider(chain))).outcomes
        assert out.trades_opened == 0 and "below one structure's max loss" in out.detail

        await session.rollback()
        runs = (await session.execute(
            select(OptionsBotRun).where(OptionsBotRun.bot_id.in_([bot_id, live_bot, poor]))
        )).scalars().all()
        assert len(runs) == 5 and all(r.completed_at is not None for r in runs)
        assert all(r.status is not OptionsBotRunStatus.FAILED for r in runs)


@pytest.mark.asyncio
async def test_a_signal_bot_and_its_scan_dashboard():
    """Phase 107 (D134): structure `signal` lets the underlying's scan pick
    a bull put or bear call spread; the scan endpoint shows the board and
    the spread it points to (or why there is none)."""
    async with (
        db_session() as session,
        options_user(session) as (uid, email),
        granted_broker(session, uid) as broker_id,
        api_client() as client,
    ):
        token = await _get_token(client, email)
        r = await client.post(
            "/options-bots", json=body(broker_id, structure_type="signal", min_signal_score=6),
            headers=_h(token),
        )
        assert r.status_code == 201, r.text
        bot = r.json()
        assert bot["structure_type"] == "signal" and bot["min_signal_score"] == 6
        r = await client.post(
            "/options-bots", json=body(broker_id, structure_type="signal", min_signal_score=11),
            headers=_h(token),
        )
        assert r.status_code == 422
        r = await client.get(f"/options-bots/{bot['id']}/scan", headers=_h(token))
        assert r.status_code == 200, r.text
        scan = r.json()
        assert scan["mode"] == "signal" and scan["underlying"] == "TQQQ.US"
        assert scan["board"]["recommendation"] in ("BUY", "SELL", "WAIT")
        assert scan["proposal_note"]
        if scan["board"]["recommendation"] == "WAIT":
            assert scan["structure_type"] is None and scan["proposal"] is None
        else:
            assert scan["structure_type"] in ("bull_put", "bear_call")
