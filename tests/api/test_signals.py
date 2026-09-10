"""Integration tests for the Phase 61 signal-engine endpoints, against a real
Postgres instance - these routes read `market_data_bars` and write
`signal_evaluations`, so there is no meaningful unit-level version of them.
That half - the crossing semantics, the explanation wording, the
insufficient-data cases in isolation - is tests/signals/test_engine.py's.

Reuses tests/api/test_admin.py's `db_session` / `api_client` / `_get_token`
and tests/api/test_strategy_backtests.py's `SMA2_DEFINITION` /
`_validated_strategy` / `_weekdays` rather than redefining any of them.

Two fixtures ARE defined here, for the same concrete reasons
tests/api/test_walk_forward.py and test_universe_scan.py give for theirs:

- `signal_user`, holding `strategy:manage` + `strategy:signal`. Its teardown
  deletes `signal_evaluations` before the strategy version, because
  `signal_evaluations.strategy_version_id` is ON DELETE RESTRICT - the schema
  genuinely refusing otherwise is that guarantee doing its job.
- `backtest_only_user`, holding `strategy:manage` + `strategy:backtest` but
  NOT `strategy:signal`, to prove the new permission is really its own gate
  and not implied by the backtest one.

Nothing is patched: these run the real engine over real persisted bars, so
they assert on the SHAPE and the STRUCTURAL guarantees - one row per
requested symbol in request order, insufficient-data rows kept not dropped,
ownership and permission enforced - rather than re-deriving the evaluator's
semantics.
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
    MarketDataBar,
    Role,
    SignalEvaluation,
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
from tests.api.test_strategies import VALID_DEFINITION
from tests.api.test_strategy_backtests import SMA2_DEFINITION, _validated_strategy, _weekdays

# A symbol whose latest seeded bars force a definite BUY: against SMA(2),
# `close crosses_above sma_2` reduces to "close rose after not being above
# the prior close". The series ends ... 100 (down from 105) then 108 (up),
# so the entry rule holds on the newest bar.
BUY_CLOSES = [100, 105, 100, 105, 100, 108]

# Only two bars - SMA(2) is defined but a crossing at the latest bar needs a
# previous bar too and the warmup buffer is not the issue; the engine reports
# insufficient data with a bar count, not a fabricated HOLD.
SHORT_CLOSES = [100, 101]

FIRST_BAR_DATE = date(2026, 2, 2)
END_DATE = date(2026, 2, 27)

NOBARS_SYMBOL = "NOSUCHSIGNAL.US"


def _seed(symbol: str, closes: list[int]) -> list[Bar]:
    """Real bars for `symbol`: `closes` laid onto the first N seeded weekdays
    (so a short list simply means a short history, which is the point)."""
    days = _weekdays(FIRST_BAR_DATE, END_DATE)[: len(closes)]
    return [
        Bar(
            symbol=symbol,
            bar_interval="1d",
            ts=datetime(day.year, day.month, day.day, 21, 0, tzinfo=UTC),
            close=Decimal(close),
            source="test-signals",
        )
        for day, close in zip(days, closes, strict=True)
    ]


@contextlib.asynccontextmanager
async def _role_user(session, *, permissions: list[str], label: str):
    role_id = uuid.uuid4()
    user_id = uuid.uuid4()
    email = f"{uuid.uuid4()}@example.com"
    session.add(Role(id=role_id, name=f"test-{label}-{role_id}", permissions=permissions))
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
        # signal_evaluations.strategy_version_id is ON DELETE RESTRICT - the
        # evaluations have to come off before the version they name.
        await session.execute(
            delete(SignalEvaluation).where(SignalEvaluation.strategy_version_id.in_(version_ids))
        )
        await session.execute(delete(Strategy).where(Strategy.owner_user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.execute(delete(Role).where(Role.id == role_id))
        await session.commit()


@contextlib.asynccontextmanager
async def signal_user(session):
    async with _role_user(
        session,
        permissions=[Permission.STRATEGY_MANAGE.value, Permission.STRATEGY_SIGNAL.value],
        label="signal",
    ) as pair:
        yield pair


@contextlib.asynccontextmanager
async def backtest_only_user(session):
    async with _role_user(
        session,
        permissions=[Permission.STRATEGY_MANAGE.value, Permission.STRATEGY_BACKTEST.value],
        label="btonly",
    ) as pair:
        yield pair


@contextlib.asynccontextmanager
async def seeded_symbols(session, spec: dict[str, list[int]]):
    """Each key gets real bars for its close list, written through
    MarketDataStore exactly as a backfill would - the route reads the
    persisted store and nothing else."""
    prefix = f"SIG{uuid.uuid4().hex[:6].upper()}"
    # Upper-case to match what the route persists (_normalize_symbol upper-cases),
    # so the bars seeded here are found by the query the route runs.
    mapping = {name: f"{prefix}{name.upper()}.US" for name in spec}
    store = MarketDataStore(session)
    for name, closes in spec.items():
        await store.upsert_bars(_seed(mapping[name], closes))
    await session.commit()
    try:
        yield mapping
    finally:
        await session.rollback()
        await session.execute(
            delete(MarketDataBar).where(MarketDataBar.symbol.in_(list(mapping.values())))
        )
        await session.commit()


@pytest.mark.asyncio
async def test_strategy_backtest_permission_does_not_grant_signal_access() -> None:
    """`strategy:signal` is its own gate. A role with `strategy:backtest` -
    which may study a version's past - is still refused every signal route,
    because a signal is a present-tense instruction, not a historical
    what-if (Permission.STRATEGY_SIGNAL's docstring)."""
    async with db_session() as session, backtest_only_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, version_id = await _validated_strategy(client, token)

            posted = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/signals",
                headers=headers,
                json={"symbols": ["ANY.US"]},
            )
            listed = await client.get(
                f"/strategies/{strategy_id}/versions/{version_id}/signals", headers=headers
            )
            by_id = await client.get(f"/signal-evaluations/{uuid.uuid4()}", headers=headers)

    assert posted.status_code == 403
    assert listed.status_code == 403
    assert by_id.status_code == 403


@pytest.mark.asyncio
async def test_a_user_with_no_permissions_at_all_is_403_at_the_router_level() -> None:
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.get(
                f"/signal-evaluations/{uuid.uuid4()}",
                headers={"Authorization": f"Bearer {token}"},
            )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_a_batch_persists_one_row_per_symbol_in_request_order_readable_three_ways() -> None:
    """The whole happy path: a BUY, an insufficient-data HOLD from too few
    bars, and an insufficient-data HOLD from no bars at all - persisted in
    request order, and the same rows readable from the list route and by id.
    """
    async with (
        db_session() as session,
        signal_user(session) as (_uid, email),
        seeded_symbols(session, {"buy": BUY_CLOSES, "short": SHORT_CLOSES}) as mapping,
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, version_id = await _validated_strategy(
                client, token, definition=SMA2_DEFINITION
            )
            requested = [mapping["buy"], mapping["short"], NOBARS_SYMBOL]

            created = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/signals",
                headers=headers,
                json={"symbols": requested},
            )
            assert created.status_code == 201, created.text
            items = created.json()["items"]
            assert [row["symbol"] for row in items] == requested  # request order preserved

            buy_row, short_row, nobars_row = items

            assert buy_row["signal"] == "buy"
            assert buy_row["insufficient_data"] is False
            assert buy_row["entry_rule_held"] is True
            assert buy_row["as_of_bar_date"] is not None
            assert Decimal(buy_row["latest_close"]) == Decimal(108)
            # The indicator the rules were compared against is kept beside the
            # verdict, as a string so no Decimal is routed through a JSON float.
            assert Decimal(buy_row["indicator_values"]["sma_2"]) == Decimal(104)
            # The explanation names concrete numbers, never a bare "BUY".
            assert "108" in buy_row["explanation"] and "-> BUY" in buy_row["explanation"]

            assert short_row["signal"] == "hold"
            assert short_row["insufficient_data"] is True
            assert short_row["latest_close"] is not None  # bars exist, just too few
            assert "insufficient data" in short_row["explanation"]

            assert nobars_row["signal"] == "hold"
            assert nobars_row["insufficient_data"] is True
            assert nobars_row["as_of_bar_date"] is None
            assert nobars_row["latest_close"] is None
            assert nobars_row["indicator_values"] == {}
            assert "no bars" in nobars_row["explanation"]

            listed = await client.get(
                f"/strategies/{strategy_id}/versions/{version_id}/signals", headers=headers
            )
            assert listed.status_code == 200
            listed_ids = {row["id"] for row in listed.json()["items"]}
            assert listed_ids == {row["id"] for row in items}  # all three persisted

            filtered = await client.get(
                f"/strategies/{strategy_id}/versions/{version_id}/signals",
                headers=headers,
                params={"symbol": mapping["buy"].lower()},  # normalized to match
            )
            assert filtered.status_code == 200
            assert [row["symbol"] for row in filtered.json()["items"]] == [mapping["buy"]]

            one = await client.get(f"/signal-evaluations/{buy_row['id']}", headers=headers)
            assert one.status_code == 200
            assert one.json()["explanation"] == buy_row["explanation"]


@pytest.mark.asyncio
async def test_a_draft_version_is_409_and_names_the_validate_route() -> None:
    async with db_session() as session, signal_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            created = await client.post(
                "/strategies",
                headers=headers,
                json={"name": "Draft", "definition": VALID_DEFINITION},
            )
            body = created.json()
            strategy_id, version_id = body["id"], body["latest_version"]["id"]

            response = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/signals",
                headers=headers,
                json={"symbols": ["ANY.US"]},
            )
    assert response.status_code == 409
    assert "VERSION_NOT_VALIDATED" in response.text
    assert "/validate" in response.text


@pytest.mark.asyncio
async def test_another_users_strategy_and_evaluation_are_403() -> None:
    async with (
        db_session() as session,
        signal_user(session) as (_owner_uid, owner_email),
        signal_user(session) as (_other_uid, other_email),
        seeded_symbols(session, {"buy": BUY_CLOSES}) as mapping,
    ):
        async with api_client() as client:
            owner_token = await _get_token(client, owner_email)
            other_token = await _get_token(client, other_email)
            strategy_id, version_id = await _validated_strategy(
                client, owner_token, definition=SMA2_DEFINITION
            )
            created = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/signals",
                headers={"Authorization": f"Bearer {owner_token}"},
                json={"symbols": [mapping["buy"]]},
            )
            evaluation_id = created.json()["items"][0]["id"]

            other = {"Authorization": f"Bearer {other_token}"}
            assert (
                await client.post(
                    f"/strategies/{strategy_id}/versions/{version_id}/signals",
                    headers=other,
                    json={"symbols": [mapping["buy"]]},
                )
            ).status_code == 403
            assert (
                await client.get(
                    f"/strategies/{strategy_id}/versions/{version_id}/signals", headers=other
                )
            ).status_code == 403
            assert (
                await client.get(f"/signal-evaluations/{evaluation_id}", headers=other)
            ).status_code == 403


@pytest.mark.asyncio
async def test_unknown_ids_are_404_not_403() -> None:
    async with db_session() as session, signal_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            assert (
                await client.get(f"/signal-evaluations/{uuid.uuid4()}", headers=headers)
            ).status_code == 404
            assert (
                await client.get(
                    f"/strategies/{uuid.uuid4()}/versions/{uuid.uuid4()}/signals", headers=headers
                )
            ).status_code == 404


@pytest.mark.asyncio
async def test_an_empty_symbol_list_is_422_before_anything_runs() -> None:
    async with db_session() as session, signal_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            strategy_id, version_id = await _validated_strategy(
                client, token, definition=SMA2_DEFINITION
            )
            response = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/signals",
                headers=headers,
                json={"symbols": []},
            )
            assert response.status_code == 422

            listed = await client.get(
                f"/strategies/{strategy_id}/versions/{version_id}/signals", headers=headers
            )
            assert listed.json()["items"] == []  # nothing was written
