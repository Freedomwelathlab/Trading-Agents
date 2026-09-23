"""Per-broker API credentials, encrypted at rest (Phase 90, D109).

Until now this platform could hold exactly one live venue's credentials,
because they came from process environment variables. That is fine for one
broker per deployment and cannot express "this user's IBKR account and
that user's Kraken account". This module stores them per broker row
instead.

Everything here is written around one assumption: **the plaintext must not
be recoverable from the database alone.** The ciphertext lives in
Postgres; the key lives in the environment; a database dump is therefore
not a set of live trading credentials.

Four rules, each of which has a convenient alternative that is wrong.

**No key, no writes.** With `broker_credential_encryption_key` unset,
`save_credentials` raises. It does NOT generate a key: a generated key
lives in one process's memory, and everything encrypted under it becomes
unreadable the moment that process restarts — silently, and only
discovered when a bot cannot trade.

**The key is fingerprinted, not stored.** Each row records a short,
salted digest of the key that encrypted it. A rotated key then produces a
clear "this row was encrypted with a different key" rather than an opaque
`InvalidToken`, and the fingerprint reveals nothing about the key itself.

**Field NAMES are stored in the clear; values never are.** The UI has to
show which fields are set without decrypting anything, and "an access
token is present" is not a secret. The names live in their own column
precisely so answering that question never needs the key.

**Nothing here returns a secret to a caller.** `credential_status` is what
routes use; `load_credentials` exists for adapter construction inside the
process and is deliberately not wired to any response model. A field
declared `PUBLIC` in the registry (an account id, DEMO vs LIVE) is
returned, because an operator has to be able to confirm WHICH account is
wired — and a wrong account is a worse failure than a visible one.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.core.config import Settings
from apps.api.app.db.models import Broker, BrokerCredential
from apps.api.app.execution.registry import CredentialKind, get_provider


class CredentialsNotConfiguredError(Exception):
    """No encryption key is set, so credentials can neither be written nor
    read. Fail closed: the alternative is a deployment that appears to
    store credentials and stores plaintext."""


class CredentialsUnreadableError(Exception):
    """The stored ciphertext cannot be decrypted with the current key —
    almost always a rotated or replaced key. Distinct from "absent" so an
    operator is told to re-enter the credentials rather than left to guess
    why a configured broker behaves as though it is not."""


class MissingCredentialFieldError(Exception):
    """A required field for this provider was not supplied."""


def _fernet(settings: Settings) -> Fernet:
    key = settings.broker_credential_encryption_key
    if not key:
        raise CredentialsNotConfiguredError(
            "NOT_CONFIGURED: BROKER_CREDENTIAL_ENCRYPTION_KEY is unset, so broker "
            "credentials cannot be stored. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"` and set it in the environment. "
            "Keep it: everything encrypted under a key is unreadable without it."
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise CredentialsNotConfiguredError(
            "NOT_CONFIGURED: BROKER_CREDENTIAL_ENCRYPTION_KEY is not a valid Fernet key "
            "(it must be 32 url-safe base64-encoded bytes)."
        ) from exc


def key_fingerprint(settings: Settings) -> str:
    """A short digest identifying WHICH key is in use, never the key.

    Salted with a fixed, public string so the digest cannot be matched
    against a rainbow table of candidate keys, and truncated because its
    only job is to tell two keys apart.
    """
    key = settings.broker_credential_encryption_key or ""
    return hashlib.sha256(b"trading-os/broker-credential/v1|" + key.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class FieldStatus:
    name: str
    label: str
    secret: bool
    required: bool
    present: bool
    help: str
    value: str | None = None
    """Populated ONLY for a non-secret field. A secret's value is never
    returned by anything that a route can reach."""


@dataclass(frozen=True)
class CredentialStatus:
    broker_id: uuid.UUID
    provider: str
    configured: bool
    fields: tuple[FieldStatus, ...]
    updated_at: datetime | None
    key_matches: bool
    """False when the row was encrypted under a different key than the one
    now configured. The row is intact; it simply cannot be read, and the
    remedy is to re-enter the credentials, not to restore a database."""
    unreadable_reason: str | None = None


async def _row(session: AsyncSession, broker_id: uuid.UUID) -> BrokerCredential | None:
    return (
        await session.execute(
            select(BrokerCredential).where(BrokerCredential.broker_id == broker_id)
        )
    ).scalar_one_or_none()


async def save_credentials(
    session: AsyncSession,
    broker: Broker,
    fields: dict[str, str],
    *,
    settings: Settings,
    actor_user_id: uuid.UUID | None,
) -> CredentialStatus:
    """Encrypt and store the whole credential set for one broker.

    A full replace, never a merge. Merging would let a half-entered form
    leave a stale secret in place beside a new one and produce a
    credential set that was never entered as a whole — which is exactly
    the kind of state nobody can reason about when a live order is
    rejected.
    """
    provider = get_provider(broker.provider)
    cleaned = {k: v for k, v in fields.items() if isinstance(v, str) and v.strip()}
    for f in provider.credential_fields:
        if f.required and f.name not in cleaned:
            raise MissingCredentialFieldError(
                f"{provider.display_name} requires {f.name!r} ({f.label})."
            )
    known = {f.name for f in provider.credential_fields}
    unknown = sorted(set(cleaned) - known)
    if unknown:
        raise MissingCredentialFieldError(
            f"{provider.display_name} has no credential field(s) {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(known))}."
        )

    token = _fernet(settings).encrypt(json.dumps(cleaned, sort_keys=True).encode())
    now = datetime.now(UTC)
    row = await _row(session, broker.id)
    if row is None:
        row = BrokerCredential(
            id=uuid.uuid4(),
            broker_id=broker.id,
            provider=broker.provider,
            created_by_user_id=actor_user_id,
        )
        session.add(row)
    row.provider = broker.provider
    row.ciphertext = token
    row.key_fingerprint = key_fingerprint(settings)
    row.field_names = sorted(cleaned)
    row.updated_at = now
    await session.flush()
    return await credential_status(session, broker, settings=settings)


async def load_credentials(
    session: AsyncSession, broker: Broker, *, settings: Settings
) -> dict[str, str] | None:
    """The decrypted set, for building an adapter INSIDE the process.

    None means nothing is stored. Never call this from a route handler and
    never put its result in a response model.
    """
    row = await _row(session, broker.id)
    if row is None:
        return None
    try:
        raw = _fernet(settings).decrypt(bytes(row.ciphertext))
    except InvalidToken as exc:
        raise CredentialsUnreadableError(
            f"The stored credentials for broker {broker.name!r} were encrypted with a "
            f"different key (stored fingerprint {row.key_fingerprint}, current "
            f"{key_fingerprint(settings)}). Re-enter them; they cannot be recovered."
        ) from exc
    decoded = json.loads(raw.decode())
    return {str(k): str(v) for k, v in decoded.items()}


async def credential_status(
    session: AsyncSession, broker: Broker, *, settings: Settings
) -> CredentialStatus:
    """What is set, without needing the key to answer.

    The non-secret VALUES do need the key, so a key mismatch degrades this
    to presence-only rather than failing: an operator whose key changed
    still needs to see which broker rows have to be re-entered.
    """
    provider = get_provider(broker.provider)
    row = await _row(session, broker.id)
    present = set(row.field_names or []) if row is not None else set()
    key_matches = row is None or row.key_fingerprint == key_fingerprint(settings)

    values: dict[str, str] = {}
    unreadable: str | None = None
    if row is not None and key_matches and any(
        f.kind is CredentialKind.PUBLIC for f in provider.credential_fields
    ):
        try:
            values = await load_credentials(session, broker, settings=settings) or {}
        except (CredentialsUnreadableError, CredentialsNotConfiguredError) as exc:
            unreadable = str(exc)
    elif row is not None and not key_matches:
        unreadable = (
            f"Encrypted under a different key (stored {row.key_fingerprint}, current "
            f"{key_fingerprint(settings)}). Re-enter these credentials."
        )

    fields = tuple(
        FieldStatus(
            name=f.name,
            label=f.label,
            secret=f.kind is CredentialKind.SECRET,
            required=f.required,
            present=f.name in present,
            help=f.help,
            value=None if f.kind is CredentialKind.SECRET else values.get(f.name),
        )
        for f in provider.credential_fields
    )
    required_ok = all(f.present for f in fields if f.required)
    return CredentialStatus(
        broker_id=broker.id,
        provider=broker.provider,
        configured=row is not None and required_ok and key_matches,
        fields=fields,
        updated_at=row.updated_at if row is not None else None,
        key_matches=key_matches,
        unreadable_reason=unreadable,
    )


async def delete_credentials(session: AsyncSession, broker: Broker) -> bool:
    row = await _row(session, broker.id)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True
