"""Integration tests for the option chain routes (Phase 89, D108).

The routes are thin, and what they decide is exactly what these assert:
that an absent vendor and an empty listing stay distinguishable (503 vs a
200 with no expiries vs 404), that calls and puts come back as two
strike-ordered lists rather than one interleaved table, and that
`quoted_contracts` counts what actually carries a market.

The provider is swapped on `app.state` for the duration of a test rather
than mocked at the HTTP layer, so the real dependency, the real response
model and the real error mapping are all exercised.
"""

import contextlib
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from apps.api.app.main import app
from apps.api.app.marketdata.option_chain_provider import OptionChain, OptionQuote
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.options.pricing import OptionRight
from tests.api.test_admin import _get_token, api_client, db_session, non_admin_user

EXPIRY = date(2026, 10, 16)


def _quote(strike: str, right: OptionRight, **over) -> OptionQuote:
    base = dict(
        contract_symbol=f"{right.value[0].upper()}{strike}",
        underlying="TQQQ.US",
        expiry=EXPIRY,
        strike=Decimal(strike),
        right=right,
    )
    base.update(over)
    return OptionQuote(**base)  # type: ignore[arg-type]


class _Provider:
    name = "stub"

    def __init__(self, *, expiries=None, chain=None, raises=None):
        self._expiries = [EXPIRY] if expiries is None else expiries
        self._chain = chain
        self._raises = raises

    async def get_expiries(self, underlying):
        if self._raises:
            raise self._raises
        return self._expiries

    async def get_chain(self, underlying, expiry):
        if self._raises:
            raise self._raises
        assert self._chain is not None
        return self._chain


@contextlib.asynccontextmanager
async def provider(value):
    """Swap the app's option chain provider and always put it back — a
    leaked stub would make every later test in the session pass or fail
    for the wrong reason."""
    previous = getattr(app.state, "option_chain_provider", None)
    app.state.option_chain_provider = value
    try:
        yield
    finally:
        app.state.option_chain_provider = previous


@pytest.mark.asyncio
async def test_no_vendor_is_503_not_configured_not_an_empty_chain():
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client, provider(None):
            token = await _get_token(client, email)
            r = await client.get(
                "/market-data/TQQQ.US/option-expiries",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 503
            assert "NOT_CONFIGURED" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_symbol_with_no_listed_options_is_a_200_with_an_empty_list():
    # "This vendor lists no options here" is a real answer and a different
    # fact from "the vendor could not be reached".
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client, provider(_Provider(expiries=[])):
            token = await _get_token(client, email)
            r = await client.get(
                "/market-data/AAPL.US/option-expiries",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200
            assert r.json()["expiries"] == []
            assert r.json()["source"] == "stub"


@pytest.mark.asyncio
async def test_a_vendor_failure_is_a_502_not_an_empty_list():
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client, provider(_Provider(raises=VendorError("boom"))):
            token = await _get_token(client, email)
            r = await client.get(
                "/market-data/TQQQ.US/option-expiries",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 502
            assert "VENDOR_ERROR" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_chain_splits_by_right_sorts_by_strike_and_counts_what_is_quoted():
    chain = OptionChain(
        underlying="TQQQ.US",
        expiry=EXPIRY,
        as_of=datetime(2026, 9, 23, 14, 0, tzinfo=UTC),
        source="stub",
        quotes=(
            _quote("85", OptionRight.CALL),  # listed, never quoted
            _quote("80", OptionRight.CALL, bid=Decimal("3.00"), ask=Decimal("3.20")),
            _quote("80", OptionRight.PUT, last_price=Decimal("1.10")),
        ),
    )
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client, provider(_Provider(chain=chain)):
            token = await _get_token(client, email)
            r = await client.get(
                f"/market-data/TQQQ.US/option-chain?expiry={EXPIRY.isoformat()}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, r.text
            body = r.json()

            # Decimal round-trips as the string it was constructed from;
            # the assertion is about ORDER, so the values are compared as
            # numbers rather than pinning a serialisation detail.
            assert [Decimal(c["strike"]) for c in body["calls"]] == [
                Decimal("80"),
                Decimal("85"),
            ]
            assert [Decimal(p["strike"]) for p in body["puts"]] == [Decimal("80")]

            # Two of the three contracts carry a market; the third is listed
            # with every field null and must not inflate the count.
            assert body["quoted_contracts"] == 2
            unquoted = body["calls"][1]
            assert unquoted["bid"] is None and unquoted["mid"] is None

            # The mid is computed from both sides, never from one.
            assert Decimal(body["calls"][0]["mid"]) == Decimal("3.1")


@pytest.mark.asyncio
async def test_an_expiry_the_vendor_lists_nothing_for_is_a_404():
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client, provider(
            _Provider(raises=DataUnavailableError("no contracts"))
        ):
            token = await _get_token(client, email)
            r = await client.get(
                f"/market-data/TQQQ.US/option-chain?expiry={EXPIRY.isoformat()}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 404
            assert "DATA_UNAVAILABLE" in r.json()["detail"]


@pytest.mark.asyncio
async def test_the_chain_requires_authentication():
    async with api_client() as client, provider(_Provider()):
        r = await client.get(f"/market-data/TQQQ.US/option-chain?expiry={EXPIRY.isoformat()}")
        assert r.status_code == 401
