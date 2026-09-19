"""Owner bootstrap (Phase 79, D097).

Pins the three properties that make this safe to expose as an env var: it
never creates an account, it is idempotent, and demotion touches only
accounts that actually hold `admin:manage`.
"""

import contextlib
import uuid

import pytest
from sqlalchemy import delete, select

from apps.api.app.auth.bootstrap import OWNER_ROLE, TRADER_ROLE, grant_owner
from apps.api.app.auth.permissions import Permission
from apps.api.app.auth.security import hash_password
from apps.api.app.db.base import get_session
from apps.api.app.db.models import Broker, BrokerGrant, BrokerKind, Role, User


@contextlib.asynccontextmanager
async def db_session():
    gen = get_session()
    session = await anext(gen)
    try:
        yield session
    finally:
        await gen.aclose()


@contextlib.asynccontextmanager
async def users_and_broker(session):
    """Two users: the target (no role) and another one holding admin."""
    tag = uuid.uuid4().hex[:8]
    admin_role = Role(id=uuid.uuid4(), name=f"t-admin-{tag}", permissions=[Permission.ADMIN.value])
    target = User(
        id=uuid.uuid4(), email=f"target-{tag}@example.com",
        hashed_password=hash_password("x" * 12), is_active=False, role_id=None,
    )
    other = User(
        id=uuid.uuid4(), email=f"other-{tag}@example.com",
        hashed_password=hash_password("x" * 12), is_active=True, role_id=admin_role.id,
    )
    broker = Broker(id=uuid.uuid4(), name=f"b-{tag}", kind=BrokerKind.PAPER,
                    provider="paper", is_active=True)
    session.add_all([admin_role, target, other, broker])
    ids = (target.id, other.id, broker.id)
    emails = (target.email, other.email)
    admin_role_id = admin_role.id
    await session.commit()
    try:
        # Plain values, not ORM objects: commit expires attributes and a
        # lazy refresh outside a greenlet raises MissingGreenlet.
        yield ids, emails
    finally:
        await session.rollback()
        for uid in ids[:2]:
            await session.execute(delete(BrokerGrant).where(BrokerGrant.user_id == uid))
            await session.execute(delete(User).where(User.id == uid))
        await session.execute(delete(Broker).where(Broker.id == ids[2]))
        await session.execute(delete(Role).where(Role.id == admin_role_id))
        await session.commit()


@pytest.mark.asyncio
async def test_an_unknown_email_is_reported_and_nothing_is_written():
    async with db_session() as session:
        report = await grant_owner(session, email="nobody-" + uuid.uuid4().hex + "@example.com")
        assert not report.found
        assert not report.changed
        await session.rollback()


@pytest.mark.asyncio
async def test_target_becomes_owner_with_every_permission_and_every_broker():
    async with db_session() as session, users_and_broker(session) as (ids, emails):
        target_id, other_id, broker_id = ids
        target_email, other_email = emails
        report = await grant_owner(session, email=target_email.upper(), demote_others=True)
        await session.commit()

        assert report.found
        target = (await session.execute(select(User).where(User.id == target_id))).scalar_one()
        role = (await session.execute(select(Role).where(Role.id == target.role_id))).scalar_one()
        assert role.name == OWNER_ROLE
        assert set(role.permissions) == {p.value for p in Permission}
        assert target.is_active

        grants = (
            await session.execute(select(BrokerGrant).where(BrokerGrant.user_id == target_id))
        ).scalars().all()
        # Every broker in the database, which includes ours (the shared dev
        # database may hold others too).
        granted = {g.broker_id for g in grants}
        assert broker_id in granted
        all_brokers = {b.id for b in (await session.execute(select(Broker))).scalars()}
        assert granted == all_brokers

        other = (await session.execute(select(User).where(User.id == other_id))).scalar_one()
        other_role = (
            await session.execute(select(Role).where(Role.id == other.role_id))
        ).scalar_one()
        assert other_role.name == TRADER_ROLE
        assert Permission.ADMIN.value not in other_role.permissions
        assert report.demoted == [other_email]


@pytest.mark.asyncio
async def test_running_twice_changes_nothing_the_second_time():
    async with db_session() as session, users_and_broker(session) as (_ids, emails):
        first = await grant_owner(session, email=emails[0], demote_others=True)
        await session.commit()
        assert first.changed
        second = await grant_owner(session, email=emails[0], demote_others=True)
        await session.commit()
        assert not second.changed
        assert second.demoted == []
