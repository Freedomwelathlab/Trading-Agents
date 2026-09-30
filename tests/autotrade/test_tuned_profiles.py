"""The Autotrade bot's `tuned` strategy mode and its optimiser profiles
(Phase 105, D126)."""

import json
from decimal import Decimal

import pytest

from apps.api.app.autotrade import profiles
from apps.api.app.marketdata.structure import Direction


def _write(tmp_path, monkeypatch, setups):
    monkeypatch.setattr(profiles, "PROFILE_DIR", tmp_path)
    profiles.load_profiles.cache_clear()
    doc = {"symbol": "TQQQ.US", "bar_interval": "5m", "setups": setups}
    (tmp_path / "TQQQ_US_5m.json").write_text(json.dumps(doc), encoding="utf-8")


def _entry(*, promising, recommended=False, exp=0.1, directions="both", score=3, buf="0.40"):
    return {
        "params": {
            "atr_stop_buffer": buf,
            "tp1_r_multiple": "1.5",
            "tp2_r_multiple": "3",
            "trail_atr_multiple": "1.5",
            "breakeven_after_tp1": True,
            "min_score": score,
            "directions": directions,
        },
        "out_of_sample": {"trades": 40, "expectancy_r": exp, "t_stat": 0.8},
        "promising": promising,
        "recommended": recommended,
    }


def test_only_promising_or_recommended_setups_are_deployable(tmp_path, monkeypatch):
    _write(
        tmp_path,
        monkeypatch,
        {
            "sweep_mss": _entry(promising=True, exp=0.118),
            "gap_fade": _entry(promising=True, exp=0.033),
            "candle_reversal": _entry(promising=False, exp=-0.22),
        },
    )
    got = profiles.deployable_profiles("TQQQ.US", "5m")
    assert [p.setup for p in got] == ["sweep_mss", "gap_fade"]  # best OOS first
    assert got[0].atr_stop_buffer == Decimal("0.40")
    assert got[0].directions == (Direction.LONG, Direction.SHORT)


def test_no_published_profile_means_nothing_deployable(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILE_DIR", tmp_path)
    profiles.load_profiles.cache_clear()
    assert profiles.deployable_profiles("QQQ.US", "5m") == []


def test_the_scan_uses_each_setups_own_parameters(tmp_path, monkeypatch):
    """Each deployable setup is scanned with its own min score (never below
    the bot's), its own directions (never wider than the bot's) and its own
    stop buffer; a demoted setup is skipped."""
    from types import SimpleNamespace

    from apps.api.app.autotrade import engine
    from apps.api.app.autotrade.scanner import ScanOutcome
    from apps.api.app.backtesting.brackets import BracketPlan

    _write(
        tmp_path,
        monkeypatch,
        {
            "sweep_mss": _entry(promising=True, exp=0.2, directions="short", score=4, buf="0.20"),
            "gap_fade": _entry(promising=True, exp=0.1, directions="both", score=1, buf="0.40"),
            "orb_failure": _entry(promising=True, exp=0.05),
        },
    )
    calls = []

    def fake_scan(symbol, bars, *, setups, market_type, min_score, plan, allow_directions):
        calls.append((setups[0], min_score, plan.atr_stop_buffer, allow_directions))
        return ScanOutcome(None, "no_signal")

    monkeypatch.setattr(engine, "scan_latest_bar", fake_scan)
    bot = SimpleNamespace(bar_interval="5m", market_type="regular")
    outcome = engine._scan_optimised(
        "TQQQ.US",
        [],
        bot=bot,
        allowed=["sweep_mss", "gap_fade"],  # orb_failure demoted by the learning loop
        min_score=2,
        plan=BracketPlan(),
        bot_directions=(Direction.LONG,),  # bot does not allow shorts
    )
    # sweep_mss is short-only and the bot is long-only -> not scanned at all.
    assert calls == [("gap_fade", 2, Decimal("0.40"), (Direction.LONG,))]
    assert outcome.hit is None
    assert "sweep_mss:direction_not_allowed" in outcome.reason
    assert "orb_failure:demoted" in outcome.reason


def test_tuned_is_a_valid_strategy_mode_and_fits_the_column():
    from apps.api.app.autotrade.service import STRATEGY_MODES

    assert "tuned" in STRATEGY_MODES
    assert all(len(m) <= 8 for m in STRATEGY_MODES)  # String(8) column


@pytest.mark.parametrize("mode", ["auto", "tuned"])
def test_the_learning_loop_still_demotes_in_tuned_mode(mode):
    from apps.api.app.autotrade.learning import SetupStats, active_setups

    stats = [
        SetupStats(
            setup_name="gap_fade",
            trades=30,
            wins=5,
            win_rate=Decimal("0.17"),
            expectancy_r=Decimal("-0.8"),
            total_r=Decimal("-24"),
            total_pnl=Decimal("-2400"),
            demoted=True,
        )
    ]
    assert active_setups(["gap_fade", "sweep_mss"], strategy_mode=mode, stats=stats) == [
        "sweep_mss"
    ]
