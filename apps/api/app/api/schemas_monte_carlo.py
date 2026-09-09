"""DTOs for the Phase 57 Monte Carlo routes.

Every money and percentage field is `Decimal`, never `float` - the same
project-wide rule schemas_strategy_backtests.py states, for the same
reason.

**No summary/detail split here, deliberately.** That split exists on the
backtest DTOs because a run owns an equity curve and a trade list, and
returning fifty curves to render a table would be absurd. A Monte Carlo
run owns no child collection at all - it is fourteen scalar columns by
design (see `MonteCarloRun`'s docstring: individual simulated paths are
never persisted, because the seed regenerates them). One response shape
therefore serves the POST, the by-id GET and the listing, and there is no
second shape that could drift out of agreement with it.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from apps.api.app.db.models import MonteCarloRunStatus


class CreateMonteCarloRunRequest(BaseModel):
    """How many paths to simulate. Everything else - the trade returns, the
    starting cash - comes from the backtest run named by the path, which is
    the whole input to a resampling.

    Bounded HERE rather than inside the engine, so an out-of-range request
    is a clean 422 that never reaches the database: nobody means to ask for
    100,000 simulations, and 100 is the floor below which the percentiles
    reported would be describing the sampling noise rather than the
    distribution. The seed is deliberately NOT accepted from the client -
    it is generated per run and persisted, which is what makes a run
    reproducible without making it steerable.
    """

    num_simulations: int = Field(default=1000, ge=100, le=10000)


class MonteCarloRunResponse(BaseModel):
    """One resampling run in full - inputs, seed, and the aggregate
    percentiles.

    Every statistic is optional because a FAILED run (too few trades in the
    source backtest) computed none of them. A null means "not computed",
    never 0 - reporting a 0% probability of ruin for a run that never
    simulated anything would be a fabricated figure
    (docs/TRADING_SAFETY.md). `error_detail` is what says why, and is null
    on a successful run for the same reason.

    `random_seed` is part of the response on purpose: it is what lets
    anyone holding this row regenerate the exact distribution behind these
    numbers, and a reproducibility guarantee nobody can see is not one.
    """

    id: uuid.UUID
    backtest_run_id: uuid.UUID
    num_simulations: int
    random_seed: int
    status: MonteCarloRunStatus
    num_trades_resampled: int | None
    median_final_equity: Decimal | None
    p5_final_equity: Decimal | None
    p95_final_equity: Decimal | None
    median_max_drawdown_pct: Decimal | None
    p5_max_drawdown_pct: Decimal | None
    p95_max_drawdown_pct: Decimal | None
    probability_of_ruin_pct: Decimal | None
    error_detail: str | None
    created_at: datetime
    completed_at: datetime | None


class ListMonteCarloRunsResponse(BaseModel):
    """Paginated listing envelope - `{items, limit, offset}`, the same shape
    as `ListBacktestRunsResponse` and every other listing in this codebase,
    with the same generic `items` key rather than a per-resource name."""

    items: list[MonteCarloRunResponse]
    limit: int
    offset: int
