"""Phase 68 (D086): one real, capped-load test of the universe-scan
orchestrator at its own documented maximum, `MAX_SCAN_SYMBOLS` (50), against
real seeded Postgres data through the real HTTP API - the same integration
pattern `tests/api/test_universe_scan.py` already uses for smaller
universes (imported and reused here rather than duplicated), just exercised
at the actual cap this codebase enforces rather than at 2-3 symbols.

**What this test is, precisely.** ONE synchronous, single-process HTTP
request that runs 50 real, independent, single-symbol backtests in a
for-loop inside `run_universe_scan`
(`apps/api/app/backtesting/universe_scan.py`), exactly as
`POST /strategies/{id}/versions/{id}/universe-scans` runs it in production
- same risk engine, same portfolio manager, same paper-broker fill math,
same persisted bar store as every other backtest in this codebase - and
asserts the whole request completes within a generous wall-clock bound.

**What this test is explicitly NOT: a concurrent-request load test.** This
codebase has no async task queue or worker pool for a universe scan to run
on. `MAX_SCAN_SYMBOLS`'s own docstring
(`apps/api/app/backtesting/universe_scan.py`) states the design constraint
directly: "A universe scan runs synchronously in-request, one backtest per
symbol... A larger universe is a real need, but it needs a job runner this
codebase does not have yet." `apps/api/app/deployments/service.py`'s
`MAX_DEPLOYMENT_SYMBOLS` docstring documents the identical constraint for a
sibling cap on the deployment runner. There is therefore nothing to
load-test CONCURRENTLY here - one worker process handles one request at a
time either way - so this test is a single request, run once, timed: a
capacity check on the documented synchronous cap, not a throughput or
concurrency benchmark. A future job-runner-backed universe scan (if this
system ever grows one) would need its own, different load test.

**The bound: 60 seconds.** Fifty modest-window (roughly two weeks of daily
bars) backtests is pure in-memory Decimal arithmetic and simulated paper
fills against an already-seeded Postgres store - there is no live vendor
call anywhere in the loop. Sixty seconds is deliberately generous headroom
over what a healthy run actually takes on ordinary test hardware, matching
this codebase's own posture elsewhere (`test_fuzz.py`'s `< 5` seconds for a
few thousand in-memory bar evaluations is the same "generous bound, not a
tight benchmark" idea one order of magnitude down); it is not a target to
approach; a run taking anywhere near it would itself be worth investigating
separately from this test's job, which is only to catch a regression that
made the cap itself impractical to use.
"""

import time

import pytest

from apps.api.app.backtesting.universe_scan import MAX_SCAN_SYMBOLS
from tests.api.test_admin import _get_token, api_client, db_session
from tests.api.test_strategy_backtests import _validated_strategy
from tests.api.test_universe_scan import _body, seeded_universe, universe_scan_user

_WALL_CLOCK_BOUND_SECONDS = 60.0


@pytest.mark.asyncio
async def test_a_universe_scan_at_the_documented_cap_completes_within_a_generous_bound() -> None:
    """Runs one real universe scan over exactly `MAX_SCAN_SYMBOLS` (50) real
    seeded symbols through the real HTTP API and real Postgres, and asserts
    it completes well inside a generous wall-clock bound. See this module's
    docstring for exactly what this test is, and - just as importantly -
    what it is not.
    """
    async with (
        db_session() as session,
        universe_scan_user(session) as (_uid, email),
        seeded_universe(session, count=MAX_SCAN_SYMBOLS) as symbols,
    ):
        assert len(symbols) == MAX_SCAN_SYMBOLS

        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, version_id = await _validated_strategy(client, token)

            started = time.monotonic()
            response = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/universe-scans",
                headers=headers,
                json=_body(symbols),
            )
            elapsed = time.monotonic() - started

    assert response.status_code == 201, response.text
    body = response.json()
    # A real, completed scan over the full requested universe - not a
    # truncated or partially-run one - is what this timing measurement is
    # actually of.
    assert body["num_symbols"] == MAX_SCAN_SYMBOLS
    assert body["status"] == "succeeded"
    assert elapsed < _WALL_CLOCK_BOUND_SECONDS, (
        f"scanning {MAX_SCAN_SYMBOLS} symbols took {elapsed:.2f}s, "
        f"expected well under {_WALL_CLOCK_BOUND_SECONDS:.0f}s"
    )


@pytest.mark.asyncio
async def test_max_scan_symbols_plus_one_is_refused_before_any_work_starts() -> None:
    """The boundary this load test's premise depends on: `MAX_SCAN_SYMBOLS`
    really is enforced, so a request for one symbol more is refused 422
    before a single backtest runs, rather than the "cap" being purely
    documentation that a caller could exceed by simply asking. Without this,
    the test above would only prove that 50 symbols happens to be fast -
    not that 50 is actually where this codebase draws its own synchronous
    line.
    """
    async with (
        db_session() as session,
        universe_scan_user(session) as (_uid, email),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, version_id = await _validated_strategy(client, token)

            over_cap_symbols = [f"OVERCAP{i}.US" for i in range(MAX_SCAN_SYMBOLS + 1)]
            response = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/universe-scans",
                headers=headers,
                json=_body(over_cap_symbols),
            )

    assert response.status_code == 422, response.text
