"""Option-selection filter tests (Phase 75, D093)."""

from decimal import Decimal

import pytest

from apps.api.app.options.selection import (
    DteBand,
    check_liquidity,
    classify_dte,
    delta_in_band,
    iv_regime_appropriate,
)


@pytest.mark.parametrize(
    ("days", "band"),
    [(0, DteBand.ZERO_DTE), (1, DteBand.TACTICAL), (3, DteBand.TACTICAL),
     (7, DteBand.REGULAR), (14, DteBand.REGULAR), (30, DteBand.TOO_LONG)],
)
def test_dte_classification(days, band):
    assert classify_dte(days) is band


def test_single_leg_liquidity_cap_is_ten_percent():
    ok = check_liquidity(
        bid=Decimal("1.00"),
        ask=Decimal("1.05"),
        open_interest=500,
        is_multi_leg=False,
    )
    assert ok.passed and ok.spread_pct is not None
    wide = check_liquidity(
        bid=Decimal("1.00"),
        ask=Decimal("1.20"),
        open_interest=500,
        is_multi_leg=False,
    )
    assert not wide.passed and "exceeds" in wide.reason


def test_multi_leg_cap_is_tighter_than_single_leg():
    # An 8% spread passes a single leg but fails the 5% multi-leg cap.
    bid, ask = Decimal("1.00"), Decimal("1.083")
    assert check_liquidity(bid=bid, ask=ask, open_interest=500, is_multi_leg=False).passed
    assert not check_liquidity(bid=bid, ask=ask, open_interest=500, is_multi_leg=True).passed


def test_crossed_or_stale_quote_is_rejected_without_a_percentage():
    crossed = check_liquidity(
        bid=Decimal("1.20"),
        ask=Decimal("1.00"),
        open_interest=500,
        is_multi_leg=False,
    )
    assert not crossed.passed
    assert crossed.spread_pct is None  # never computed from a broken market
    zero = check_liquidity(
        bid=Decimal("0"),
        ask=Decimal("0"),
        open_interest=500,
        is_multi_leg=False,
    )
    assert not zero.passed


def test_thin_open_interest_is_rejected():
    thin = check_liquidity(
        bid=Decimal("1.00"),
        ask=Decimal("1.02"),
        open_interest=10,
        is_multi_leg=False,
    )
    assert not thin.passed and "open interest" in thin.reason


def test_delta_band_uses_magnitude():
    assert delta_in_band(-0.30, 0.20, 0.35)  # a 0.30-delta put
    assert delta_in_band(0.60, 0.55, 0.70)
    assert not delta_in_band(0.45, 0.55, 0.70)


def test_iv_regime_debit_prefers_low_credit_prefers_high():
    # Debit trades want cheap premium (low IV); credit trades want rich IV.
    assert iv_regime_appropriate(iv_rank=0.20, is_debit=True)
    assert not iv_regime_appropriate(iv_rank=0.80, is_debit=True)
    assert iv_regime_appropriate(iv_rank=0.80, is_debit=False)
    assert not iv_regime_appropriate(iv_rank=0.20, is_debit=False)


def test_iv_rank_out_of_range_is_refused():
    with pytest.raises(ValueError, match="between 0 and 1"):
        iv_regime_appropriate(iv_rank=1.5, is_debit=True)
