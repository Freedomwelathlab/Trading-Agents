"""The read-only connection probe and env-supplied credentials (Phase 94, D113).

Separate from `test_broker_bridge.py` because these tests are about a
different question. That file asks what must never come OUT of the bridge
(a secret, a claim of an adapter it lacks); this one asks whether the
bridge can be REACHED, and whether its answer is honest in both
directions.

The one rule every test here enforces: a connection check never trades.
The `submit_order` on the reachable stub raises if it is ever called.
"""

from decimal import Decimal

import pytest

from apps.api.app.risk.models import AccountState
from tests.api.test_admin import _get_token, admin_user, api_client, db_session, non_admin_user
from tests.api.test_broker_bridge import KEY, _h, broker, encryption_key

BUILD_ADAPTER = "apps.api.app.api.routes.broker_bridge.build_adapter"


def _account(cash: str) -> AccountState:
    return AccountState(
        equity=Decimal(cash), cash=Decimal(cash), current_exposure=Decimal("0")
    )


@pytest.mark.asyncio
async def test_the_catalogue_names_the_environment_variable_for_every_field():
    """An operator with env vars and no console must be told the exact
    string, not left to infer the prefix and the case."""
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            body = (await client.get("/brokers/providers", headers=_h(token))).json()

    by_name = {p["provider"]: p for p in body["providers"]}
    ig = {f["name"]: f for f in by_name["ig"]["credential_fields"]}
    assert ig["password"]["env_var"] == "IG_PASSWORD"
    assert ig["account_type"]["env_var"] == "IG_ACCOUNT_TYPE"
    kraken = {f["name"]: f for f in by_name["kraken"]["credential_fields"]}
    assert kraken["api_secret"]["env_var"] == "KRAKEN_API_SECRET"


@pytest.mark.asyncio
async def test_a_probe_without_credentials_names_the_variables_to_set(monkeypatch):
    """The probe's most common answer has to be actionable."""
    for name in ("KRAKEN_API_KEY", "KRAKEN_API_SECRET"):
        monkeypatch.delenv(name, raising=False)

    async with db_session() as session, admin_user(session) as (_uid, email):
        async with broker(session, "kraken") as broker_id, api_client() as client:
            async with encryption_key(KEY):
                token = await _get_token(client, email)
                r = await client.post(f"/brokers/{broker_id}/connection-check", headers=_h(token))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reachable"] is False
    assert body["credential_source"] is None
    assert body["cash"] is None
    assert "NOT_CONFIGURED" in body["detail"]
    assert "KRAKEN_API_KEY" in body["detail"]
    assert "KRAKEN_API_SECRET" in body["detail"]


@pytest.mark.asyncio
async def test_credentials_do_not_conjure_an_adapter_for_a_catalogued_venue(monkeypatch):
    """moomoo is catalogued only (gateway-only, D109). The probe must say
    so rather than let it read as a venue failure the operator would go and
    investigate. Binance was the example until it gained an adapter (D116)."""
    monkeypatch.setenv("MOOMOO_OPEND_HOST", "127.0.0.1")
    monkeypatch.setenv("MOOMOO_OPEND_PORT", "11111")
    monkeypatch.setenv("MOOMOO_TRADE_PASSWORD", "s")

    async with db_session() as session, admin_user(session) as (_uid, email):
        async with broker(session, "moomoo") as broker_id, api_client() as client:
            async with encryption_key(KEY):
                token = await _get_token(client, email)
                r = await client.post(f"/brokers/{broker_id}/connection-check", headers=_h(token))

    body = r.json()
    assert body["reachable"] is False
    assert body["credential_source"] == "environment"
    assert "NO_ADAPTER" in body["detail"]


@pytest.mark.asyncio
async def test_a_venue_refusal_is_a_finding_not_a_server_error(monkeypatch):
    """A rejected login is an ANSWER. A 500 would send an operator to read
    this platform's logs for a problem that is entirely at the venue."""
    monkeypatch.setenv("KRAKEN_API_KEY", "k")
    monkeypatch.setenv("KRAKEN_API_SECRET", "s")

    class Refusing:
        @property
        def cash(self):
            raise RuntimeError("EAPI:Invalid key")

        def get_account_state(self, *, marks):
            raise RuntimeError("EAPI:Invalid key")

        @property
        def positions(self):
            return {}

    monkeypatch.setattr(BUILD_ADAPTER, lambda provider, credentials: Refusing())

    async with db_session() as session, admin_user(session) as (_uid, email):
        async with broker(session, "kraken") as broker_id, api_client() as client:
            async with encryption_key(KEY):
                token = await _get_token(client, email)
                r = await client.post(f"/brokers/{broker_id}/connection-check", headers=_h(token))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reachable"] is False
    assert "EAPI:Invalid key" in body["detail"]
    assert body["cash"] is None, "a failed probe must not report a cash figure"


@pytest.mark.asyncio
async def test_a_reachable_venue_reports_its_real_balance_and_places_no_order(monkeypatch):
    monkeypatch.setenv("KRAKEN_API_KEY", "k")
    monkeypatch.setenv("KRAKEN_API_SECRET", "s")

    class Reachable:
        @property
        def cash(self):
            return Decimal("1234.56")

        def get_account_state(self, *, marks):
            return _account("1234.56")

        @property
        def positions(self):
            return {"XBTUSD": Decimal("0.5")}

        def submit_order(self, order, *, market_price):  # pragma: no cover
            raise AssertionError("a connection check must never submit an order")

    monkeypatch.setattr(BUILD_ADAPTER, lambda provider, credentials: Reachable())

    async with db_session() as session, admin_user(session) as (_uid, email):
        async with broker(session, "kraken") as broker_id, api_client() as client:
            async with encryption_key(KEY):
                token = await _get_token(client, email)
                r = await client.post(f"/brokers/{broker_id}/connection-check", headers=_h(token))

    body = r.json()
    assert body["reachable"] is True
    assert body["credential_source"] == "environment"
    # The venue's own figure, as a string - no decimal lost in JSON.
    assert body["cash"] == "1234.56"
    # Symbols only: a position's SIZE is not needed to prove a credential.
    assert body["position_symbols"] == ["XBTUSD"]


@pytest.mark.asyncio
async def test_the_probe_works_with_no_encryption_key_set(monkeypatch):
    """The whole point of D113: env credentials do not need the store, so
    a deployment with no `BROKER_CREDENTIAL_ENCRYPTION_KEY` can still
    reach a venue instead of being refused before it starts."""
    monkeypatch.setenv("KRAKEN_API_KEY", "k")
    monkeypatch.setenv("KRAKEN_API_SECRET", "s")

    class Reachable:
        @property
        def cash(self):
            return Decimal("10")

        def get_account_state(self, *, marks):
            return _account("10")

        @property
        def positions(self):
            return {}

    monkeypatch.setattr(BUILD_ADAPTER, lambda provider, credentials: Reachable())

    async with db_session() as session, admin_user(session) as (_uid, email):
        async with broker(session, "kraken") as broker_id, api_client() as client:
            async with encryption_key(None):
                token = await _get_token(client, email)
                r = await client.post(f"/brokers/{broker_id}/connection-check", headers=_h(token))

    body = r.json()
    assert body["reachable"] is True
    assert body["credential_source"] == "environment"


@pytest.mark.asyncio
async def test_stored_credentials_beat_the_environment(monkeypatch):
    """Both sources exist. The set a person entered for THIS broker wins,
    and the loser is not consulted for fields the winner lacks."""
    monkeypatch.setenv("KRAKEN_API_KEY", "from-env")
    monkeypatch.setenv("KRAKEN_API_SECRET", "from-env")
    seen: dict[str, str] = {}

    class Recording:
        @property
        def cash(self):
            return Decimal("1")

        def get_account_state(self, *, marks):
            return _account("1")

        @property
        def positions(self):
            return {}

    def _build(provider, credentials):
        seen.update(credentials)
        return Recording()

    monkeypatch.setattr(BUILD_ADAPTER, _build)

    async with db_session() as session, admin_user(session) as (_uid, email):
        async with broker(session, "kraken") as broker_id, api_client() as client:
            async with encryption_key(KEY):
                token = await _get_token(client, email)
                saved = await client.put(
                    f"/brokers/{broker_id}/credentials",
                    headers=_h(token),
                    json={"fields": {"api_key": "from-store", "api_secret": "from-store"}},
                )
                assert saved.status_code == 200, saved.text
                r = await client.post(f"/brokers/{broker_id}/connection-check", headers=_h(token))

    assert r.json()["credential_source"] == "stored"
    assert seen["api_key"] == "from-store"


@pytest.mark.asyncio
async def test_the_probe_is_admin_only():
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with broker(session, "kraken") as broker_id, api_client() as client:
            token = await _get_token(client, email)
            r = await client.post(f"/brokers/{broker_id}/connection-check", headers=_h(token))
    assert r.status_code == 403

# --- Phase 95 (D114): which account answered, and probing without a row -----


@pytest.mark.asyncio
async def test_the_probe_says_which_account_answered_and_never_a_secret(monkeypatch):
    """A green tick that does not name the account is the dangerous kind.

    Two IG credential sets can sit in one environment and only one of them
    is the demo. `account_type` is PUBLIC in the registry precisely so an
    operator can tell them apart; `password` is SECRET and must not appear
    in any shape.
    """
    monkeypatch.setenv("IG_API_KEY", "ig-key")
    monkeypatch.setenv("IG_USERNAME", "ig-user")
    monkeypatch.setenv("IG_PASSWORD", "SUPER-SECRET-PASSWORD")
    monkeypatch.setenv("IG_ACCOUNT_TYPE", "DEMO")

    class Reachable:
        @property
        def cash(self):
            return Decimal("500")

        def get_account_state(self, *, marks):
            return _account("500")

        @property
        def positions(self):
            return {}

    monkeypatch.setattr(BUILD_ADAPTER, lambda provider, credentials: Reachable())

    async with db_session() as session, admin_user(session) as (_uid, email):
        async with broker(session, "ig") as broker_id, api_client() as client:
            async with encryption_key(KEY):
                token = await _get_token(client, email)
                r = await client.post(f"/brokers/{broker_id}/connection-check", headers=_h(token))

    body = r.json()
    assert body["reachable"] is True
    account = {f["name"]: f["value"] for f in body["account"]}
    assert account["account_type"] == "DEMO"
    assert account["username"] == "ig-user"
    # The secret is absent from the field list AND from the whole body.
    assert "password" not in account
    assert "SUPER-SECRET-PASSWORD" not in r.text


@pytest.mark.asyncio
async def test_a_provider_can_be_probed_with_no_broker_row(monkeypatch):
    """The point of this route: answer "does this key work" before asking
    anyone to create a broker row and grant it to themselves."""
    monkeypatch.setenv("KRAKEN_API_KEY", "k")
    monkeypatch.setenv("KRAKEN_API_SECRET", "s")

    class Reachable:
        @property
        def cash(self):
            return Decimal("42.5")

        def get_account_state(self, *, marks):
            return _account("42.5")

        @property
        def positions(self):
            return {}

    monkeypatch.setattr(BUILD_ADAPTER, lambda provider, credentials: Reachable())

    async with db_session() as session, admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            r = await client.post(
                "/brokers/providers/kraken/connection-check", headers=_h(token)
            )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reachable"] is True
    assert body["broker_id"] is None
    assert body["credential_source"] == "environment"
    assert body["cash"] == "42.5"


@pytest.mark.asyncio
async def test_the_provider_route_is_not_parsed_as_a_broker_id(monkeypatch):
    """`providers` must not be read as a UUID.

    FastAPI matches in registration order with no preference for a static
    segment, so this is an ordering property of the router and not a
    property of the path. Phase 90 hit exactly this on GET /providers;
    asserting it here is what stops a later edit reintroducing it.
    """
    for name in ("KRAKEN_API_KEY", "KRAKEN_API_SECRET"):
        monkeypatch.delenv(name, raising=False)

    async with db_session() as session, admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            r = await client.post(
                "/brokers/providers/kraken/connection-check", headers=_h(token)
            )

    assert r.status_code == 200, r.text  # not 422 from a UUID parse
    assert r.json()["reachable"] is False
    assert "KRAKEN_API_SECRET" in r.json()["detail"]


@pytest.mark.asyncio
async def test_probing_an_unknown_provider_is_a_404():
    async with db_session() as session, admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            r = await client.post(
                "/brokers/providers/not-a-venue/connection-check", headers=_h(token)
            )
    assert r.status_code == 404
    assert "UNKNOWN_PROVIDER" in r.json()["detail"]


@pytest.mark.asyncio
async def test_the_provider_probe_is_admin_only():
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            r = await client.post(
                "/brokers/providers/kraken/connection-check", headers=_h(token)
            )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_the_probe_does_not_need_a_price_for_every_asset_held(monkeypatch):
    """Regression for the first production probe of a working Kraken key.

    The account held SOL; the probe called `get_account_state(marks={})`;
    that correctly refused to value SOL with no price. Proving a credential
    needs the venue to ANSWER, not a mark for every holding, so the probe
    reads cash and positions and never asks for a valuation.
    """
    monkeypatch.setenv("KRAKEN_API_KEY", "k")
    monkeypatch.setenv("KRAKEN_API_SECRET", "s")

    class HoldsSol:
        @property
        def cash(self):
            return Decimal("12.34")

        @property
        def positions(self):
            return {"SOL": Decimal("3.5")}

        def get_account_state(self, *, marks):
            raise AssertionError(
                "the probe must not value the account - it has no mark for SOL"
            )

    monkeypatch.setattr(BUILD_ADAPTER, lambda provider, credentials: HoldsSol())

    async with db_session() as session, admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            r = await client.post(
                "/brokers/providers/kraken/connection-check", headers=_h(token)
            )

    body = r.json()
    assert body["reachable"] is True, body["detail"]
    assert body["cash"] == "12.34"
    assert body["position_symbols"] == ["SOL"]


@pytest.mark.asyncio
async def test_the_probe_masks_an_api_key_it_reports(monkeypatch):
    """A report gets screenshotted. The first production probe printed a
    whole Kraken API key; the key's first and last four characters are
    enough to tell two keys apart."""
    full_key = "HxEBabcdefghijklmnopqrstuvwxyz0123456789ZZZZ"
    monkeypatch.setenv("KRAKEN_API_KEY", full_key)
    monkeypatch.setenv("KRAKEN_API_SECRET", "s")

    class Reachable:
        @property
        def cash(self):
            return Decimal("1")

        @property
        def positions(self):
            return {}

    monkeypatch.setattr(BUILD_ADAPTER, lambda provider, credentials: Reachable())

    async with db_session() as session, admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            r = await client.post(
                "/brokers/providers/kraken/connection-check", headers=_h(token)
            )

    assert full_key not in r.text
    account = {f["name"]: f["value"] for f in r.json()["account"]}
    assert account["api_key"] == "HxEB\u2026ZZZZ"


def test_a_short_identifier_is_masked_whole_rather_than_half_revealed():
    from apps.api.app.api.routes.broker_bridge import mask_identifier

    assert mask_identifier("abc123") == "\u2022" * 8
    assert "abc" not in mask_identifier("abc123")


@pytest.mark.asyncio
async def test_the_paper_simulator_tests_green_without_credentials():
    async with db_session() as session, admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            r = await client.post("/brokers/providers/paper/connection-check", headers=_h(token))
    body = r.json()
    assert body["reachable"] is True
    assert body["cash"] is None  # the provider has no book; a broker row does
    assert "no credentials" in body["detail"]


@pytest.mark.asyncio
async def test_a_paper_broker_row_reports_its_own_book_without_trading():
    async with db_session() as session, admin_user(session) as (_uid, email):
        async with broker(session, "paper") as broker_id, api_client() as client:
            token = await _get_token(client, email)
            r = await client.post(f"/brokers/{broker_id}/connection-check", headers=_h(token))
    body = r.json()
    assert r.status_code == 200, r.text
    assert body["reachable"] is True
    assert "not traded yet" in body["detail"]


@pytest.mark.asyncio
async def test_an_environment_probe_names_the_variable_family_that_answered(monkeypatch):
    for name in ("LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LONGPORT_APP_KEY", "a")
    monkeypatch.setenv("LONGPORT_APP_SECRET", "b")
    monkeypatch.setenv("LONGPORT_ACCESS_TOKEN", "c")

    class Reachable:
        cash = Decimal("100")
        positions: dict[str, Decimal] = {}

    monkeypatch.setattr(BUILD_ADAPTER, lambda provider, credentials: Reachable())
    async with db_session() as session, admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            r = await client.post(
                "/brokers/providers/longbridge/connection-check", headers=_h(token)
            )
    body = r.json()
    assert body["reachable"] is True
    assert body["credential_source"] == "environment"
    assert body["credential_variables"] == "LONGPORT_*"
