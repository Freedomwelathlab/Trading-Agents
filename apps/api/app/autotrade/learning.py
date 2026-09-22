"""The bot's improvement loop (Phase 81 → 83, D098/D099).

What "learning" means here, precisely:

1. **Statistics over this bot's own closed trades**, at three
   granularities — per setup, per (setup, symbol), and per (setup, hour of
   day) — count, win rate, expectancy in R, total R, plus the maximum
   favourable / adverse excursion the trades recorded.
2. **One automatic consequence**: in `auto` strategy mode a setup is
   DEMOTED (not run) once it has enough of its own evidence that it loses.
   Since Phase 83 the rule is applied per (setup, symbol) where that pair
   has enough trades, falling back to the setup as a whole — a setup that
   loses on one ticker and wins on another is switched off only where it
   loses.
3. **A daily insights journal** (`autotrade_bot_insights`): after each
   session, the aggregate plus deterministic, thresholded FINDINGS —
   "60% of stopped trades reached +1R first", "score-3 signals lose while
   score-4+ win", "the 15:00 hour loses" — recorded for the operator to
   read and act on. The loop never changes a bot's parameters from a
   finding; that stays a human decision, made with the numbers in view.

What it is not: a model, a weight vector, or anything that changes how a
setup detects. Re-fitting detectors to the last few dozen trades would be
the overfitting machine D090 warns about, at a sample size where noise
dominates. Every threshold below carries a minimum sample size for that
reason.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.db.models import AutotradeBotInsight, AutotradeBotTrade
from apps.api.app.marketdata.sessions import to_exchange_time

DEMOTE_MIN_TRADES = 20
"""Fewer closed trades than this and a setup keeps trading whatever its
numbers say — a 5-trade losing streak is noise, not evidence."""

DEMOTE_BELOW_EXPECTANCY_R = Decimal("-0.10")
"""Demoted when expectancy is worse than this per trade. Not zero: a
setup hovering around breakeven after costs is inconclusive, and flipping
it in and out of rotation on every trade would itself be noise."""

INSIGHT_MIN_TRADES = 5
"""A finding needs at least this many trades in the slice it describes."""

_ZERO = Decimal(0)


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
    symbol: str | None = None
    hour: int | None = None
    avg_mfe_r: Decimal | None = None
    """Mean maximum favourable excursion, in R."""
    avg_mae_r: Decimal | None = None
    """Mean maximum adverse excursion, in R (negative)."""


def _r(trade: AutotradeBotTrade, price: Decimal | None) -> Decimal | None:
    risk = trade.entry_price - trade.initial_stop_price
    if price is None or risk <= 0:
        return None
    return (price - trade.entry_price) / risk


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return (sum(values, _ZERO) / len(values)) if values else None


def _aggregate(
    name: str, trades: Sequence[AutotradeBotTrade], *, symbol=None, hour=None
) -> SetupStats:
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    pnls = [t.realized_pnl for t in trades if t.realized_pnl is not None]
    n = len(trades)
    wins = sum(1 for p in pnls if p > 0)
    total_r = sum(rs, _ZERO)
    expectancy = _mean(rs)
    mfe = [r for r in (_r(t, t.peak_price) for t in trades) if r is not None]
    mae = [r for r in (_r(t, t.trough_price) for t in trades) if r is not None]
    return SetupStats(
        setup_name=name,
        trades=n,
        wins=wins,
        win_rate=(Decimal(wins) / n) if n else None,
        expectancy_r=expectancy,
        total_r=total_r,
        total_pnl=sum(pnls, _ZERO),
        demoted=is_demoted(n, expectancy),
        symbol=symbol,
        hour=hour,
        avg_mfe_r=_mean(mfe),
        avg_mae_r=_mean(mae),
    )


def _hour_et(trade: AutotradeBotTrade) -> int:
    return to_exchange_time(trade.opened_at).hour


async def closed_trades(session: AsyncSession, bot_id: uuid.UUID) -> list[AutotradeBotTrade]:
    return list(
        (
            await session.execute(
                select(AutotradeBotTrade).where(
                    AutotradeBotTrade.bot_id == bot_id,
                    AutotradeBotTrade.closed_at.is_not(None),
                )
            )
        ).scalars().all()
    )


def stats_by_setup(trades: Iterable[AutotradeBotTrade]) -> list[SetupStats]:
    groups: dict[str, list[AutotradeBotTrade]] = defaultdict(list)
    for t in trades:
        groups[t.setup_name].append(t)
    return [_aggregate(name, groups[name]) for name in sorted(groups)]


def stats_by_setup_symbol(trades: Iterable[AutotradeBotTrade]) -> list[SetupStats]:
    groups: dict[tuple[str, str], list[AutotradeBotTrade]] = defaultdict(list)
    for t in trades:
        groups[(t.setup_name, t.symbol)].append(t)
    return [_aggregate(n, groups[(n, s)], symbol=s) for n, s in sorted(groups)]


def stats_by_setup_hour(trades: Iterable[AutotradeBotTrade]) -> list[SetupStats]:
    groups: dict[tuple[str, int], list[AutotradeBotTrade]] = defaultdict(list)
    for t in trades:
        groups[(t.setup_name, _hour_et(t))].append(t)
    return [_aggregate(n, groups[(n, h)], hour=h) for n, h in sorted(groups)]


async def setup_stats(session: AsyncSession, bot_id: uuid.UUID) -> list[SetupStats]:
    """Per-setup numbers (the Phase 81 shape, kept for the routes)."""
    return stats_by_setup(await closed_trades(session, bot_id))


def is_demoted(trades: int, expectancy_r: Decimal | None) -> bool:
    return (
        trades >= DEMOTE_MIN_TRADES
        and expectancy_r is not None
        and expectancy_r < DEMOTE_BELOW_EXPECTANCY_R
    )


def active_setups(
    configured: list[str], *, strategy_mode: str, stats: list[SetupStats]
) -> list[str]:
    """Which setups may fire this cycle, at the setup level.

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


def active_setups_for_symbol(
    configured: list[str],
    *,
    strategy_mode: str,
    symbol: str,
    by_setup: list[SetupStats],
    by_pair: list[SetupStats],
) -> list[str]:
    """Phase 83: the per-symbol refinement of `active_setups`.

    For each configured setup, the (setup, symbol) pair's own numbers
    decide if — and only if — that pair has reached `DEMOTE_MIN_TRADES`;
    otherwise the setup-level verdict applies. So a setup that has proven
    itself a loser on TQQQ specifically is switched off there even while
    its overall figure is fine, and a setup with too little evidence on a
    new ticker inherits its overall verdict rather than a blank slate.
    """
    if strategy_mode != "auto":
        return list(configured)
    setup_level = {s.setup_name: s for s in by_setup}
    pair_level = {(s.setup_name, s.symbol): s for s in by_pair}
    out: list[str] = []
    for name in configured:
        pair = pair_level.get((name, symbol))
        if pair is not None and pair.trades >= DEMOTE_MIN_TRADES:
            if not pair.demoted:
                out.append(name)
            continue
        overall = setup_level.get(name)
        if overall is None or not overall.demoted:
            out.append(name)
    return out


# --- insights ----------------------------------------------------------------


@dataclass(frozen=True)
class SessionInsight:
    session_date: date
    trades: int
    wins: int
    total_r: Decimal
    total_pnl: Decimal
    best_setup: str | None
    worst_setup: str | None
    findings: list[str] = field(default_factory=list)
    demoted_setups: list[str] = field(default_factory=list)


def _pct(n: int, d: int) -> int:
    return int(round(100 * n / d)) if d else 0


def derive_findings(
    day_trades: Sequence[AutotradeBotTrade],
    all_trades: Sequence[AutotradeBotTrade],
    *,
    min_score: int,
) -> list[str]:
    """Deterministic, thresholded statements about what the numbers show.

    Each one names its sample size, so a reader can weigh it. Each is a
    question for the operator, not an instruction the loop follows.
    """
    findings: list[str] = []

    # 1. Stops: did losers first go the right way? (all history — this is
    #    a property of the bracket, not of one day)
    losers = [t for t in all_trades if t.realized_pnl is not None and t.realized_pnl < 0]
    if len(losers) >= INSIGHT_MIN_TRADES:
        reached_1r = [t for t in losers if (_r(t, t.peak_price) or _ZERO) >= 1]
        share = _pct(len(reached_1r), len(losers))
        if share >= 50:
            findings.append(
                f"{share}% of the {len(losers)} losing trades reached +1R before losing — "
                f"a trailing stop or a partial take-profit at 1R would have kept some of it."
            )
        straight = [t for t in losers if (_r(t, t.peak_price) or _ZERO) <= Decimal("0.25")]
        share_s = _pct(len(straight), len(losers))
        if share_s >= 60:
            findings.append(
                f"{share_s}% of the {len(losers)} losing trades never got past +0.25R — "
                f"they were wrong at entry, not stopped too tight. Look at the signal, "
                f"not the stop."
            )

    # 2. Targets: winners that left a lot on the table.
    winners = [t for t in all_trades if t.realized_pnl is not None and t.realized_pnl > 0]
    if len(winners) >= INSIGHT_MIN_TRADES:
        left = [
            t for t in winners
            if (_r(t, t.peak_price) or _ZERO) - (t.r_multiple or _ZERO) >= 1
        ]
        share = _pct(len(left), len(winners))
        if share >= 50:
            findings.append(
                f"{share}% of the {len(winners)} winning trades peaked at least 1R above where "
                f"they exited — the target or the trailing take-profit is giving back a lot."
            )

    # 3. Exit reasons: are session-end flats doing the work?
    closed = [t for t in all_trades if t.exit_reason]
    if len(closed) >= INSIGHT_MIN_TRADES:
        se = [t for t in closed if t.exit_reason == "session_end"]
        share = _pct(len(se), len(closed))
        if share >= 50:
            rs = [t.r_multiple for t in se if t.r_multiple is not None]
            avg = _mean(rs)
            findings.append(
                f"{share}% of exits were session-end flats (avg {avg:.2f}R) — neither the stop "
                f"nor the target is being reached inside the session; the bracket may be too "
                f"wide for a 5-minute intraday trade."
            )

    # 4. Score bands: do low-score signals lose while higher ones win?
    by_score: dict[int, list[Decimal]] = defaultdict(list)
    for t in all_trades:
        if t.r_multiple is not None:
            by_score[t.score].append(t.r_multiple)
    low = by_score.get(min_score, [])
    high = [r for s, rs in by_score.items() if s > min_score for r in rs]
    if len(low) >= 10 and len(high) >= 10:
        low_e, high_e = _mean(low), _mean(high)
        if low_e is not None and high_e is not None and low_e < 0 < high_e:
            findings.append(
                f"score-{min_score} signals average {low_e:.2f}R over {len(low)} trades while "
                f"score-{min_score + 1}+ average {high_e:.2f}R over {len(high)} — raising the "
                f"minimum score to {min_score + 1} would have dropped the losers."
            )

    # 5. Hours: an hour of the ET day that consistently loses.
    for s in stats_by_setup_hour(all_trades):
        if (
            s.trades >= INSIGHT_MIN_TRADES
            and s.expectancy_r is not None
            and s.expectancy_r <= Decimal("-0.30")
            and s.hour is not None
        ):
            findings.append(
                f"{s.setup_name} opened in the {s.hour:02d}:00 ET hour averages "
                f"{s.expectancy_r:.2f}R over {s.trades} trades."
            )

    # 6. The day itself.
    day_rs = [t.r_multiple for t in day_trades if t.r_multiple is not None]
    if day_rs:
        total = sum(day_rs, _ZERO)
        if total <= Decimal("-3"):
            findings.append(
                f"Today closed {total:.2f}R across {len(day_rs)} trades — a daily loss limit "
                f"(the playbook's §18) would have stopped the session earlier."
            )
    return findings


def build_session_insight(
    session_date: date,
    all_trades: Sequence[AutotradeBotTrade],
    *,
    min_score: int,
) -> SessionInsight:
    day = [t for t in all_trades if t.session_date == session_date and t.closed_at is not None]
    rs = [t.r_multiple for t in day if t.r_multiple is not None]
    pnls = [t.realized_pnl for t in day if t.realized_pnl is not None]
    per_setup = stats_by_setup(day)
    ranked = sorted(
        (s for s in per_setup if s.expectancy_r is not None),
        key=lambda s: s.expectancy_r or _ZERO,
    )
    overall = stats_by_setup(all_trades)
    return SessionInsight(
        session_date=session_date,
        trades=len(day),
        wins=sum(1 for p in pnls if p > 0),
        total_r=sum(rs, _ZERO),
        total_pnl=sum(pnls, _ZERO),
        best_setup=ranked[-1].setup_name if ranked else None,
        worst_setup=ranked[0].setup_name if ranked else None,
        findings=derive_findings(day, all_trades, min_score=min_score),
        demoted_setups=[s.setup_name for s in overall if s.demoted],
    )


async def write_pending_insights(
    session: AsyncSession,
    bot_id: uuid.UUID,
    *,
    min_score: int,
    before: date,
    include: date | None = None,
) -> list[AutotradeBotInsight]:
    """Write an insight row for every session that has closed trades, is
    older than `before` (or equals `include`, for the session that just
    ended), and has no row yet. Idempotent by the unique constraint."""
    trades = await closed_trades(session, bot_id)
    if not trades:
        return []
    have = {
        row.session_date
        for row in (
            await session.execute(
                select(AutotradeBotInsight).where(AutotradeBotInsight.bot_id == bot_id)
            )
        ).scalars()
    }
    days = {t.session_date for t in trades}
    due = sorted(d for d in days if d not in have and (d < before or d == include))
    written: list[AutotradeBotInsight] = []
    for d in due:
        ins = build_session_insight(d, trades, min_score=min_score)
        row = AutotradeBotInsight(
            id=uuid.uuid4(),
            bot_id=bot_id,
            session_date=ins.session_date,
            trades=ins.trades,
            wins=ins.wins,
            total_r=ins.total_r,
            total_pnl=ins.total_pnl,
            best_setup=ins.best_setup,
            worst_setup=ins.worst_setup,
            findings=list(ins.findings),
            demoted_setups=list(ins.demoted_setups),
        )
        session.add(row)
        written.append(row)
    if written:
        await session.flush()
    return written
