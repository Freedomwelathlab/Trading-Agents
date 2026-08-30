"""Multi-worker safety for the portfolio snapshot scheduler (Phase 38,
docs/DECISIONS.md D047), closing the last open item D030's "Consequences"
section recorded: "Each API worker process runs its own independent loop, so
running more than one uvicorn/gunicorn worker would multiply snapshot rows -
the single clearest trigger for revisiting the 'no scheduler library'
choice (leader election or an external trigger would then be warranted)."

THE PROBLEM
-----------
`PortfolioSnapshotScheduler` is an in-process asyncio task owned by the
FastAPI lifespan (D030). That is a complete answer to "run this coroutine
every N seconds" for exactly one process. Run the app under
`uvicorn --workers 4` or gunicorn, and lifespan runs once per worker, so
four independent loops tick against one shared Postgres. Each would
enumerate the same brokers, fetch the same quotes, and append its own row -
producing four near-simultaneous rows per interval in an APPEND-ONLY table
whose entire purpose is to be a readable equity time series. Nothing about
those rows is individually *wrong*; the series they form is, and because
the table is append-only, it stays wrong.

THE FIX, AND WHY IT NEEDS NO NEW DEPENDENCY
-------------------------------------------
Before a cycle does any real work, it takes a **Postgres session-level
advisory lock** with `pg_try_advisory_lock(classid, objid)`. Exactly one
worker wins; every other worker's `pg_try_advisory_lock` returns false
*immediately* (it is the non-blocking variant) and that worker's cycle
skips cleanly, recording `SnapshotCycleLockDecision.SKIPPED_LOCK_HELD`.
The winner releases with `pg_advisory_unlock` in a `finally`, on every path
including an exception.

Postgres advisory locks were chosen over the alternatives specifically
because they add nothing:

  * **No new pip dependency.** `SELECT pg_try_advisory_lock(...)` is one
    raw statement over the SQLAlchemy/asyncpg engine this app already owns.
    APScheduler, a Redis lock library, or a leader-election library would
    each need their own configuration, failure modes, and safety review.
  * **Redis is provisioned but not wired.** docker-compose.yml runs a
    redis service and `Settings.redis_url` exists, but as of this phase no
    Python code in this repo opens a Redis connection - the `redis` package
    is an unused declared dependency. Building the first Redis client in
    the codebase to protect a snapshot loop would mean introducing a whole
    new runtime dependency edge (and a new "what if Redis is down" failure
    mode) for a lock Postgres already provides. Postgres, by contrast, is
    the one service this cycle CANNOT run without anyway: if it is
    unreachable the cycle has nothing to do, so the lock adds no new thing
    that can fail independently of the work it guards.
  * **The lock dies with its holder.** A session-level advisory lock is
    released automatically when its backend connection ends. A worker that
    is SIGKILLed mid-cycle therefore cannot wedge the schedule the way a
    row in a `scheduler_leader` table with a manually-managed expiry could.
    That is also why no new table and no migration were needed.

TWO int4 KEYS, NOT hashtext()
-----------------------------
`pg_try_advisory_lock` has two forms: one bigint key, or two int4 keys.
This module uses the **two-key** form with two fixed constants below,
rather than `hashtext('portfolio_snapshot_scheduler')::bigint`, because:

  * `hashtext()` is an undocumented internal function whose hashing is not
    contractually stable across major Postgres versions. A value that
    changed under a server upgrade would silently split the lock in two
    during a rolling upgrade - the exact window in which two workers of
    different vintages are most likely to be running at once.
  * `pg_locks` exposes the two-key form as its own `classid`/`objid`
    columns, so `SELECT * FROM pg_locks WHERE locktype = 'advisory'` shows
    an operator the same two numbers that appear literally in this file.
    A single hashed bigint would show one opaque number matching nothing
    greppable.
  * Two keys give a namespace/object split for free, so a future second
    background job in this repo takes the same classid and its own objid
    without any chance of colliding with this one.

SCOPE: ONE PROCESS PER CYCLE, NOT ONE ROW PER BROKER
----------------------------------------------------
The lock guards a whole cycle, not an individual broker's capture. It is
deliberately NOT a general "only one snapshot may ever be written at a
time" mutex: the manual `POST /brokers/{id}/portfolio/snapshots` endpoint
(D027) is unaffected and still writes whenever a human asks it to. A human
asking for a snapshot is a deliberate act with a caller who sees the
result; two schedulers duplicating each other is an accident with nobody
watching. Only the second needs suppressing.

SINGLE-WORKER BEHAVIOUR IS UNCHANGED
------------------------------------
With one process there is never a contender, so `pg_try_advisory_lock`
always returns true and the cycle proceeds into byte-identical code with
byte-identical arguments. The lock costs one extra pooled connection and
two trivial statements per cycle. It can also be disabled outright with
`PORTFOLIO_SNAPSHOT_CYCLE_LOCK_ENABLED=false`, which restores D030's
exact pre-Phase-38 behaviour (no lock statement is issued at all).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.core.logging import get_logger

logger = get_logger(__name__)

SNAPSHOT_LOCK_CLASSID = 1953653092
"""Namespace key: the ASCII bytes ``b"trad"`` read big-endian
(0x74726164). A fixed, positive, in-range int4 chosen to be recognisable
in `pg_locks.classid` and to make an accidental collision with another
application sharing this database vanishingly unlikely. Shared by any
future background job in this repo; the objid below is what distinguishes
them."""

SNAPSHOT_LOCK_OBJID = 1936613744
"""Object key for THIS job: the ASCII bytes ``b"snap"`` read big-endian
(0x736E6170). Together with the classid above this pair is the stable
identity of "the portfolio snapshot cycle", and it must never change
without a deliberate decision: two application versions using different
keys would not exclude each other, which is precisely the failure this
module exists to prevent."""


class SnapshotCycleLockDecision(str, Enum):  # noqa: UP042 (str mixin for log/JSON interop)
    """Why a cycle was allowed to do work, or was not. Typed and exhaustive
    for the same reason `ScheduledSnapshotStatus` and
    `MarketHoursDecision` are: "no snapshot exists for 14:00" must stay
    distinguishable in the logs from "another worker took 14:00" and from
    "the vendor was down at 14:00".
    """

    ACQUIRED = "acquired"
    """This process won the advisory lock and ran the cycle. The only value
    under which any broker is enumerated, any quote is fetched, or any row
    is written."""

    SKIPPED_LOCK_HELD = "skipped_lock_held"
    """Another process already held the lock, so this worker's cycle did
    nothing beyond the failed `pg_try_advisory_lock` call. This is an
    ordinary, expected outcome under multiple workers - NOT an error. It
    means the safety net worked."""

    LOCK_DISABLED = "lock_disabled"
    """No lock was attempted, because the lock is switched off (or no lock
    was supplied by a caller predating Phase 38). The cycle ran
    unconditionally - D030's exact behaviour. Reported distinctly from
    ACQUIRED so a log reader can tell "this worker won" from "nobody was
    checking"."""

    @property
    def should_run(self) -> bool:
        return self is not SnapshotCycleLockDecision.SKIPPED_LOCK_HELD


@dataclass(frozen=True)
class SnapshotCycleLock:
    """The cross-process mutual exclusion policy for one snapshot cycle.

    Frozen, and holds no connection of its own: the caller supplies the
    `AsyncSession` whose backend connection will own the lock, exactly as
    `MarketHoursGate` takes the instant rather than reading a clock. That
    keeps the lifetime rule explicit and visible at the call site, which
    matters here because a session-level advisory lock lives and dies with
    that connection.
    """

    enabled: bool = True
    """Default TRUE. Unlike the scheduler switch itself (fail-closed at
    false), this one's safer state is ON: "on" is the side that writes
    FEWER rows, and it is a no-op for the single-worker deployments that
    are the current norm. Set false to restore D030's unguarded
    behaviour."""

    classid: int = SNAPSHOT_LOCK_CLASSID
    objid: int = SNAPSHOT_LOCK_OBJID

    async def _try_acquire(self, session: AsyncSession) -> bool:
        acquired = await session.scalar(
            text("SELECT pg_try_advisory_lock(:classid, :objid)"),
            {"classid": self.classid, "objid": self.objid},
        )
        return bool(acquired)

    async def _release(self, session: AsyncSession) -> bool:
        released = await session.scalar(
            text("SELECT pg_advisory_unlock(:classid, :objid)"),
            {"classid": self.classid, "objid": self.objid},
        )
        return bool(released)

    @asynccontextmanager
    async def hold(self, session: AsyncSession) -> AsyncIterator[SnapshotCycleLockDecision]:
        """Yields this cycle's lock decision, releasing the lock on exit if
        (and only if) this call actually took it.

        Non-blocking by construction: `pg_try_advisory_lock` returns false
        immediately rather than queueing. A contender must skip its cycle,
        never wait for the winner - waiting would just stack workers up
        until the winner finished and then let them all run in sequence,
        writing exactly the duplicate rows this exists to prevent.

        The release runs in a `finally`, so an exception anywhere inside
        the cycle still frees the lock for the next interval rather than
        parking it until the process (and its connection) dies. Nothing is
        released when nothing was held: the disabled and lock-held paths
        never call `pg_advisory_unlock`, which matters because advisory
        unlocks are per-holder refcounted and an unmatched unlock is a
        Postgres WARNING against a lock this session does not own.

        The supplied session is deliberately never committed here. A
        session-level advisory lock belongs to the backend connection, and
        the session holds that connection for as long as its transaction is
        open; committing mid-cycle would return the connection to the pool
        and could silently hand the lock away.
        """
        if not self.enabled:
            yield SnapshotCycleLockDecision.LOCK_DISABLED
            return

        if not await self._try_acquire(session):
            # info, not warning: under multiple workers this fires on every
            # interval for every loser, by design. It is the mechanism
            # working, not a fault, and logging it at warning would train
            # operators to ignore the level that real snapshot skips use.
            logger.info(
                "portfolio_snapshot_cycle_lock_not_acquired",
                classid=self.classid,
                objid=self.objid,
                reason=(
                    "another process already holds the portfolio snapshot advisory lock, "
                    "so this worker skipped its cycle without querying brokers or the "
                    "market data vendor. Expected under multiple API workers (D047)."
                ),
            )
            yield SnapshotCycleLockDecision.SKIPPED_LOCK_HELD
            return

        try:
            yield SnapshotCycleLockDecision.ACQUIRED
        finally:
            if not await self._release(session):
                # Should be unreachable: this session demonstrably holds the
                # lock. Logged rather than raised because the cycle's real
                # work has already completed successfully at this point, and
                # because the lock is released anyway when the connection
                # closes - so failing the cycle here would turn a bookkeeping
                # surprise into a lost snapshot.
                logger.error(
                    "portfolio_snapshot_cycle_lock_release_unexpected",
                    classid=self.classid,
                    objid=self.objid,
                    detail=(
                        "pg_advisory_unlock returned false for a lock this session "
                        "acquired; the lock will still be freed when the connection "
                        "closes."
                    ),
                )
