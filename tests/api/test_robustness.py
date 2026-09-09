"""Integration tests for the Phase 58 robustness endpoints, against a real
Postgres instance - these routes read `market_data_bars` and write
`robustness_runs` / `robustness_perturbations`, so there is no meaningful
unit-level version of them. That half - the aggregate math and the
failure-mode branching in isolation, with both engine helpers patched out -
is tests/backtesting/test_robustness.py's.

Reuses tests/api/test_admin.py's `db_session` / `api_client` / `_get_token`
fixtures, tests/api/test_strategies.py's `strategy_user`, and
tests/api/test_strategy_backtests.py's `_weekdays` and `_validated_strategy`
rather than redefining any of them.

Three things ARE defined here, each for a concrete reason rather than
convenience:

- `robustness_user`, for the reason tests/api/test_walk_forward.py gives for
  defining its own: `robustness_runs.strategy_version_id` is ON DELETE
  RESTRICT, so a fixture that deletes strategies without clearing this
  module's rows first is refused by the schema - which is that guarantee
  doing its job. Perturbation rows come off before their runs, runs before
  the version.
- `SMA10_DEFINITION`, because a +/-10% nudge of SMA(2) rounds straight back
  to 2 in both directions; period 10 perturbs cleanly to 11 and 9, which is
  what makes the per-variant assertions below say something.
- `seeded_symbol` over a wide date range, because the largest perturbed
  period needs its own longer warmup - the orchestrator recomputes the
  warmup per definition, and this fixture is what lets every variant find
  it.

Nothing is patched: these tests run the real orchestrator over the real
generator, the real engine helpers and real persisted bars. Every asserted
number is therefore checked against ANOTHER real result - the run's own
perturbation rows, and for the baseline an ordinary `POST .../backtests` over
the identical window - rather than against a hard-coded figure, so this
module cannot drift out of agreement with the engine it measures.

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
    RobustnessPerturbationResult,
    RobustnessRun,
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
from tests.api.test_strategy_backtests import _validated_strategy, _weekdays

SMA10_DEFINITION = {
    "indicators": [{"id": "sma_10", "type": "sma", "period": 10}],
    "entry_rule": {"op": "crosses_above", "left": "close", "right": "sma_10"},
    "exit_rule": {"op": "crosses_below", "left": "close", "right": "sma_10"},
    "position_sizing": {"type": "all_in"},
}
"""Period 10 so a +/-10% nudge lands on 11 and 9 - two genuinely different
definitions, each needing its own warmup length. `all_in` sizing carries no
number, so this definition's only perturbable parameter is the period, which
makes the expected perturbation set exactly two rows."""

# Bars start almost two months before the window so that even the LONGEST
# perturbed period finds its full warmup in front of START_DATE - the
# orchestrator recomputes the warmup per definition, and this range is what
# lets every variant succeed rather than testing the warmup-gap path by
# accident.
FIRST_BAR_DATE = date(2026, 1, 5)
START_DATE = date(2026, 3, 2)
END_DATE = date(2026, 3, 27)
STARTING_CASH = Decimal(10_000)


def _seed_bars(symbol: str) -> list[Bar]:
    """Closes alternating 100 / 105 across every seeded weekday, the same
    series tests/api/test_strategy_backtests.py seeds and for the same
    reason: an alternating close guarantees the entry and exit rules both
    fire repeatedly against a mid-range moving average, so every variant
    produces real round trips without this test having to reason about the
    evaluator's crossing logic itself."""
    return [
        Bar(
            symbol=symbol,
            bar_interval="1d",
            ts=datetime(day.year, day.month, day.day, 21, 0, tzinfo=UTC),
            close=Decimal(105) if index % 2 else Decimal(100),
            source="test-robustness",
        )
        for index, day in enumerate(_weekdays(FIRST_BAR_DATE, END_DATE))
    ]


@contextlib.asynccontextmanager
async def robustness_user(session):
    """An active user holding both `strategy:manage` and `strategy:backtest`
    - the former only because these tests have to author and validate the
    strategy they then test.

    Teardown order is dictated by the schema, not by preference:
    `robustness_perturbations.robustness_run_id` is ON DELETE CASCADE but
    `robustness_runs.strategy_version_id` is ON DELETE RESTRICT, so the runs
    have to come off before the version can. The backtest rows are cleared
    too because one test deliberately runs an ordinary backtest to check the
    robustness baseline against it.
    """
    role_id = uuid.uuid4()
    user_id = uuid.uuid4()
    email = f"{uuid.uuid4()}@example.com"
    session.add(
        Role(
            id=role_id,
            name=f"test-robustness-{role_id}",
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
        robustness_run_ids = select(RobustnessRun.id).where(
            RobustnessRun.strategy_version_id.in_(version_ids)
        )
        run_ids = select(BacktestRun.id).where(BacktestRun.strategy_version_id.in_(version_ids))
        await session.execute(
            delete(RobustnessPerturbationResult).where(
                RobustnessPerturbationResult.robustness_run_id.in_(robustness_run_ids)
            )
        )
        await session.execute(
            delete(RobustnessRun).where(RobustnessRun.strategy_version_id.in_(version_ids))
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
    symbol = f"RBQA{uuid.uuid4().hex[:8].upper()}.US"
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
        "start_date": START_DATE.isoformat(),
        "end_date": END_DATE.isoformat(),
        "starting_cash": str(STARTING_CASH),
        "magnitude_pct": "10",
        **overrides,
    }


@pytest.mark.asyncio
async def test_holding_strategy_manage_without_strategy_backtest_is_403() -> None:
    """The robustness surface is gated on `strategy:backtest`, the same
    permission the persisted-backtest and walk-forward surfaces use - so
    `strategy_user`, which holds only `strategy:manage`, is refused at the
    router's own dependency on all three routes."""
    async with db_session() as session, strategy_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, version_id = await _validated_strategy(client, token)

            created = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/robustness",
                headers=headers,
                json=_body("ANY.US"),
            )
            listing = await client.get(
                f"/strategies/{strategy_id}/versions/{version_id}/robustness",
                headers=headers,
            )
            by_id = await client.get(f"/robustness-runs/{uuid.uuid4()}", headers=headers)

    assert created.status_code == 403
    assert listing.status_code == 403
    assert by_id.status_code == 403


@pytest.mark.asyncio
async def test_a_user_with_no_permissions_at_all_is_403_at_the_router_level() -> None:
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.get(
                f"/robustness-runs/{uuid.uuid4()}",
                headers={"Authorization": f"Bearer {token}"},
            )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_a_robustness_run_persists_and_is_readable_three_ways() -> None:
    """The whole happy path in one test, because the three routes describe
    the same row and asserting they agree is the point.

    Two independent cross-checks make every number here real rather than
    hard-coded: the aggregates are re-derived from the run's OWN perturbation
    rows, and the baseline is compared against an ordinary
    `POST .../backtests` over the identical window - which must agree,
    because the baseline is deliberately the same engine over the same bars
    rather than a reading of some earlier run.
    """
    async with db_session() as session, robustness_user(session) as (_uid, email):
        async with seeded_symbol(session) as symbol:
            async with api_client() as client:
                token = await _get_token(client, email)
                headers = {"Authorization": f"Bearer {token}"}
                strategy_id, version_id = await _validated_strategy(
                    client, token, definition=SMA10_DEFINITION
                )

                created = await client.post(
                    f"/strategies/{strategy_id}/versions/{version_id}/robustness",
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
                assert Decimal(run["starting_cash"]) == STARTING_CASH
                assert Decimal(run["magnitude_pct"]) == Decimal(10)

                # The baseline really ran, and produced both of its metrics.
                assert run["baseline_return_pct"] is not None
                assert run["baseline_max_drawdown_pct"] is not None
                baseline_return = Decimal(run["baseline_return_pct"])

                # `all_in` sizing carries no number, so SMA(10)'s period is
                # the only perturbable parameter: exactly one variant up and
                # one down, both real definitions that really replayed.
                perturbations = run["perturbations"]
                assert len(perturbations) == 2
                assert {row["direction"] for row in perturbations} == {"+", "-"}
                assert {row["parameter_path"] for row in perturbations} == {
                    "indicators[0].period"
                }
                assert {Decimal(row["original_value"]) for row in perturbations} == {
                    Decimal(10)
                }
                assert {Decimal(row["perturbed_value"]) for row in perturbations} == {
                    Decimal(11),
                    Decimal(9),
                }
                assert all(row["status"] == "succeeded" for row in perturbations)
                assert all(row["error_detail"] is None for row in perturbations)
                assert all(row["clamped"] is False for row in perturbations)

                assert run["num_perturbations"] == 2
                assert run["num_succeeded_perturbations"] == 2

                # The aggregates really are aggregates OF those two rows.
                returns = [Decimal(row["total_return_pct"]) for row in perturbations]
                mean = sum(returns) / len(returns)
                assert abs(Decimal(run["mean_perturbed_return_pct"]) - mean) <= Decimal(
                    "0.0001"
                )
                assert run["stddev_perturbed_return_pct"] is not None
                deviation = max(abs(value - baseline_return) for value in returns)
                assert abs(
                    Decimal(run["max_return_deviation_pct"]) - deviation
                ) <= Decimal("0.0001")

                # The baseline is the ORDINARY backtest of this version over
                # this window - same engine, same bars, same cash - so the two
                # must report the same return.
                backtest = await client.post(
                    f"/strategies/{strategy_id}/versions/{version_id}/backtests",
                    headers=headers,
                    json={
                        "symbol": symbol,
                        "bar_interval": "1d",
                        "start_date": START_DATE.isoformat(),
                        "end_date": END_DATE.isoformat(),
                        "starting_cash": str(STARTING_CASH),
                    },
                )
                assert backtest.status_code == 201, backtest.text
                assert backtest.json()["status"] == "succeeded"
                assert Decimal(backtest.json()["total_return_pct"]) == baseline_return

                detail = await client.get(f"/robustness-runs/{run['id']}", headers=headers)
                assert detail.status_code == 200
                assert detail.json() == run

                listing = await client.get(
                    f"/strategies/{strategy_id}/versions/{version_id}/robustness",
                    headers=headers,
                )
                assert listing.status_code == 200
                listed = listing.json()
                assert listed["limit"] == 50
                assert listed["offset"] == 0
                assert [item["id"] for item in listed["items"]] == [run["id"]]
                # A summary deliberately carries no perturbation breakdown.
                assert "perturbations" not in listed["items"][0]


@pytest.mark.asyncio
async def test_a_symbol_with_no_bars_is_a_persisted_failed_run() -> None:
    """The baseline cannot be replayed at all, so there is nothing for a
    perturbation to be measured against - but that is a real, readable
    resource naming the real reason, not a 4xx that loses the request."""
    async with db_session() as session, robustness_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, version_id = await _validated_strategy(
                client, token, definition=SMA10_DEFINITION
            )

            created = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/robustness",
                headers=headers,
                json=_body("NOSUCHSYMBOL.US"),
            )
            assert created.status_code == 201, created.text
            run = created.json()

            assert run["status"] == "failed"
            assert "BASELINE_FAILED" in run["error_detail"]
            assert run["perturbations"] == []
            # Never a fabricated statistic for a run that computed none.
            assert run["baseline_return_pct"] is None
            assert run["num_perturbations"] is None
            assert run["mean_perturbed_return_pct"] is None
            assert run["max_return_deviation_pct"] is None

            reread = await client.get(f"/robustness-runs/{run['id']}", headers=headers)
            assert reread.status_code == 200
            assert reread.json()["status"] == "failed"


@pytest.mark.asyncio
async def test_a_definition_with_nothing_to_perturb_is_a_persisted_failed_run() -> None:
    """`VALID_DEFINITION` is SMA(20) with `all_in` sizing, so its period IS
    perturbable - the no-parameters case needs a definition with no
    indicators at all, whose sizing carries no number either. That run fails
    rather than succeeding with zero perturbations, because "this question
    cannot be asked of this definition" is not the same finding as "this
    strategy is insensitive".
    """
    no_parameters = {
        "indicators": [],
        "entry_rule": {"op": "gt", "left": "close", "right": 0},
        "exit_rule": {"op": "lt", "left": "close", "right": 0},
        "position_sizing": {"type": "all_in"},
    }
    async with db_session() as session, robustness_user(session) as (_uid, email):
        async with seeded_symbol(session) as symbol:
            async with api_client() as client:
                token = await _get_token(client, email)
                headers = {"Authorization": f"Bearer {token}"}
                strategy_id, version_id = await _validated_strategy(
                    client, token, definition=no_parameters
                )

                created = await client.post(
                    f"/strategies/{strategy_id}/versions/{version_id}/robustness",
                    headers=headers,
                    json=_body(symbol),
                )
                assert created.status_code == 201, created.text
                run = created.json()

    assert run["status"] == "failed"
    assert "NO_PERTURBABLE_PARAMETERS" in run["error_detail"]
    assert run["perturbations"] == []
    # The baseline still ran and is still reported - only the perturbation
    # half of the run had nothing to do.
    assert run["baseline_return_pct"] is not None
    assert run["num_perturbations"] is None


@pytest.mark.asyncio
async def test_robustness_on_a_draft_version_is_409_and_names_the_validate_route() -> None:
    async with db_session() as session, robustness_user(session) as (_uid, email):
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
                f"/strategies/{strategy_id}/versions/{version_id}/robustness",
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
    `GET /robustness-runs/{id}` - holding a run id is not authorization."""
    async with db_session() as session:
        async with robustness_user(session) as (_owner_id, owner_email):
            async with robustness_user(session) as (_other_id, other_email):
                async with seeded_symbol(session) as symbol:
                    async with api_client() as client:
                        owner_token = await _get_token(client, owner_email)
                        other_token = await _get_token(client, other_email)
                        other_headers = {"Authorization": f"Bearer {other_token}"}

                        strategy_id, version_id = await _validated_strategy(
                            client, owner_token, definition=SMA10_DEFINITION
                        )
                        created = await client.post(
                            f"/strategies/{strategy_id}/versions/{version_id}/robustness",
                            headers={"Authorization": f"Bearer {owner_token}"},
                            json=_body(symbol),
                        )
                        assert created.status_code == 201, created.text
                        run_id = created.json()["id"]

                        assert (
                            await client.post(
                                f"/strategies/{strategy_id}/versions/{version_id}"
                                "/robustness",
                                headers=other_headers,
                                json=_body(symbol),
                            )
                        ).status_code == 403
                        assert (
                            await client.get(
                                f"/strategies/{strategy_id}/versions/{version_id}"
                                "/robustness",
                                headers=other_headers,
                            )
                        ).status_code == 403
                        assert (
                            await client.get(
                                f"/robustness-runs/{run_id}", headers=other_headers
                            )
                        ).status_code == 403


@pytest.mark.asyncio
async def test_unknown_ids_are_404_not_403() -> None:
    """A nonexistent id must not 403 merely because nobody can own a row that
    isn't there - the same order and codes `_load_owned_strategy` uses, which
    is exactly why that helper is imported rather than reimplemented."""
    async with db_session() as session, robustness_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, _version_id = await _validated_strategy(client, token)
            unknown = uuid.uuid4()

            assert (
                await client.get(f"/robustness-runs/{unknown}", headers=headers)
            ).status_code == 404
            assert (
                await client.post(
                    f"/strategies/{unknown}/versions/{unknown}/robustness",
                    headers=headers,
                    json=_body("ANY.US"),
                )
            ).status_code == 404
            # The strategy exists and is the caller's; the VERSION does not.
            assert (
                await client.post(
                    f"/strategies/{strategy_id}/versions/{unknown}/robustness",
                    headers=headers,
                    json=_body("ANY.US"),
                )
            ).status_code == 404
            assert (
                await client.get(
                    f"/strategies/{strategy_id}/versions/{unknown}/robustness",
                    headers=headers,
                )
            ).status_code == 404


@pytest.mark.asyncio
async def test_a_malformed_request_is_422_before_anything_runs() -> None:
    """A range that does not move forward, and a `magnitude_pct` outside
    `(0, 50]`, are malformed requests rather than empty or misleading
    results - and none of them creates a row. The upper bound is the
    load-bearing one: a 200% "perturbation" is a different strategy, not a
    nudge, so measuring it under the heading "how sensitive is yours" would
    misdescribe what was measured.
    """
    async with db_session() as session, robustness_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, version_id = await _validated_strategy(client, token)
            url = f"/strategies/{strategy_id}/versions/{version_id}/robustness"

            backwards = await client.post(
                url, headers=headers, json=_body("ANY.US", end_date=START_DATE.isoformat())
            )
            assert backwards.status_code == 422

            too_large = await client.post(
                url, headers=headers, json=_body("ANY.US", magnitude_pct="50.0001")
            )
            assert too_large.status_code == 422

            zero = await client.post(
                url, headers=headers, json=_body("ANY.US", magnitude_pct="0")
            )
            assert zero.status_code == 422

            negative_cash = await client.post(
                url, headers=headers, json=_body("ANY.US", starting_cash="0")
            )
            assert negative_cash.status_code == 422

            listing = await client.get(url, headers=headers)
            assert listing.json()["items"] == []
