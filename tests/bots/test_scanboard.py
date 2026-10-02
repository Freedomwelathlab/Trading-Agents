"""The scan board behind the bot dashboards (Phase 107, D133).

Stub setups replace the registry so each verdict is deterministic: a board
must recommend only a setup this bot would actually trade, and say why it
waits otherwise.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apps.api.app.autotrade.engine import position_size
from apps.api.app.backtesting import setups as setups_mod
from apps.api.app.backtesting.brackets import BracketPlan
from apps.api.app.backtesting.setups import SetupSignal
from apps.api.app.backtesting.signal_calibration import Bucket
from apps.api.app.bots import scanboard
from apps.api.app.bots.scanboard import asset_class_of, build_board
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.structure import Direction

START = datetime(2026, 9, 26, 0, 5, tzinfo=UTC)  # a Saturday: crypto only


def _bars(n=40):
    out = []
    for k in range(n):
        px = Decimal("100") + Decimal(k % 5)
        out.append(Bar(symbol="BTC-USD", bar_interval="5m", ts=START + timedelta(minutes=5 * k),
                       open=px, high=px + 2, low=px - 2, close=px, volume=10, source="t"))
    return out


def _stub(direction, score, stop_offset=Decimal("2")):
    def detect(ctx, atr_stop_buffer=Decimal("0.30")):
        last = ctx.bars[ctx.index]
        stop = last.close - stop_offset if direction is Direction.LONG else last.close + stop_offset
        return SetupSignal(setup_name="", direction=direction, entry_index=ctx.index,
                           entry_price=last.close, stop_price=stop, score=score)
    return detect


@pytest.fixture
def registry(monkeypatch):
    reg = {
        "alpha": _stub(Direction.LONG, 7),
        "beta": _stub(Direction.SHORT, 8),
        "gamma": _stub(Direction.LONG, 3),
        "quiet": lambda ctx, atr_stop_buffer=Decimal("0.3"): None,
    }
    monkeypatch.setattr(setups_mod, "SETUPS", reg)
    monkeypatch.setattr(scanboard, "SETUPS", reg)
    monkeypatch.setattr(scanboard, "stop_quality_ok", lambda **k: True)
    return reg


def _board(**over):
    args = dict(enabled_setups=["alpha", "beta", "gamma"], market_type="24h",
                plan=BracketPlan(), min_score=5, allow_directions=(Direction.LONG,))
    args.update(over)
    return build_board("BTC-USD", _bars(), **args)


def test_the_best_qualifying_setup_is_the_recommendation(registry):
    b = _board()
    # beta scores higher but is short and this bot is long-only.
    assert b.recommendation == "BUY" and b.best.setup == "alpha"
    verdicts = {r.setup: r.verdict for r in b.setups}
    assert verdicts == {"alpha": "qualifies", "beta": "direction_not_allowed",
                        "gamma": "below_min_score", "quiet": "no_signal"}
    alpha = next(r for r in b.setups if r.setup == "alpha")
    assert alpha.target_1r - alpha.entry == alpha.entry - alpha.stop  # 1R target


def test_shorts_allowed_picks_the_higher_score(registry):
    b = _board(allow_directions=(Direction.LONG, Direction.SHORT))
    assert b.recommendation == "SELL" and b.best.setup == "beta"


def test_wait_names_why(registry):
    b = _board(min_score=9)
    assert b.recommendation == "WAIT"
    assert "beta" in b.headline or "alpha" in b.headline
    assert "minimum 9" in b.headline or "does not trade" in b.headline


def test_a_disabled_setup_is_scored_but_never_recommended(registry):
    b = _board(enabled_setups=["gamma"], min_score=0)
    assert b.recommendation == "BUY" and b.best.setup == "gamma"
    assert {r.setup: r.verdict for r in b.setups}["alpha"] == "not_enabled"


def test_measured_confidence_is_attached_only_from_a_real_bucket(registry):
    cal = {("alpha", "long", 7): Bucket(resolved=40, wins=26)}
    b = _board(calibration=cal)
    alpha = next(r for r in b.setups if r.setup == "alpha")
    assert alpha.confidence_pct == Decimal("65.0") and alpha.sample == 40
    gamma = next(r for r in b.setups if r.setup == "gamma")
    assert gamma.confidence_pct is None  # no bucket -> no number, never a guess


def test_no_bars_waits_with_a_plain_reason(registry):
    b = build_board("BTC-USD", [], enabled_setups=["alpha"], market_type="24h",
                    plan=BracketPlan(), min_score=0, allow_directions=(Direction.LONG,))
    assert b.recommendation == "WAIT" and "No bars" in b.headline


def test_asset_classes_and_sizing():
    assert asset_class_of("BTC-USD") == "crypto"
    assert asset_class_of("EURUSD.FX") == "forex"
    assert asset_class_of("TQQQ.US") == "equity"
    # Crypto buys fractional coins; everything else whole units.
    assert position_size("BTC-USD", Decimal("1000"), Decimal("80000")) == Decimal("0.01250000")
    assert position_size("TQQQ.US", Decimal("1000"), Decimal("80")) == Decimal("12")
    assert position_size("EURUSD.FX", Decimal("1000"), Decimal("1.1355")) == Decimal("880")
