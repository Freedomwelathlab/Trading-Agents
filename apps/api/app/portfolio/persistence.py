"""Persisting a computed `PortfolioSnapshot` as append-only rows.

Extracted from apps/api/app/api/routes/portfolio.py in Phase 27 (D030) so
the manual `POST .../portfolio/snapshots` route (D027) and the new
scheduled capture (apps/api/app/portfolio/scheduler.py) write *byte-for-
byte the same rows* through one code path. Two independent writers of the
same append-only history is exactly how a schema drifts, and a scheduled
snapshot that recorded subtly different numbers than a manual one would
make the resulting time series untrustworthy for the performance
attribution it exists to feed.

This module never computes anything and never sources a price. It only
ever writes what `compute_portfolio_snapshot()` already returned - the
no-fabrication rule (docs/TRADING_SAFETY.md, spec Sec57) is enforced
upstream, in the caller that had to obtain real marks before it could get
a `PortfolioSnapshot` at all.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.db.models import PortfolioSnapshotPositionRow, PortfolioSnapshotRow
from apps.api.app.portfolio.models import CostBasisMethod, PortfolioSnapshot


async def persist_portfolio_snapshot(
    session: AsyncSession,
    broker_id: uuid.UUID,
    computed: PortfolioSnapshot,
    *,
    cost_basis_method: CostBasisMethod = CostBasisMethod.AVERAGE,
) -> PortfolioSnapshotRow:
    """Inserts `computed` as one `portfolio_snapshots` row plus one
    `portfolio_snapshot_positions` child row per open position, commits,
    and returns the persisted parent with `positions` loaded.

    `captured_at` is left to the column's `server_default=func.now()` so
    the database clock - not the application process's - stamps every
    snapshot, manual and scheduled alike. That matters more once a
    scheduler exists: a drifting app-server clock would otherwise write a
    time series whose ordering disagreed with the DB's own.

    `cost_basis_method` must be the same method the caller passed to
    `compute_portfolio_snapshot()` to produce `computed` (D044). It is
    recorded, not re-derived, because this module deliberately computes
    nothing - but that also means nothing here can detect a caller that
    computed under FIFO and persisted under AVERAGE. Both existing callers
    thread one variable through both calls for exactly that reason; a new
    caller must too. It defaults to AVERAGE so the parameter is optional
    for the pre-D044 call shape, which was average-only by construction.
    """
    row = PortfolioSnapshotRow(
        broker_id=broker_id,
        cash=computed.cash,
        total_equity=computed.total_equity,
        total_unrealized_pnl=computed.total_unrealized_pnl,
        total_realized_pnl=computed.total_realized_pnl,
        cost_basis_method=cost_basis_method.value,
    )
    row.positions = [
        PortfolioSnapshotPositionRow(
            symbol=position.symbol,
            quantity=position.quantity,
            avg_cost=position.avg_cost,
            current_value=position.current_value,
            unrealized_pnl=position.unrealized_pnl,
            realized_pnl=position.realized_pnl,
        )
        for position in computed.positions
    ]
    session.add(row)
    await session.commit()
    await session.refresh(row, attribute_names=["positions"])
    return row
