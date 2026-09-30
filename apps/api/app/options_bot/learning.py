"""The options bot's learning summary (Phase 102, D122).

Arithmetic over the bot's OWN closed trades, grouped by structure type (and
by exit reason): count, win rate, expectancy in dollars and in return on
risk (P&L / max loss - the options analogue of R), total P&L, and the mean
best / worst unrealised P&L % each trade saw while open.

Like `autotrade/learning.py` it is a record for the operator, not a model:
nothing here changes what the bot does. Unlike the autotrade loop it does
not demote anything - one structure type per bot means there is nothing to
choose between, and a demotion rule needs a sample this bot will take
months to produce.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.db.models import OptionsBotTrade

_ZERO = Decimal(0)


@dataclass(frozen=True)
class StructureStats:
    key: str
    """The structure type (or exit reason, for the by-exit breakdown)."""
    trades: int
    wins: int
    win_rate: Decimal | None
    expectancy: Decimal | None
    """Mean realised P&L per trade, in account currency."""
    expectancy_on_risk: Decimal | None
    """Mean realised P&L / max loss per trade."""
    total_pnl: Decimal
    avg_peak_pnl_pct: Decimal | None
    avg_trough_pnl_pct: Decimal | None


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return (sum(values, _ZERO) / len(values)) if values else None


def aggregate(key: str, trades: Sequence[OptionsBotTrade]) -> StructureStats:
    pnls = [t.realized_pnl for t in trades if t.realized_pnl is not None]
    rors = [t.return_on_risk for t in trades if t.return_on_risk is not None]
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    return StructureStats(
        key=key,
        trades=n,
        wins=wins,
        win_rate=(Decimal(wins) / n) if n else None,
        expectancy=_mean(pnls),
        expectancy_on_risk=_mean(rors),
        total_pnl=sum(pnls, _ZERO),
        avg_peak_pnl_pct=_mean([t.peak_pnl_pct for t in trades if t.peak_pnl_pct is not None]),
        avg_trough_pnl_pct=_mean(
            [t.trough_pnl_pct for t in trades if t.trough_pnl_pct is not None]
        ),
    )


def stats_by(trades: Sequence[OptionsBotTrade], *, field: str) -> list[StructureStats]:
    groups: dict[str, list[OptionsBotTrade]] = defaultdict(list)
    for t in trades:
        if t.closed_at is None or t.realized_pnl is None:
            continue
        groups[str(getattr(t, field) or "unknown")].append(t)
    return [aggregate(k, groups[k]) for k in sorted(groups)]


def stats_by_structure(trades: Sequence[OptionsBotTrade]) -> list[StructureStats]:
    return stats_by(trades, field="structure_type")


def stats_by_exit_reason(trades: Sequence[OptionsBotTrade]) -> list[StructureStats]:
    return stats_by(trades, field="exit_reason")


async def closed_bot_trades(session: AsyncSession, bot_id: uuid.UUID) -> list[OptionsBotTrade]:
    return list(
        (
            await session.execute(
                select(OptionsBotTrade)
                .where(OptionsBotTrade.bot_id == bot_id, OptionsBotTrade.closed_at.is_not(None))
                .order_by(OptionsBotTrade.closed_at.asc())
            )
        )
        .scalars()
        .all()
    )
