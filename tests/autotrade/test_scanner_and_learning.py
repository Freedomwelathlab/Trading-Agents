"""Scanner gates and the learning loop's demotion rule (Phase 81, D098)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import apps.api.app.autotrade.scanner as scanner_module
from apps.api.app.autotrade.learning import (
    DEMOTE_MIN_TRADES,
    SetupStats,
    active_setups,
    is_demoted,
)
from apps.api.app.autotrade.scanner import scan_latest_bar
from apps.api.app.backtesting.brackets import BracketPlan
from apps.api.app.backtesting.setups import SetupSignal
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.structure import Direction

OPEN_UTC = datetime(2026, 6, 17, 13, 30, tzinfo=UTC)  # 09:30 ET


def bars(n: int, *, start: datetime = OPEN_UTC, price: str = "100") -> list[Bar]:
    p = Decimal(price)
    return [
        Bar(symbol="X.US", bar_interval="5m", ts=start + timedelta(minutes=5 * i),
            open=p, high=p + 1, low=p - 1, close=p, volume=100, source="t")
        for i in range(n)
    ]


def stub(direction: Direction, score: int = 5):
    def detect(ctx) -> SetupSignal | None:
        return SetupSignal(
            setup_name="stub", direction=direction, entry_index=ctx.index,
            entry_price=ctx.bar.close, stop_price=ctx.bar.close * Decimal("0.98"), score=score,
        )
    return detect


def test_signal_in_regular_hours_is_reported(monkeypatch):
    monkeypatch.setitem(scanner_module.SETUPS, "stub", stub(Direction.LONG))
    out = scan_latest_bar("X.US", bars(8), setups=["stub"], market_type="regular",
                          min_score=3, plan=BracketPlan())
    assert out.hit is not None and out.reason == "signal"
    assert out.hit.signal.setup_name == "stub"


def test_premarket_bar_is_not_traded_by_a_regular_hours_bot(monkeypatch):
    monkeypatch.setitem(scanner_module.SETUPS, "stub", stub(Direction.LONG))
    pre = bars(8, start=datetime(2026, 6, 17, 9, 0, tzinfo=UTC))  # 05:00 ET
    out = scan_latest_bar("X.US", pre, setups=["stub"], market_type="regular",
                          min_score=3, plan=BracketPlan())
    assert out.hit is None and "pre_market" in out.reason
    # ...but an `auto` bot may act on it.
    out_auto = scan_latest_bar("X.US", pre, setups=["stub"], market_type="auto",
                               min_score=3, plan=BracketPlan())
    assert out_auto.hit is not None


def test_short_signals_are_rejected_and_the_reason_says_so(monkeypatch):
    monkeypatch.setitem(scanner_module.SETUPS, "stub", stub(Direction.SHORT))
    out = scan_latest_bar("X.US", bars(8), setups=["stub"], market_type="regular",
                          min_score=3, plan=BracketPlan())
    assert out.hit is None and "direction_short" in out.reason


def test_score_below_minimum_is_rejected(monkeypatch):
    monkeypatch.setitem(scanner_module.SETUPS, "stub", stub(Direction.LONG, score=2))
    out = scan_latest_bar("X.US", bars(8), setups=["stub"], market_type="regular",
                          min_score=3, plan=BracketPlan())
    assert out.hit is None and "score_2<3" in out.reason


def test_too_few_bars_in_the_session_is_a_stated_reason(monkeypatch):
    monkeypatch.setitem(scanner_module.SETUPS, "stub", stub(Direction.LONG))
    out = scan_latest_bar("X.US", bars(3), setups=["stub"], market_type="regular",
                          min_score=3, plan=BracketPlan())
    assert out.hit is None and out.reason == "only_3_bars_in_phase"


# --- learning ----------------------------------------------------------------


def stats(name, trades, expectancy):
    return SetupStats(
        setup_name=name, trades=trades, wins=0, win_rate=None,
        expectancy_r=Decimal(expectancy) if expectancy is not None else None,
        total_r=Decimal(0), total_pnl=Decimal(0),
        demoted=is_demoted(trades, Decimal(expectancy) if expectancy is not None else None),
    )


def test_a_losing_setup_is_demoted_only_with_enough_of_its_own_evidence():
    assert not is_demoted(DEMOTE_MIN_TRADES - 1, Decimal("-1"))
    assert is_demoted(DEMOTE_MIN_TRADES, Decimal("-0.5"))
    assert not is_demoted(DEMOTE_MIN_TRADES, Decimal("-0.05"))  # near breakeven: inconclusive
    assert not is_demoted(DEMOTE_MIN_TRADES, None)


def test_auto_mode_drops_demoted_setups_and_explicit_modes_never_do():
    s = [stats("a", 30, "-0.4"), stats("b", 30, "0.2"), stats("c", 5, "-2")]
    assert active_setups(["a", "b", "c"], strategy_mode="auto", stats=s) == ["b", "c"]
    assert active_setups(["a", "b", "c"], strategy_mode="multi", stats=s) == ["a", "b", "c"]
    assert active_setups(["a"], strategy_mode="single", stats=s) == ["a"]


def test_auto_mode_with_everything_demoted_runs_nothing():
    s = [stats("a", 30, "-0.4")]
    assert active_setups(["a"], strategy_mode="auto", stats=s) == []
