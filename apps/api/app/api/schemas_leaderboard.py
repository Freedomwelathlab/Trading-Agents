"""DTOs for the Phase 59 strategy leaderboard.

Every points/percentage field is `Decimal`, never `float` - the project-wide
rule for anything numeric that a person will read as a figure, stated in
schemas_strategy_backtests.py and schemas_robustness.py before it.

These schemas carry no result the API could not explain: every component
brings its own `detail` sentence and the score brings its own
`status_reason`, so a client can render "why" without reimplementing
apps/api/app/strategies/scoring.py's arithmetic - and cannot render a verdict
this backend did not actually justify.
"""

import uuid
from decimal import Decimal

from pydantic import BaseModel


class ScoreComponentResponse(BaseModel):
    """One scored dimension: its points, the maximum it could have earned,
    and a sentence naming the real column value and the scale behind it.

    `max_points` is sent explicitly rather than assumed to be 25 forever, so
    a client renders `points/max_points` from the response instead of from a
    constant it would have to keep in sync with the backend.

    A component that could NOT be measured is absent from
    `StrategyScoreResponse.components` entirely - it is never sent with
    `points: 0`. "Not measured" and "measured, scored zero" are opposite
    findings (docs/TRADING_SAFETY.md's no-fabrication rule), and only the
    second one is a statement about the strategy.
    """

    name: str
    points: Decimal
    max_points: Decimal
    detail: str


class StrategyScoreResponse(BaseModel):
    """A whole score, with its provenance.

    `percentage` - not `total_points` - is the ranking key, and the two can
    disagree by design: a strategy measured on 2 components scoring 40/50
    ranks exactly level with one measured on 4 scoring 80/100, because
    ranking by raw points would reward never having run the walk-forward or
    robustness tests that might have gone badly. `components_measured` is
    sent alongside so a reader always sees how much was actually checked.

    `status` is one of `insufficient_data`, `promising`, `validated`,
    `overfit_risk` and `status_reason` always names the real counts that
    produced it. A `validated` status means both available checks ran and
    neither raised a flag - never that a strategy is expected to be
    profitable.

    The four `latest_*_run_id` fields point at the exact persisted runs each
    number came from, so any component can be independently re-checked
    through `/backtest-runs/{id}`, `/walk-forward-runs/{id}`,
    `/monte-carlo-runs/{id}` and `/robustness-runs/{id}`. The Monte Carlo id
    is reported but scores nothing this phase (see scoring.py).
    """

    strategy_version_id: uuid.UUID
    components: list[ScoreComponentResponse]
    total_points: Decimal
    max_possible_points: Decimal
    percentage: Decimal
    components_measured: int
    status: str
    status_reason: str
    latest_backtest_run_id: uuid.UUID
    latest_walk_forward_run_id: uuid.UUID | None
    latest_monte_carlo_run_id: uuid.UUID | None
    latest_robustness_run_id: uuid.UUID | None


class LeaderboardEntryResponse(BaseModel):
    """One ranked strategy: which strategy, which VERSION of it was scored,
    and the score.

    The version is always the strategy's latest by `version_number` - a
    strategy is ranked by its newest work rather than by whichever old
    version happens to have the best numbers - and both its id and its
    number are sent so a client can link straight to the version the score
    actually describes rather than to the strategy in general.
    """

    strategy_id: uuid.UUID
    strategy_name: str
    strategy_version_id: uuid.UUID
    strategy_version_number: int
    score: StrategyScoreResponse


class LeaderboardResponse(BaseModel):
    """Paginated listing envelope - `{items, limit, offset}`, the same shape
    and the same generic `items` key as every other listing in this codebase.

    `items` is sorted by `score.percentage` DESCENDING, best first, with
    `components_measured` descending and then `strategy_id` as deterministic
    tiebreakers so `offset` paging is stable across requests.

    An EMPTY `items` is an ordinary, correct answer - a 200, never an error.
    A user whose strategies are all unbacktested, or a `min_status=validated`
    filter nothing currently meets, gets `{"items": [], ...}`: "no
    sufficiently robust strategy found" is the honest result, and it is
    preferable to promoting a strategy that does not meet the bar.
    """

    items: list[LeaderboardEntryResponse]
    limit: int
    offset: int
