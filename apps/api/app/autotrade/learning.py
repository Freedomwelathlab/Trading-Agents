"""The bot's improvement loop (Phase 81, D098).

What "learning" means here, precisely: per-setup statistics over THIS
bot's own closed trades — count, win rate, expectancy in R, total R —
persisted implicitly by the trade ledger and recomputed on demand. In
`auto` strategy mode the bot demotes a setup once it has enough of its own
evidence that the setup loses money, and re-admits it if the evidence
later turns.

What it is not: a model, a weight vector, or anything that changes how a
setup detects. The detectors are fixed code (D095 measured them); the loop
only decides which of them are allowed to fire for this bot. That is
deliberate: a loop that tuned detector parameters against the bot's own
recent trades would be the overfitting machine D090 warns about, with a
sample size of dozens.

Every demotion is visible: the run row records `setups_active` after the
rule is applied, and `GET /autotrade/bots/{id}/stats` shows the numbers it
was applied to.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.db.models import AutotradeBotTrade

DEMOTE_MIN_TRADES = 20
"""Fewer closed trades than this and a setup keeps trading whatever its
numbers say — a 5-trade losing streak is noise, not evidence."""

DEMOTE_BELOW_EXPECTANCY_R = Decimal("-0.10")
"""Demoted when expectancy is worse than this per trade. Not zero: a
setup hovering around breakeven after costs is inconclusive, and flipping
it in and out of rotation on every trade would itself be noise."""


@dataclass(frozen=True)
class SetupStats:
    setup_name: str
    trades: int
    wins: int
    win_rate: Decimal | None
    expectancy_r: Decimal | None
    total_r: Decimal
    total_pnl: Decimal
    demoted: bool


async def setup_stats(session: AsyncSession, bot_id: uuid.UUID) -> list[SetupStats]:
    rows = (
        await session.execute(
            select(AutotradeBotTrade).where(
                AutotradeBotTrade.bot_id == bot_id,
                AutotradeBotTrade.closed_at.is_not(None),
            )
        )
    ).scalars().all()

    by_setup: dict[str, list[AutotradeBotTrade]] = {}
    for t in rows:
        by_setup.setdefault(t.setup_name, []).append(t)

    out: list[SetupStats] = []
    for name in sorted(by_setup):
        trades = by_setup[name]
        rs = [t.r_multiple for t in trades if t.r_multiple is not None]
        pnls = [t.realized_pnl for t in trades if t.realized_pnl is not None]
        n = len(trades)
        wins = sum(1 for p in pnls if p > 0)
        total_r = sum(rs, Decimal(0))
        expectancy = (total_r / len(rs)) if rs else None
        out.append(
            SetupStats(
                setup_name=name,
                trades=n,
                wins=wins,
                win_rate=(Decimal(wins) / n) if n else None,
                expectancy_r=expectancy,
                total_r=total_r,
                total_pnl=sum(pnls, Decimal(0)),
                demoted=is_demoted(n, expectancy),
            )
        )
    return out


def is_demoted(trades: int, expectancy_r: Decimal | None) -> bool:
    return (
        trades >= DEMOTE_MIN_TRADES
        and expectancy_r is not None
        and expectancy_r < DEMOTE_BELOW_EXPECTANCY_R
    )


def active_setups(
    configured: list[str], *, strategy_mode: str, stats: list[SetupStats]
) -> list[str]:
    """Which setups may fire this cycle.

    `single` / `multi`: exactly what the operator chose — an explicit
    choice is never overridden by statistics.
    `auto`: the configured list minus every setup the evidence demotes.
    If that would leave nothing, nothing is what runs: an all-losing book
    is a reason to stop, not to trade the least-bad loser.
    """
    if strategy_mode != "auto":
        return list(configured)
    demoted = {s.setup_name for s in stats if s.demoted}
    return [name for name in configured if name not in demoted]
