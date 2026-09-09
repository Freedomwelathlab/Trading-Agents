"""Integration tests for the Phase 57 Monte Carlo endpoints, against a real
Postgres instance - these routes read `backtest_trades` and write
`monte_carlo_runs`, so there is no meaningful unit-level version of them.

Reuses tests/api/test_strategy_backtests.py's fixtures wholesale
(`backtest_user`, `_validated_strategy`, `_weekdays`) and, through it,
tests/api/test_admin.py's `db_session` / `api_client` / `_get_token` -
rather than redefining any of them. The one thing defined here is the BAR
SERIES: the Phase 55 module's alternating 100/105 closes yield four round
trips over its window, and this surface needs at least five before it will
resample anything, so a longer series is built below instead of stretching
that module's window (which would change what its own tests assert).

The wrapper `monte_carlo_user` exists for one reason:
`monte_carlo_runs.backtest_run_id` is ON DELETE RESTRICT, so
`backtest_user`'s teardown - which deletes the backtest runs - cannot run
until this module's `monte_carlo_runs` rows are gone. Entering it INSIDE
`backtest_user` is what puts the two teardowns in that order. The schema
forcing this is the guarantee doing its job.

The database is shared with every other test module and is not reset
between tests, so nothing here asserts on an absolute row count outside the
rows it seeded itself.
"""

import contextlib
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from apps.api.app.db.models import (
    BacktestRun,
    MarketDataBar,
    MonteCarloRun,
    Strategy,
    StrategyVersion,
)
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.store import MarketDataStore
from tests.api.test_admin import _get_token, api_client, db_session, non_admin_user
from tests.api.test_strategy_backtests import (
    _validated_strategy,
    _weekdays,
    backtest_user,
)

# Repeated four-bar blocks, each shaped `[100, 101, 105, exit]`. Against the
# SMA(2) crossover definition tests/api/test_strategy_backtests.py already
# defines, `close > sma_2` reduces to `close > previous close`, so a block
# crosses UP early and back DOWN on its last bar - exactly one complete round
# trip each. Varying only the exit close gives the seven trades seven
# different returns, which is what makes the resampled percentiles genuinely
# spread out instead of collapsing onto a single value.
#
# Every exit is BELOW its entry, deliberately, and that is a fact about the
# RISK ENGINE rather than a preference about the strategy. `risk_max_position_
# pct_of_equity` caps an order's notional at 10% of equity, and it caps the
# closing SELL as well as the opening BUY: an entry sized right up against
# that cap cannot be fully closed at a HIGHER price, because the sell's own
# notional would exceed it, and a partially closed position is not a round
# trip at all. A series of small losses therefore reliably produces seven
# CLOSED trades, where a series of gains silently produces two or three and a
# position stuck open. (This is the same reason the Phase 55 module's
# alternating 100/105 series buys the up-day and sells the down-day.)
_BLOCKS = [
    [100, 101, 105, 99],
    [100, 101, 105, 95],
    [100, 101, 105, 90],
    [100, 101, 105, 98],
    [100, 101, 105, 92],
    [100, 101, 105, 97],
    [100, 101, 105, 94],
]
# Three flat bars: SMA(2)'s warmup is `max(period) + 1 = 3` bars before the
# window's first day.
_CLOSES = [100, 100, 100] + [close for block in _BLOCKS for close in block]

FIRST_BAR_DATE = date(2026, 2, 2)
BAR_DATES = _weekdays(FIRST_BAR_DATE, date(2026, 12, 31))[: len(_CLOSES)]
START_DATE = BAR_DATES[3]
END_DATE = BAR_DATES[-1]

EXPECTED_TRADES = len(_BLOCKS)
"""One closed round trip per block - seven, comfortably above the
resampler's five-trade floor."""


def _seed_bars(symbol: str) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            bar_interval="1d",
            ts=datetime(day.year, day.month, day.day, 21, 0, tzinfo=UTC),
            close=Decimal(close),
            source="test-monte-carlo",
        )
        for day, close in zip(BAR_DATES, _CLOSES, strict=True)
    ]


@contextlib.asynccontextmanager
async def monte_carlo_user(session):
    """`backtest_user`, plus a teardown that clears this module's
    `monte_carlo_runs` rows FIRST.

    Nested rather than copied: the permissions, the user and the strategy
    cleanup are all `backtest_user`'s, and duplicating a fixture that grants
    permissions is exactly how two test modules end up disagreeing about
    what a role can do.
    """
    async with backtest_user(session) as (user_id, email):
        try:
            yield user_id, email
        finally:
            await session.rollback()
            version_ids = select(StrategyVersion.id).where(
                StrategyVersion.strategy_id.in_(
                    select(Strategy.id).where(Strategy.owner_user_id == user_id)
                )
            )
            run_ids = select(BacktestRun.id).where(
                BacktestRun.strategy_version_id.in_(version_ids)
            )
            await session.execute(
                delete(MonteCarloRun).where(MonteCarloRun.backtest_run_id.in_(run_ids))
            )
            await session.commit()


@contextlib.asynccontextmanager
async def seeded_symbol(session):
    """A unique symbol whose bars are written through MarketDataStore
    exactly as a backfill would - the backtest route reads the persisted
    store and nothing else, so there is no way to hand it a bar that was not
    really stored."""
    symbol = f"MCQA{uuid.uuid4().hex[:8].upper()}.US"
    await MarketDataStore(session).upsert_bars(_seed_bars(symbol))
    await session.commit()
    try:
        yield symbol
    finally:
        await session.rollback()
        await session.execute(delete(MarketDataBar).where(MarketDataBar.symbol == symbol))
        await session.commit()


def _backtest_body(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "bar_interval": "1d",
        "start_date": START_DATE.isoformat(),
        "end_date": END_DATE.isoformat(),
        "starting_cash": "10000",
    }


async def _succeeded_backtest(client, token, symbol) -> dict:
    """A real, persisted, SUCCEEDED backtest run with `EXPECTED_TRADES`
    closed round trips - the input every test below resamples."""
    headers = {"Authorization": f"Bearer {token}"}
    strategy_id, version_id = await _validated_strategy(client, token)
    created = await client.post(
        f"/strategies/{strategy_id}/versions/{version_id}/backtests",
        headers=headers,
        json=_backtest_body(symbol),
    )
    assert created.status_code == 201, created.text
    run = created.json()
    assert run["status"] == "succeeded", run
    assert run["num_trades"] == EXPECTED_TRADES, run["num_trades"]
    return run


@pytest.mark.asyncio
async def test_a_monte_carlo_run_persists_and_is_readable_three_ways() -> None:
    """The whole happy path in one test, because the three routes describe
    the same row and asserting they agree is the point.

    The percentile ordering (`p5 <= median <= p95`, for BOTH distributions)
    is the substantive assertion here: it holds by construction, so a
    percentile computed against the wrong sorted list - or two of them
    swapped - shows up immediately.
    """
    async with db_session() as session, monte_carlo_user(session) as (_uid, email):
        async with seeded_symbol(session) as symbol:
            async with api_client() as client:
                token = await _get_token(client, email)
                headers = {"Authorization": f"Bearer {token}"}
                backtest = await _succeeded_backtest(client, token, symbol)

                created = await client.post(
                    f"/backtest-runs/{backtest['id']}/monte-carlo",
                    headers=headers,
                    json={"num_simulations": 250},
                )
                assert created.status_code == 201, created.text
                run = created.json()

                assert run["status"] == "succeeded"
                assert run["error_detail"] is None
                assert run["completed_at"] is not None
                assert run["backtest_run_id"] == backtest["id"]
                assert run["num_simulations"] == 250
                assert run["num_trades_resampled"] == EXPECTED_TRADES
                # Generated, never client-supplied, and always persisted -
                # it is what regenerates this exact distribution later.
                assert 0 <= run["random_seed"] < 2**63

                p5_equity = Decimal(run["p5_final_equity"])
                median_equity = Decimal(run["median_final_equity"])
                p95_equity = Decimal(run["p95_final_equity"])
                assert p5_equity <= median_equity <= p95_equity
                # Seven varied returns must not collapse to one outcome.
                assert p5_equity < p95_equity

                p5_dd = Decimal(run["p5_max_drawdown_pct"])
                median_dd = Decimal(run["median_max_drawdown_pct"])
                p95_dd = Decimal(run["p95_max_drawdown_pct"])
                assert p5_dd <= median_dd <= p95_dd
                # Every one of these trades loses, so every resampled path
                # dips below its own starting equity - a real drawdown, not
                # the structural 0 an all-winners series would report.
                assert p5_dd > 0

                # These trades never take an account to zero, so the
                # computed figure is exactly 0 - not null, because this run
                # really did simulate.
                assert Decimal(run["probability_of_ruin_pct"]) == 0

                detail = await client.get(f"/monte-carlo-runs/{run['id']}", headers=headers)
                assert detail.status_code == 200
                assert detail.json() == run

                listing = await client.get(
                    f"/backtest-runs/{backtest['id']}/monte-carlo", headers=headers
                )
                assert listing.status_code == 200
                listed = listing.json()
                assert listed["limit"] == 50
                assert listed["offset"] == 0
                assert [item["id"] for item in listed["items"]] == [run["id"]]


@pytest.mark.asyncio
async def test_the_listing_is_newest_first_across_repeated_analyses() -> None:
    """A backtest run may reasonably be resampled more than once - at a
    different `num_simulations`, or simply again - so this is a real
    collection, newest first."""
    async with db_session() as session, monte_carlo_user(session) as (_uid, email):
        async with seeded_symbol(session) as symbol:
            async with api_client() as client:
                token = await _get_token(client, email)
                headers = {"Authorization": f"Bearer {token}"}
                backtest = await _succeeded_backtest(client, token, symbol)

                ids = []
                for simulations in (100, 300):
                    response = await client.post(
                        f"/backtest-runs/{backtest['id']}/monte-carlo",
                        headers=headers,
                        json={"num_simulations": simulations},
                    )
                    assert response.status_code == 201, response.text
                    ids.append(response.json()["id"])

                listing = await client.get(
                    f"/backtest-runs/{backtest['id']}/monte-carlo", headers=headers
                )
    assert [item["id"] for item in listing.json()["items"]] == list(reversed(ids))


@pytest.mark.asyncio
async def test_num_simulations_defaults_to_one_thousand() -> None:
    """An omitted body field is the documented default, not a 422 - the
    caller who has no opinion about sample size should not have to have
    one."""
    async with db_session() as session, monte_carlo_user(session) as (_uid, email):
        async with seeded_symbol(session) as symbol:
            async with api_client() as client:
                token = await _get_token(client, email)
                headers = {"Authorization": f"Bearer {token}"}
                backtest = await _succeeded_backtest(client, token, symbol)

                created = await client.post(
                    f"/backtest-runs/{backtest['id']}/monte-carlo", headers=headers, json={}
                )
    assert created.status_code == 201, created.text
    assert created.json()["num_simulations"] == 1000


@pytest.mark.asyncio
async def test_num_simulations_outside_its_range_is_422_before_anything_runs() -> None:
    """The bound lives in the request schema, which is what makes an
    accidental 100,000-simulation request a clean rejection rather than a
    very long-running one. Nothing is created for a rejected request."""
    async with db_session() as session, monte_carlo_user(session) as (_uid, email):
        async with seeded_symbol(session) as symbol:
            async with api_client() as client:
                token = await _get_token(client, email)
                headers = {"Authorization": f"Bearer {token}"}
                backtest = await _succeeded_backtest(client, token, symbol)

                too_few = await client.post(
                    f"/backtest-runs/{backtest['id']}/monte-carlo",
                    headers=headers,
                    json={"num_simulations": 99},
                )
                too_many = await client.post(
                    f"/backtest-runs/{backtest['id']}/monte-carlo",
                    headers=headers,
                    json={"num_simulations": 100_001},
                )
                listing = await client.get(
                    f"/backtest-runs/{backtest['id']}/monte-carlo", headers=headers
                )

    assert too_few.status_code == 422
    assert too_many.status_code == 422
    assert listing.json()["items"] == []


@pytest.mark.asyncio
async def test_resampling_a_backtest_that_did_not_succeed_is_409() -> None:
    """A FAILED backtest run produced no trades at all, so there is nothing
    to bootstrap from - and an empty sample would otherwise be resampled
    into a confident-looking distribution of nothing. The 409 names the
    run's actual status."""
    async with db_session() as session, monte_carlo_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, version_id = await _validated_strategy(client, token)

            # No bars exist for this symbol, so the run is persisted FAILED -
            # a 201 resource, exactly as tests/api/test_strategy_backtests.py
            # pins.
            failed = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/backtests",
                headers=headers,
                json=_backtest_body("NOSUCHSYMBOL.US"),
            )
            assert failed.status_code == 201, failed.text
            assert failed.json()["status"] == "failed"

            response = await client.post(
                f"/backtest-runs/{failed.json()['id']}/monte-carlo",
                headers=headers,
                json={"num_simulations": 100},
            )

    assert response.status_code == 409
    assert "BACKTEST_NOT_SUCCEEDED" in response.json()["detail"]
    assert "failed" in response.json()["detail"]


@pytest.mark.asyncio
async def test_another_users_backtest_run_and_monte_carlo_run_are_403() -> None:
    """Ownership is re-derived on every route, one join deeper than Phase
    55's - holding a run id is not authorization, on either resource."""
    async with db_session() as session:
        async with monte_carlo_user(session) as (_owner_id, owner_email):
            async with monte_carlo_user(session) as (_other_id, other_email):
                async with seeded_symbol(session) as symbol:
                    async with api_client() as client:
                        owner_token = await _get_token(client, owner_email)
                        other_token = await _get_token(client, other_email)
                        owner_headers = {"Authorization": f"Bearer {owner_token}"}
                        other_headers = {"Authorization": f"Bearer {other_token}"}

                        backtest = await _succeeded_backtest(client, owner_token, symbol)
                        created = await client.post(
                            f"/backtest-runs/{backtest['id']}/monte-carlo",
                            headers=owner_headers,
                            json={"num_simulations": 100},
                        )
                        assert created.status_code == 201, created.text
                        mc_run_id = created.json()["id"]

                        assert (
                            await client.post(
                                f"/backtest-runs/{backtest['id']}/monte-carlo",
                                headers=other_headers,
                                json={"num_simulations": 100},
                            )
                        ).status_code == 403
                        assert (
                            await client.get(
                                f"/backtest-runs/{backtest['id']}/monte-carlo",
                                headers=other_headers,
                            )
                        ).status_code == 403
                        assert (
                            await client.get(
                                f"/monte-carlo-runs/{mc_run_id}", headers=other_headers
                            )
                        ).status_code == 403


@pytest.mark.asyncio
async def test_unknown_ids_are_404_not_403() -> None:
    """A nonexistent id must not 403 merely because nobody can own a row
    that isn't there - the same order and codes every other Strategy Lab
    route uses."""
    async with db_session() as session, monte_carlo_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            unknown = uuid.uuid4()

            assert (
                await client.post(
                    f"/backtest-runs/{unknown}/monte-carlo",
                    headers=headers,
                    json={"num_simulations": 100},
                )
            ).status_code == 404
            assert (
                await client.get(f"/backtest-runs/{unknown}/monte-carlo", headers=headers)
            ).status_code == 404
            assert (
                await client.get(f"/monte-carlo-runs/{unknown}", headers=headers)
            ).status_code == 404


@pytest.mark.asyncio
async def test_a_user_with_no_permissions_at_all_is_403_at_the_router_level() -> None:
    """`strategy:backtest` gates this surface at the router's own
    dependency, before any handler or ownership check runs - so a caller
    without it is refused on every route, including the ones whose ids do
    not exist."""
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            unknown = uuid.uuid4()

            post = await client.post(
                f"/backtest-runs/{unknown}/monte-carlo",
                headers=headers,
                json={"num_simulations": 100},
            )
            listing = await client.get(
                f"/backtest-runs/{unknown}/monte-carlo", headers=headers
            )
            by_id = await client.get(f"/monte-carlo-runs/{unknown}", headers=headers)

    assert post.status_code == 403
    assert listing.status_code == 403
    assert by_id.status_code == 403
