"""Deterministic helpers behind the /strategies routes (Phase 54).

No FastAPI, no HTTPException, no request or response types live here - the
same separation apps/api/app/execution/persistence.py and
apps/api/app/portfolio_manager/manager.py keep. A function here answers a
question about strategies; deciding what HTTP status that answer deserves
is the route's job, and keeping the two apart is what makes these
individually unit-testable without an app, a client, or a token.

Sessions are passed in as an explicit first argument rather than held by a
class, again following execution/persistence.py: an AsyncSession is
request-scoped, so a long-lived object owning one would either be wrong or
would have to be rebuilt per request anyway, at which point the class is
just a namespace with extra steps. (MarketDataStore is a class only because
HistoricalBarProvider's Protocol shape calls for one; nothing here
implements a Protocol.)
"""

import hashlib
import json
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.db.models import StrategyVersion


def compute_definition_hash(definition: dict) -> str:
    """sha256 of the definition's CANONICAL JSON - sorted keys, no
    whitespace - as 64 lowercase hex characters.

    Canonical rather than "whatever json.dumps happens to emit" so the hash
    is a function of the definition's MEANING, not of the byte order a
    particular client sent it in. `{"a": 1, "b": 2}` and `{"b": 2, "a": 1}`
    are the same strategy and must hash identically, or every fork of an
    unchanged definition would look like a change and a later phase keying
    a cached backtest on this hash would miss every time.

    Not a security primitive: this identifies a definition, it does not
    authenticate one. sha256 is used because it is the same hash
    `password_reset_tokens.token_hash` already relies on and there is no
    reason for this codebase to carry two.
    """
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def next_version_number(session: AsyncSession, strategy_id: uuid.UUID) -> int:
    """The version number a new version of this strategy should take:
    `max(existing) + 1`, or 1 when the strategy has none yet.

    Version numbers are dense and start at 1, so they read as "the third
    version of this strategy" rather than as opaque ids. The uniqueness
    guarantee is the database's - `uq_strategy_version_number` on
    `(strategy_id, version_number)` - not this function's: two concurrent
    forks of the same strategy can both compute the same next number, and
    the constraint is what makes the second one fail rather than silently
    creating a duplicate version 4. This function exists to pick the
    number, not to defend the invariant.
    """
    highest = (
        await session.execute(
            select(func.max(StrategyVersion.version_number)).where(
                StrategyVersion.strategy_id == strategy_id
            )
        )
    ).scalar_one_or_none()
    return 1 if highest is None else int(highest) + 1


async def latest_version(
    session: AsyncSession, strategy_id: uuid.UUID
) -> StrategyVersion | None:
    """This strategy's highest-numbered version, or None when it has none.

    "Latest" is by `version_number`, never by `created_at`: the numbers are
    what the API and the UI address a version by, and ordering by timestamp
    would put two versions created within the same clock tick in an
    arbitrary order.

    None is structurally possible (nothing in the schema forces a strategy
    to have a version) even though `POST /strategies` always creates
    version 1 alongside the strategy in one transaction - so callers handle
    it rather than assuming the row exists.
    """
    return (
        await session.execute(
            select(StrategyVersion)
            .where(StrategyVersion.strategy_id == strategy_id)
            .order_by(StrategyVersion.version_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
