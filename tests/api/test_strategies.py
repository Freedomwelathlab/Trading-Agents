"""Integration tests for the Phase 54 /strategies endpoints, against a real
Postgres instance - these routes read and write `strategies` /
`strategy_versions` directly, so there is no meaningful unit-level version
of them.

Reuses `db_session` / `api_client` / `_get_token` / `non_admin_user` from
tests/api/test_admin.py rather than redefining them, exactly as
tests/api/test_admin_market_data.py does. The one fixture defined here is
`strategy_user`, because no existing fixture grants
`Permission.STRATEGY_MANAGE`.

The database is shared with every other test module and is not reset
between tests, so nothing here asserts on an absolute row count - each test
seeds and tears down the rows it owns.

Three claims carry most of the weight:

1. **Ownership.** A strategy is reachable only by its owner. Someone
   else's is 403, a nonexistent one is 404, and the listing never shows
   another user's rows.
2. **Immutability.** A version that is not a draft cannot be edited or
   re-validated - 409, with the fork endpoint named in the message.
3. **Validation is honest and complete.** A malformed definition answers
   422 with every problem itemized and mutates nothing.
"""

import contextlib
import json
import uuid

import pytest
from sqlalchemy import delete, func, select

from apps.api.app.agents.provider import LLMProviderError
from apps.api.app.agents.strategy_research_assistant import StrategyResearchAssistant
from apps.api.app.api.dependencies import get_strategy_research_assistant
from apps.api.app.auth.permissions import Permission
from apps.api.app.auth.security import hash_password
from apps.api.app.db.models import Role, Strategy, StrategyVersion, User
from apps.api.app.main import app
from tests.api.test_admin import (
    TEST_PASSWORD,
    _get_token,
    api_client,
    db_session,
    non_admin_user,
)

VALID_DEFINITION = {
    "indicators": [{"id": "sma_20", "type": "sma", "period": 20}],
    "entry_rule": {"op": "crosses_above", "left": "sma_20", "right": "close"},
    "exit_rule": {"op": "crosses_below", "left": "sma_20", "right": "close"},
    "position_sizing": {"type": "all_in"},
}

MALFORMED_DEFINITION = {
    "indicators": [{"id": "sma_20", "type": "ema", "period": -5}],
    "entry_rule": {"op": "crosses_above", "left": "sma_99", "right": "close"},
    "exit_rule": {"op": "crosses_below", "left": "sma_20", "right": "close"},
    "position_sizing": {"type": "martingale"},
}


@contextlib.asynccontextmanager
async def strategy_user(session):
    """An active user whose role grants Permission.STRATEGY_MANAGE and
    nothing else - the weakest identity these routes must work for, and the
    one that proves the router-level gate is this permission rather than
    something broader."""
    role_id = uuid.uuid4()
    user_id = uuid.uuid4()
    email = f"{uuid.uuid4()}@example.com"
    session.add(
        Role(
            id=role_id,
            name=f"test-strategist-{role_id}",
            permissions=[Permission.STRATEGY_MANAGE.value],
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
        # strategy_versions goes with it via ON DELETE CASCADE (migration 0017).
        await session.execute(delete(Strategy).where(Strategy.owner_user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.execute(delete(Role).where(Role.id == role_id))
        await session.commit()


async def _create_strategy(client, token, *, name="Test Strategy", definition=None) -> dict:
    response = await client.post(
        "/strategies",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": name,
            "description": "created by tests/api/test_strategies.py",
            "definition": VALID_DEFINITION if definition is None else definition,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_a_user_without_the_permission_is_403_at_the_router_level():
    """`non_admin_user` has no role at all, so it holds no permissions -
    the router-level dependency must refuse it before any handler runs."""
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            listing = await client.get("/strategies", headers=headers)
            creation = await client.post(
                "/strategies", headers=headers, json={"name": "nope", "definition": {}}
            )
    assert listing.status_code == 403
    assert creation.status_code == 403


@pytest.mark.asyncio
async def test_create_returns_version_1_as_a_draft_and_it_appears_in_the_listing():
    async with db_session() as session, strategy_user(session) as (user_id, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            body = await _create_strategy(client, token, name="SMA crossover")

            assert body["owner_user_id"] == str(user_id)
            assert body["name"] == "SMA crossover"
            assert body["status"] == "active"
            latest = body["latest_version"]
            assert latest["version_number"] == 1
            assert latest["status"] == "draft"
            assert latest["validated_at"] is None
            assert latest["definition"] == VALID_DEFINITION
            assert len(latest["definition_hash"]) == 64

            listing = await client.get(
                "/strategies", headers={"Authorization": f"Bearer {token}"}
            )
            assert listing.status_code == 200
            listed = listing.json()
            assert listed["limit"] == 50
            assert listed["offset"] == 0
            mine = [item for item in listed["items"] if item["id"] == body["id"]]
            assert len(mine) == 1
            assert mine[0]["latest_version_number"] == 1
            assert mine[0]["latest_version_status"] == "draft"
            # A summary row deliberately carries no definition.
            assert "definition" not in mine[0]


@pytest.mark.asyncio
async def test_an_empty_definition_is_a_legal_draft_to_create():
    """A strategy is named before it is written - refusing to store an
    incomplete draft would leave the user nowhere to put work in
    progress."""
    async with db_session() as session, strategy_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            body = await _create_strategy(client, token, definition={})
    assert body["latest_version"]["definition"] == {}


@pytest.mark.asyncio
async def test_another_users_strategy_is_invisible_in_the_listing_and_403_by_id():
    async with db_session() as session:
        async with strategy_user(session) as (_owner_id, owner_email):
            async with strategy_user(session) as (_other_id, other_email):
                async with api_client() as client:
                    owner_token = await _get_token(client, owner_email)
                    other_token = await _get_token(client, other_email)
                    created = await _create_strategy(client, owner_token, name="Private")
                    strategy_id = created["id"]
                    version_id = created["latest_version"]["id"]
                    other_headers = {"Authorization": f"Bearer {other_token}"}

                    listing = await client.get("/strategies", headers=other_headers)
                    assert listing.status_code == 200
                    assert all(
                        item["id"] != strategy_id for item in listing.json()["items"]
                    )

                    assert (
                        await client.get(f"/strategies/{strategy_id}", headers=other_headers)
                    ).status_code == 403
                    assert (
                        await client.patch(
                            f"/strategies/{strategy_id}",
                            headers=other_headers,
                            json={"name": "stolen"},
                        )
                    ).status_code == 403
                    assert (
                        await client.post(
                            f"/strategies/{strategy_id}/versions",
                            headers=other_headers,
                            json={},
                        )
                    ).status_code == 403
                    assert (
                        await client.post(
                            f"/strategies/{strategy_id}/versions/{version_id}/validate",
                            headers=other_headers,
                        )
                    ).status_code == 403
                    assert (
                        await client.get(
                            f"/strategies/{strategy_id}/versions/{version_id}",
                            headers=other_headers,
                        )
                    ).status_code == 403


@pytest.mark.asyncio
async def test_an_unknown_strategy_id_is_404_not_403():
    """A nonexistent id must not 403 merely because nobody can own a row
    that isn't there - the same order `_owned_watchlist` uses."""
    async with db_session() as session, strategy_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            unknown = uuid.uuid4()
            assert (await client.get(f"/strategies/{unknown}", headers=headers)).status_code == 404
            assert (
                await client.patch(
                    f"/strategies/{unknown}", headers=headers, json={"name": "x"}
                )
            ).status_code == 404
            assert (
                await client.post(
                    f"/strategies/{unknown}/versions", headers=headers, json={}
                )
            ).status_code == 404


@pytest.mark.asyncio
async def test_a_version_id_from_another_strategy_is_404_not_a_cross_strategy_read():
    async with db_session() as session, strategy_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            first = await _create_strategy(client, token, name="First")
            second = await _create_strategy(client, token, name="Second")
            foreign_version = second["latest_version"]["id"]

            response = await client.get(
                f"/strategies/{first['id']}/versions/{foreign_version}", headers=headers
            )
            assert response.status_code == 404

            fork = await client.post(
                f"/strategies/{first['id']}/versions",
                headers=headers,
                json={"from_version_id": foreign_version},
            )
            assert fork.status_code == 404


@pytest.mark.asyncio
async def test_get_detail_lists_every_version_newest_first():
    async with db_session() as session, strategy_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            created = await _create_strategy(client, token)
            strategy_id = created["id"]

            fork = await client.post(
                f"/strategies/{strategy_id}/versions", headers=headers, json={}
            )
            assert fork.status_code == 201

            detail = await client.get(f"/strategies/{strategy_id}", headers=headers)
            assert detail.status_code == 200
            body = detail.json()
            assert [v["version_number"] for v in body["versions"]] == [2, 1]
            assert body["latest_version"]["version_number"] == 2
            assert body["latest_version"]["definition"] == VALID_DEFINITION
            assert "definition" not in body["versions"][0]


@pytest.mark.asyncio
async def test_patch_updates_only_the_fields_present_in_the_body():
    async with db_session() as session, strategy_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            created = await _create_strategy(client, token, name="Before")
            strategy_id = created["id"]

            renamed = await client.patch(
                f"/strategies/{strategy_id}", headers=headers, json={"name": "After"}
            )
            assert renamed.status_code == 200
            assert renamed.json()["name"] == "After"
            # description was not in the body, so it is untouched.
            assert renamed.json()["description"] == created["description"]
            assert renamed.json()["status"] == "active"

            archived = await client.patch(
                f"/strategies/{strategy_id}", headers=headers, json={"status": "archived"}
            )
            assert archived.status_code == 200
            assert archived.json()["status"] == "archived"
            assert archived.json()["name"] == "After"

            nulled = await client.patch(
                f"/strategies/{strategy_id}", headers=headers, json={"name": None}
            )
            assert nulled.status_code == 422


@pytest.mark.asyncio
async def test_forking_copies_the_definition_and_editing_the_fork_leaves_the_source_alone():
    async with db_session() as session, strategy_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            created = await _create_strategy(client, token)
            strategy_id = created["id"]
            source_version_id = created["latest_version"]["id"]

            fork = await client.post(
                f"/strategies/{strategy_id}/versions", headers=headers, json={}
            )
            assert fork.status_code == 201
            forked = fork.json()
            assert forked["version_number"] == 2
            assert forked["status"] == "draft"
            assert forked["definition"] == VALID_DEFINITION
            assert forked["definition_hash"] == created["latest_version"]["definition_hash"]

            edited_definition = {
                **VALID_DEFINITION,
                "indicators": [{"id": "sma_20", "type": "sma", "period": 50}],
            }
            edit = await client.patch(
                f"/strategies/{strategy_id}/versions/{forked['id']}",
                headers=headers,
                json={"definition": edited_definition},
            )
            assert edit.status_code == 200
            assert edit.json()["definition"] == edited_definition
            assert edit.json()["definition_hash"] != forked["definition_hash"]

            source = await client.get(
                f"/strategies/{strategy_id}/versions/{source_version_id}", headers=headers
            )
            assert source.status_code == 200
            assert source.json()["definition"] == VALID_DEFINITION


@pytest.mark.asyncio
async def test_forking_from_an_explicit_older_version_is_allowed():
    async with db_session() as session, strategy_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            created = await _create_strategy(client, token)
            strategy_id = created["id"]
            v1_id = created["latest_version"]["id"]

            v2 = (
                await client.post(
                    f"/strategies/{strategy_id}/versions", headers=headers, json={}
                )
            ).json()
            await client.patch(
                f"/strategies/{strategy_id}/versions/{v2['id']}",
                headers=headers,
                json={"definition": {}},
            )

            v3 = await client.post(
                f"/strategies/{strategy_id}/versions",
                headers=headers,
                json={"from_version_id": v1_id},
            )
            assert v3.status_code == 201
            assert v3.json()["version_number"] == 3
            assert v3.json()["definition"] == VALID_DEFINITION


@pytest.mark.asyncio
async def test_validating_a_well_formed_definition_succeeds_and_freezes_the_version():
    async with db_session() as session, strategy_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            created = await _create_strategy(client, token)
            strategy_id = created["id"]
            version_id = created["latest_version"]["id"]

            response = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/validate", headers=headers
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["status"] == "validated"
            assert body["validated_at"] is not None

            # And it really is persisted, not just echoed back.
            reread = await client.get(
                f"/strategies/{strategy_id}/versions/{version_id}", headers=headers
            )
            assert reread.json()["status"] == "validated"


@pytest.mark.asyncio
async def test_a_validated_version_can_no_longer_be_edited_or_re_validated():
    async with db_session() as session, strategy_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            created = await _create_strategy(client, token)
            strategy_id = created["id"]
            version_id = created["latest_version"]["id"]
            await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/validate", headers=headers
            )

            edit = await client.patch(
                f"/strategies/{strategy_id}/versions/{version_id}",
                headers=headers,
                json={"definition": {}},
            )
            assert edit.status_code == 409
            assert "VERSION_NOT_DRAFT" in edit.json()["detail"]
            assert f"/strategies/{strategy_id}/versions" in edit.json()["detail"]

            revalidate = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/validate", headers=headers
            )
            assert revalidate.status_code == 409
            assert "cannot be re-validated" in revalidate.json()["detail"]

            # The definition is genuinely unchanged, not merely reported so.
            reread = await client.get(
                f"/strategies/{strategy_id}/versions/{version_id}", headers=headers
            )
            assert reread.json()["definition"] == VALID_DEFINITION


@pytest.mark.asyncio
async def test_validating_a_malformed_definition_is_422_with_itemized_errors_and_no_mutation():
    async with db_session() as session, strategy_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            created = await _create_strategy(client, token, definition=MALFORMED_DEFINITION)
            strategy_id = created["id"]
            version_id = created["latest_version"]["id"]

            response = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/validate", headers=headers
            )
            assert response.status_code == 422
            errors = response.json()["detail"]["errors"]
            assert "indicators[0].type 'ema' is not one of: sma, rsi" in errors
            assert "indicators[0].period must be a positive integer, got -5" in errors
            assert "entry_rule.left references undeclared indicator id 'sma_99'" in errors
            assert (
                "position_sizing.type 'martingale' is not one of: "
                "all_in, fixed_fraction, fixed_notional" in errors
            )

            # Nothing was written: still an editable draft with no validated_at.
            reread = await client.get(
                f"/strategies/{strategy_id}/versions/{version_id}", headers=headers
            )
            assert reread.json()["status"] == "draft"
            assert reread.json()["validated_at"] is None


@pytest.mark.asyncio
async def test_a_failed_validation_can_be_fixed_in_place_and_then_validated():
    """The draft stays editable after a failed validation, which is the
    whole reason editing does not validate and validating does not edit."""
    async with db_session() as session, strategy_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            created = await _create_strategy(client, token, definition=MALFORMED_DEFINITION)
            strategy_id = created["id"]
            version_id = created["latest_version"]["id"]

            first = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/validate", headers=headers
            )
            assert first.status_code == 422

            fix = await client.patch(
                f"/strategies/{strategy_id}/versions/{version_id}",
                headers=headers,
                json={"definition": VALID_DEFINITION},
            )
            assert fix.status_code == 200

            second = await client.post(
                f"/strategies/{strategy_id}/versions/{version_id}/validate", headers=headers
            )
            assert second.status_code == 200
            assert second.json()["status"] == "validated"


@pytest.mark.asyncio
async def test_version_numbers_are_dense_and_unique_per_strategy_in_the_database():
    async with db_session() as session, strategy_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            created = await _create_strategy(client, token)
            strategy_id = uuid.UUID(created["id"])
            for _ in range(2):
                assert (
                    await client.post(
                        f"/strategies/{strategy_id}/versions", headers=headers, json={}
                    )
                ).status_code == 201

        numbers = (
            (
                await session.execute(
                    select(StrategyVersion.version_number)
                    .where(StrategyVersion.strategy_id == strategy_id)
                    .order_by(StrategyVersion.version_number.asc())
                )
            )
            .scalars()
            .all()
        )
        assert list(numbers) == [1, 2, 3]


# --- Phase 67 (D085): POST /strategies/research/propose -------------------
#
# Hermetic w.r.t. the LLM provider the same way tests/api/test_agent_trades.py
# is: no test here relies on LLM_PROVIDER_* being set in .env. The
# NOT_CONFIGURED test relies on the test environment's default (no provider
# wired); the 200 tests override get_strategy_research_assistant explicitly
# with a fake provider - never a real network call.

INVALID_RESEARCH_DEFINITION = {
    "indicators": [{"id": "macd_1", "type": "macd", "period": 12}],
    "entry_rule": {"op": "crosses_below", "left": "macd_1", "right": "close"},
    "exit_rule": {"op": "crosses_above", "left": "macd_1", "right": "close"},
    "position_sizing": {"type": "fixed_fraction"},
}


class FakeLLMProvider:
    name = "fake-llm"

    def __init__(self, response: str) -> None:
        self._response = response

    async def complete(self, *, system: str, user: str, max_tokens: int) -> str:
        return self._response


async def _strategy_row_count(session, user_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(Strategy).where(Strategy.owner_user_id == user_id)
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_propose_without_a_wired_llm_provider_is_400_not_configured():
    """Explicitly overrides the dependency to None rather than relying on no
    LLM_PROVIDER_* being set in the environment - .env may configure a real
    one for local dev (as tests/api/test_agent_trades.py's own
    `_no_real_analysts` fixture docstring notes), and this test must stay
    hermetic regardless."""
    async with db_session() as session, strategy_user(session) as (_uid, email):
        async with api_client() as client:
            app.dependency_overrides[get_strategy_research_assistant] = lambda: None
            try:
                token = await _get_token(client, email)
                response = await client.post(
                    "/strategies/research/propose",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"brief": "a mean-reversion idea using RSI"},
                )
            finally:
                del app.dependency_overrides[get_strategy_research_assistant]
    assert response.status_code == 400
    assert "NOT_CONFIGURED" in response.json()["detail"]


@pytest.mark.asyncio
async def test_propose_requires_the_permission_like_every_other_strategy_route():
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.post(
                "/strategies/research/propose",
                headers={"Authorization": f"Bearer {token}"},
                json={"brief": "anything"},
            )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_a_valid_draft_returns_200_is_valid_and_writes_nothing():
    fake_assistant_response = json.dumps(
        {
            "name": "RSI mean reversion",
            "definition": VALID_DEFINITION,
            "rationale": "Buys when RSI turns up from oversold, exits on the reverse cross.",
        }
    )
    fake_assistant = StrategyResearchAssistant(FakeLLMProvider(fake_assistant_response))

    async with db_session() as session, strategy_user(session) as (user_id, email):
        before = await _strategy_row_count(session, user_id)
        async with api_client() as client:
            app.dependency_overrides[get_strategy_research_assistant] = lambda: fake_assistant
            try:
                token = await _get_token(client, email)
                response = await client.post(
                    "/strategies/research/propose",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"brief": "a mean-reversion idea using RSI"},
                )
            finally:
                del app.dependency_overrides[get_strategy_research_assistant]
        after = await _strategy_row_count(session, user_id)

    assert response.status_code == 200
    body = response.json()
    assert body["is_valid"] is True
    assert body["validation_errors"] == []
    assert body["definition"] == VALID_DEFINITION
    assert body["name"] == "RSI mean reversion"
    assert after == before  # no Strategy row was created by this route


@pytest.mark.asyncio
async def test_an_invalid_draft_still_returns_200_with_real_errors_and_writes_nothing():
    fake_assistant_response = json.dumps(
        {
            "name": "MACD idea",
            "definition": INVALID_RESEARCH_DEFINITION,
            "rationale": "Buys on a MACD signal cross.",
        }
    )
    fake_assistant = StrategyResearchAssistant(FakeLLMProvider(fake_assistant_response))

    async with db_session() as session, strategy_user(session) as (user_id, email):
        before = await _strategy_row_count(session, user_id)
        async with api_client() as client:
            app.dependency_overrides[get_strategy_research_assistant] = lambda: fake_assistant
            try:
                token = await _get_token(client, email)
                response = await client.post(
                    "/strategies/research/propose",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"brief": "a MACD-based idea"},
                )
            finally:
                del app.dependency_overrides[get_strategy_research_assistant]
        after = await _strategy_row_count(session, user_id)

    assert response.status_code == 200
    body = response.json()
    assert body["is_valid"] is False
    assert body["validation_errors"]  # non-empty, real errors
    assert after == before  # a structurally invalid draft still writes nothing


@pytest.mark.asyncio
async def test_a_provider_error_is_502_agent_output_invalid():
    class ExplodingProvider:
        name = "fake-llm"

        async def complete(self, *, system: str, user: str, max_tokens: int) -> str:
            raise LLMProviderError("rate limited")

    fake_assistant = StrategyResearchAssistant(ExplodingProvider())

    async with db_session() as session, strategy_user(session) as (_uid, email):
        async with api_client() as client:
            app.dependency_overrides[get_strategy_research_assistant] = lambda: fake_assistant
            try:
                token = await _get_token(client, email)
                response = await client.post(
                    "/strategies/research/propose",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"brief": "anything"},
                )
            finally:
                del app.dependency_overrides[get_strategy_research_assistant]

    assert response.status_code == 502
    assert "AGENT_OUTPUT_INVALID" in response.json()["detail"]
