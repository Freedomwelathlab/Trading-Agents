"""Unit tests for the snapshot cycle lock's contract (Phase 38, D047) -
the parts that can be proven without a database: the key constants, the
decision enum, and the `hold()` context manager's release discipline
(release exactly when acquired, never otherwise, including on an
exception).

The half that actually matters - that a lock held by a *second real
Postgres connection* makes a real cycle skip and write nothing - is
necessarily DB-backed and lives in
tests/api/test_snapshot_scheduler_multiworker.py. Nothing here claims to
have proven cross-process exclusion; these tests prove the plumbing that
carries it.

The only double used here is a recording stand-in for the SQLAlchemy
session, so that "what SQL did it issue, and in what order" is directly
assertable. Every DB-backed test uses a real session against real
Postgres.
"""

import pytest

from apps.api.app.portfolio.cycle_lock import (
    SNAPSHOT_LOCK_CLASSID,
    SNAPSHOT_LOCK_OBJID,
    SnapshotCycleLock,
    SnapshotCycleLockDecision,
)


class RecordingSession:
    """Stands in for AsyncSession.scalar() only, recording the statement
    text and parameters of every call so the release discipline is
    observable. Returns queued results in order."""

    def __init__(self, results: list[bool]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, dict[str, int]]] = []

    async def scalar(self, statement, params=None):  # type: ignore[no-untyped-def]
        self.calls.append((str(statement), dict(params or {})))
        return self._results.pop(0)

    @property
    def functions(self) -> list[str]:
        """Just the pg function each call invoked, which is what the
        release discipline is really about."""
        names = []
        for sql, _ in self.calls:
            if "pg_try_advisory_lock" in sql:
                names.append("pg_try_advisory_lock")
            elif "pg_advisory_unlock" in sql:
                names.append("pg_advisory_unlock")
            else:  # pragma: no cover - would mean an unexpected statement
                names.append(sql)
        return names


def test_the_lock_keys_are_the_documented_ascii_constants():
    """These two numbers are the stable cross-process identity of "the
    portfolio snapshot cycle". Two app versions disagreeing about them
    would not exclude each other at all - the exact bug D047 exists to
    prevent - so they are pinned here rather than left to drift, along
    with the ASCII derivation the module docstring claims for them."""
    assert SNAPSHOT_LOCK_CLASSID == int.from_bytes(b"trad", "big") == 1953653092
    assert SNAPSHOT_LOCK_OBJID == int.from_bytes(b"snap", "big") == 1936613744
    # Both must fit a signed int4, or Postgres rejects the two-key form.
    for key in (SNAPSHOT_LOCK_CLASSID, SNAPSHOT_LOCK_OBJID):
        assert 0 < key <= 2**31 - 1

    assert SnapshotCycleLock().classid == SNAPSHOT_LOCK_CLASSID
    assert SnapshotCycleLock().objid == SNAPSHOT_LOCK_OBJID


def test_only_the_lock_held_decision_stops_a_cycle():
    """A "skip" and a "ran without checking" must never be conflated: the
    first means a sibling worker filled this interval, the second means
    nobody was looking."""
    assert SnapshotCycleLockDecision.ACQUIRED.should_run is True
    assert SnapshotCycleLockDecision.LOCK_DISABLED.should_run is True
    assert SnapshotCycleLockDecision.SKIPPED_LOCK_HELD.should_run is False


def test_the_lock_defaults_to_enabled():
    """Unlike the scheduler switch itself, the safe default here is ON -
    'on' is the side that writes fewer rows into an append-only table."""
    assert SnapshotCycleLock().enabled is True


@pytest.mark.asyncio
async def test_acquiring_yields_acquired_and_releases_exactly_once():
    session = RecordingSession([True, True])
    lock = SnapshotCycleLock()

    async with lock.hold(session) as decision:  # type: ignore[arg-type]
        assert decision is SnapshotCycleLockDecision.ACQUIRED
        # Still held while the body runs - the release must not be eager.
        assert session.functions == ["pg_try_advisory_lock"]

    assert session.functions == ["pg_try_advisory_lock", "pg_advisory_unlock"]
    # Both statements must name the same key pair, or the unlock frees
    # nothing and the next cycle deadlocks itself out forever.
    assert session.calls[0][1] == session.calls[1][1]
    assert session.calls[0][1] == {
        "classid": SNAPSHOT_LOCK_CLASSID,
        "objid": SNAPSHOT_LOCK_OBJID,
    }


@pytest.mark.asyncio
async def test_the_lock_is_released_even_when_the_cycle_raises():
    """The whole point of the try/finally: one exploding cycle must not
    park the lock until the process dies, or every later interval - in
    every worker - silently stops recording history."""
    session = RecordingSession([True, True])

    with pytest.raises(RuntimeError, match="cycle blew up"):
        async with SnapshotCycleLock().hold(session) as decision:  # type: ignore[arg-type]
            assert decision is SnapshotCycleLockDecision.ACQUIRED
            raise RuntimeError("cycle blew up")

    assert session.functions == ["pg_try_advisory_lock", "pg_advisory_unlock"]


@pytest.mark.asyncio
async def test_a_contended_lock_yields_skipped_and_never_unlocks():
    """An unmatched pg_advisory_unlock is a Postgres WARNING against a lock
    this session does not hold - and worse, if the refcount were ever
    shared it would release someone else's work. A loser must release
    nothing."""
    session = RecordingSession([False])

    async with SnapshotCycleLock().hold(session) as decision:  # type: ignore[arg-type]
        assert decision is SnapshotCycleLockDecision.SKIPPED_LOCK_HELD

    assert session.functions == ["pg_try_advisory_lock"]


@pytest.mark.asyncio
async def test_a_disabled_lock_issues_no_sql_at_all():
    """Disabling must restore D030's behaviour exactly, not merely make the
    lock always succeed: no statement, no extra round-trip."""
    session = RecordingSession([])

    async with SnapshotCycleLock(enabled=False).hold(session) as decision:  # type: ignore[arg-type]
        assert decision is SnapshotCycleLockDecision.LOCK_DISABLED

    assert session.calls == []


@pytest.mark.asyncio
async def test_a_false_release_is_logged_rather_than_raised():
    """The cycle's real work is already done by then; failing here would
    turn a bookkeeping surprise into a lost snapshot."""
    session = RecordingSession([True, False])

    async with SnapshotCycleLock().hold(session) as decision:  # type: ignore[arg-type]
        assert decision is SnapshotCycleLockDecision.ACQUIRED

    assert session.functions == ["pg_try_advisory_lock", "pg_advisory_unlock"]
