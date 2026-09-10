"""The strategy leaderboard - the Strategy Lab's fifth surface (Phase 59).

**A read-model, not a new resource.** `GET /strategies/leaderboard` writes
nothing, creates nothing, and has no table behind it: every score is computed
on request from `backtest_runs` / `walk_forward_runs` / `monte_carlo_runs` /
`robustness_runs` rows four earlier phases already persisted. See
apps/api/app/strategies/scoring.py for the model itself and for why it is
recomputed rather than stored.

**`strategy:manage`, deliberately NOT a new permission.** A leaderboard of
your own strategies is a READ over the resource `strategy:manage` already
gates - it exposes no strategy, and no run, that `GET /strategies` and the
run listings do not already expose to the same caller. Inventing a
`strategy:rank` permission would mean a role could hold every capability
needed to author, backtest and read a strategy and still be refused a
summary of what it had already been shown.

**Scoped to the caller's OWN strategies, in the query itself.**
`WHERE owner_user_id = :caller` is applied in SQL rather than after the fact,
mirroring `list_strategies` exactly (D071's two-part authorization: the
permission says this account may work with strategies at all, the ownership
column says which). There is no cross-user leaderboard and no ADMIN
override - `Permission.STRATEGY_MANAGE`'s own docstring rules that out.

**An empty leaderboard is a 200, never an error.** A caller whose strategies
have no succeeded backtests, or a `min_status=validated` filter nothing
currently meets, gets `{"items": [], "limit": ..., "offset": ...}`. The
master spec is explicit that the platform should be willing to answer "no
sufficiently robust strategy found," and that an honest empty answer is
preferable to presenting a misleading strategy - an empty array IS that
answer, so treating it as a 404 or a 422 would turn the correct result into
a failure.

**Route ordering matters here and is the one reason this router is
registered before `routes/strategies.py`'s in main.py.** `/strategies/
{strategy_id}` is declared with a `uuid.UUID` path parameter, so a request
for `/strategies/leaderboard` reaching that route first would be a 422 about
a malformed UUID rather than this endpoint. FastAPI matches in registration
order, so this static path is registered first.
"""

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.api.app.api.routes.strategies import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT
from apps.api.app.api.schemas_leaderboard import (
    LeaderboardEntryResponse,
    LeaderboardResponse,
    ScoreComponentResponse,
    StrategyScoreResponse,
)
from apps.api.app.auth.dependencies import get_current_user, require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.core.logging import get_logger
from apps.api.app.db.base import get_session
from apps.api.app.db.models import Strategy, User
from apps.api.app.strategies.scoring import (
    StrategyScore,
    compute_strategy_score,
    satisfies_min_status,
)

router = APIRouter(
    prefix="/strategies",
    tags=["leaderboard"],
    dependencies=[Depends(require_permission(Permission.STRATEGY_MANAGE))],
)

logger = get_logger(__name__)

MinStatus = Literal["insufficient_data", "promising", "validated"]
"""The tiers a caller may filter on. `overfit_risk` is deliberately not
offerable as a floor - it is a warning, not a rung on the ladder, and a
request for "at least flagged strategies" is not a question anyone needs to
ask. It is still a status a returned entry can carry when no filter is
applied; see `satisfies_min_status`."""


def _score_response(score: StrategyScore) -> StrategyScoreResponse:
    return StrategyScoreResponse(
        strategy_version_id=score.strategy_version_id,
        components=[
            ScoreComponentResponse(
                name=component.name,
                points=component.points,
                max_points=component.max_points,
                detail=component.detail,
            )
            for component in score.components
        ],
        total_points=score.total_points,
        max_possible_points=score.max_possible_points,
        percentage=score.percentage,
        components_measured=score.components_measured,
        status=score.status,
        status_reason=score.status_reason,
        latest_backtest_run_id=score.latest_backtest_run_id,
        latest_walk_forward_run_id=score.latest_walk_forward_run_id,
        latest_monte_carlo_run_id=score.latest_monte_carlo_run_id,
        latest_robustness_run_id=score.latest_robustness_run_id,
    )


@router.get("/leaderboard", response_model=LeaderboardResponse)
async def get_strategy_leaderboard(
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    min_status: MinStatus | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> LeaderboardResponse:
    """The caller's own strategies, scored and ranked best-first.

    Each strategy is scored on its LATEST version by `version_number` - the
    newest work, not whichever old version happened to score well - and a
    strategy whose latest version has no succeeded backtest is EXCLUDED
    entirely rather than ranked last with a zero. There is nothing to
    measure there, and a zero would be a claim the data does not support.

    `min_status` filters to strategies at or above a tier
    (`insufficient_data` < `promising` < `validated`). `overfit_risk` is its
    own tier and satisfies nothing above the floor: a flagged strategy must
    not surface for `min_status=promising` merely because the components
    that were measured happened to score well. That is the entire point of
    the flag.

    Ranking happens over EVERY qualifying strategy before `limit`/`offset`
    slice it, because a leaderboard paged before it is ranked is not a
    leaderboard. That means the scores of all the caller's strategies are
    computed per request - four indexed single-row lookups each, over a set
    bounded by how many strategies one person has authored. Revisit with a
    materialized ranking if a single user ever holds thousands.

    An empty result is a normal 200. See the module docstring.
    """
    rows = (
        (
            await session.execute(
                select(Strategy)
                .options(selectinload(Strategy.versions))
                .where(Strategy.owner_user_id == current_user.id)
                .order_by(Strategy.created_at.asc(), Strategy.id.asc())
            )
        )
        .scalars()
        .all()
    )

    ranked: list[LeaderboardEntryResponse] = []
    for row in rows:
        if not row.versions:
            # Structurally impossible through this API (every strategy is
            # created with version 1 in the same transaction) - skipped
            # rather than failing a whole leaderboard over one hand-edited
            # row, exactly as `list_strategies` does.
            continue
        newest = max(row.versions, key=lambda version: version.version_number)
        score = await compute_strategy_score(session, newest)
        if score is None:
            continue
        if not satisfies_min_status(score.status, min_status):
            continue
        ranked.append(
            LeaderboardEntryResponse(
                strategy_id=row.id,
                strategy_name=row.name,
                strategy_version_id=newest.id,
                strategy_version_number=newest.version_number,
                score=_score_response(score),
            )
        )

    ranked.sort(key=_rank_key, reverse=True)

    logger.info(
        "strategy_leaderboard_read",
        actor_user_id=str(current_user.id),
        strategies_considered=len(rows),
        strategies_ranked=len(ranked),
        min_status=min_status,
    )
    return LeaderboardResponse(
        items=ranked[offset : offset + limit], limit=limit, offset=offset
    )


def _rank_key(entry: LeaderboardEntryResponse) -> tuple[Decimal, int, str]:
    """Percentage first, then how much was actually measured, then the id.

    `components_measured` breaks ties toward the more thoroughly tested
    strategy: at an equal percentage, the one that survived four checks has
    demonstrably more behind its number than one that survived two. The id is
    the final, arbitrary-but-STABLE tiebreaker that keeps `offset` paging
    deterministic - `str(uuid)` because the sort is descending and a plain
    UUID has no total order under `reverse=True` in a mixed tuple.
    """
    return (
        entry.score.percentage,
        entry.score.components_measured,
        str(entry.strategy_id),
    )
