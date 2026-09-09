"""Integration tests for the Phase 57 walk-forward endpoints, against a real
Postgres instance - these routes read `market_data_bars` and write
`walk_forward_runs` / `walk_forward_windows` plus one real `backtest_runs`
row (and its curve and trades) per window, so there is no meaningful
unit-level version of them. That half - the boundary arithmetic and the
aggregate math in isolation - is tests/backtesting/test_walk_forward.py's.

Reuses tests/api/test_admin.py's `db_session` / `api_client` / `_get_token`
fixtures and tests/api/test_strategy_backtests.py's `SMA2_DEFINITION`,
`_weekdays`, `_validated_strategy` and `strategy_user` rather than
redefining any of them.

Two things ARE defined here, both for concrete reasons rather than
convenience:

- `walk_forward_user`, because `backtest_user`'s teardown deletes backtest
  runs while `walk_forward_windows.backtest_run_id` is ON DELETE RESTRICT -
  the walk-forward rows have to come off first, and the schema genuinely
  refusing otherwise is that guarantee doing its job. Everything else about
  the fixture (the two permissions, the delete ordering discipline) follows
  test_strategy_backtests.py's pattern exactly.
- `seeded_symbol` over a wider date range, because that module seeds four
  weeks of bars and three windows plus each window's own indicator warmup
  needs more than that.

Nothing is patched: these tests run the real orchestrator over the real
engine over real persisted bars. They therefore assert on the SHAPE and the
STRUCTURAL guarantees of a run - three windows, contiguous inclusive dates,
aggregates consistent with the per-window backtests they were computed from
- rather than re-deriving the evaluator's crossing semantics, which
tests/backtesting/test_executor.py owns.

The database is shared with every other test module and is not reset between
tests, so nothing here asserts on an absolute row count outside the rows it
seeded itself.
"""

import contextlib
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from apps.api.app.auth.permissions import Permission
from apps.api.app.auth.security import hash_password
from apps.api.app.db.models import (
    BacktestEquityPoint,
    BacktestRun,
    BacktestTrade,
    MarketDataBar,
    Role,
    Strategy,
    StrategyVersion,
    User,
    WalkForwardRun,
    WalkForwardWindow,
)
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.store import MarketDataStore
from tests.api.test_admin import (
    TEST_PASSWORD,
    _get_token,
    api_client,
    db_session,
    non_admin_user,
)
from tests.api.test_strategies import VALID_DEFINITION, strategy_user
from tests.api.test_strategy_backtests import _validated_strategy, _weekdays

# Three 7-day windows: [03-02, 03-08], [03-09, 03-15], [03-16, 03-22]. Each
# is a full Mon-Sun week, so each contains exactly five weekday bars, and
# 03-22 is the last day of the third - no partial tail is involved here, so
# what this module tests is the whole path rather than the tail rule
# (tests/backtesting/test_walk_forward.py pins that).
OVERALL_START = date(2026, 3, 2)
OVERALL_END = date(2026, 3, 22)
WINDOW_DAYS = 7
EXPECTED_WINDOWS = [
    (date(2026, 3, 2), date(2026, 3, 8)),
    (date(2026, 3, 9), date(2026, 3, 15)),
    (date(2026, 3, 16), date(2026, 3, 22)),
]

# Bars start well before the first window so that EVERY window - including
# the first - has real history in front of it for SMA(2)'s three-bar warmup.
FIRST_BAR_DATE = date(2026, 2, 16)


def _seed_bars(symbol: str) -> list[Bar]:
    """Closes alternating 100 / 105 across every seeded weekday, exactly as
    tests/api/test_strategy_backtests.py seeds them and for the same reason:
    against SMA(2), `close > sma_2` reduces to `close > previous close`, so a
    strictly alternating series produces complete round trips in every window
    without this test having to reason about the evaluator's crossing
    logic."""
    return [
        Bar(
            symbol=symbol,
            bar_interval="1d",
            ts=datetime(day.year, day.month, day.day, 21, 0, tzinfo=UTC),
            close=Decimal(105) if index % 2 else Decimal(100),
            source="test-walk-forward",
        )
        for index, day in enumerate(_weekdays(FIRST_BAR_DATE, OVERALL_END))
    ]


@contextlib.asynccontextmanager
async def walk_forward_user(session):
    """An active user holding both `strategy:manage` and `strategy:backtest`
    - the former only because these tests have to author and validate the
    strategy they then test.

    Teardown order is dictated by the schema, not by preference:
    `walk_forward_windows.backtest_run_id` and
    `walk_forward_runs.strategy_version_id` are both ON DELETE RESTRICT, so
    windows go before backtest runs and walk-forward runs go before the
    version. Having to work around that is the guarantee doing its job.
    """
    role_id = uuid.uuid4()
    user_id = uuid.uuid4()
    email = f"{uuid.uuid4()}@example.com"
    session.add(
        Role(
            id=role_id,
            name=f"test-walkforward-{role_id}",
            permissions=[
                Permission.STRATEGY_MANAGE.value,
                Permission.STRATEGY_BACKTEST.value,
            ],
        )
    )
    session.add(
        User(
            id=user_id,
            email=email,
            hashed_password=hash_password(TEST_PASSWORD),
            is_active=True,
            role_id=role_id,
        )
    )
    await session.commit()
    try:
        yield user_id, email
    finally:
        await session.rollback()
        version_ids = select(StrategyVersion.id).where(
            StrategyVersion.strategy_id.in_(
                select(Strategy.id).where(Strategy.owner_user_id == user_id)
            )
        )
        wf_run_ids = select(WalkForwardRun.id).where(
            WalkForwardRun.strategy_version_id.in_(version_ids)
        )
        run_ids = select(BacktestRun.id).where(BacktestRun.strategy_version_id.in_(version_ids))
        await session.execute(
            delete(WalkForwardWindow).where(
                WalkForwardWindow.walk_forward_run_id.in_(wf_run_ids)
            )
        )
        await session.execute(
            delete(WalkForwardRun).where(WalkForwardRun.strategy_version_id.in_(version_ids))
        )
        await session.execute(
            delete(BacktestTrade).where(BacktestTrade.backtest_run_id.in_(run_ids))
        )
        await session.execute(
            delete(BacktestEquityPoint).where(BacktestEquityPoint.backtest_run_id.in_(run_ids))
        )
        await session.execute(
            delete(BacktestRun).where(BacktestRun.strategy_version_id.in_(version_ids))
        )
        await session.execute(delete(Strategy).where(Strategy.owner_user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.execute(delete(Role).where(Role.id == role_id))
        await session.commit()


@contextlib.asynccontextmanager
async def seeded_symbol(session):
    """A unique symbol with real bars in `market_data_bars`, written through
    MarketDataStore exactly as a backfill would - the routes read the
    persisted store and nothing else, so there is no way to hand them a bar
    that was not really stored."""
    symbol = f"WFQA{uuid.uuid4().hex[:8].upper()}.US"
    await MarketDataStore(session).upsert_bars(_seed_bars(symbol))
    await session.commit()
    try:
        yield symbol
    finally:
        await session.rollback()
        await session.execute(delete(MarketDataBar).where(MarketDataBar.symbol == symbol))
        await session.commit()


def _body(symbol: str, **overrides) -> dict:
    return {
        "symbol": symbol,
        "bar_interval": "1d",
        "overall_start_date": OVERALL_START.isoformat(),
        "overall_end_date": OVERALL_END.isoformat(),
        "window_days": WINDOW_DAYS,
        "starting_cash": "10000",
        **overrides,
    }


@pytest.mark.asyncio
async def test_holding_strategy_manage_without_strategy_backtest_is_403() -> None:
    """The walk-forward surface is gated on `strategy:backtest`, the same
    permission the persisted-backtest surface uses - so `strategy_user`,
    which holds only `strategy:manage`, is refused at the router's own
    dependency on all three routes."""
    async with db_session() as session, strategy_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, version_id = await _validated_strategy(client, token)

            created = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/walk-forward",
                headers=headers,
                json=_body("ANY.US"),
            )
            listing = await client.get(
                f"/strategies/{strategy_id}/versions/{version_id}/walk-forward",
                headers=headers,
            )
            by_id = await client.get(f"/walk-forward-runs/{uuid.uuid4()}", headers=headers)

    assert created.status_code == 403
    assert listing.status_code == 403
    assert by_id.status_code == 403


@pytest.mark.asyncio
async def test_a_user_with_no_permissions_at_all_is_403_at_the_router_level() -> None:
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.get(
                f"/walk-forward-runs/{uuid.uuid4()}",
                headers={"Authorization": f"Bearer {token}"},
            )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_a_walk_forward_run_persists_and_is_readable_three_ways() -> None:
    """The whole happy path in one test, because the three routes describe
    the same row and asserting they agree is the point.

    Every number asserted here is checked against the WINDOW BACKTESTS it
    was computed from - each window's own `GET /backtest-runs/{id}` - rather
    than against a hard-coded figure, so this test cannot drift out of
    agreement with the engine it aggregates.
    """
    async with db_session() as session, walk_forward_user(session) as (_uid, email):
        async with seeded_symbol(session) as symbol:
            async with api_client() as client:
                token = await _get_token(client, email)
                headers = {"Authorization": f"Bearer {token}"}
                strategy_id, version_id = await _validated_strategy(client, token)

                created = await client.post(
                    f"/strategies/{strategy_id}/versions/{version_id}/walk-forward",
                    headers=headers,
                    json=_body(symbol),
                )
                assert created.status_code == 201, created.text
                run = created.json()

                assert run["status"] == "succeeded"
                assert run["error_detail"] is None
                assert run["completed_at"] is not None
                assert run["strategy_version_id"] == version_id
                assert run["symbol"] == symbol
                assert run["bar_interval"] == "1d"
                assert run["window_days"] == WINDOW_DAYS
                assert Decimal(run["starting_cash"]) == Decimal(10_000)

                # Three sequential, non-overlapping, inclusive windows, in
                # index order, none extending past overall_end_date.
                assert [
                    (window["start_date"], window["end_date"]) for window in run["windows"]
                ] == [(start.isoformat(), end.isoformat()) for start, end in EXPECTED_WINDOWS]
                assert [window["window_index"] for window in run["windows"]] == [0, 1, 2]

                assert run["num_windows"] == 3
                assert run["num_succeeded_windows"] == 3
                assert run["mean_return_pct"] is not None
                assert run["stddev_return_pct"] is not None

                # Each window points at a REAL backtest run, fetchable on its
                # own - which is why the walk-forward response does not
                # inline any equity curve.
                window_returns = []
                for window in run["windows"]:
                    detail = await client.get(
                        f"/backtest-runs/{window['backtest_run_id']}", headers=headers
                    )
                    assert detail.status_code == 200, detail.text
                    window_run = detail.json()
                    assert window_run["status"] == "succeeded"
                    assert window_run["start_date"] == window["start_date"]
                    assert window_run["end_date"] == window["end_date"]
                    # Every window started fresh at the same cash - they do
                    # not compound through a running balance.
                    assert Decimal(window_run["starting_cash"]) == Decimal(10_000)
                    assert window_run["equity_curve"]
                    window_returns.append(Decimal(window_run["total_return_pct"]))

                # The aggregates really are aggregates OF those windows.
                assert Decimal(run["best_window_return_pct"]) == max(window_returns)
                assert Decimal(run["worst_window_return_pct"]) == min(window_returns)
                assert run["num_profitable_windows"] == sum(
                    1 for value in window_returns if value > 0
                )
                mean = sum(window_returns) / len(window_returns)
                assert abs(Decimal(run["mean_return_pct"]) - mean) <= Decimal("0.0001")

                detail = await client.get(f"/walk-forward-runs/{run['id']}", headers=headers)
                assert detail.status_code == 200
                assert detail.json() == run

                listing = await client.get(
                    f"/strategies/{strategy_id}/versions/{version_id}/walk-forward",
                    headers=headers,
                )
                assert listing.status_code == 200
                listed = listing.json()
                assert listed["limit"] == 50
                assert listed["offset"] == 0
                assert [item["id"] for item in listed["items"]] == [run["id"]]
                # A summary deliberately carries no window breakdown.
                assert "windows" not in listed["items"][0]


@pytest.mark.asyncio
async def test_a_range_too_narrow_for_two_windows_is_a_persisted_failed_run() -> None:
    """One window says nothing about consistency, so this fails - but as a
    real, readable resource naming the actual count, not as a 4xx that loses
    the request."""
    async with db_session() as session, walk_forward_user(session) as (_uid, email):
        async with seeded_symbol(session) as symbol:
            async with api_client() as client:
                token = await _get_token(client, email)
                headers = {"Authorization": f"Bearer {token}"}
                strategy_id, version_id = await _validated_strategy(client, token)

                created = await client.post(
                    f"/strategies/{strategy_id}/versions/{version_id}/walk-forward",
                    headers=headers,
                    json=_body(symbol, window_days=60),
                )
                assert created.status_code == 201, created.text
                run = created.json()

                assert run["status"] == "failed"
                assert "INSUFFICIENT_WINDOWS" in run["error_detail"]
                assert run["windows"] == []
                # Never a fabricated statistic for a run that computed none.
                assert run["num_windows"] is None
                assert run["mean_return_pct"] is None
                assert run["stddev_return_pct"] is None

                reread = await client.get(f"/walk-forward-runs/{run['id']}", headers=headers)
                assert reread.status_code == 200
                assert reread.json()["status"] == "failed"


@pytest.mark.asyncio
async def test_a_symbol_with_no_bars_fails_with_every_window_recorded() -> None:
    """Every window's backtest fails on a data gap, so there is nothing to
    aggregate - but each attempted window is still recorded, pointing at its
    own real FAILED backtest run, rather than the gap being smoothed over."""
    async with db_session() as session, walk_forward_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, version_id = await _validated_strategy(client, token)

            created = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/walk-forward",
                headers=headers,
                json=_body("NOSUCHSYMBOL.US"),
            )
            assert created.status_code == 201, created.text
            run = created.json()

            assert run["status"] == "failed"
            assert "ALL_WINDOWS_FAILED" in run["error_detail"]
            assert run["num_windows"] == 3
            assert run["num_succeeded_windows"] == 0
            assert run["mean_return_pct"] is None
            assert len(run["windows"]) == 3

            for window in run["windows"]:
                detail = await client.get(
                    f"/backtest-runs/{window['backtest_run_id']}", headers=headers
                )
                assert detail.status_code == 200
                assert detail.json()["status"] == "failed"


@pytest.mark.asyncio
async def test_walk_forward_on_a_draft_version_is_409_and_names_the_validate_route() -> None:
    async with db_session() as session, walk_forward_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            created = await client.post(
                "/strategies",
                headers=headers,
                json={"name": "Still a draft", "definition": VALID_DEFINITION},
            )
            assert created.status_code == 201
            strategy_id = created.json()["id"]
            version_id = created.json()["latest_version"]["id"]

            response = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/walk-forward",
                headers=headers,
                json=_body("ANY.US"),
            )

    assert response.status_code == 409
    assert "VERSION_NOT_VALIDATED" in response.json()["detail"]
    assert (
        f"/strategies/{strategy_id}/versions/{version_id}/validate"
        in response.json()["detail"]
    )


@pytest.mark.asyncio
async def test_another_users_strategy_and_run_are_403() -> None:
    """Ownership is re-derived on every route, including on
    `GET /walk-forward-runs/{id}` - holding a run id is not authorization."""
    async with db_session() as session:
        async with walk_forward_user(session) as (_owner_id, owner_email):
            async with walk_forward_user(session) as (_other_id, other_email):
                async with seeded_symbol(session) as symbol:
                    async with api_client() as client:
                        owner_token = await _get_token(client, owner_email)
                        other_token = await _get_token(client, other_email)
                        other_headers = {"Authorization": f"Bearer {other_token}"}

                        strategy_id, version_id = await _validated_strategy(
                            client, owner_token
                        )
                        created = await client.post(
                            f"/strategies/{strategy_id}/versions/{version_id}/walk-forward",
                            headers={"Authorization": f"Bearer {owner_token}"},
                            json=_body(symbol),
                        )
                        assert created.status_code == 201, created.text
                        run_id = created.json()["id"]

                        assert (
                            await client.post(
                                f"/strategies/{strategy_id}/versions/{version_id}"
                                "/walk-forward",
                                headers=other_headers,
                                json=_body(symbol),
                            )
                        ).status_code == 403
                        assert (
                            await client.get(
                                f"/strategies/{strategy_id}/versions/{version_id}"
                                "/walk-forward",
                                headers=other_headers,
                            )
                        ).status_code == 403
                        assert (
                            await client.get(
                                f"/walk-forward-runs/{run_id}", headers=other_headers
                            )
                        ).status_code == 403


@pytest.mark.asyncio
async def test_unknown_ids_are_404_not_403() -> None:
    """A nonexistent id must not 403 merely because nobody can own a row that
    isn't there - the same order and codes `_load_owned_strategy` uses, which
    is exactly why that helper is imported rather than reimplemented."""
    async with db_session() as session, walk_forward_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, _version_id = await _validated_strategy(client, token)
            unknown = uuid.uuid4()

            assert (
                await client.get(f"/walk-forward-runs/{unknown}", headers=headers)
            ).status_code == 404
            assert (
                await client.post(
                    f"/strategies/{unknown}/versions/{unknown}/walk-forward",
                    headers=headers,
                    json=_body("ANY.US"),
                )
            ).status_code == 404
            # The strategy exists and is the caller's; the VERSION does not.
            assert (
                await client.post(
                    f"/strategies/{strategy_id}/versions/{unknown}/walk-forward",
                    headers=headers,
                    json=_body("ANY.US"),
                )
            ).status_code == 404
            assert (
                await client.get(
                    f"/strategies/{strategy_id}/versions/{unknown}/walk-forward",
                    headers=headers,
                )
            ).status_code == 404


@pytest.mark.asyncio
async def test_a_malformed_request_is_422_before_anything_runs() -> None:
    """An overall range that does not move forward, and a non-positive
    `window_days`, are both malformed requests rather than empty results -
    and neither creates a row."""
    async with db_session() as session, walk_forward_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, version_id = await _validated_strategy(client, token)
            url = f"/strategies/{strategy_id}/versions/{version_id}/walk-forward"

            backwards = await client.post(
                url,
                headers=headers,
                json=_body("ANY.US", overall_end_date=OVERALL_START.isoformat()),
            )
            assert backwards.status_code == 422

            zero_days = await client.post(
                url, headers=headers, json=_body("ANY.US", window_days=0)
            )
            assert zero_days.status_code == 422

            listing = await client.get(url, headers=headers)
            assert listing.json()["items"] == []
