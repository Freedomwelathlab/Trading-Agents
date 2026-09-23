"""The broker bridge's HTTP surface (Phase 90, D109).

Two things live here: the provider catalogue, and write-only credential
management.

**The catalogue is unauthenticated-adjacent but not secret.** It says what
each venue trades and which fields it needs — public facts about public
APIs — so the frontend can render a credential form for a broker it has
never heard of. It carries `adapter_status` on every entry, because a
capability list for a venue this build cannot reach would otherwise read
as a promise.

**Credentials are write-only.** A secret goes in through `PUT` and never
comes back out of anything here: `GET` returns presence per field, plus
the values of the fields the registry marks PUBLIC (an account id, DEMO
vs LIVE) because an operator has to be able to confirm WHICH account is
wired, and a wrong account is a worse failure than a visible one.

**`ADMIN` gates the writes, and a broker grant does not.** A grant says
"you may trade this broker"; setting its credentials is configuring the
venue for everyone who holds a grant on it, which is an administrative
act. The two are deliberately different permissions.

The assistant that built this cannot use it: writing a real credential is
the operator's own action, performed here, through this route.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.auth.dependencies import get_current_user, require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.core.config import Settings, get_settings
from apps.api.app.db.base import get_session
from apps.api.app.db.models import Broker, User
from apps.api.app.execution.credentials import (
    CredentialsNotConfiguredError,
    CredentialStatus,
    MissingCredentialFieldError,
    credential_status,
    delete_credentials,
    save_credentials,
)
from apps.api.app.execution.registry import PROVIDERS, UnknownProviderError, get_provider

router = APIRouter(prefix="/brokers", tags=["brokers"])


# --- schemas ----------------------------------------------------------------


class CredentialFieldSpec(BaseModel):
    name: str
    label: str
    secret: bool
    required: bool
    help: str


class ProviderResponse(BaseModel):
    provider: str
    display_name: str
    asset_classes: list[str]
    order_types: list[str]
    supports_short: bool
    supports_extended_hours: bool
    supports_fractional: bool
    supports_cancel: bool
    adapter_status: str
    """`implemented` or `catalogued`. A catalogued provider's capabilities
    describe the VENUE; nothing can trade through it in this build."""
    credential_fields: list[CredentialFieldSpec]
    notes: str


class ProvidersResponse(BaseModel):
    providers: list[ProviderResponse]
    note: str


class FieldStatusResponse(BaseModel):
    name: str
    label: str
    secret: bool
    required: bool
    present: bool
    help: str
    value: str | None = None
    """Null for every secret, always. Populated only for a field the
    registry marks PUBLIC."""


class CredentialStatusResponse(BaseModel):
    broker_id: uuid.UUID
    provider: str
    configured: bool
    fields: list[FieldStatusResponse]
    updated_at: datetime | None = None
    key_matches: bool
    unreadable_reason: str | None = None


class SaveCredentialsRequest(BaseModel):
    fields: dict[str, str] = Field(default_factory=dict)
    """The whole set. A full replace, never a merge — see
    `execution/credentials.py` for why a half-entered form must not leave
    a stale secret beside a new one."""


CATALOGUE_NOTE = (
    "`adapter_status: catalogued` means this platform knows what the venue is and has no "
    "adapter for it yet: the capabilities describe the broker, not this build's ability to "
    "reach it. Credentials can be stored for such a provider — that is how an operator "
    "prepares — but no order will be routed through it."
)


def _provider_response(name: str) -> ProviderResponse:
    p = get_provider(name)
    return ProviderResponse(
        provider=p.provider,
        display_name=p.display_name,
        asset_classes=sorted(a.value for a in p.asset_classes),
        order_types=sorted(p.order_types),
        supports_short=p.supports_short,
        supports_extended_hours=p.supports_extended_hours,
        supports_fractional=p.supports_fractional,
        supports_cancel=p.supports_cancel,
        adapter_status=p.adapter_status,
        credential_fields=[
            CredentialFieldSpec(
                name=f.name,
                label=f.label,
                secret=f.kind.value == "secret",
                required=f.required,
                help=f.help,
            )
            for f in p.credential_fields
        ],
        notes=p.notes,
    )


def _status_response(status: CredentialStatus) -> CredentialStatusResponse:
    return CredentialStatusResponse(
        broker_id=status.broker_id,
        provider=status.provider,
        configured=status.configured,
        fields=[
            FieldStatusResponse(
                name=f.name,
                label=f.label,
                secret=f.secret,
                required=f.required,
                present=f.present,
                help=f.help,
                value=f.value,
            )
            for f in status.fields
        ],
        updated_at=status.updated_at,
        key_matches=status.key_matches,
        unreadable_reason=status.unreadable_reason,
    )


# --- routes -----------------------------------------------------------------


@router.get("/providers", response_model=ProvidersResponse)
async def list_providers(_current_user: User = Depends(get_current_user)) -> ProvidersResponse:
    """Every venue this platform knows, with what it trades and needs.

    Authentication only: these are public facts about public APIs, and the
    list is what a signed-in operator needs to choose a broker. It carries
    no credential and no account identifier.
    """
    return ProvidersResponse(
        providers=[_provider_response(name) for name in sorted(PROVIDERS)],
        note=CATALOGUE_NOTE,
    )


async def _load_broker(session: AsyncSession, broker_id: uuid.UUID) -> Broker:
    broker = await session.get(Broker, broker_id)
    if broker is None:
        raise HTTPException(status_code=404, detail=f"No broker {broker_id}.")
    try:
        get_provider(broker.provider)
    except UnknownProviderError as exc:
        raise HTTPException(status_code=409, detail=f"UNKNOWN_PROVIDER: {exc}") from None
    return broker


@router.get("/{broker_id}/credentials", response_model=CredentialStatusResponse)
async def get_broker_credentials(
    broker_id: uuid.UUID,
    _current_user: User = Depends(require_permission(Permission.ADMIN)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> CredentialStatusResponse:
    """What is set, per field. No secret is ever in this response."""
    broker = await _load_broker(session, broker_id)
    return _status_response(await credential_status(session, broker, settings=settings))


@router.put("/{broker_id}/credentials", response_model=CredentialStatusResponse)
async def put_broker_credentials(
    broker_id: uuid.UUID,
    payload: SaveCredentialsRequest,
    current_user: User = Depends(require_permission(Permission.ADMIN)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> CredentialStatusResponse:
    """Encrypt and store this broker's whole credential set.

    503 NOT_CONFIGURED when no encryption key is set — the store refuses
    rather than writing plaintext, and rather than generating a key that
    would be lost at the next restart along with everything encrypted
    under it.
    """
    broker = await _load_broker(session, broker_id)
    try:
        status = await save_credentials(
            session,
            broker,
            payload.fields,
            settings=settings,
            actor_user_id=current_user.id,
        )
    except CredentialsNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except MissingCredentialFieldError as exc:
        raise HTTPException(status_code=422, detail=f"MISSING_FIELD: {exc}") from None
    await session.commit()
    return _status_response(status)


@router.delete("/{broker_id}/credentials", status_code=204)
async def delete_broker_credentials(
    broker_id: uuid.UUID,
    _current_user: User = Depends(require_permission(Permission.ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Forget this broker's credentials. Idempotent: deleting what is not
    there is a 204, because the caller's intent — "this broker has no
    credentials" — is satisfied either way."""
    broker = await _load_broker(session, broker_id)
    await delete_credentials(session, broker)
    await session.commit()
