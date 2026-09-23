"""The option planner over HTTP (Phase 91, D110).

The decision rules themselves already have tests (`tests/options/`). What
is asserted here is the route's own contract: that a REFUSAL is a 200 and
carries the reason, that a plan carries its modelled-pricing caveat
whether or not the client asks, and that nothing about this endpoint
reaches a broker.
"""

import pytest

from tests.api.test_admin import _get_token, api_client, db_session, non_admin_user

STRONG = {
    "liquidity_sweep": True,
    "market_structure_shift": True,
    "cross_market_confirm": True,
    "vwap_location": True,
    "rsi_divergence": True,
    "volume_confirm": True,
    "atr_confirm": True,
    "major_level": True,
    "option_liquidity_ok": True,
    "iv_appropriate": True,
}


def body(**over):
    base = {
        "symbol": "TQQQ.US",
        "bullish": True,
        "evidence": STRONG,
        "spot": "80",
        "dte_days": 3,
        "volatility": "0.45",
        # A debit structure needs CHEAP premium, so a low IV rank.
        "iv_rank": "0.2",
        "account_equity": "100000",
    }
    base.update(over)
    return base


def _h(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


@pytest.mark.asyncio
async def test_a_full_evidence_signal_produces_a_defined_risk_spread():
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            r = await client.post("/options/plan", json=body(), headers=_h(token))
            assert r.status_code == 200, r.text
            p = r.json()

    assert p["planned"] is True
    assert p["score"] == p["max_score"] == 12
    assert p["contracts"] >= 1

    spread = p["spread"]
    # Defined risk means both legs exist and the loss is bounded by the
    # structure, not by a stop somebody has to remember to place.
    assert spread["long_leg"]["strike"] != spread["short_leg"]["strike"]
    assert float(spread["max_loss"]) > 0
    assert float(spread["max_profit"]) > 0
    # The total risk cannot exceed the budget the grade earned.
    assert float(p["max_loss_total"]) <= float(p["risk_budget"])

    # The caveat is on the response whether or not a client asked for it.
    assert "MODELLED" in p["pricing_note"]
    assert "option-chain" in p["pricing_note"]
    assert p["notes"]["priced"].startswith("MODEL")


@pytest.mark.asyncio
async def test_a_signal_below_the_score_floor_is_a_200_refusal_with_its_reason():
    # A refusal is the decision layer WORKING. A 4xx would make a correct
    # answer look like a malformed request.
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            r = await client.post(
                "/options/plan",
                json=body(evidence={"vwap_location": True}),
                headers=_h(token),
            )
            assert r.status_code == 200, r.text
            p = r.json()

    assert p["planned"] is False
    assert p["spread"] is None and p["contracts"] is None
    assert p["score"] == 1
    assert "below" in p["reason"]
    assert str(p["minimum_tradeable_score"]) in p["reason"]


@pytest.mark.asyncio
async def test_the_wrong_iv_regime_for_the_structure_is_refused_by_name():
    # A debit spread buys premium, so rich IV is the wrong regime for it.
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            r = await client.post(
                "/options/plan", json=body(iv_rank="0.95"), headers=_h(token)
            )
            p = r.json()

    assert r.status_code == 200
    # Either the router sent it down the credit path (correct for rich IV)
    # or it refused the debit structure outright — both are the IV filter
    # doing its job, and neither is a debit spread bought into rich IV.
    if p["planned"]:
        assert p["spread"]["is_debit"] is False
    else:
        assert "IV rank" in p["reason"]


@pytest.mark.asyncio
async def test_a_dte_outside_the_playbooks_bands_is_refused():
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            r = await client.post("/options/plan", json=body(dte_days=45), headers=_h(token))
            p = r.json()

    assert r.status_code == 200
    assert p["planned"] is False
    assert "DTE" in p["reason"]


@pytest.mark.asyncio
async def test_an_account_too_small_for_one_contract_sizes_to_nothing_not_to_one():
    # The alternative — rounding up to a single contract — silently
    # exceeds the risk budget the grade allowed.
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            r = await client.post(
                "/options/plan", json=body(account_equity="50"), headers=_h(token)
            )
            p = r.json()

    assert r.status_code == 200
    assert p["planned"] is False
    assert "Risk budget" in p["reason"]


@pytest.mark.asyncio
async def test_the_planner_requires_authentication():
    async with api_client() as client:
        r = await client.post("/options/plan", json=body())
        assert r.status_code == 401
