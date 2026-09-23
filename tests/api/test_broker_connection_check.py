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
    """Binance is catalogued only. The probe must say so rather than let
    it read as a venue failure the operator would go and investigate."""
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")

    async with db_session() as session, admin_user(session) as (_uid, email):
        async with broker(session, "binance") as broker_id, api_client() as client:
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
