"""Create the first admin user, interactively, on a database you point it at.

Trading OS has no public registration: accounts are created through
`POST /admin/users`, which itself requires `admin:manage`. The very first
admin therefore has to be inserted directly, which is what this does.

**The password is typed by the operator and never leaves this process.**
It is read with `getpass` (not echoed), hashed with the application's own
`hash_password`, and only the bcrypt hash is written. It is never printed,
never logged, never passed as an argument — so it does not land in shell
history, in a process list, or in a transcript. That is deliberate: a
password supplied on a command line is visible to every other process on
the machine.

Usage:

    # DATABASE_URL points at the target database. For Railway, copy it
    # from the Postgres service's Variables tab.
    export DATABASE_URL='postgresql+asyncpg://user:pass@host:5432/railway'
    python scripts/create_admin.py

Safe to re-run: an existing email is reported and left alone rather than
overwritten, so this cannot silently reset someone's password.

To grant LIVE trading permissions, pass `--live`. Off by default, because
the deliberate reading of this project's safety rules is that live
capability is granted on purpose and never by a convenience default. It
grants the PERMISSION only — `TRADING_MODE` and `LIVE_TRADING_ENABLED`
still gate execution independently, and this script touches neither.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.api.app.auth.permissions import Permission  # noqa: E402
from apps.api.app.auth.security import hash_password  # noqa: E402
from apps.api.app.db.models import Role, User  # noqa: E402

PAPER_PERMISSIONS = [
    Permission.SUBMIT_PAPER_TRADE.value,
    Permission.VIEW_PORTFOLIO.value,
    Permission.ADMIN.value,
    Permission.STRATEGY_MANAGE.value,
    Permission.STRATEGY_BACKTEST.value,
    Permission.STRATEGY_SIGNAL.value,
    Permission.STRATEGY_DEPLOY.value,
    Permission.STRATEGY_APPROVE_DEPLOYMENT.value,
]

LIVE_ONLY_PERMISSIONS = [
    Permission.SUBMIT_LIVE_TRADE.value,
    Permission.STRATEGY_APPROVE_LIVE_DEPLOYMENT.value,
]

MIN_PASSWORD_LENGTH = 12
"""Longer than the API's own 8-character floor. This account is an admin on
an internet-reachable deployment, and it is being created once, by hand, by
someone who can choose freely."""


def _read_password() -> str:
    """Twice, never echoed, never returned to a caller that prints it."""
    while True:
        first = getpass.getpass("Password (not echoed): ")
        if len(first) < MIN_PASSWORD_LENGTH:
            print(f"  Too short — use at least {MIN_PASSWORD_LENGTH} characters.")
            continue
        second = getpass.getpass("Confirm password: ")
        if first != second:
            print("  They do not match. Try again.")
            continue
        return first


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", help="Prompted for if omitted.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Also grant live-trading PERMISSIONS (not enabled by default).",
    )
    parser.add_argument(
        "--role-name",
        default="admin",
        help="Role to create or reuse (default: admin).",
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set. Point it at the target database first.")
        return 2
    if "+asyncpg" not in database_url:
        # Railway hands out a psycopg-style URL; this driver needs the async one.
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    # The host is echoed so the operator can see WHICH database is about to
    # be written to. Credentials in the URL are never printed.
    visible = database_url.split("@")[-1] if "@" in database_url else database_url
    print(f"Target database: {visible}")

    email = (args.email or input("Email: ")).strip().lower()
    if not email or "@" not in email:
        print("That does not look like an email address.")
        return 2

    permissions = list(PAPER_PERMISSIONS)
    if args.live:
        permissions += LIVE_ONLY_PERMISSIONS
        print(
            "\n  NOTE: granting live-trading PERMISSIONS. Execution is still gated\n"
            "  independently by TRADING_MODE and LIVE_TRADING_ENABLED, which this\n"
            "  script does not touch."
        )

    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as session:
            existing = (
                await session.execute(select(User).where(User.email == email))
            ).scalar_one_or_none()
            if existing is not None:
                # Reported, not overwritten: silently resetting a password
                # would be a way to take over an account by typo.
                print(
                    f"\nA user with email {email!r} already exists (id {existing.id}).\n"
                    "Nothing was changed. Use the password-reset flow to change its "
                    "password, or pick a different email."
                )
                return 1

            # Asked for only once the email is known to be free, so nobody
            # types a password twice and is then told the address was taken.
            password = _read_password()

            role = (
                await session.execute(select(Role).where(Role.name == args.role_name))
            ).scalar_one_or_none()
            if role is None:
                role = Role(
                    id=uuid.uuid4(),
                    name=args.role_name,
                    description="Created by scripts/create_admin.py",
                    permissions=permissions,
                )
                session.add(role)
                await session.flush()
                print(f"Created role {args.role_name!r} with {len(permissions)} permissions.")
            else:
                # Reused as-is. Widening an existing role would silently
                # escalate everyone already holding it, which is a worse
                # outcome than a new account that turns out to be narrow.
                have = set(role.permissions or [])
                missing = [p for p in permissions if p not in have]
                print(
                    f"Reusing existing role {args.role_name!r} with its current "
                    f"{len(have)} permission(s) — NOT widened."
                )
                if missing:
                    print(
                        "  This account will therefore NOT have: "
                        + ", ".join(missing)
                        + "\n  Pass --role-name with a new name to get the full set."
                    )

            user = User(
                id=uuid.uuid4(),
                email=email,
                hashed_password=hash_password(password),
                is_active=True,
                role_id=role.id,
            )
            session.add(user)
            await session.commit()

            print(f"\nCreated admin {email} (id {user.id}).")
            print("The password was not written anywhere but the hash. Sign in with it.")
            return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
