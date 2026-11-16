"""The Strategy Library (Phase 108, D135): the engine cannot look ahead,
costs are charged, the vendor's bad ticks are dropped and its unexplained
jumps are not trusted, and the verdict is a fixed rule over the numbers."""

import math
from datetime import date, timedelta

import pytest

from apps.api.app.library.engine import PriceTable, run_strategy
from apps.api.app.library.metrics import compute_metrics, max_drawdown, yearly_returns
from apps.api.app.library.quality import DataQualityError, screen_history
from apps.api.app.library.service import LibraryError, build_report, judge
from apps.api.app.library.strategies import (
    BASELINE,
    PRESETS,
    STRATEGIES,
    default_cost_bps,
    get_preset,
    get_strategy,
    market_of,
)
from apps.api.app.marketdata.providers.yahoo import DailyHistory


def weekdays(n: int, start: date = date(2010, 1, 4)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def hist(symbol: str, closes: list[float], start: date = date(2010, 1, 4)) -> DailyHistory:
    return DailyHistory(
        symbol=symbol,
        vendor_symbol=symbol,
        currency="USD",
        dates=tuple(weekdays(len(closes), start)),
        closes=tuple(closes),
        adj_closes=tuple(closes),
    )


def trend(n: int, daily: float, start: float = 100.0) -> list[float]:
    return [start * (1 + daily) ** k for k in range(n)]


def noisy(n: int, drift: float, amp: float = 0.01, start: float = 100.0) -> list[float]:
    """A drifting series with real up and down days, deterministic."""
    out = [start]
    for k in range(n - 1):
        out.append(out[-1] * (1 + drift + (amp if k % 2 else -amp)))
    return out


def no_cost(_: str) -> float:
    return 0.0


def always(symbol: str):
    def fn(table, i, params, state):
        return {symbol: 1.0}

    return fn


# ---------------------------------------------------------------- engine


def test_a_decision_first_earns_two_sessions_later_with_the_default_lag() -> None:
    table = PriceTable([hist("A.US", [100, 110, 121, 133.1, 146.41])])
    run = run_strategy(table, always("A.US"), {}, eval_index=0, lag=1, cost_bps=no_cost)
    # Decided on close 0, bought on close 1, so the first 10% move is missed.
    assert run.curve == pytest.approx([1.0, 1.0, 1.1, 1.21, 1.331])


def test_lag_zero_trades_on_the_signal_close() -> None:
    table = PriceTable([hist("A.US", [100, 110, 121])])
    run = run_strategy(table, always("A.US"), {}, eval_index=0, lag=0, cost_bps=no_cost)
    assert run.curve == pytest.approx([1.0, 1.1, 1.21])


def test_costs_are_charged_on_the_weight_traded() -> None:
    table = PriceTable([hist("A.US", [100, 100, 100, 100])])
    run = run_strategy(table, always("A.US"), {}, eval_index=0, lag=0, cost_bps=lambda s: 50.0)
    assert run.trades == 1
    assert run.curve[-1] == pytest.approx(1 - 0.005)
    assert run.cost_paid == pytest.approx(0.005)


def test_a_symbol_that_has_not_listed_cannot_be_held() -> None:
    a = hist("A.US", [100.0] * 6)
    b = hist("B.US", [50.0, 100.0], start=date(2010, 1, 8))  # lists on the 5th session
    table = PriceTable([a, b])
    assert table.price("B.US", 0) is None
    run = run_strategy(table, always("B.US"), {}, eval_index=0, lag=0, cost_bps=no_cost)
    assert run.curve[:5] == pytest.approx([1.0, 1.0, 1.0, 1.0, 1.0])
    assert run.curve[-1] == pytest.approx(2.0)


def test_a_closed_market_is_carried_at_its_last_close() -> None:
    a = hist("A.US", [100, 101, 102])
    b = DailyHistory(
        "B.SG", "B.SI", "SGD", (date(2010, 1, 4), date(2010, 1, 6)), (10.0, 11.0), (10.0, 11.0)
    )
    table = PriceTable([a, b])
    assert table.px["B.SG"] == [10.0, 10.0, 11.0]
    assert table.bars("B.SG", 1) == 1


def test_returns_before_the_evaluation_start_are_not_counted() -> None:
    table = PriceTable([hist("A.US", trend(10, 0.01))])
    run = run_strategy(table, always("A.US"), {}, eval_index=5, lag=0, cost_bps=lambda s: 100.0)
    assert len(run.curve) == 5
    assert run.curve[0] == 1.0
    assert run.trades == 0  # the position was opened in the warm-up
    assert run.curve[-1] == pytest.approx(1.01**4)


def test_moving_average_and_channels_match_hand_values() -> None:
    table = PriceTable([hist("A.US", [1, 2, 3, 4, 5, 4, 3])])
    assert table.sma("A.US", 4, 5) == pytest.approx(3.0)
    assert table.sma("A.US", 3, 5) is None
    assert table.rsi("A.US", 2, 2) == 100.0  # two rises, no fall
    assert table.rsi("A.US", 1, 2) is None
    assert table.highest("A.US", 5, 3) == 5 and table.lowest("A.US", 5, 3) == 3
    assert table.highest("A.US", 2, 3) is None  # only two sessions precede it


# ---------------------------------------------------------------- metrics


def test_metrics_for_a_steady_riser() -> None:
    days = weekdays(505)
    curve = trend(505, 0.0004, 1.0)
    m = compute_metrics(days, curve)
    assert m is not None
    assert m.cagr == pytest.approx(curve[-1] ** (365.25 / (days[-1] - days[0]).days) - 1)
    assert m.max_drawdown == 0.0
    assert m.sortino is None  # no losing day: undefined, not infinite or zero
    assert m.calmar is None


def test_a_short_curve_reports_no_annualised_figures() -> None:
    m = compute_metrics(weekdays(20), trend(20, 0.01, 1.0))
    assert m is not None and m.cagr is None and m.sharpe is None
    assert m.total_return == pytest.approx(1.01**19 - 1)


def test_drawdown_and_calendar_years() -> None:
    assert max_drawdown([1, 2, 1, 3]) == pytest.approx(-0.5)
    days = [date(2020, 12, 30), date(2020, 12, 31), date(2021, 1, 4), date(2021, 12, 31)]
    assert yearly_returns(days, [1.0, 1.1, 1.21, 1.1]) == [
        (2020, pytest.approx(0.1)),
        (2021, pytest.approx(0.0)),
    ]


# ---------------------------------------------------------------- quality


def test_a_spike_that_reverts_is_dropped_and_reported() -> None:
    closes = [100.0] * 30
    closes[10] = 10.0
    closes[11] = 10.0
    h = screen_history(hist("NIFTYBEES.IN", closes))
    assert len(h) == 28
    assert len(h.removed) == 2 and h.truncated_before is None
    assert min(h.adj_closes) == 100.0


def test_a_jump_that_stays_discards_everything_before_it() -> None:
    closes = [100.0] * 80 + [300.0] * 100
    h = screen_history(hist("TCS.IN", closes))
    assert len(h) == 100
    assert h.truncated_before == h.dates[0]
    assert set(h.adj_closes) == {300.0}


def test_too_little_left_after_a_jump_is_refused() -> None:
    with pytest.raises(DataQualityError) as err:
        screen_history(hist("X.IN", [100.0] * 80 + [300.0] * 10))
    assert "refused" in str(err.value)


def test_a_real_crypto_crash_is_not_a_bad_tick() -> None:
    closes = [100.0] * 20 + [55.0] * 20  # -45%: inside the crypto limit
    h = screen_history(hist("BTC-USD", closes))
    assert len(h) == 40 and not h.removed and h.truncated_before is None


def test_clean_history_is_returned_unchanged() -> None:
    h = hist("SPY.US", trend(50, 0.001))
    assert screen_history(h) is h


# ---------------------------------------------------------------- strategies


def test_every_strategy_declares_evidence_and_a_publication_date() -> None:
    assert len({s.id for s in STRATEGIES}) == len(STRATEGIES)
    for s in STRATEGIES:
        assert s.evidence, s.id
        assert s.rules and s.caveats, s.id
        assert 1900 < s.published.year <= 2026
        assert s.resolve_params(None) == s.resolve_params({})
    for p in PRESETS:
        for sid in p.strategies:
            assert len(p.symbols) >= get_strategy(sid).min_symbols


def test_parameters_are_range_checked_and_unknown_keys_refused() -> None:
    s = get_strategy("trend_200d")
    assert s.resolve_params({"sma_days": 100}) == {"sma_days": 100}
    with pytest.raises(ValueError, match="between"):
        s.resolve_params({"sma_days": 5})
    with pytest.raises(ValueError, match="Unknown"):
        s.resolve_params({"nope": 1})


def test_trend_filter_is_out_of_a_falling_market_and_in_a_rising_one() -> None:
    closes = trend(300, -0.002) + trend(400, 0.003, 100 * (1 - 0.002) ** 299)
    h = hist("A.US", closes)
    report = build_report(get_strategy("trend_200d"), [h], [h], {"sma_days": 200})
    whole = report.segments[0]
    assert whole.strategy is not None and whole.benchmark is not None
    assert report.target == {"A.US": 1.0}
    assert "above" in report.explain["A.US"]
    assert report.trades >= 1
    assert abs(whole.strategy.max_drawdown) < abs(whole.benchmark.max_drawdown)


def test_dual_momentum_shelters_when_nothing_is_rising() -> None:
    risk = hist("RISK.US", trend(600, -0.001))
    shelter = hist("BOND.US", trend(600, 0.0002))
    report = build_report(
        get_strategy("dual_momentum"), [risk, shelter], [risk], {"lookback_days": 252}
    )
    assert report.target == {"BOND.US": 1.0}


def test_rotation_holds_the_strongest_names_only() -> None:
    hs = [
        hist("A.US", trend(600, 0.002)),
        hist("B.US", trend(600, 0.001)),
        hist("C.US", trend(600, -0.001)),
    ]
    s = get_strategy("momentum_rotation")
    report = build_report(s, hs, hs, s.resolve_params({"top_n": 2}))
    assert report.target == {"A.US": 0.5, "B.US": 0.5}


def test_rotation_leaves_a_slot_in_cash_rather_than_buy_a_loser() -> None:
    hs = [
        hist("A.US", trend(600, 0.002)),
        hist("B.US", trend(600, -0.001)),
        hist("C.US", trend(600, -0.002)),
    ]
    s = get_strategy("momentum_rotation")
    report = build_report(s, hs, hs, s.resolve_params({"top_n": 2}))
    assert report.target == {"A.US": 0.5}


def test_rsi2_buys_a_dip_in_an_uptrend_and_sells_the_bounce() -> None:
    closes = trend(260, 0.002)
    last = closes[-1]
    closes += [last * 0.97, last * 0.94, last * 0.99, last * 1.02, last * 1.03]
    closes += [last * 1.03] * 260
    s = get_strategy("rsi2_reversion")
    h = hist("A.US", closes)
    report = build_report(s, [h], [h], s.resolve_params(None))
    assert report.trades == 2  # one entry, one exit
    assert report.target == {}


def test_donchian_enters_on_a_new_high_and_exits_on_a_new_low() -> None:
    closes = [100.0] * 60 + trend(60, 0.01, 101.0) + trend(260, -0.01, 180.0)
    s = get_strategy("donchian_breakout")
    report = build_report(s, [hist("A.US", closes)], [], s.resolve_params(None))
    assert report.trades == 2 and report.target == {}


def test_costs_follow_the_market() -> None:
    assert market_of("SPY.US") == "US" and market_of("D05.SG") == "SG"
    assert market_of("RELIANCE.IN") == "IN" and market_of("BTC-USD") == "CRYPTO"
    assert market_of("EURUSD.FX") == "FX"
    assert default_cost_bps("BTC-USD") > default_cost_bps("D05.SG") > default_cost_bps("SPY.US")


# ---------------------------------------------------------------- report


def test_too_little_history_is_refused_not_estimated() -> None:
    with pytest.raises(LibraryError) as err:
        build_report(
            get_strategy("trend_200d"), [hist("A.US", trend(300, 0.001))], [], {"sma_days": 200}
        )
    assert err.value.code == "INSUFFICIENT_DATA"


def test_publication_splits_the_report_into_before_and_after() -> None:
    h = hist("A.US", trend(3000, 0.0003), start=date(2002, 1, 1))
    report = build_report(get_strategy("trend_200d"), [h], [h], {"sma_days": 200})
    labels = [s.label for s in report.segments]
    assert labels[0] == "Whole period"
    assert labels[1].startswith("Before publication")
    assert labels[2].startswith("After publication")
    after = report.segments[2].strategy
    assert after is not None and after.start >= date(2007, 2, 1)


def test_a_survivor_list_can_never_be_called_a_win() -> None:
    hs = [hist(f"{c}.SG", noisy(900, r)) for c, r in (("A", 0.003), ("B", 0.001), ("C", 0.0005))]
    bench = [hist("IDX.SG", noisy(900, 0.0005))]
    s = get_strategy("momentum_rotation")
    report = build_report(
        s, hs, bench, s.resolve_params({"top_n": 1}), preset=get_preset("sg_bluechips")
    )
    assert report.verdict.code == "survivorship_biased"
    assert any("Survivorship" in w for w in report.warnings)


def test_the_verdict_is_a_fixed_rule() -> None:
    days = weekdays(600)
    good = compute_metrics(days, trend(600, 0.0008, 1.0))
    wobble = [1.0]
    for k in range(599):
        wobble.append(wobble[-1] * (1.0 + (0.012 if k % 2 else -0.0112)))
    meh = compute_metrics(days, wobble)
    assert meh is not None and meh.sharpe is not None and math.isfinite(meh.sharpe)
    assert judge(None, good, label="x").code == "insufficient_history"
    assert judge(meh, None, label="x").code == "no_edge"


def test_the_baseline_holds_everything_equally() -> None:
    hs = [hist("A.US", [100.0] * 30), hist("B.US", [50.0] * 30)]
    run = run_strategy(PriceTable(hs), BASELINE.fn, {}, eval_index=0, lag=0, cost_bps=no_cost)
    assert run.target == {"A.US": 0.5, "B.US": 0.5}
