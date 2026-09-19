"""Owner bootstrap: widen ONE existing account to every permission.

Shared by `scripts/grant_owner.py` (run by hand against any database) and
the API's startup (`OWNER_BOOTSTRAP_EMAIL`, for a deployment where the
operator can set an environment variable but has no shell — Railway).

Why this exists: the account that needs widening is, by definition, the
one that cannot call `/admin/*` yet (docs/DECISIONS.md D013). Both entry
points call the same function so they cannot drift.

What it will NOT do: create an account. An email with no account is
reported and nothing is written, so a typo — or an attacker who can set
env vars — cannot plant a new owner; they could only widen an account that
already exists, which is the same power they'd already have over the
database itself.

Granting the two live-trading PERMISSIONS is not enabling live EXECUTION:
`TRADING_MODE`, `LIVE_TRADING_ENABLED` and
`STRATEGY_LIVE_AUTO_EXECUTION_ENABLED` gate that independently and this
module touches none of them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.auth.permissions import Permission
from apps.api.app.db.models import Broker, BrokerGrant, Role, User

OWNER_ROLE = "owner"
TRADER_ROLE = "trader"

ALL_PERMISSIONS = [p.value for p in Permission]

TRADER_PERMISSIONS = [
    Permission.SUBMIT_PAPER_TRADE.value,
    Permission.VIEW_PORTFOLIO.value,
    Permission.STRATEGY_MANAGE.value,
    Permission.STRATEGY_BACKTEST.value,
    Permission.STRATEGY_SIGNAL.value,
    Permission.STRATEGY_DEPLOY.value,
]


@dataclass
class BootstrapReport:
    email: str
    found: bool = False
    role_created: list[str] = field(default_factory=list)
    role_updated: list[str] = field(default_factory=list)
    brokers_granted: list[str] = field(default_factory=list)
    demoted: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.role_created or self.role_updated or self.brokers_granted or self.demoted)


async def _ensure_role(
    session: AsyncSession, report: BootstrapReport, name: str, permissions: list[str], desc: str
) -> Role:
    role = (await session.execute(select(Role).where(Role.name == name))).scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name=name, description=desc, permissions=permissions)
        session.add(role)
        await session.flush()
        report.role_created.append(name)
    elif sorted(role.permissions or []) != sorted(permissions):
        # Only the roles THIS module owns are ever rewritten; any other role
        # name is never touched, so an existing custom role cannot be
        # silently widened.
        role.permissions = permissions
        await session.flush()
        report.role_updated.append(name)
    return role


async def grant_owner(
    session: AsyncSession, *, email: str, demote_others: bool = False
) -> BootstrapReport:
    """Widen `email` to owner; grant every broker; optionally demote every
    other `admin:manage` holder to `trader`. Flushes but does NOT commit —
    the caller decides (the script commits; a dry run rolls back)."""
    email = email.strip().lower()
    report = BootstrapReport(email=email)

    user = (
        await session.execute(select(User).where(func.lower(User.email) == email))
    ).scalar_one_or_none()
    if user is None:
        return report
    report.found = True

    owner = await _ensure_role(
        session, report, OWNER_ROLE, ALL_PERMISSIONS, "Platform owner — every permission."
    )
    if user.role_id != owner.id or not user.is_active:
        user.role_id = owner.id
        user.is_active = True
        report.role_updated.append(f"{user.email}->{OWNER_ROLE}")

    brokers = (await session.execute(select(Broker))).scalars().all()
    held = {
        g.broker_id
        for g in (
            await session.execute(select(BrokerGrant).where(BrokerGrant.user_id == user.id))
        ).scalars()
    }
    for broker in brokers:
        if broker.id not in held:
            session.add(BrokerGrant(id=uuid.uuid4(), user_id=user.id, broker_id=broker.id))
            report.brokers_granted.append(broker.name)

    if demote_others:
        trader = await _ensure_role(
            session, report, TRADER_ROLE, TRADER_PERMISSIONS, "Paper trader — no administration."
        )
        others = (await session.execute(select(User).where(User.id != user.id))).scalars().all()
        for other in others:
            role = (
                await session.execute(select(Role).where(Role.id == other.role_id))
            ).scalar_one_or_none()
            if role is not None and Permission.ADMIN.value in (role.permissions or []):
                other.role_id = trader.id
                report.demoted.append(other.email)

    await session.flush()
    return report
