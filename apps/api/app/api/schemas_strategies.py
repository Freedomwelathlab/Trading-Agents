"""DTOs for the /strategies routes (Phase 54).

`definition` is a plain `dict` on every request and response rather than a
typed Pydantic model of the rule language. That is deliberate and it is the
same decision `strategy_versions.definition` being JSONB reflects: the rule
vocabulary is still moving across the Strategy Lab's later phases, and a
Pydantic model here would mean the shape is enforced in two places that can
drift apart. Structural checking happens in exactly one place -
apps/api/app/strategies/validation.py - which reports every problem at once
as concrete strings, where a Pydantic model would report the first
`ValidationError` in a shape a builder UI has to reverse-engineer.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from apps.api.app.db.models import StrategyStatus, StrategyVersionStatus


class CreateStrategyRequest(BaseModel):
    """`definition` defaults to `{}` - an empty, deliberately incomplete
    draft is a legal thing to create. A strategy is named before it is
    written, and refusing to store a half-finished one would mean the user
    has nowhere to put work in progress. Validation is its own explicit
    step (`POST /strategies/{id}/versions/{version_id}/validate`), which is
    where an empty definition is told, in those words, that `entry_rule is
    required`."""

    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    definition: dict = Field(default_factory=dict)


class UpdateStrategyRequest(BaseModel):
    """Same `model_fields_set` convention as `UpdateUserRequest` /
    `UpdateRoleRequest`: only keys actually present in the JSON body are
    applied, so `{"description": null}` clears the description while
    omitting it entirely leaves it untouched.

    `definition` is deliberately absent - a strategy row carries no
    definition, its versions do, and editing one goes through
    `PATCH /strategies/{id}/versions/{version_id}`."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    status: StrategyStatus | None = None


class UpdateStrategyVersionRequest(BaseModel):
    """A FULL REPLACE of the definition, not a partial merge - which is why
    `definition` is required rather than optional.

    A strategy definition is one coherent whole: its rules reference its own
    indicator ids, so field-by-field patching would let a caller delete the
    indicator an entry rule names and leave the version in a state neither
    half of the request intended. Sending the whole definition means the
    client always states the complete intended result."""

    definition: dict


class ForkStrategyVersionRequest(BaseModel):
    """`from_version_id` omitted means "fork from this strategy's current
    highest version", which is what a user clicking "new draft" almost
    always means. Given explicitly, it must name a version of THIS
    strategy - a version id belonging to someone else's strategy is a 404,
    not a cross-strategy copy."""

    from_version_id: uuid.UUID | None = None


class StrategyVersionResponse(BaseModel):
    """One version in full, including its `definition`. Returned by the
    single-version GET, both mutating version routes, and the validate
    route."""

    id: uuid.UUID
    strategy_id: uuid.UUID
    version_number: int
    definition: dict
    definition_hash: str
    status: StrategyVersionStatus
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    validated_at: datetime | None


class StrategyVersionSummary(BaseModel):
    """One version WITHOUT its definition, for the strategy-detail listing.

    A strategy with twenty versions would otherwise return twenty full rule
    sets to render a version picker that shows a number and a status. The
    full definition is one request away per version
    (`GET /strategies/{id}/versions/{version_id}`), which is the shape the
    UI actually needs: it displays one at a time."""

    id: uuid.UUID
    version_number: int
    status: StrategyVersionStatus
    created_at: datetime
    validated_at: datetime | None


class StrategyResponse(BaseModel):
    """A strategy plus its latest version in full - the shape returned by
    create and patch, where the caller has just acted on the strategy and
    the version they are about to edit is the one they want next."""

    id: uuid.UUID
    owner_user_id: uuid.UUID | None
    name: str
    description: str | None
    status: StrategyStatus
    created_at: datetime
    updated_at: datetime
    latest_version: StrategyVersionResponse


class StrategySummary(BaseModel):
    """One row of the strategy listing. Carries the latest version's number
    and status but not its definition or id - a list view needs to show
    "v3, validated", and anything more is a detail request."""

    id: uuid.UUID
    name: str
    description: str | None
    status: StrategyStatus
    created_at: datetime
    updated_at: datetime
    latest_version_number: int
    latest_version_status: StrategyVersionStatus


class StrategyDetailResponse(BaseModel):
    """`StrategyResponse` plus every version as a summary, newest
    `version_number` first - newest first because that is the one being
    worked on, unlike the listing endpoints' oldest-first ordering, which
    exists to make `offset` paging stable and has no paging here to
    stabilize."""

    id: uuid.UUID
    owner_user_id: uuid.UUID | None
    name: str
    description: str | None
    status: StrategyStatus
    created_at: datetime
    updated_at: datetime
    latest_version: StrategyVersionResponse
    versions: list[StrategyVersionSummary]


class ProposeStrategyRequest(BaseModel):
    """The one input to `POST /strategies/research/propose` (Phase 67,
    D085): a short research goal in the caller's own words, e.g. "a
    mean-reversion idea using RSI". Bounded 1-500 chars for the same reason
    every free-text field on this platform is bounded - an unbounded prompt
    is an unbounded cost with no benefit past a couple of sentences of
    intent."""

    brief: str = Field(min_length=1, max_length=500)


class ProposeStrategyResponse(BaseModel):
    """The agent's draft, plus the SAME structural verdict a manually
    authored strategy gets from `POST .../versions/{id}/validate`
    (`apps/api/app/strategies/validation.py::validate_definition`, called
    verbatim). `is_valid: false` with a real `validation_errors` list is a
    normal, expected response - not an error - exactly like this agent's own
    `StrategyProposal.is_valid`/`validation_errors`. Nothing behind this
    response is persisted: no `Strategy` or `StrategyVersion` row is created
    by this endpoint, whatever `is_valid` says."""

    name: str
    definition: dict
    rationale: str
    is_valid: bool
    validation_errors: list[str]


class ListStrategiesResponse(BaseModel):
    """Paginated listing envelope - `{items, limit, offset}`, the same
    shape as `ListUsersResponse` / `ListRolesResponse` / the portfolio
    history endpoint (D027/D030). Named `items` rather than `strategies`
    because this is the first of a family of Strategy Lab resources
    (backtest runs, scans) that will all want the same envelope, and one
    generic key beats a differently-named list per resource."""

    items: list[StrategySummary]
    limit: int
    offset: int
