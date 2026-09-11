"""Strategy definitions - the Strategy Lab's first surface (Phase 54).

**Two-permission-free-zones aside, this is the codebase's second
user-scoped resource.** Like watchlists (Phase 50) a strategy belongs to
exactly one user and is reachable only by that user; unlike watchlists it
additionally requires a permission, `strategy:manage`, applied once at the
router level the way `routes/admin.py` applies `admin:manage`. The
permission and the ownership check answer different questions and neither
substitutes for the other: the permission says this account may work with
strategies at all, the `owner_user_id` check says which strategies. A
strategy is not merely reading material - Phase 55 runs backtests off one
and Phase 62 will let one place paper orders - so unlike a watchlist it is
not something every authenticated account should be able to create by
default.

**Not-yours is 403, not-there is 404.** Ownership is checked in exactly one
place (`_load_owned_strategy`) and reproduces the order and the two status
codes `require_broker_access` and `_owned_watchlist` already use: look the
row up first (404 if no such id exists anywhere), then check the caller
owns it (403 if they do not). A version id that exists but belongs to a
different strategy is a 404 rather than a 403, deliberately - answering 403
there would confirm the existence of a version under a strategy the caller
was not asking about.

**Versions are immutable once they leave `draft`.** `PATCH
.../versions/{id}` and the validate route both answer 409 on a non-draft
and name the fork endpoint in the message. That is the whole enforcement
mechanism - there is no database trigger (see
`StrategyVersion`'s docstring for why), and there does not need to be:
these routes are the only writer.

**Editing a draft does not validate it.** `PATCH .../versions/{id}` stores
whatever definition it is given without running `validate_definition`, on
purpose. A definition under construction passes through many invalid
intermediate states - an indicator declared before the rule that uses it,
a rule written before its indicator - and a builder that refused to save
any of them would be unusable. Validation is its own explicit step, and it
is the only thing that can move a version to `validated`.

**No code execution anywhere in this path.** A definition is JSON that gets
structurally checked (apps/api/app/strategies/validation.py) and stored. It
is never compiled, evaluated, or executed here or in any later phase - see
that module's docstring for why that is a hard boundary on this platform.
"""

import copy
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.api.app.agents.strategy_research_assistant import StrategyResearchAssistant
from apps.api.app.agents.technical_analyst import AnalystOutputError
from apps.api.app.api.dependencies import get_strategy_research_assistant
from apps.api.app.api.schemas_strategies import (
    CreateStrategyRequest,
    ForkStrategyVersionRequest,
    ListStrategiesResponse,
    ProposeStrategyRequest,
    ProposeStrategyResponse,
    StrategyDetailResponse,
    StrategyResponse,
    StrategySummary,
    StrategyVersionResponse,
    StrategyVersionSummary,
    UpdateStrategyRequest,
    UpdateStrategyVersionRequest,
)
from apps.api.app.auth.dependencies import get_current_user, require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.core.logging import get_logger
from apps.api.app.db.base import get_session
from apps.api.app.db.models import (
    Strategy,
    StrategyVersion,
    StrategyVersionStatus,
    User,
)
from apps.api.app.strategies.service import (
    compute_definition_hash,
    latest_version,
    next_version_number,
)
from apps.api.app.strategies.validation import validate_definition

router = APIRouter(
    prefix="/strategies",
    tags=["strategies"],
    dependencies=[Depends(require_permission(Permission.STRATEGY_MANAGE))],
)

logger = get_logger(__name__)

DEFAULT_LIST_LIMIT = 50
"""Same default page size as the admin listings (D030), D034's broker
discovery, D027's portfolio history and the watchlist listing - this
codebase has one pagination convention, not five."""
MAX_LIST_LIMIT = 500
"""Same hard ceiling and same reasoning: a caller needing more pages rather
than the server ever building an unbounded response."""


def _version_not_draft_detail(
    version: StrategyVersion, strategy_id: uuid.UUID, action: str
) -> str:
    return (
        f"VERSION_NOT_DRAFT: version {version.id} is {version.status.value} and "
        f"{action}. POST /strategies/{strategy_id}/versions to fork a new draft from it."
    )


async def _load_owned_strategy(
    session: AsyncSession, strategy_id: uuid.UUID, current_user: User
) -> Strategy:
    """404 when no strategy has this id, 403 when one does but the caller
    does not own it. Existence is checked first for the same reason
    `_owned_watchlist` checks it first: a nonexistent id must not 403
    merely because nobody can own a row that isn't there."""
    strategy = (
        await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    ).scalar_one_or_none()
    if strategy is None:
        raise HTTPException(status_code=404, detail=f"No strategy with id {strategy_id}.")
    if strategy.owner_user_id != current_user.id:
        raise HTTPException(status_code=403, detail=f"Strategy {strategy_id} is not yours.")
    return strategy


async def _load_version(
    session: AsyncSession, strategy_id: uuid.UUID, version_id: uuid.UUID
) -> StrategyVersion:
    """404 when the version does not exist OR does not belong to
    `strategy_id`. One query, both conditions - answering differently for
    "exists elsewhere" would leak that a version by that id exists under
    some other user's strategy."""
    version = (
        await session.execute(
            select(StrategyVersion).where(
                StrategyVersion.id == version_id,
                StrategyVersion.strategy_id == strategy_id,
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise HTTPException(
            status_code=404,
            detail=f"No version with id {version_id} on strategy {strategy_id}.",
        )
    return version


def _version_response(version: StrategyVersion) -> StrategyVersionResponse:
    return StrategyVersionResponse(
        id=version.id,
        strategy_id=version.strategy_id,
        version_number=version.version_number,
        definition=version.definition,
        definition_hash=version.definition_hash,
        status=version.status,
        created_by_user_id=version.created_by_user_id,
        created_at=version.created_at,
        validated_at=version.validated_at,
    )


def _version_summary(version: StrategyVersion) -> StrategyVersionSummary:
    return StrategyVersionSummary(
        id=version.id,
        version_number=version.version_number,
        status=version.status,
        created_at=version.created_at,
        validated_at=version.validated_at,
    )


def _strategy_response(strategy: Strategy, version: StrategyVersion) -> StrategyResponse:
    return StrategyResponse(
        id=strategy.id,
        owner_user_id=strategy.owner_user_id,
        name=strategy.name,
        description=strategy.description,
        status=strategy.status,
        created_at=strategy.created_at,
        updated_at=strategy.updated_at,
        latest_version=_version_response(version),
    )


async def _require_latest_version(
    session: AsyncSession, strategy: Strategy
) -> StrategyVersion:
    """Every strategy is created together with its version 1 in one
    transaction, so a strategy with no version means the database was
    edited by hand. That is a 500-class fact about the data, not a 404
    about the request, so it is stated plainly rather than hidden behind an
    empty response."""
    version = await latest_version(session, strategy.id)
    if version is None:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Strategy {strategy.id} has no versions. Every strategy is created with "
                "version 1 in the same transaction, so this row is inconsistent."
            ),
        )
    return version


@router.post("", response_model=StrategyResponse, status_code=status.HTTP_201_CREATED)
async def create_strategy(
    payload: CreateStrategyRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StrategyResponse:
    """Creates the strategy AND its version 1 in one transaction - a
    strategy with no version would be a row nothing could ever be run
    against, and making the client issue a second request to reach a usable
    state would leave that unusable row behind whenever it didn't.

    Version 1 starts as a `draft` whatever the definition contains,
    including `{}`. Nothing is validated here (see the module docstring):
    creating a strategy and asserting its rules are well formed are
    separate acts, and only the second one can produce a `validated`
    version.

    Names are not unique, deliberately - two strategies may share one. The
    id is what every other route addresses, and uniqueness would be a
    constraint on how a person labels their own research.
    """
    strategy = Strategy(
        id=uuid.uuid4(),
        owner_user_id=current_user.id,
        name=payload.name,
        description=payload.description,
    )
    session.add(strategy)

    version = StrategyVersion(
        id=uuid.uuid4(),
        strategy_id=strategy.id,
        version_number=1,
        definition=payload.definition,
        definition_hash=compute_definition_hash(payload.definition),
        status=StrategyVersionStatus.DRAFT,
        created_by_user_id=current_user.id,
    )
    session.add(version)
    await session.commit()
    await session.refresh(strategy)
    await session.refresh(version)

    logger.info(
        "strategy_created",
        strategy_id=str(strategy.id),
        version_id=str(version.id),
        actor_user_id=str(current_user.id),
    )
    return _strategy_response(strategy, version)


@router.get("", response_model=ListStrategiesResponse)
async def list_strategies(
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListStrategiesResponse:
    """The caller's OWN strategies only - `WHERE owner_user_id = :caller`
    is in the query itself, not applied after the fact, so there is no
    ordering of events in which another user's row is fetched and then
    filtered out.

    Ordered by `(created_at, id)` ascending, the same stable sort
    `list_users`/`list_roles`/`list_watchlists` use, with the id as the
    tiebreaker that keeps `offset` paging deterministic when two rows share
    a timestamp.

    `selectinload` fetches every page's versions in one additional query
    rather than one per strategy, and the latest version is picked in
    Python. At a page of at most 500 strategies that is cheaper and far
    simpler than the correlated subquery or lateral join that would compute
    it in SQL; revisit if a page ever needs to cover a user with thousands
    of versions per strategy.
    """
    rows = (
        (
            await session.execute(
                select(Strategy)
                .options(selectinload(Strategy.versions))
                .where(Strategy.owner_user_id == current_user.id)
                .order_by(Strategy.created_at.asc(), Strategy.id.asc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    items: list[StrategySummary] = []
    for row in rows:
        if not row.versions:
            # Structurally impossible through this API (see
            # _require_latest_version) - skipped rather than crashing a
            # whole listing over one hand-edited row.
            continue
        newest = max(row.versions, key=lambda version: version.version_number)
        items.append(
            StrategySummary(
                id=row.id,
                name=row.name,
                description=row.description,
                status=row.status,
                created_at=row.created_at,
                updated_at=row.updated_at,
                latest_version_number=newest.version_number,
                latest_version_status=newest.status,
            )
        )

    return ListStrategiesResponse(items=items, limit=limit, offset=offset)


@router.get("/{strategy_id}", response_model=StrategyDetailResponse)
async def get_strategy(
    strategy_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StrategyDetailResponse:
    """The strategy, its latest version in full, and every version as a
    summary, newest `version_number` first. The summaries carry no
    definitions - see `StrategyVersionSummary` for why."""
    strategy = await _load_owned_strategy(session, strategy_id, current_user)
    versions = (
        (
            await session.execute(
                select(StrategyVersion)
                .where(StrategyVersion.strategy_id == strategy_id)
                .order_by(StrategyVersion.version_number.desc())
            )
        )
        .scalars()
        .all()
    )
    if not versions:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Strategy {strategy.id} has no versions. Every strategy is created with "
                "version 1 in the same transaction, so this row is inconsistent."
            ),
        )

    newest = versions[0]
    return StrategyDetailResponse(
        id=strategy.id,
        owner_user_id=strategy.owner_user_id,
        name=strategy.name,
        description=strategy.description,
        status=strategy.status,
        created_at=strategy.created_at,
        updated_at=strategy.updated_at,
        latest_version=_version_response(newest),
        versions=[_version_summary(version) for version in versions],
    )


@router.patch("/{strategy_id}", response_model=StrategyResponse)
async def update_strategy(
    strategy_id: uuid.UUID,
    payload: UpdateStrategyRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StrategyResponse:
    """Only keys actually present in the JSON body are applied
    (`model_fields_set`), matching `PATCH /admin/users/{id}` and
    `PATCH /admin/roles/{id}` - so `{"description": null}` clears the
    description while omitting it leaves it untouched.

    `name` and `status` are NOT NULL columns, so an explicit `null` for
    either is a 422 rather than a database error. `status: "archived"` is
    how a strategy is retired: there is no delete endpoint, because a
    version of this strategy may already be the recorded input of a
    backtest run (Phase 55) and deleting it would make that result
    reference nothing.
    """
    strategy = await _load_owned_strategy(session, strategy_id, current_user)

    fields_set = payload.model_fields_set
    if "name" in fields_set and payload.name is None:
        raise HTTPException(status_code=422, detail="name cannot be null.")
    if "status" in fields_set and payload.status is None:
        raise HTTPException(status_code=422, detail="status cannot be null.")

    if "name" in fields_set and payload.name is not None:
        strategy.name = payload.name
    if "description" in fields_set:
        strategy.description = payload.description
    if "status" in fields_set and payload.status is not None:
        strategy.status = payload.status

    await session.commit()
    await session.refresh(strategy)
    version = await _require_latest_version(session, strategy)
    return _strategy_response(strategy, version)


@router.get(
    "/{strategy_id}/versions/{version_id}",
    response_model=StrategyVersionResponse,
)
async def get_strategy_version(
    strategy_id: uuid.UUID,
    version_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StrategyVersionResponse:
    """One version in full, definition included. 404 when the version does
    not exist or does not belong to this strategy - see `_load_version`."""
    await _load_owned_strategy(session, strategy_id, current_user)
    version = await _load_version(session, strategy_id, version_id)
    return _version_response(version)


@router.patch(
    "/{strategy_id}/versions/{version_id}",
    response_model=StrategyVersionResponse,
)
async def update_strategy_version(
    strategy_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: UpdateStrategyVersionRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StrategyVersionResponse:
    """Replaces a DRAFT version's definition wholesale and recomputes its
    `definition_hash`. 409 on any non-draft version - that is where "never
    silently mutate a validated strategy" is actually enforced, and the
    message names the fork endpoint so the caller's next move is obvious
    rather than something they have to go and look up.

    Deliberately does NOT run `validate_definition`. A draft passes through
    many invalid intermediate states while it is being written, and a save
    that refused all of them would make the builder unusable. Validation is
    `POST .../validate`, and it is the only route that can change a
    version's status.
    """
    await _load_owned_strategy(session, strategy_id, current_user)
    version = await _load_version(session, strategy_id, version_id)

    if version.status is not StrategyVersionStatus.DRAFT:
        raise HTTPException(
            status_code=409,
            detail=_version_not_draft_detail(
                version, strategy_id, "can no longer be edited"
            ),
        )

    version.definition = payload.definition
    version.definition_hash = compute_definition_hash(payload.definition)
    await session.commit()
    await session.refresh(version)
    return _version_response(version)


@router.post(
    "/{strategy_id}/versions",
    response_model=StrategyVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def fork_strategy_version(
    strategy_id: uuid.UUID,
    payload: ForkStrategyVersionRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StrategyVersionResponse:
    """Forks a new DRAFT version from an existing one - the only way to
    change a strategy whose current version has been validated.

    The source is `from_version_id` when given (404 if it does not belong
    to this strategy) and this strategy's highest-numbered version
    otherwise. Any version may be forked, including an already-archived
    one: going back to an older rule set is a legitimate thing to want, and
    forking it copies rather than resurrects it.

    The source's definition is DEEP-COPIED into the new draft. A shallow
    copy would leave the two versions sharing the same nested lists and
    dicts, so editing the new draft's `indicators` would silently rewrite
    the definition of the version it was forked from - which is exactly the
    thing this whole versioning scheme exists to make impossible.

    Concurrency: two simultaneous forks of the same strategy can compute
    the same next version number, and `uq_strategy_version_number` is what
    makes the loser fail rather than create a duplicate. That surfaces as a
    500 today rather than a retry, which is the honest behaviour for a race
    nothing in this phase can produce (one user, one browser) and is worth
    revisiting only if a programmatic client ever forks in parallel.
    """
    strategy = await _load_owned_strategy(session, strategy_id, current_user)

    if payload.from_version_id is not None:
        source = await _load_version(session, strategy_id, payload.from_version_id)
    else:
        source = await _require_latest_version(session, strategy)

    definition = copy.deepcopy(source.definition)
    version = StrategyVersion(
        id=uuid.uuid4(),
        strategy_id=strategy_id,
        version_number=await next_version_number(session, strategy_id),
        definition=definition,
        # Recomputed rather than copied from the source row. The two are
        # equal by construction (the definition is a verbatim copy), so
        # this costs one sha256 and buys not having to trust that a stored
        # hash was ever right.
        definition_hash=compute_definition_hash(definition),
        status=StrategyVersionStatus.DRAFT,
        created_by_user_id=current_user.id,
    )
    session.add(version)
    await session.commit()
    await session.refresh(version)

    logger.info(
        "strategy_version_forked",
        strategy_id=str(strategy_id),
        version_id=str(version.id),
        version_number=version.version_number,
        source_version_id=str(source.id),
        actor_user_id=str(current_user.id),
    )
    return _version_response(version)


@router.post(
    "/{strategy_id}/versions/{version_id}/validate",
    response_model=StrategyVersionResponse,
)
async def validate_strategy_version(
    strategy_id: uuid.UUID,
    version_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StrategyVersionResponse:
    """Runs the structural validator over a DRAFT version and, if it finds
    nothing, marks the version `validated` and immutable.

    A failure is a 422 carrying `{"errors": [...]}` - EVERY problem found,
    not the first one - and mutates nothing: a version that failed
    validation is still exactly the draft it was, still editable, and its
    `validated_at` is still null. There is no partial state in which a
    version is "mostly validated."

    409 on a non-draft. A version that is already `validated` is not
    re-validated even though doing so would be harmless and would pass:
    re-running it would imply the result could differ, and the whole point
    of the immutability rule is that it cannot.

    This is the ONLY route that writes `status` or `validated_at`.
    """
    await _load_owned_strategy(session, strategy_id, current_user)
    version = await _load_version(session, strategy_id, version_id)

    if version.status is not StrategyVersionStatus.DRAFT:
        raise HTTPException(
            status_code=409,
            detail=_version_not_draft_detail(
                version, strategy_id, "cannot be re-validated"
            ),
        )

    errors = validate_definition(version.definition)
    if errors:
        logger.info(
            "strategy_version_validation_failed",
            strategy_id=str(strategy_id),
            version_id=str(version.id),
            error_count=len(errors),
            actor_user_id=str(current_user.id),
        )
        raise HTTPException(status_code=422, detail={"errors": errors})

    version.status = StrategyVersionStatus.VALIDATED
    version.validated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(version)

    logger.info(
        "strategy_version_validated",
        strategy_id=str(strategy_id),
        version_id=str(version.id),
        version_number=version.version_number,
        definition_hash=version.definition_hash,
        actor_user_id=str(current_user.id),
    )
    return _version_response(version)


@router.post("/research/propose", response_model=ProposeStrategyResponse)
async def propose_strategy(
    payload: ProposeStrategyRequest,
    current_user: User = Depends(get_current_user),
    assistant: StrategyResearchAssistant | None = Depends(get_strategy_research_assistant),
) -> ProposeStrategyResponse:
    """Phase 67 (D085): call the LLM, run the SAME structural validator every
    manually-authored strategy must pass, return the result. NO database
    write of any kind happens in this route - no session dependency is even
    requested, so there is nothing for it to commit. A caller who likes the
    draft creates it for real through the existing `POST /strategies` +
    `POST /strategies/{id}/versions/{id}/validate` path, which is the only
    place a `Strategy` or `StrategyVersion` row is ever written.

    Gated by `Permission.STRATEGY_MANAGE` (reused verbatim via this router's
    own `dependencies=[...]`, same as every other route here) rather than a
    new permission - proposing a draft for review is a strictly weaker
    capability than the strategy-creation permission a caller already needs
    to save what it proposes, so a separate permission would gate nothing a
    stronger one doesn't already cover.

    400 NOT_CONFIGURED when no LLM provider is wired - this mirrors
    `get_trader_agent`'s convention (`POST /brokers/{id}/agent-trades`), not
    `get_technical_analyst`'s: this agent's output IS the response, so there
    is no proposal to return without it, unlike an analyst whose absence
    just narrows another agent's context.

    502 on `AnalystOutputError` - a malformed/unparseable LLM response, not
    a structural verdict on a strategy (that verdict is 200 with
    `is_valid: false`, see below). Mirrors the existing precedent for a
    PRIMARY agent call's own output failure: `POST /brokers/{id}/agent-trades`
    answers the equivalent `AgentOutputError` from `TraderAgent.propose()`
    with `502 AGENT_OUTPUT_INVALID: ...` (`apps/api/app/api/routes/
    trades.py`) rather than the 400 it uses for NOT_CONFIGURED - a reachable
    provider that returned something unusable is a different failure than no
    provider being configured at all, and 502 Bad Gateway reads correctly
    for "the upstream provider gave us something we could not use."

    A structurally invalid draft is NOT an error response: `validate_definition`
    running against the LLM's own draft can find real problems the same way
    it can against a human's, and `is_valid: false` with an itemized
    `validation_errors` list is exactly as informative to the caller as any
    other rejected draft - see `strategy_research_assistant.py`'s module
    docstring for why this is a 200, never a 4xx/5xx.
    """
    if assistant is None:
        raise HTTPException(
            status_code=400,
            detail="NOT_CONFIGURED: no LLM provider is wired (see docs/DECISIONS.md D018).",
        )

    try:
        proposal = await assistant.propose(brief=payload.brief)
    except AnalystOutputError as exc:
        raise HTTPException(status_code=502, detail=f"AGENT_OUTPUT_INVALID: {exc}") from None

    logger.info(
        "strategy_research_proposal_generated",
        is_valid=proposal.is_valid,
        error_count=len(proposal.validation_errors),
        actor_user_id=str(current_user.id),
    )
    return ProposeStrategyResponse(
        name=proposal.name,
        definition=proposal.definition,
        rationale=proposal.rationale,
        is_valid=proposal.is_valid,
        validation_errors=proposal.validation_errors,
    )
