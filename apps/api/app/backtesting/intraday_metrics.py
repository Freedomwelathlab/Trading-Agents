"""The measurement list from the playbook's backtesting specification
(Phase 73, D091).

Section 21 names what must be recorded for every strategy and adds the
rule that gives the list its shape: *"a strategy should not be judged by
win rate alone"*. Everything here is built around that. Expectancy is in
R, so a system that wins often and loses large is not flattered; profit
factor and the streak measures describe the path, not just the endpoint;
and the hit-rate breakdown separates "reached the first target" from
"kept anything", which a scaled-out strategy makes very different
questions.

Every figure is `None` when the data cannot support it, never zero. A
profit factor of zero means "lost money and never won", while an absent
one means "never lost, so the ratio is undefined" - and reporting the
second as the first would rank a flawless strategy last.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from apps.api.app.backtesting.brackets import BracketTrade
from apps.api.app.marketdata.sessions import to_exchange_time


@dataclass(frozen=True)
class IntradayMetrics:
    total_trades: int
    wins: int
    losses: int
    win_rate: Decimal | None
    average_win_r: Decimal | None
    average_loss_r: Decimal | None
    expectancy_r: Decimal | None
    profit_factor: Decimal | None
    total_r: Decimal
    gross_pnl: Decimal
    max_drawdown_r: Decimal
    longest_winning_streak: int
    longest_losing_streak: int
    tp1_hit_rate: Decimal | None
    tp2_hit_rate: Decimal | None
    stop_out_rate: Decimal | None
    average_holding_minutes: Decimal | None
    capped_trades: int
    long_trades: int
    short_trades: int

    def as_row(self) -> dict[str, object]:
        """Flat mapping for reporting, with `None` preserved."""
        return {
            "trades": self.total_trades,
            "win_rate": self.win_rate,
            "expectancy_r": self.expectancy_r,
            "profit_factor": self.profit_factor,
            "total_r": self.total_r,
            "gross_pnl": self.gross_pnl,
            "max_dd_r": self.max_drawdown_r,
            "avg_win_r": self.average_win_r,
            "avg_loss_r": self.average_loss_r,
            "tp1": self.tp1_hit_rate,
            "tp2": self.tp2_hit_rate,
            "stopped": self.stop_out_rate,
            "hold_min": self.average_holding_minutes,
        }


def _rate(n: int, total: int) -> Decimal | None:
    return (Decimal(n) / Decimal(total)) if total else None


def compute_metrics(trades: Sequence[BracketTrade]) -> IntradayMetrics:
    """Every figure derives from realised R, in trade order."""
    rs = [t.r_multiple for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    total = len(trades)

    # Drawdown on the cumulative R curve rather than on equity, so it is
    # comparable across account sizes and across instruments.
    peak = Decimal(0)
    running = Decimal(0)
    max_dd = Decimal(0)
    for r in rs:
        running += r
        peak = max(peak, running)
        max_dd = min(max_dd, running - peak)

    best_win_streak = best_loss_streak = win_streak = loss_streak = 0
    for r in rs:
        if r > 0:
            win_streak, loss_streak = win_streak + 1, 0
        elif r < 0:
            loss_streak, win_streak = loss_streak + 1, 0
        else:
            win_streak = loss_streak = 0
        best_win_streak = max(best_win_streak, win_streak)
        best_loss_streak = max(best_loss_streak, loss_streak)

    gross_win = sum(wins, Decimal(0))
    gross_loss = -sum(losses, Decimal(0))

    holds = [
        Decimal((t.exit_ts - t.entry_ts).total_seconds()) / Decimal(60)
        for t in trades
        if t.exit_ts is not None
    ]

    return IntradayMetrics(
        total_trades=total,
        wins=len(wins),
        losses=len(losses),
        win_rate=_rate(len(wins), total),
        average_win_r=(gross_win / len(wins)) if wins else None,
        average_loss_r=(sum(losses, Decimal(0)) / len(losses)) if losses else None,
        # Expectancy per trade, which is the playbook's core formula
        # rearranged: (win rate x avg win) - (loss rate x avg loss) is
        # identical to the mean of R, and the mean cannot disagree with
        # the components the way two separately-rounded terms can.
        expectancy_r=(sum(rs, Decimal(0)) / total) if total else None,
        # `None`, not infinity and not zero, when nothing was lost.
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else None,
        total_r=sum(rs, Decimal(0)),
        gross_pnl=sum((t.gross_pnl for t in trades), Decimal(0)),
        max_drawdown_r=max_dd,
        longest_winning_streak=best_win_streak,
        longest_losing_streak=best_loss_streak,
        tp1_hit_rate=_rate(sum(1 for t in trades if t.hit_tp1), total),
        tp2_hit_rate=_rate(sum(1 for t in trades if t.hit_tp2), total),
        stop_out_rate=_rate(sum(1 for t in trades if t.stopped_out), total),
        average_holding_minutes=(sum(holds, Decimal(0)) / len(holds)) if holds else None,
        capped_trades=sum(1 for t in trades if t.size_was_capped),
        long_trades=sum(1 for t in trades if t.direction.value == "long"),
        short_trades=sum(1 for t in trades if t.direction.value == "short"),
    )


def by_hour(trades: Sequence[BracketTrade]) -> dict[int, IntradayMetrics]:
    """Performance by exchange-local entry hour.

    Local, not UTC: "the first hour of trading" and "the final hour" are
    the playbook's session rules, and a UTC hour is neither of those for
    part of the year.
    """
    buckets: dict[int, list[BracketTrade]] = {}
    for trade in trades:
        buckets.setdefault(to_exchange_time(trade.entry_ts).hour, []).append(trade)
    return {hour: compute_metrics(group) for hour, group in sorted(buckets.items())}


def by_weekday(trades: Sequence[BracketTrade]) -> dict[str, IntradayMetrics]:
    buckets: dict[str, list[BracketTrade]] = {}
    for trade in trades:
        buckets.setdefault(
            to_exchange_time(trade.entry_ts).strftime("%a"), []
        ).append(trade)
    order = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    return {
        day: compute_metrics(buckets[day]) for day in order if day in buckets
    }


def by_setup(trades: Sequence[BracketTrade]) -> dict[str, IntradayMetrics]:
    buckets: dict[str, list[BracketTrade]] = {}
    for trade in trades:
        buckets.setdefault(trade.setup_name, []).append(trade)
    return {name: compute_metrics(group) for name, group in sorted(buckets.items())}


def by_direction(trades: Sequence[BracketTrade]) -> dict[str, IntradayMetrics]:
    """Longs and shorts measured separately.

    A leveraged long ETF in a rising market can show a positive total
    while its short side loses steadily, and the blended figure hides
    that the system is really one-directional.
    """
    buckets: dict[str, list[BracketTrade]] = {}
    for trade in trades:
        buckets.setdefault(trade.direction.value, []).append(trade)
    return {name: compute_metrics(group) for name, group in sorted(buckets.items())}
