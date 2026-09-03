"""Integration tests for the Phase 50 watchlist endpoints, against a real
Postgres instance (same level as tests/api/test_brokers.py - these routes
read and write `watchlists`/`watchlist_items` directly).

The database is shared with every other test module and is not reset
between tests, so nothing here asserts on an absolute total row count.
Each test seeds and tears down the rows it owns.

Two claims carry most of the weight:

1. **Scoping.** A watchlist is reachable only by its owner. Someone else's
   watchlist is a 403, a nonexistent one is a 404 - the same order and the
   same two codes `require_broker_access` uses.
2. **No fabrication.** `GET /watchlists/{id}/quotes` returns one row per
   stored symbol whatever the vendor does. A symbol that cannot be priced
   keeps its row and carries a `DATA_UNAVAILABLE:` sentinel; with no
   vendor configured at all every row does. There is no input to these
   endpoints that produces a price the fake vendor did not return.
"""

import contextlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from apps.api.app.api.dependencies import get_market_data_router
from apps.api.app.auth.security import hash_password
from apps.api.app.db.base import get_session
from apps.api.app.db.models import User, Watchlist
from apps.api.app.main import app
from apps.api.app.marketdata.models import MarketSnapshot
from apps.api.app.marketdata.provider import DataUnavailableError
from apps.api.app.marketdata.router import MarketDataRouter

TEST_PASSWORD = "correct-horse-battery-staple"


class SelectiveFakeProvider:
    """Prices exactly the symbols it was given and raises
    DataUnavailableError for anything else - the same exception a real
    vendor raises for an unlisted ticker, so the router's own
    NO_DATA_AVAILABLE path is what runs, not a test-only shortcut."""

    name = "fake-vendor"

    def __init__(self, prices: dict[str, str]) -> None:
        self._prices = prices

    async def get_snapshot(self, symbol: str) -> MarketSnapshot:
        if symbol not in self._prices:
            raise DataUnavailableError(f"fake vendor has no data for {symbol}")
        return MarketSnapshot(
            symbol=symbol,
            price=Decimal(self._prices[symbol]),
            as_of=datetime.now(UTC),
            source=self.name,
        )


@contextlib.asynccontextmanager
async def db_session():
    gen = get_session()
    session = await anext(gen)
    try:
        yield session
    finally:
        await gen.aclose()


@contextlib.asynccontextmanager
async def plain_user(session):
    """A user with no role at all - watchlists are authentication-only, so
    this is the weakest identity that must still work end to end."""
    user_id = uuid.uuid4()
    email = f"{uuid.uuid4()}@example.com"
    session.add(
        User(id=user_id, email=email, hashed_password=hash_password(TEST_PASSWORD), is_active=True)
    )
    await session.commit()
    try:
        yield user_id, email
    finally:
        await session.rollback()
        # watchlist_items goes with it via ON DELETE CASCADE (migration 0014).
        await session.execute(delete(Watchlist).where(Watchlist.user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()


@contextlib.asynccontextmanager
async def api_client():
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@contextlib.contextmanager
def market_data(router: MarketDataRouter | None):
    """Override the one dependency both this route and
    `GET /market-data/{symbol}/quote` resolve quotes through. `None` is the
    real NOT_CONFIGURED state (D008/D015), not a stub for one."""
    app.dependency_overrides[get_market_data_router] = lambda: router
    try:
        yield
    finally:
        del app.dependency_overrides[get_market_data_router]


async def _get_token(client: AsyncClient, email: str) -> str:
    response = await client.post(
        "/auth/login", data={"username": email, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
    return response.json()["access_token"]


async def _create_watchlist(client: AsyncClient, token: str, name: str) -> str:
    response = await client.post(
        "/watchlists", headers={"Authorization": f"Bearer {token}"}, json={"name": name}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _add(client: AsyncClient, token: str, watchlist_id: str, symbol: str):
    return await client.post(
        f"/watchlists/{watchlist_id}/items",
        headers={"Authorization": f"Bearer {token}"},
        json={"symbol": symbol},
    )


# ---------------------------------------------------------------------
# Create / list / delete
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_then_list_returns_the_watchlist_with_no_symbols():
    async with db_session() as session, plain_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            created = await client.post(
                "/watchlists",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Semis"},
            )
            listing = await client.get(
                "/watchlists", headers={"Authorization": f"Bearer {token}"}
            )

    assert created.status_code == 201
    body = created.json()
    assert set(body) == {"id", "name", "created_at", "symbols"}
    assert body["name"] == "Semis"
    assert body["symbols"] == []

    assert listing.status_code == 200
    rows = listing.json()["watchlists"]
    assert [r["id"] for r in rows] == [body["id"]]
    assert rows[0]["symbols"] == []


@pytest.mark.asyncio
async def test_a_fresh_account_has_no_auto_created_default_watchlist():
    """The explicit-creation decision, asserted rather than assumed: a GET
    must not write a row, so a user who has created nothing sees nothing."""
    async with db_session() as session, plain_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            first = await client.get("/watchlists", headers={"Authorization": f"Bearer {token}"})
            second = await client.get("/watchlists", headers={"Authorization": f"Bearer {token}"})

    assert first.status_code == 200
    assert first.json()["watchlists"] == []
    # Reading twice still creates nothing.
    assert second.json()["watchlists"] == []


@pytest.mark.asyncio
async def test_blank_and_oversized_names_are_rejected():
    async with db_session() as session, plain_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            blank = await client.post("/watchlists", headers=headers, json={"name": "   "})
            too_long = await client.post("/watchlists", headers=headers, json={"name": "x" * 65})

    assert blank.status_code == 422
    assert too_long.status_code == 422


@pytest.mark.asyncio
async def test_delete_removes_the_watchlist_and_its_items():
    async with db_session() as session, plain_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            watchlist_id = await _create_watchlist(client, token, "Doomed")
            assert (await _add(client, token, watchlist_id, "AAPL.US")).status_code == 201

            deleted = await client.delete(f"/watchlists/{watchlist_id}", headers=headers)
            listing = await client.get("/watchlists", headers=headers)
            # The cascade means the items are gone too, which is only
            # observable through the now-404 quotes route.
            quotes = await client.get(f"/watchlists/{watchlist_id}/quotes", headers=headers)

    assert deleted.status_code == 204
    assert listing.json()["watchlists"] == []
    assert quotes.status_code == 404


@pytest.mark.asyncio
async def test_delete_of_a_nonexistent_watchlist_is_404():
    missing = uuid.uuid4()
    async with db_session() as session, plain_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.delete(
                f"/watchlists/{missing}", headers={"Authorization": f"Bearer {token}"}
            )
    assert response.status_code == 404
    assert str(missing) in response.json()["detail"]


@pytest.mark.asyncio
async def test_every_route_is_401_without_a_token():
    async with api_client() as client:
        assert (await client.get("/watchlists")).status_code == 401
        assert (await client.post("/watchlists", json={"name": "x"})).status_code == 401
        some_id = uuid.uuid4()
        assert (await client.delete(f"/watchlists/{some_id}")).status_code == 401
        assert (
            await client.post(f"/watchlists/{some_id}/items", json={"symbol": "AAPL"})
        ).status_code == 401
        assert (await client.delete(f"/watchlists/{some_id}/items/AAPL")).status_code == 401
        assert (await client.get(f"/watchlists/{some_id}/quotes")).status_code == 401


@pytest.mark.asyncio
async def test_listing_rejects_an_out_of_range_limit_and_a_negative_offset():
    async with db_session() as session, plain_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            over = await client.get("/watchlists?limit=501", headers=headers)
            negative = await client.get("/watchlists?offset=-1", headers=headers)
    assert over.status_code == 422
    assert negative.status_code == 422


# ---------------------------------------------------------------------
# Items: add, duplicate, remove
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_and_remove_a_symbol():
    async with db_session() as session, plain_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            watchlist_id = await _create_watchlist(client, token, "Research")

            added = await _add(client, token, watchlist_id, "AAPL.US")
            after_add = await client.get("/watchlists", headers=headers)
            removed = await client.delete(
                f"/watchlists/{watchlist_id}/items/AAPL.US", headers=headers
            )
            after_remove = await client.get("/watchlists", headers=headers)

    assert added.status_code == 201
    assert added.json()["symbols"] == ["AAPL.US"]
    assert after_add.json()["watchlists"][0]["symbols"] == ["AAPL.US"]
    assert removed.status_code == 204
    assert after_remove.json()["watchlists"][0]["symbols"] == []


@pytest.mark.asyncio
async def test_a_duplicate_symbol_is_rejected_with_409():
    async with db_session() as session, plain_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            watchlist_id = await _create_watchlist(client, token, "Dupes")
            first = await _add(client, token, watchlist_id, "MSFT.US")
            second = await _add(client, token, watchlist_id, "MSFT.US")
            listing = await client.get(
                "/watchlists", headers={"Authorization": f"Bearer {token}"}
            )

    assert first.status_code == 201
    assert second.status_code == 409
    assert "MSFT.US" in second.json()["detail"]
    # The rejected add left no second row behind.
    assert listing.json()["watchlists"][0]["symbols"] == ["MSFT.US"]


@pytest.mark.asyncio
async def test_symbols_are_normalized_so_case_cannot_dodge_the_duplicate_check():
    async with db_session() as session, plain_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            watchlist_id = await _create_watchlist(client, token, "Casing")
            first = await _add(client, token, watchlist_id, "  nvda.us ")
            second = await _add(client, token, watchlist_id, "NVDA.US")
            listing = await client.get("/watchlists", headers=headers)
            # The path segment is normalized the same way on the way out.
            removed = await client.delete(
                f"/watchlists/{watchlist_id}/items/nvda.us", headers=headers
            )
            after = await client.get("/watchlists", headers=headers)

    assert first.status_code == 201
    assert first.json()["symbols"] == ["NVDA.US"]
    assert second.status_code == 409
    assert listing.json()["watchlists"][0]["symbols"] == ["NVDA.US"]
    assert removed.status_code == 204
    assert after.json()["watchlists"][0]["symbols"] == []


@pytest.mark.asyncio
async def test_removing_a_symbol_that_is_not_on_the_list_is_404():
    async with db_session() as session, plain_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            watchlist_id = await _create_watchlist(client, token, "Sparse")
            response = await client.delete(
                f"/watchlists/{watchlist_id}/items/TSLA.US",
                headers={"Authorization": f"Bearer {token}"},
            )
    assert response.status_code == 404
    assert "TSLA.US" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_blank_symbol_is_rejected():
    async with db_session() as session, plain_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            watchlist_id = await _create_watchlist(client, token, "Blank")
            response = await _add(client, token, watchlist_id, "   ")
    assert response.status_code == 422


# ---------------------------------------------------------------------
# Cross-user access
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_another_users_watchlist_is_invisible_in_the_listing():
    async with db_session() as session:
        async with plain_user(session) as (_a, email_a), plain_user(session) as (_b, email_b):
            async with api_client() as client:
                token_a = await _get_token(client, email_a)
                token_b = await _get_token(client, email_b)
                await _create_watchlist(client, token_a, "A's list")
                listing_b = await client.get(
                    "/watchlists", headers={"Authorization": f"Bearer {token_b}"}
                )

            assert listing_b.status_code == 200
            assert listing_b.json()["watchlists"] == []


@pytest.mark.asyncio
async def test_every_watchlist_scoped_route_403s_for_a_non_owner():
    """One test over all four id-addressed routes: a watchlist that exists
    but is not yours answers 403 everywhere, never 200 and never 404 - a
    404 here would be a different answer from the one this codebase gives
    for an existing-but-ungranted broker."""
    async with db_session() as session:
        async with plain_user(session) as (_a, email_a), plain_user(session) as (_b, email_b):
            async with api_client() as client:
                token_a = await _get_token(client, email_a)
                token_b = await _get_token(client, email_b)
                watchlist_id = await _create_watchlist(client, token_a, "A's private list")
                assert (await _add(client, token_a, watchlist_id, "AAPL.US")).status_code == 201

                headers_b = {"Authorization": f"Bearer {token_b}"}
                responses = {
                    "quotes": await client.get(
                        f"/watchlists/{watchlist_id}/quotes", headers=headers_b
                    ),
                    "add_item": await client.post(
                        f"/watchlists/{watchlist_id}/items",
                        headers=headers_b,
                        json={"symbol": "GOOG.US"},
                    ),
                    "remove_item": await client.delete(
                        f"/watchlists/{watchlist_id}/items/AAPL.US", headers=headers_b
                    ),
                    "delete": await client.delete(
                        f"/watchlists/{watchlist_id}", headers=headers_b
                    ),
                }
                # Owner's list is untouched by any of the above.
                owner_listing = await client.get(
                    "/watchlists", headers={"Authorization": f"Bearer {token_a}"}
                )

            for name, response in responses.items():
                assert response.status_code == 403, f"{name} returned {response.status_code}"
                assert "not yours" in response.json()["detail"]

            assert owner_listing.json()["watchlists"][0]["symbols"] == ["AAPL.US"]


@pytest.mark.asyncio
async def test_a_nonexistent_watchlist_is_404_not_403():
    missing = uuid.uuid4()
    async with db_session() as session, plain_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.get(
                f"/watchlists/{missing}/quotes", headers={"Authorization": f"Bearer {token}"}
            )
    assert response.status_code == 404


# ---------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quotes_mixes_real_prices_and_data_unavailable_rows():
    """The headline claim. Two symbols the fake vendor prices, one it does
    not: three rows come back, in stored order, and the unpriceable one
    carries a sentinel rather than a number or a missing row."""
    router = MarketDataRouter([SelectiveFakeProvider({"AAPL.US": "195.25", "MSFT.US": "410.10"})])

    async with db_session() as session, plain_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            watchlist_id = await _create_watchlist(client, token, "Mixed")
            for symbol in ("AAPL.US", "MSFT.US", "NOSUCH.US"):
                assert (await _add(client, token, watchlist_id, symbol)).status_code == 201

            with market_data(router):
                response = await client.get(
                    f"/watchlists/{watchlist_id}/quotes",
                    headers={"Authorization": f"Bearer {token}"},
                )

    assert response.status_code == 200
    body = response.json()
    assert body["watchlist_id"] == watchlist_id
    assert body["name"] == "Mixed"
    assert body["market_data_configured"] is True

    rows = {row["symbol"]: row for row in body["quotes"]}
    # No row is dropped: the response is exactly as long as the watchlist.
    assert [row["symbol"] for row in body["quotes"]] == ["AAPL.US", "MSFT.US", "NOSUCH.US"]

    assert Decimal(rows["AAPL.US"]["price"]) == Decimal("195.25")
    assert rows["AAPL.US"]["source"] == "fake-vendor"
    assert rows["AAPL.US"]["as_of"] is not None
    assert rows["AAPL.US"]["unavailable"] is None

    assert Decimal(rows["MSFT.US"]["price"]) == Decimal("410.10")

    bad = rows["NOSUCH.US"]
    assert bad["price"] is None
    assert bad["as_of"] is None
    assert bad["source"] is None
    assert bad["unavailable"].startswith("DATA_UNAVAILABLE:")
    # The real underlying cause is preserved, not replaced.
    assert "NO_DATA_AVAILABLE" in bad["unavailable"]
    assert "NOSUCH.US" in bad["unavailable"]


@pytest.mark.asyncio
async def test_quotes_with_no_vendor_configured_marks_every_symbol_unavailable():
    """The NOT_CONFIGURED case (D008/D015). Not one fabricated price, not
    a silently empty list - one row per symbol, each saying why."""
    async with db_session() as session, plain_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            watchlist_id = await _create_watchlist(client, token, "No vendor")
            for symbol in ("AAPL.US", "MSFT.US"):
                assert (await _add(client, token, watchlist_id, symbol)).status_code == 201

            with market_data(None):
                response = await client.get(
                    f"/watchlists/{watchlist_id}/quotes",
                    headers={"Authorization": f"Bearer {token}"},
                )

    assert response.status_code == 200
    body = response.json()
    assert body["market_data_configured"] is False
    assert len(body["quotes"]) == 2
    for row in body["quotes"]:
        assert row["price"] is None
        assert row["as_of"] is None
        assert row["source"] is None
        assert row["unavailable"].startswith("DATA_UNAVAILABLE:")
        assert "NOT_CONFIGURED" in row["unavailable"]


@pytest.mark.asyncio
async def test_quotes_on_an_empty_watchlist_is_an_empty_list_not_an_error():
    router = MarketDataRouter([SelectiveFakeProvider({})])
    async with db_session() as session, plain_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            watchlist_id = await _create_watchlist(client, token, "Empty")
            with market_data(router):
                response = await client.get(
                    f"/watchlists/{watchlist_id}/quotes",
                    headers={"Authorization": f"Bearer {token}"},
                )

    assert response.status_code == 200
    assert response.json()["quotes"] == []
    assert response.json()["market_data_configured"] is True


@pytest.mark.asyncio
async def test_quotes_uses_the_same_resolution_path_as_the_single_quote_endpoint():
    """Same override, same vendor, same symbol: the price and source the
    watchlist reports are the ones `GET /market-data/{symbol}/quote`
    reports. If the two ever stopped sharing `resolve_quote` this would be
    the test that noticed."""
    router = MarketDataRouter([SelectiveFakeProvider({"AAPL.US": "195.25"})])

    async with db_session() as session, plain_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            watchlist_id = await _create_watchlist(client, token, "Parity")
            assert (await _add(client, token, watchlist_id, "AAPL.US")).status_code == 201

            with market_data(router):
                single = await client.get("/market-data/AAPL.US/quote", headers=headers)
                listed = await client.get(
                    f"/watchlists/{watchlist_id}/quotes", headers=headers
                )
                # And the unavailable side of the same parity.
                single_missing = await client.get("/market-data/NOPE.US/quote", headers=headers)

    assert single.status_code == 200
    row = listed.json()["quotes"][0]
    assert Decimal(row["price"]) == Decimal(single.json()["price"])
    assert row["source"] == single.json()["source"]

    # The single-quote endpoint still maps the same unavailability to its
    # own documented 404 - factoring the decision out changed no contract.
    assert single_missing.status_code == 404
    assert single_missing.json()["detail"].startswith("NO_DATA_AVAILABLE:")


@pytest.mark.asyncio
async def test_single_quote_endpoint_still_503s_when_no_vendor_is_configured():
    """The other half of that contract check: NOT_CONFIGURED is still a
    503 there, even though the same condition is a 200-with-sentinels on
    the watchlist route."""
    async with db_session() as session, plain_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            with market_data(None):
                response = await client.get(
                    "/market-data/AAPL.US/quote",
                    headers={"Authorization": f"Bearer {token}"},
                )
    assert response.status_code == 503
    assert response.json()["detail"].startswith("NOT_CONFIGURED:")
