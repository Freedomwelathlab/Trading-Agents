"""DTOs for the broker-discovery routes (docs/DECISIONS.md D034).

Deliberately narrow: id, name, kind, provider, is_active and nothing else.
`brokers` is the row that a live broker's credentials will eventually hang
off (spec Sec51 keeps paper and live credentials in structurally separate
rows), so this response shape is an explicit allow-list, never a dump of
the ORM row - adding a credential-shaped column to `Broker` later must not
silently start returning it here.
"""

import uuid

from pydantic import BaseModel

from apps.api.app.db.models import BrokerKind


class BrokerResponse(BaseModel):
    """One broker the calling user actually holds a BrokerGrant for.

    `kind` is the paper/live discriminator - the frontend shows it so a
    user can never be unsure which kind of broker a pre-filled broker_id
    points at. `is_active` is reported as the backend has it, never
    filtered on: an inactive broker the user has a grant for is still a
    real fact about their access, and hiding it would make the listing
    disagree with what `POST /brokers/{id}/trades` says.
    """

    id: uuid.UUID
    name: str
    kind: BrokerKind
    provider: str
    is_active: bool


class ListBrokersResponse(BaseModel):
    """Same `{items, limit, offset}` envelope as D027's portfolio history
    and D031's admin listings - one pagination convention in this
    codebase, not three."""

    brokers: list[BrokerResponse]
    limit: int
    offset: int
