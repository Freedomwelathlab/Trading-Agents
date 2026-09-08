"""Integration tests for the Phase 55 backtest endpoints, against a real
Postgres instance - these routes read `market_data_bars` and write
`backtest_runs` / `backtest_equity_points` / `backtest_trades`, so there is
no meaningful unit-level version of them.

Reuses `db_session` / `api_client` / `_get_token` from tests/api/test_admin.py
and `strategy_user` / `VALID_DEFINITION` from tests/api/test_strategies.py
rather than redefining them, exactly as test_strategies.py itself reuses the
admin fixtures. The one fixture defined here is `backtest_user`, because no
existing fixture grants `Permission.STRATEGY_BACKTEST` - and `strategy_user`,
which grants only `strategy:manage`, is precisely what proves these two
permissions really are separate rather than one implying the other.

Unlike tests/backtesting/test_engine_v2.py, NOTHING is patched here: these
tests run the real signal evaluator over real persisted bars. They therefore
assert on the SHAPE and the STRUCTURAL guarantees of a run (it succeeded, it
has one equity point per seeded bar in the window, its trades are closed
round trips) rather than re-deriving the evaluator's exact crossing
semantics, which tests/backtesting/test_executor.py owns.

The database is shared with every other test module and is not reset between
tests, so nothing here asserts on an absolute row count outside the rows it
seeded itself.
"""

import contextlib
import uuid
from datetime import UTC, date, datetime, timedelta
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

# SMA(2) rather than Phase 54's SMA(20) example, so three warmup bars are
# enough and the seeded history stays small and hand-checkable. Still the
# same crossover shape, and still `validate_definition`-clean.
SMA2_DEFINITION = {
    "indicators": [{"id": "sma_2", "type": "sma", "period": 2}],
    "entry_rule": {"op": "crosses_above", "left": "close", "right": "sma_2"},
    "exit_rule": {"op": "crosses_below", "left": "close", "right": "sma_2"},
    "position_sizing": {"type": "all_in"},
}

FIRST_BAR_DATE = date(2026, 2, 2)
START_DATE = date(2026, 2, 16)
END_DATE = date(2026, 2, 27)


def _weekdays(first: date, last: date) -> list[date]:
    """Mon-Fri dates in [first, last]. A best-effort trading calendar that
    does not know about market holidays - which is fine precisely because
    these are the days bars are SEEDED on, so whatever this produces is
    exactly what exists, gaps included."""
    days, current = [], first
    while current <= last:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def _seed_bars(symbol: str) -> list[Bar]:
    """Closes alternating 100 / 105 across every seeded weekday.

    Against SMA(2), `close > sma_2` reduces to `close > previous close`, so
    a strictly alternating series makes the entry rule fire on every up-day
    and the exit rule on every down-day - a series guaranteed to produce
    several complete round trips without this test having to reason about
    the evaluator's crossing logic itself.
    """
    return [
        Bar(
            symbol=symbol,
            bar_interval="1d",
            ts=datetime(day.year, day.month, day.day, 21, 0, tzinfo=UTC),
            close=Decimal(105) if index % 2 else Decimal(100),
            source="test-strategy-backtests",
        )
        for index, day in enumerate(_weekdays(FIRST_BAR_DATE, END_DATE))
    ]


WINDOW_DATES = _weekdays(START_DATE, END_DATE)


@contextlib.asynccontextmanager
async def backtest_user(session):
    """An active user holding BOTH `strategy:manage` and
    `strategy:backtest` - the former only because these tests have to
    author and validate the strategy they then backtest. The separation of
    the two permissions is proved by `strategy_user` (manage only) being
    refused below.

    Teardown deletes backtest rows BEFORE the strategy, because
    `backtest_runs.strategy_version_id` is ON DELETE RESTRICT - the schema
    genuinely refuses to let a run's input disappear, and this fixture
    having to work around that is the guarantee doing its job.
    """
    role_id = uuid.uuid4()
    user_id = uuid.uuid4()
    email = f"{uuid.uuid4()}@example.com"
    session.add(
        Role(
            id=role_id,
            name=f"test-backtester-{role_id}",
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
        run_ids = select(BacktestRun.id).where(
            BacktestRun.strategy_version_id.in_(version_ids)
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
    """A unique symbol with real (synthetic-but-really-ingested) bars in
    `market_data_bars`, written through MarketDataStore exactly as a
    backfill would - the routes read the persisted store and nothing else,
    so there is no way to hand them a bar that was not really stored."""
    symbol = f"BTQA{uuid.uuid4().hex[:8].upper()}.US"
    await MarketDataStore(session).upsert_bars(_seed_bars(symbol))
    await session.commit()
    try:
        yield symbol
    finally:
        await session.rollback()
        await session.execute(delete(MarketDataBar).where(MarketDataBar.symbol == symbol))
        await session.commit()


async def _validated_strategy(client, token, *, definition=None) -> tuple[str, str]:
    """Create a strategy, replace its draft version 1 with `definition`, and
    validate it. Returns `(strategy_id, version_id)`."""
    headers = {"Authorization": f"Bearer {token}"}
    created = await client.post(
        "/strategies",
        headers=headers,
        json={
            "name": "Backtested strategy",
            "description": "created by tests/api/test_strategy_backtests.py",
            "definition": SMA2_DEFINITION if definition is None else definition,
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    strategy_id, version_id = body["id"], body["latest_version"]["id"]

    validated = await client.post(
        f"/strategies/{strategy_id}/versions/{version_id}/validate", headers=headers
    )
    assert validated.status_code == 200, validated.text
    return strategy_id, version_id


def _backtest_body(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "bar_interval": "1d",
        "start_date": START_DATE.isoformat(),
        "end_date": END_DATE.isoformat(),
        "starting_cash": "10000",
    }


@pytest.mark.asyncio
async def test_holding_strategy_manage_without_strategy_backtest_is_403() -> None:
    """The two permissions really are separate. `strategy_user` can create
    and validate a strategy (it holds `strategy:manage`) and is still
    refused at the backtest router's own dependency - so granting one never
    silently grants the other."""
    async with db_session() as session, strategy_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, version_id = await _validated_strategy(client, token)

            run = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/backtests",
                headers=headers,
                json=_backtest_body("ANY.US"),
            )
            listing = await client.get(
                f"/strategies/{strategy_id}/versions/{version_id}/backtests", headers=headers
            )
            by_id = await client.get(f"/backtest-runs/{uuid.uuid4()}", headers=headers)

    assert run.status_code == 403
    assert listing.status_code == 403
    assert by_id.status_code == 403


@pytest.mark.asyncio
async def test_a_user_with_no_permissions_at_all_is_403_at_the_router_level() -> None:
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.get(
                f"/backtest-runs/{uuid.uuid4()}",
                headers={"Authorization": f"Bearer {token}"},
            )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_a_backtest_runs_persists_and_is_readable_three_ways() -> None:
    """The whole happy path in one test, because the three routes describe
    the same row and asserting they agree is the point.

    The POST returns the run IN FULL, `GET /backtest-runs/{id}` returns the
    same run in full, and the version's listing returns it as a summary with
    no curve and no trades attached.
    """
    async with db_session() as session, backtest_user(session) as (_uid, email):
        async with seeded_symbol(session) as symbol:
            async with api_client() as client:
                token = await _get_token(client, email)
                headers = {"Authorization": f"Bearer {token}"}
                strategy_id, version_id = await _validated_strategy(client, token)

                created = await client.post(
                    f"/strategies/{strategy_id}/versions/{version_id}/backtests",
                    headers=headers,
                    json=_backtest_body(symbol),
                )
                assert created.status_code == 201, created.text
                run = created.json()

                assert run["status"] == "succeeded"
                assert run["error_detail"] is None
                assert run["completed_at"] is not None
                assert run["strategy_version_id"] == version_id
                assert run["symbol"] == symbol
                assert run["bar_interval"] == "1d"
                assert Decimal(run["starting_cash"]) == Decimal(10_000)
                assert run["final_equity"] is not None
                assert run["total_return_pct"] is not None
                assert run["max_drawdown_pct"] is not None
                assert run["win_rate_pct"] is not None

                # One equity point per SEEDED BAR in the window - not per
                # calendar day. The weekend between 02-20 and 02-23 has no
                # bar and therefore no point; it is never interpolated.
                assert [point["date"] for point in run["equity_curve"]] == [
                    day.isoformat() for day in WINDOW_DATES
                ]

                # Real trades from the real evaluator over the alternating
                # series - every one a CLOSED long round trip.
                assert run["num_trades"] == len(run["trades"])
                assert run["num_trades"] >= 1
                for trade in run["trades"]:
                    assert trade["side"] == "buy"
                    assert trade["exit_date"] is not None
                    assert trade["exit_price"] is not None
                    assert trade["return_pct"] is not None
                    assert Decimal(trade["quantity"]) > 0

                detail = await client.get(f"/backtest-runs/{run['id']}", headers=headers)
                assert detail.status_code == 200
                assert detail.json() == run

                listing = await client.get(
                    f"/strategies/{strategy_id}/versions/{version_id}/backtests",
                    headers=headers,
                )
                assert listing.status_code == 200
                listed = listing.json()
                assert listed["limit"] == 50
                assert listed["offset"] == 0
                assert [item["id"] for item in listed["items"]] == [run["id"]]
                # A summary deliberately carries neither of the heavy fields.
                assert "equity_curve" not in listed["items"][0]
                assert "trades" not in listed["items"][0]


@pytest.mark.asyncio
async def test_the_listing_is_newest_first() -> None:
    async with db_session() as session, backtest_user(session) as (_uid, email):
        async with seeded_symbol(session) as symbol:
            async with api_client() as client:
                token = await _get_token(client, email)
                headers = {"Authorization": f"Bearer {token}"}
                strategy_id, version_id = await _validated_strategy(client, token)

                ids = []
                for _ in range(2):
                    response = await client.post(
                        f"/strategies/{strategy_id}/versions/{version_id}/backtests",
                        headers=headers,
                        json=_backtest_body(symbol),
                    )
                    assert response.status_code == 201, response.text
                    ids.append(response.json()["id"])

                listing = await client.get(
                    f"/strategies/{strategy_id}/versions/{version_id}/backtests",
                    headers=headers,
                )
    assert [item["id"] for item in listing.json()["items"]] == list(reversed(ids))


@pytest.mark.asyncio
async def test_a_missing_bar_window_is_a_persisted_failed_run_not_a_400() -> None:
    """A data gap does not lose the request. The run is created, finished as
    FAILED with the real reason, returned as a 201 resource, and stays
    readable afterwards - the deliberate difference from D025's
    `POST /backtests`, which has nothing to show for a failed attempt and so
    answers 400."""
    async with db_session() as session, backtest_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, version_id = await _validated_strategy(client, token)

            created = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/backtests",
                headers=headers,
                json=_backtest_body("NOSUCHSYMBOL.US"),
            )
            assert created.status_code == 201, created.text
            run = created.json()

            assert run["status"] == "failed"
            assert run["error_detail"]
            # Never a fabricated 0% return for a window that has no data.
            assert run["final_equity"] is None
            assert run["total_return_pct"] is None
            assert run["num_trades"] is None
            assert run["equity_curve"] == []
            assert run["trades"] == []

            reread = await client.get(f"/backtest-runs/{run['id']}", headers=headers)
            assert reread.status_code == 200
            assert reread.json()["status"] == "failed"


@pytest.mark.asyncio
async def test_backtesting_a_draft_version_is_409_and_names_the_validate_route() -> None:
    """A draft's definition is half-written by design (Phase 54's editing
    rules depend on it), so running one would mean evaluating rules nothing
    ever checked were expressible."""
    async with db_session() as session, backtest_user(session) as (_uid, email):
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
                f"/strategies/{strategy_id}/versions/{version_id}/backtests",
                headers=headers,
                json=_backtest_body("ANY.US"),
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
    `GET /backtest-runs/{id}` - holding a run id is not authorization."""
    async with db_session() as session:
        async with backtest_user(session) as (_owner_id, owner_email):
            async with backtest_user(session) as (_other_id, other_email):
                async with seeded_symbol(session) as symbol:
                    async with api_client() as client:
                        owner_token = await _get_token(client, owner_email)
                        other_token = await _get_token(client, other_email)
                        other_headers = {"Authorization": f"Bearer {other_token}"}

                        strategy_id, version_id = await _validated_strategy(
                            client, owner_token
                        )
                        created = await client.post(
                            f"/strategies/{strategy_id}/versions/{version_id}/backtests",
                            headers={"Authorization": f"Bearer {owner_token}"},
                            json=_backtest_body(symbol),
                        )
                        assert created.status_code == 201, created.text
                        run_id = created.json()["id"]

                        assert (
                            await client.post(
                                f"/strategies/{strategy_id}/versions/{version_id}/backtests",
                                headers=other_headers,
                                json=_backtest_body(symbol),
                            )
                        ).status_code == 403
                        assert (
                            await client.get(
                                f"/strategies/{strategy_id}/versions/{version_id}/backtests",
                                headers=other_headers,
                            )
                        ).status_code == 403
                        assert (
                            await client.get(
                                f"/backtest-runs/{run_id}", headers=other_headers
                            )
                        ).status_code == 403


@pytest.mark.asyncio
async def test_unknown_ids_are_404_not_403() -> None:
    """A nonexistent id must not 403 merely because nobody can own a row
    that isn't there - the same order and codes `_load_owned_strategy` uses,
    which is exactly why that helper is imported rather than reimplemented."""
    async with db_session() as session, backtest_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, _version_id = await _validated_strategy(client, token)
            unknown = uuid.uuid4()

            assert (
                await client.get(f"/backtest-runs/{unknown}", headers=headers)
            ).status_code == 404
            assert (
                await client.post(
                    f"/strategies/{unknown}/versions/{unknown}/backtests",
                    headers=headers,
                    json=_backtest_body("ANY.US"),
                )
            ).status_code == 404
            # The strategy exists and is the caller's; the VERSION does not.
            assert (
                await client.post(
                    f"/strategies/{strategy_id}/versions/{unknown}/backtests",
                    headers=headers,
                    json=_backtest_body("ANY.US"),
                )
            ).status_code == 404
            assert (
                await client.get(
                    f"/strategies/{strategy_id}/versions/{unknown}/backtests",
                    headers=headers,
                )
            ).status_code == 404


@pytest.mark.asyncio
async def test_an_end_date_not_after_start_date_is_422_before_anything_runs() -> None:
    async with db_session() as session, backtest_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, version_id = await _validated_strategy(client, token)

            response = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/backtests",
                headers=headers,
                json={
                    **_backtest_body("ANY.US"),
                    "start_date": END_DATE.isoformat(),
                    "end_date": END_DATE.isoformat(),
                },
            )
            assert response.status_code == 422

            # Nothing was created for that rejected request.
            listing = await client.get(
                f"/strategies/{strategy_id}/versions/{version_id}/backtests", headers=headers
            )
            assert listing.json()["items"] == []
