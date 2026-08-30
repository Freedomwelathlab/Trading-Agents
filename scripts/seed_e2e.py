"""Seed the real database with the fixtures the Playwright e2e suite drives
(docs/DECISIONS.md D045).

This is the same direct-SQL bootstrap every phase of this project has used to
create the very first admin user (D013) — there is no user holding
``admin:manage`` to call ``/admin/*`` with the first time — extended with the
two paper brokers and the grants the e2e specs need.

Everything written here is a real row in a real Postgres database. The e2e
suite then drives the real UI against the real API against these rows; no
response is ever mocked. The one thing this script fabricates is *nothing* —
prices are supplied by the specs themselves as an explicit ``estimated_price``
on each trade (D017's caller-authoritative path), so no market-data vendor is
consulted and no quote is invented.

Usage (from the repo root, with DATABASE_URL pointing at the target DB and
migrations already applied)::

    python scripts/seed_e2e.py

Idempotent: re-running deletes and recreates only the rows it owns, keyed by
the fixed UUIDs below. Those UUIDs are duplicated in
``apps/web/e2e/fixtures.ts`` so the specs can address a specific broker
without scraping one out of the UI first.

NEVER run this against a database that holds anything you care about, and
never against a live-trading deployment: the credentials below are public,
committed, throwaway paper-only test credentials.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from apps.api.app.auth.security import hash_password
from apps.api.app.core.config import get_settings

# --- Fixed fixture identity (mirrored in apps/web/e2e/fixtures.ts) ---------

ADMIN_ROLE_ID = "1e2e0000-0000-4000-8000-000000000001"
TRADER_ROLE_ID = "1e2e0000-0000-4000-8000-000000000002"

ADMIN_USER_ID = "2e2e0000-0000-4000-8000-000000000001"
TRADER_USER_ID = "2e2e0000-0000-4000-8000-000000000002"

FLAT_BROKER_ID = "3e2e0000-0000-4000-8000-000000000001"
CONCENTRATED_BROKER_ID = "3e2e0000-0000-4000-8000-000000000002"
# A real, active paper broker that NO fixture user holds a grant for, so the
# suite can assert the backend's real 403 (not a 404 for a non-existent id).
UNGRANTED_BROKER_ID = "3e2e0000-0000-4000-8000-000000000003"

ADMIN_EMAIL = "e2e-admin@example.test"
ADMIN_PASSWORD = "e2e-admin-password-2026"
TRADER_EMAIL = "e2e-trader@example.test"
TRADER_PASSWORD = "e2e-trader-password-2026"

# The concentrated broker is sized so that a BUY of 100 @ 100.00 passes the
# deterministic Risk Engine (notional 10_000 == 10% of 100_000 equity, per-trade
# risk 100 * 1.00 stop distance == 100 <= 1% of equity, exposure 30_000 <= 50%)
# and is then SHRUNK by the trade-path Portfolio Manager, whose 25%-of-equity
# per-symbol cap leaves only 5_000 of headroom above the 20_000 already held —
# i.e. a real MODIFY to 50 shares, produced by real code, not a stubbed verdict.
CONCENTRATED_CASH = Decimal("80000")
CONCENTRATED_SYMBOL = "AAPL.US"
CONCENTRATED_QUANTITY = Decimal("200")

FLAT_CASH = Decimal("100000")

_BROKER_IDS = [FLAT_BROKER_ID, CONCENTRATED_BROKER_ID, UNGRANTED_BROKER_ID]


async def seed() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    admin_hash = hash_password(ADMIN_PASSWORD)
    trader_hash = hash_password(TRADER_PASSWORD)

    async with engine.begin() as conn:
        # Delete in FK-safe order. Only fixture-owned rows are touched, plus
        # any orders/fills the previous run's trades wrote against the fixture
        # brokers (so each suite run starts from the same book).
        await conn.execute(
            text(
                "DELETE FROM fills WHERE order_id IN "
                "(SELECT id FROM orders WHERE broker_id = ANY(:brokers))"
            ),
            {"brokers": _BROKER_IDS},
        )
        for stmt, params in (
            ("DELETE FROM orders WHERE broker_id = ANY(:brokers)", "brokers"),
            ("DELETE FROM broker_positions WHERE broker_id = ANY(:brokers)", "brokers"),
            ("DELETE FROM broker_accounts WHERE broker_id = ANY(:brokers)", "brokers"),
            ("DELETE FROM broker_grants WHERE broker_id = ANY(:brokers)", "brokers"),
        ):
            await conn.execute(
                text(stmt), {params: _BROKER_IDS}
            )
        await conn.execute(
            text("DELETE FROM brokers WHERE id = ANY(:ids)"),
            {"ids": _BROKER_IDS},
        )
        await conn.execute(
            text("DELETE FROM users WHERE id = ANY(:ids)"),
            {"ids": [ADMIN_USER_ID, TRADER_USER_ID]},
        )
        await conn.execute(
            text("DELETE FROM roles WHERE id = ANY(:ids)"),
            {"ids": [ADMIN_ROLE_ID, TRADER_ROLE_ID]},
        )

        await conn.execute(
            text(
                "INSERT INTO roles (id, name, description, permissions) VALUES "
                "(:aid, 'e2e-admin', 'Playwright e2e admin fixture', :aperms), "
                "(:tid, 'e2e-trader', 'Playwright e2e trader fixture', :tperms)"
            ),
            {
                "aid": ADMIN_ROLE_ID,
                "aperms": ["admin:manage", "trade:submit:paper", "portfolio:view"],
                "tid": TRADER_ROLE_ID,
                "tperms": ["trade:submit:paper", "portfolio:view"],
            },
        )

        await conn.execute(
            text(
                "INSERT INTO users (id, email, hashed_password, is_active, role_id) VALUES "
                "(:aid, :aemail, :ahash, true, :arole), "
                "(:tid, :temail, :thash, true, :trole)"
            ),
            {
                "aid": ADMIN_USER_ID,
                "aemail": ADMIN_EMAIL,
                "ahash": admin_hash,
                "arole": ADMIN_ROLE_ID,
                "tid": TRADER_USER_ID,
                "temail": TRADER_EMAIL,
                "thash": trader_hash,
                "trole": TRADER_ROLE_ID,
            },
        )

        await conn.execute(
            text(
                "INSERT INTO brokers (id, name, kind, provider, is_active) VALUES "
                "(:fid, 'E2E Flat Paper', 'paper', 'paper', true), "
                "(:cid, 'E2E Concentrated Paper', 'paper', 'paper', true), "
                "(:uid, 'E2E Ungranted Paper', 'paper', 'paper', true)"
            ),
            {
                "fid": FLAT_BROKER_ID,
                "cid": CONCENTRATED_BROKER_ID,
                "uid": UNGRANTED_BROKER_ID,
            },
        )

        for user_id in (ADMIN_USER_ID, TRADER_USER_ID):
            for broker_id in (FLAT_BROKER_ID, CONCENTRATED_BROKER_ID):
                await conn.execute(
                    text(
                        "INSERT INTO broker_grants (id, user_id, broker_id) "
                        "VALUES (gen_random_uuid(), :u, :b)"
                    ),
                    {"u": user_id, "b": broker_id},
                )

        await conn.execute(
            text(
                "INSERT INTO broker_accounts (broker_id, cash) VALUES "
                "(:fid, :fcash), (:cid, :ccash)"
            ),
            {
                "fid": FLAT_BROKER_ID,
                "fcash": FLAT_CASH,
                "cid": CONCENTRATED_BROKER_ID,
                "ccash": CONCENTRATED_CASH,
            },
        )

        await conn.execute(
            text(
                "INSERT INTO broker_positions (id, broker_id, symbol, quantity) "
                "VALUES (gen_random_uuid(), :b, :s, :q)"
            ),
            {
                "b": CONCENTRATED_BROKER_ID,
                "s": CONCENTRATED_SYMBOL,
                "q": CONCENTRATED_QUANTITY,
            },
        )

    await engine.dispose()
    print(
        f"seeded: users {ADMIN_EMAIL}, {TRADER_EMAIL}; "
        f"brokers {FLAT_BROKER_ID}, {CONCENTRATED_BROKER_ID}"
    )


if __name__ == "__main__":
    asyncio.run(seed())
