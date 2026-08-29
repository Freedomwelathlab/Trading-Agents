"""Integration tests for the persisted, live-flippable emergency stop
(Phase 33 / docs/DECISIONS.md D039), against a real Postgres instance.

The point of every test here is that the kill switch's authority moved
from `Settings` (an `.env` value that needs a restart) to a persisted
`emergency_stop_events` row that an admin flips over HTTP - so the tests
that matter most are the ones that flip it through the real API and then
observe a real trade submission being rejected, with
`Settings.emergency_stop_active` left FALSE throughout.

Every test empties `emergency_stop_events` on teardown: the table's state
is global to the database, and a leftover row would change the behavior of
unrelated trade tests (including test_trades.py's original
settings-fallback test, which is deliberately left in place to prove the
fallback still works).
"""

import contextlib
import uuid

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
    EmergencyStopEvent,
    Role,
    User,
)
from apps.api.app.db.models import Fill as FillRow
from apps.api.app.db.models import Order as OrderRow
from apps.api.app.main import app
from apps.api.app.safety.emergency_stop import get_emergency_stop_state

TEST_PASSWORD = "correct-horse-battery-staple"

TRADE_BODY = {
    "symbol": "AAPL",
    "side": "buy",
    "quantity": "10",
    "estimated_price": "100",
    "stop_price": "95",
}


@contextlib.asynccontextmanager
async def db_session():
    gen = get_session()
    session = await anext(gen)
    try:
        yield session
    finally:
        await gen.aclose()


@contextlib.asynccontextmanager
async def clean_emergency_stop_table(session):
    """The switch's state is global, so both entry and exit clear it - a
    test must neither inherit nor leak a flip."""
    await session.execute(delete(EmergencyStopEvent))
    await session.commit()
    try:
        yield
    finally:
        await session.rollback()
        await session.execute(delete(EmergencyStopEvent))
        await session.commit()


@contextlib.asynccontextmanager
async def user_with(session, *, permissions: tuple[str, ...] | None):
    """`permissions=None` yields a user with no role at all."""
    user_id = uuid.uuid4()
    email = f"{uuid.uuid4()}@example.com"
    role_id = None
    if permissions is not None:
        role_id = uuid.uuid4()
        session.add(
            Role(id=role_id, name=f"test-role-{role_id}", permissions=list(permissions))
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
        await session.rollback()
        await session.execute(
            delete(EmergencyStopEvent).where(EmergencyStopEvent.actor_user_id == user_id)
        )
        await session.execute(delete(BrokerGrant).where(BrokerGrant.user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        if role_id is not None:
            await session.execute(delete(Role).where(Role.id == role_id))
        await session.commit()


@contextlib.asynccontextmanager
async def paper_broker_row(session):
    broker_id = uuid.uuid4()
    session.add(
        Broker(id=broker_id, name="Test Broker", kind=BrokerKind.PAPER, provider="paper-sim")
    )
    await session.commit()
    try:
        yield broker_id
    finally:
        await session.rollback()
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
    grant_id = uuid.uuid4()
    session.add(BrokerGrant(id=grant_id, user_id=user_id, broker_id=broker_id))
    await session.commit()
    try:
        yield
    finally:
        await session.rollback()
        await session.execute(delete(BrokerGrant).where(BrokerGrant.id == grant_id))
        await session.commit()


@contextlib.asynccontextmanager
async def api_client():
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def _get_token(client: AsyncClient, email: str) -> str:
    response = await client.post(
        "/auth/login", data={"username": email, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


ADMIN = (Permission.ADMIN.value,)
TRADER = (Permission.SUBMIT_PAPER_TRADE.value,)
ADMIN_AND_TRADER = (Permission.ADMIN.value, Permission.SUBMIT_PAPER_TRADE.value)


# --- status endpoint scoping (authentication only, not admin:manage) ---


@pytest.mark.asyncio
async def test_status_requires_authentication():
    async with api_client() as client:
        response = await client.get("/admin/emergency-stop")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_any_authenticated_user_can_read_the_status_without_admin():
    """D039/D034 scoping: knowing you're blocked is not privileged."""
    async with db_session() as session, clean_emergency_stop_table(session):
        async with user_with(session, permissions=TRADER) as (_uid, email):
            async with api_client() as client:
                token = await _get_token(client, email)
                response = await client.get(
                    "/admin/emergency-stop", headers={"Authorization": f"Bearer {token}"}
                )
    assert response.status_code == 200
    assert response.json()["active"] is False


@pytest.mark.asyncio
async def test_status_reports_the_settings_fallback_when_no_flip_was_ever_persisted():
    async with db_session() as session, clean_emergency_stop_table(session):
        async with user_with(session, permissions=TRADER) as (_uid, email):
            async with api_client() as client:
                token = await _get_token(client, email)
                response = await client.get(
                    "/admin/emergency-stop", headers={"Authorization": f"Bearer {token}"}
                )
    body = response.json()
    assert body["source"] == "settings_default"
    # Nothing invented: no row means no actor, no reason, no timestamp.
    assert body["reason"] is None
    assert body["actor_user_id"] is None
    assert body["changed_at"] is None


# --- write endpoints are admin:manage-gated ---


@pytest.mark.asyncio
async def test_non_admin_cannot_activate_the_emergency_stop():
    async with db_session() as session, clean_emergency_stop_table(session):
        async with user_with(session, permissions=TRADER) as (_uid, email):
            async with api_client() as client:
                token = await _get_token(client, email)
                response = await client.post(
                    "/admin/emergency-stop",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"reason": "should not be allowed"},
                )
                # And nothing was written.
                state = await get_emergency_stop_state(session, settings_default=False)
    assert response.status_code == 403
    assert state.source == "settings_default"


@pytest.mark.asyncio
async def test_non_admin_cannot_deactivate_the_emergency_stop():
    async with db_session() as session, clean_emergency_stop_table(session):
        async with user_with(session, permissions=TRADER) as (_uid, email):
            async with api_client() as client:
                token = await _get_token(client, email)
                response = await client.post(
                    "/admin/emergency-stop/deactivate",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"reason": "should not be allowed"},
                )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_unauthenticated_cannot_activate_the_emergency_stop():
    async with api_client() as client:
        response = await client.post("/admin/emergency-stop", json={"reason": "nope"})
    assert response.status_code == 401


# --- reason is required, on both directions ---


@pytest.mark.asyncio
async def test_activating_without_a_reason_is_rejected():
    async with db_session() as session, clean_emergency_stop_table(session):
        async with user_with(session, permissions=ADMIN) as (_uid, email):
            async with api_client() as client:
                token = await _get_token(client, email)
                response = await client.post(
                    "/admin/emergency-stop", headers={"Authorization": f"Bearer {token}"}, json={}
                )
                state = await get_emergency_stop_state(session, settings_default=False)
    assert response.status_code == 422
    assert state.source == "settings_default"


@pytest.mark.asyncio
async def test_activating_with_a_blank_reason_is_rejected():
    async with db_session() as session, clean_emergency_stop_table(session):
        async with user_with(session, permissions=ADMIN) as (_uid, email):
            async with api_client() as client:
                token = await _get_token(client, email)
                response = await client.post(
                    "/admin/emergency-stop",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"reason": "    "},
                )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_deactivating_also_requires_a_reason():
    async with db_session() as session, clean_emergency_stop_table(session):
        async with user_with(session, permissions=ADMIN) as (_uid, email):
            async with api_client() as client:
                token = await _get_token(client, email)
                response = await client.post(
                    "/admin/emergency-stop/deactivate",
                    headers={"Authorization": f"Bearer {token}"},
                    json={},
                )
    assert response.status_code == 422


# --- the audit trail ---


@pytest.mark.asyncio
async def test_activation_persists_who_why_and_when():
    async with db_session() as session, clean_emergency_stop_table(session):
        async with user_with(session, permissions=ADMIN) as (user_id, email):
            async with api_client() as client:
                token = await _get_token(client, email)
                response = await client.post(
                    "/admin/emergency-stop",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"reason": "  vendor feed went stale  "},
                )
            rows = (
                (await session.execute(select(EmergencyStopEvent))).scalars().all()
            )

            assert response.status_code == 200, response.text
            body = response.json()
            assert body["active"] is True
            assert body["source"] == "database"
            # Whitespace stripped, never stored padded.
            assert body["reason"] == "vendor feed went stale"
            assert body["actor_user_id"] == str(user_id)
            assert body["changed_at"] is not None

            assert len(rows) == 1
            assert rows[0].active is True
            assert rows[0].reason == "vendor feed went stale"
            assert rows[0].actor_user_id == user_id


@pytest.mark.asyncio
async def test_every_flip_appends_a_row_and_the_latest_one_wins():
    async with db_session() as session, clean_emergency_stop_table(session):
        async with user_with(session, permissions=ADMIN) as (_uid, email):
            async with api_client() as client:
                token = await _get_token(client, email)
                headers = {"Authorization": f"Bearer {token}"}
                await client.post(
                    "/admin/emergency-stop", headers=headers, json={"reason": "first halt"}
                )
                await client.post(
                    "/admin/emergency-stop/deactivate",
                    headers=headers,
                    json={"reason": "resumed"},
                )
                await client.post(
                    "/admin/emergency-stop", headers=headers, json={"reason": "second halt"}
                )
                status = await client.get("/admin/emergency-stop", headers=headers)

            rows = (
                (
                    await session.execute(
                        select(EmergencyStopEvent).order_by(EmergencyStopEvent.id.asc())
                    )
                )
                .scalars()
                .all()
            )

            # Nothing is overwritten: the history of all three flips remains.
            assert [(r.active, r.reason) for r in rows] == [
                (True, "first halt"),
                (False, "resumed"),
                (True, "second halt"),
            ]
            assert status.json()["active"] is True
            assert status.json()["reason"] == "second halt"


@pytest.mark.asyncio
async def test_a_repeated_activation_is_still_recorded():
    """A no-op flip is an event too - see set_emergency_stop's docstring."""
    async with db_session() as session, clean_emergency_stop_table(session):
        async with user_with(session, permissions=ADMIN) as (_uid, email):
            async with api_client() as client:
                token = await _get_token(client, email)
                headers = {"Authorization": f"Bearer {token}"}
                await client.post(
                    "/admin/emergency-stop", headers=headers, json={"reason": "halt once"}
                )
                await client.post(
                    "/admin/emergency-stop", headers=headers, json={"reason": "halt again"}
                )
            count = len(
                (await session.execute(select(EmergencyStopEvent))).scalars().all()
            )
    assert count == 2


# --- the actual point: it blocks real trades, live, with no restart ---


@pytest.mark.asyncio
async def test_activating_over_http_blocks_a_real_trade_with_settings_left_false():
    from apps.api.app.core.config import get_settings

    settings = get_settings()
    assert settings.emergency_stop_active is False, (
        "this test proves the DB - not .env - is authoritative, so the "
        "settings fallback must be false for it to mean anything"
    )

    async with db_session() as session, clean_emergency_stop_table(session):
        async with (
            user_with(session, permissions=ADMIN_AND_TRADER) as (user_id, email),
            paper_broker_row(session) as broker_id,
            broker_grant(session, user_id=user_id, broker_id=broker_id),
        ):
            async with api_client() as client:
                token = await _get_token(client, email)
                headers = {"Authorization": f"Bearer {token}"}

                before = await client.post(
                    f"/brokers/{broker_id}/trades", headers=headers, json=TRADE_BODY
                )
                assert before.status_code == 200, before.text
                assert before.json()["status"] == "filled"

                halt = await client.post(
                    "/admin/emergency-stop", headers=headers, json={"reason": "live halt test"}
                )
                assert halt.status_code == 200, halt.text

                # No restart, no .env edit, no re-created app between these
                # two submissions - only the persisted row changed.
                during = await client.post(
                    f"/brokers/{broker_id}/trades", headers=headers, json=TRADE_BODY
                )
                assert during.status_code == 200, during.text
                assert during.json()["status"] == "rejected"
                assert during.json()["block_reason"] == "emergency_stop_active"

                resume = await client.post(
                    "/admin/emergency-stop/deactivate",
                    headers=headers,
                    json={"reason": "all clear"},
                )
                assert resume.status_code == 200, resume.text
                assert resume.json()["active"] is False

                # A different quantity, deliberately: an identical repeat
                # of the first (filled) order within D024's 5s window
                # would be blocked as a duplicate, which would prove
                # nothing about the emergency stop.
                after = await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers=headers,
                    json={**TRADE_BODY, "quantity": "7"},
                )
                assert after.status_code == 200, after.text
                assert after.json()["status"] == "filled", after.text


def test_the_risk_engine_stays_free_of_the_persistence_layer():
    """D039's non-negotiable: the DB read happens one layer ABOVE the Risk
    Engine. This asserts it structurally rather than by convention - if
    someone ever "simplifies" evaluate_trade() by having it fetch the flag
    itself, this fails.

    Both routes that can reach a broker share `_execute_trade()`, which is
    the single place the state is read, so a human-submitted and an
    agent-originated trade cannot diverge on this control.
    """
    import inspect

    import apps.api.app.risk.engine as engine
    import apps.api.app.risk.models as models
    from apps.api.app.api.routes import trades

    for module in (engine, models):
        imported = {
            line.strip()
            for line in inspect.getsource(module).splitlines()
            if line.startswith(("import ", "from "))
        }
        forbidden = ("safety", "sqlalchemy", "db.", "session", "httpx", "agents")
        for line in imported:
            assert not any(token in line for token in forbidden), line

    # Still a plain boolean parameter, unchanged.
    signature = inspect.signature(engine.evaluate_trade)
    assert signature.parameters["emergency_stop_active"].annotation is bool

    # Exactly one read site, in the shared helper both routes call.
    assert inspect.getsource(trades).count("is_emergency_stop_active(") == 1
    assert "is_emergency_stop_active(" in inspect.getsource(trades._execute_trade)


@pytest.mark.asyncio
async def test_the_persisted_row_beats_a_true_settings_default():
    """Once a row exists, `Settings` is not consulted at all - even when it
    says the stop is on and the row says it is off."""
    from apps.api.app.core.config import get_settings

    settings = get_settings()
    async with db_session() as session, clean_emergency_stop_table(session):
        async with (
            user_with(session, permissions=ADMIN_AND_TRADER) as (user_id, email),
            paper_broker_row(session) as broker_id,
            broker_grant(session, user_id=user_id, broker_id=broker_id),
        ):
            async with api_client() as client:
                token = await _get_token(client, email)
                headers = {"Authorization": f"Bearer {token}"}
                await client.post(
                    "/admin/emergency-stop", headers=headers, json={"reason": "halt"}
                )
                await client.post(
                    "/admin/emergency-stop/deactivate",
                    headers=headers,
                    json={"reason": "explicitly resumed"},
                )

                original = settings.emergency_stop_active
                settings.emergency_stop_active = True
                try:
                    response = await client.post(
                        f"/brokers/{broker_id}/trades", headers=headers, json=TRADE_BODY
                    )
                    status = await client.get("/admin/emergency-stop", headers=headers)
                finally:
                    settings.emergency_stop_active = original

    assert response.json()["status"] == "filled"
    assert status.json()["active"] is False
    assert status.json()["source"] == "database"


@pytest.mark.asyncio
async def test_the_state_survives_a_fresh_app_lifespan_and_a_fresh_connection():
    """Restart survival, as far as an in-process test can prove it: the
    flip is made under one app lifespan and one DB session, and observed
    under a brand-new lifespan through a brand-new session with no shared
    in-memory state. (The full process-restart check is done live against
    a real uvicorn process - see docs/DECISIONS.md D039.)"""
    async with db_session() as session, clean_emergency_stop_table(session):
        async with user_with(session, permissions=ADMIN) as (_uid, email):
            async with api_client() as client:
                token = await _get_token(client, email)
                halt = await client.post(
                    "/admin/emergency-stop",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"reason": "survives"},
                )
                assert halt.status_code == 200, halt.text

            # A completely separate lifespan + client + session.
            async with api_client() as client2:
                token2 = await _get_token(client2, email)
                status = await client2.get(
                    "/admin/emergency-stop", headers={"Authorization": f"Bearer {token2}"}
                )

            async with db_session() as fresh_session:
                state = await get_emergency_stop_state(fresh_session, settings_default=False)

    assert status.json()["active"] is True
    assert status.json()["reason"] == "survives"
    assert state.active is True
    assert state.source == "database"
