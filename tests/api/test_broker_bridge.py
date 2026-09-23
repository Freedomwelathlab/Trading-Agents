"""The broker bridge's HTTP surface (Phase 90, D109).

The tests that matter here are the ones about what must NOT come back. A
credential store is only as good as its worst response, so a secret must
be absent from every shape a route can return, a catalogued provider must
never look implemented, and a rotated key must produce an actionable
message rather than a broker that silently behaves as though it was never
configured.

`/brokers/providers` is also asserted to resolve at all: it shares a
prefix with the discovery router's `/brokers/{broker_id}`, and a static
segment losing to a path parameter is the kind of thing that only shows
up at request time.
"""

import contextlib
import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import delete

from apps.api.app.core.config import get_settings
from apps.api.app.db.models import Broker, BrokerCredential, BrokerKind
from tests.api.test_admin import (
    _get_token,
    admin_user,
    api_client,
    db_session,
    non_admin_user,
)

KEY = Fernet.generate_key().decode()
OTHER_KEY = Fernet.generate_key().decode()


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@contextlib.asynccontextmanager
async def broker(session, provider: str = "kraken"):
    broker_id = uuid.uuid4()
    session.add(
        Broker(id=broker_id, name=f"bridge-{provider}", kind=BrokerKind.PAPER, provider=provider)
    )
    await session.commit()
    try:
        yield broker_id
    finally:
        await session.rollback()
        await session.execute(
            delete(BrokerCredential).where(BrokerCredential.broker_id == broker_id)
        )
        await session.execute(delete(Broker).where(Broker.id == broker_id))
        await session.commit()


@contextlib.asynccontextmanager
async def encryption_key(value: str | None):
    """Set the process's encryption key for one test and always restore it.

    `get_settings` is cached, so this mutates the cached instance — the
    same thing the emergency-stop tests do with their own settings.
    """
    settings = get_settings()
    previous = settings.broker_credential_encryption_key
    settings.broker_credential_encryption_key = value
    try:
        yield settings
    finally:
        settings.broker_credential_encryption_key = previous


@pytest.mark.asyncio
async def test_the_catalogue_resolves_and_never_claims_an_adapter_it_lacks():
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            r = await client.get("/brokers/providers", headers=_h(token))
            assert r.status_code == 200, r.text
            body = r.json()

    by_name = {p["provider"]: p for p in body["providers"]}
    # Every venue the bridge was asked for is listed.
    for name in ("ibkr", "ig", "moomoo", "longbridge", "kraken", "binance", "paper"):
        assert name in by_name, name

    # Nothing claims an adapter this build does not have.
    assert all(p["adapter_status"] in ("implemented", "catalogued") for p in body["providers"])
    assert "catalogued" in body["note"]

    # Capabilities are the venue's, and they differ — a catalogue where
    # every entry trades everything would be describing nothing.
    assert by_name["kraken"]["asset_classes"] == ["crypto"]
    assert "forex" in by_name["ibkr"]["asset_classes"]
    assert "forex" not in by_name["longbridge"]["asset_classes"]

    # The credential form is described by the catalogue, so the UI never
    # has to know a broker by name.
    ig_fields = {f["name"]: f for f in by_name["ig"]["credential_fields"]}
    assert ig_fields["password"]["secret"] is True
    assert ig_fields["api_key"]["secret"] is False


@pytest.mark.asyncio
async def test_credentials_round_trip_without_the_secret_ever_coming_back():
    async with db_session() as session, admin_user(session) as (_uid, email):
        async with broker(session, "kraken") as broker_id, api_client() as client:
            async with encryption_key(KEY):
                token = await _get_token(client, email)

                r = await client.get(f"/brokers/{broker_id}/credentials", headers=_h(token))
                assert r.status_code == 200, r.text
                assert r.json()["configured"] is False
                assert all(f["present"] is False for f in r.json()["fields"])

                r = await client.put(
                    f"/brokers/{broker_id}/credentials",
                    json={"fields": {"api_key": "KRAKEN-PUBLIC", "api_secret": "s3cr3t-value"}},
                    headers=_h(token),
                )
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["configured"] is True
                assert body["key_matches"] is True

                fields = {f["name"]: f for f in body["fields"]}
                assert fields["api_secret"]["present"] is True
                # The secret is never in the response, in any field.
                assert fields["api_secret"]["value"] is None
                assert "s3cr3t-value" not in r.text
                # A non-secret IS returned: an operator has to be able to
                # confirm which account is wired.
                assert fields["api_key"]["value"] == "KRAKEN-PUBLIC"

                # And not on the read path either.
                r = await client.get(f"/brokers/{broker_id}/credentials", headers=_h(token))
                assert "s3cr3t-value" not in r.text
                assert r.json()["configured"] is True


@pytest.mark.asyncio
async def test_a_rotated_key_says_re_enter_rather_than_looking_unconfigured():
    async with db_session() as session, admin_user(session) as (_uid, email):
        async with broker(session, "kraken") as broker_id, api_client() as client:
            async with encryption_key(KEY):
                token = await _get_token(client, email)
                r = await client.put(
                    f"/brokers/{broker_id}/credentials",
                    json={"fields": {"api_key": "K", "api_secret": "S"}},
                    headers=_h(token),
                )
                assert r.status_code == 200

            async with encryption_key(OTHER_KEY):
                r = await client.get(f"/brokers/{broker_id}/credentials", headers=_h(token))
                body = r.json()
                # The row is intact and the fields still read as present;
                # what changed is that they cannot be decrypted.
                assert body["key_matches"] is False
                assert body["configured"] is False
                assert "re-enter" in (body["unreadable_reason"] or "").lower()


@pytest.mark.asyncio
async def test_no_encryption_key_refuses_the_write_instead_of_storing_plaintext():
    async with db_session() as session, admin_user(session) as (_uid, email):
        async with broker(session, "kraken") as broker_id, api_client() as client:
            async with encryption_key(None):
                token = await _get_token(client, email)
                r = await client.put(
                    f"/brokers/{broker_id}/credentials",
                    json={"fields": {"api_key": "K", "api_secret": "S"}},
                    headers=_h(token),
                )
                assert r.status_code == 503
                assert "NOT_CONFIGURED" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_missing_required_field_is_a_422_naming_it():
    async with db_session() as session, admin_user(session) as (_uid, email):
        async with broker(session, "kraken") as broker_id, api_client() as client:
            async with encryption_key(KEY):
                token = await _get_token(client, email)
                r = await client.put(
                    f"/brokers/{broker_id}/credentials",
                    json={"fields": {"api_key": "K"}},
                    headers=_h(token),
                )
                assert r.status_code == 422
                assert "api_secret" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_field_the_provider_does_not_have_is_refused_not_silently_dropped():
    # Silently dropping it would let an operator believe they had wired
    # something they had not.
    async with db_session() as session, admin_user(session) as (_uid, email):
        async with broker(session, "kraken") as broker_id, api_client() as client:
            async with encryption_key(KEY):
                token = await _get_token(client, email)
                r = await client.put(
                    f"/brokers/{broker_id}/credentials",
                    json={"fields": {"api_key": "K", "api_secret": "S", "account_id": "X"}},
                    headers=_h(token),
                )
                assert r.status_code == 422
                assert "account_id" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_non_admin_cannot_read_or_write_credentials():
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with broker(session, "kraken") as broker_id, api_client() as client:
            token = await _get_token(client, email)
            r = await client.get(f"/brokers/{broker_id}/credentials", headers=_h(token))
            assert r.status_code == 403
            r = await client.put(
                f"/brokers/{broker_id}/credentials",
                json={"fields": {"api_key": "K", "api_secret": "S"}},
                headers=_h(token),
            )
            assert r.status_code == 403


@pytest.mark.asyncio
async def test_deleting_credentials_is_idempotent():
    async with db_session() as session, admin_user(session) as (_uid, email):
        async with broker(session, "kraken") as broker_id, api_client() as client:
            async with encryption_key(KEY):
                token = await _get_token(client, email)
                r = await client.delete(f"/brokers/{broker_id}/credentials", headers=_h(token))
                assert r.status_code == 204
                await client.put(
                    f"/brokers/{broker_id}/credentials",
                    json={"fields": {"api_key": "K", "api_secret": "S"}},
                    headers=_h(token),
                )
                assert (
                    await client.delete(
                        f"/brokers/{broker_id}/credentials", headers=_h(token)
                    )
                ).status_code == 204
                r = await client.get(f"/brokers/{broker_id}/credentials", headers=_h(token))
                assert r.json()["configured"] is False
