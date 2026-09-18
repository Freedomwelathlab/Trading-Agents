# Option strategies — US market (TQQQ and any liquid US underlying)

Task-5 deliverable: the `TQQQ_Longbridge_Auto_Options_Strategies` playbook
mapped onto the platform. Nothing here is TQQQ-specific — the underlying is
a parameter (`spot`, `volatility`, `strike_increment`), so the same layer
applies to any US underlying with a liquid chain.

## The two layers

| Layer | Module | What it does |
|---|---|---|
| Foundation (Phase 75, D093) | `options/pricing.py` | Black-Scholes price + Greeks; `implied_delta_strike` inverts delta→strike |
| | `options/structures.py` | `build_vertical` — the four verticals, with the defined-risk guarantee enforced, `contracts_for_risk` |
| | `options/selection.py` | DTE bands, liquidity caps (10% single / 5% multi), delta bands, IV filter |
| **Decision (Phase 78, D096)** | **`options/strategies.py`** | **§5 score → §16 floor → §10A router → strike selection → §3 sizing** |

## The decision path, in order

1. **Score the evidence (§5, 0–12).** Liquidity sweep +2, market-structure
   shift +2, cross-market (QQQ/NDX) confirmation +1, VWAP location +1, RSI
   divergence +1, volume +1, ATR/volatility +1, major HTF level +1, option
   liquidity +1, IV condition +1. Grades: 10–12 A+, 8–9 candidate, 6–7
   watchlist, <6 no trade.
2. **Hard floor (§16).** Below 8/12 → `TradeRefusal`. Watchlist is not
   tradeable.
3. **Route the regime (§10A).** Directional edge → **reversal path** (debit
   vertical). Range + IV rank ≥ 0.50 → **premium path** (credit vertical).
   Otherwise refuse. Directional wins ties.
4. **Pick the structure.** Bullish reversal → bull call debit; bearish
   reversal → bear put debit; bullish premium → bull put credit; bearish
   premium → bear call credit. The §2 IV filter is re-checked against the
   structure actually chosen, not the one the caller expected.
5. **Pick strikes by delta**, then snap to the chain grid. Debit: long 0.60 /
   short 0.30. Credit: short 0.30 / protective long 0.15.
6. **Size (§3).** A+ 0.50%, normal 0.35%, 0DTE ≤ 0.20% of equity, divided by
   the structure's *exact* max loss. Under one contract → refusal.

Every refusal carries a reason, a score and a grade. There is no silent
`None`.

```python
plan = plan_option_trade(
    bullish=True, evidence=SignalEvidence(liquidity_sweep=True, ...),
    spot=64.20, dte_days=7, volatility=0.52, iv_rank=0.31,
    account_equity=Decimal("100000"),
)
# -> OptionTradePlan(path=REVERSAL, spread=BULL_CALL 64/67, contracts=3, ...)
#    or TradeRefusal("Signal score 7/12 is below the 8 minimum (§16) ...")
```

## What is deliberately NOT built

- **Iron condor, iron fly, calendar, double calendar** (playbook P3–P6).
  These are multi-leg structures whose execution depends on Longbridge
  multi-leg order capability that has **not been verified**. Building an
  order path that cannot be submitted would be fabrication.
- **Live option pricing.** There is no historical option-chain data in this
  platform, so every leg here is priced by **model**. Each plan carries a
  `priced: MODEL` note; live use must re-price against real quotes first.
- **Duplicate-trade protection (§11), the macro/event filter (§12) and the
  daily-loss / consecutive-loss breakers (§3)** are portfolio-state rules,
  not per-signal rules. They belong with the persisted engine
  (`docs/AUDIT.md` gap B) and are not in this module.

## The honest caveat

This layer converts a signal into a defined-risk structure. **It does not
create edge.** The underlying TQQQ intraday signals it would consume
measured significantly negative on 124 sessions of real 5-minute data
(composite t = −3.32; best single setup t = +0.61, not significant) — see
D095 and `docs/DECISIONS.md`. A well-shaped option structure on a negative
signal is still a negative expectancy trade. Validate the underlying signal
before trading any of this, including on paper.
