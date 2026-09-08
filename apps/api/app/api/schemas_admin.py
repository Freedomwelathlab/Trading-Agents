"""DTOs for the /admin routes. Never echo a password or password hash back
in any response - CreateUserResponse deliberately has no such field."""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from apps.api.app.db.models import BrokerKind


class CreateUserRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8)
    is_active: bool = True
    role_id: uuid.UUID | None = None


class CreateUserResponse(BaseModel):
    id: uuid.UUID
    email: str
    is_active: bool
    role_id: uuid.UUID | None


class UpdateUserRequest(BaseModel):
    """All fields optional - only keys actually present in the JSON body
    are applied (checked via `model_fields_set`, not `is not None`), so
    `{"role_id": null}` unassigns the role while omitting `role_id`
    entirely leaves it untouched. This is how `is_active: false` doubles
    as "deactivate" - no separate delete/deactivate endpoint exists."""

    is_active: bool | None = None
    role_id: uuid.UUID | None = None


class CreateRoleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = None
    permissions: list[str] = Field(default_factory=list)


class CreateRoleResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    permissions: list[str]


class UpdateRoleRequest(BaseModel):
    """Same `model_fields_set` convention as `UpdateUserRequest`. `name`
    is deliberately not updatable here - roles are looked up and referenced
    by name in a few places (see tests), and renaming isn't part of this
    endpoint's scope."""

    description: str | None = None
    permissions: list[str] | None = None


class CreateBrokerGrantRequest(BaseModel):
    user_id: uuid.UUID
    broker_id: uuid.UUID


class BrokerGrantResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    broker_id: uuid.UUID


class ListUsersResponse(BaseModel):
    """Paginated listing envelope, same `{items, limit, offset}` shape the
    portfolio history endpoint uses (D027) - see docs/DECISIONS.md D030.
    Reuses CreateUserResponse per row rather than defining a second user
    DTO, so a listed user can never accidentally expose a field the
    create/update responses deliberately omit (password, hash)."""

    users: list[CreateUserResponse]
    limit: int
    offset: int


class ListRolesResponse(BaseModel):
    roles: list[CreateRoleResponse]
    limit: int
    offset: int


class ListBrokerGrantsResponse(BaseModel):
    grants: list[BrokerGrantResponse]
    limit: int
    offset: int


class CreateBrokerRequest(BaseModel):
    """Phase 43 (docs/DECISIONS.md D058). Until this endpoint existed,
    broker rows could only be created by direct SQL insert, which meant the
    paper/live distinction - the thing that decides which adapter a trade
    is routed to - was unmanageable through the API at all."""

    name: str = Field(min_length=1, max_length=64)
    kind: BrokerKind
    provider: str = Field(min_length=1, max_length=64)
    is_active: bool = False

    confirm_live: bool = False
    """Required when `kind` is `live`, ignored otherwise. Creating a
    live-kind broker is what makes a broker id eligible for the real-money
    execution path at all, so it is stated explicitly rather than being a
    one-character difference from a paper row that nobody reviews. It is
    NOT by itself an authorization to trade - a live broker still cannot
    place an order unless TRADING_MODE=live, LIVE_TRADING_ENABLED=true,
    the LONGPORT_LIVE_* trio is set, the caller holds trade:submit:live,
    and that individual request carries `confirm: true`."""


class UpdateBrokerModeRequest(BaseModel):
    """Phase 43 (D058): the per-broker paper/live toggle.

    `kind` IS the trading-mode switch for a broker. `POST
    /brokers/{broker_id}/trades` reads it on every submission and routes to
    `PaperBrokerAdapter` or `LiveBrokerAdapter` accordingly - there is one
    endpoint, and the broker row decides, so a client cannot select the
    execution mode from the request body.
    """

    kind: BrokerKind
    confirm_live: bool = False
    """Required to flip a broker TO live; ignored when flipping to paper.
    Flipping toward real money is the consequential direction, and only
    that direction is made deliberately awkward - a live -> paper flip is
    always allowed to proceed without it, because that direction can only
    make the system safer."""


class EmergencyStopRequest(BaseModel):
    """`reason` is required and non-blank on BOTH activate and deactivate
    (docs/DECISIONS.md D039). Turning the kill switch back on for the whole
    platform with no recorded justification would make the audit trail
    useless exactly where it matters most, so the schema - not a convention
    - enforces it. Trailing/leading whitespace is stripped and the result
    must still be non-empty, so `"   "` is a 422, not a blank audit row."""

    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def _reason_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason must not be blank")
        return stripped


class CreateMarketDataBackfillRequest(BaseModel):
    """Phase 53 (docs/DECISIONS.md D070): manually trigger ingestion of
    real historical OHLCV bars for one symbol over one date range. No
    scheduled/automatic backfill exists yet - see
    apps/api/app/marketdata/ingestion/backfill.py's module docstring."""

    symbol: str = Field(min_length=1, max_length=32)
    bar_interval: Literal["1d"] = "1d"
    """Only daily bars are ingested in Phase 53 - the market_data_bars
    column exists for future intraday intervals, but nothing populates
    them yet, so any other value is a 422, not a silently-ignored request."""
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def _validate_range(self) -> "CreateMarketDataBackfillRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        return self


class MarketDataBackfillJobResponse(BaseModel):
    id: uuid.UUID
    symbol: str
    bar_interval: str
    requested_start_date: date
    requested_end_date: date
    status: str
    bars_ingested: int
    earliest_bar_date: date | None
    latest_bar_date: date | None
    error_detail: str | None
    """Set only when status is "failed" - the real VendorError/
    DataUnavailableError message, never a generic placeholder."""


class EmergencyStopStatusResponse(BaseModel):
    """`source` is "database" once any flip has been persisted and
    "settings_default" while the table is still empty and
    Settings.emergency_stop_active is acting as the bootstrap default -
    reported verbatim so a caller can always tell which one is in force
    (D039). The provenance fields are None in the settings_default case
    rather than invented."""

    active: bool
    source: str
    reason: str | None = None
    actor_user_id: uuid.UUID | None = None
    changed_at: datetime | None = None
