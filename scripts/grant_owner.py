"""Make ONE existing account the platform owner, and (optionally) demote
every other account that holds `admin:manage`.

`scripts/create_admin.py` creates the FIRST admin on an empty database. This
is the complement for a database that already has accounts: it takes an
account that already exists (the operator has already signed in with it)
and gives it the full permission set, a broker grant on every configured
broker, and — with `--demote-others` — takes `admin:manage` away from
everyone else. The logic lives in `apps/api/app/auth/bootstrap.py`, which
the API also runs at startup when `OWNER_BOOTSTRAP_EMAIL` is set — so a
Railway deployment can do this with one environment variable and no shell.

Usage:

    export DATABASE_URL='postgresql://...'   # Railway → Postgres → Variables
    python scripts/grant_owner.py --email you@example.com --demote-others

Refuses if the account does not exist — it never creates one. Idempotent.
Never prints a hash, a token, or the database credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.api.app.auth.bootstrap import grant_owner  # noqa: E402


def _normalise_url(url: str) -> str:
    if "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--demote-others",
        action="store_true",
        help="Remove admin:manage from every other account (moves them to 'trader').",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report, change nothing.")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set. Point it at the target database first.")
        return 2
    database_url = _normalise_url(database_url)
    visible = database_url.split("@")[-1] if "@" in database_url else database_url
    print(f"Target database: {visible}")

    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            report = await grant_owner(session, email=args.email, demote_others=args.demote_others)
            if not report.found:
                print(f"No account with email {report.email!r}. This script never creates one.")
                return 1
            for name in report.role_created:
                print(f"  created role {name!r}")
            for name in report.role_updated:
                print(f"  updated {name}")
            for name in report.brokers_granted:
                print(f"  granted broker {name!r}")
            for email in report.demoted:
                print(f"  demoted {email} -> trader")
            if not report.changed:
                print("  nothing to change — already the owner with every grant.")
            if args.dry_run:
                await session.rollback()
                print("\nDry run — nothing was written.")
                return 0
            await session.commit()
            print(f"\nDone. {report.email} is the owner. Sign out and back in to pick up the role.")
            return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
