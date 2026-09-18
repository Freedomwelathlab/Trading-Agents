# ASTA Strategies — from course materials to the platform

Task-6 deliverable: the Avadhut Sathe Trading Academy course materials
("Notes and Materials 00–04") read, named, summarized, and mapped to the
Trading OS engine. The full concept knowledge base is the `asta-trading`
book-to-skill skill; this document is the engineering mapping — what is
mechanized, following the material's own rules, and what is not.

## The four modules

| Module | Name | Subject | Mechanizable core |
|---|---|---|---|
| **SMM** | Smart Money | Dow theory, demand/supply, relative strength, candlesticks | candlestick patterns ✅ |
| **PAPA** | Price Action Pattern Analysis | candle anatomy, combination, S/R, HA/BB/ADX | candle patterns ✅; BB/ADX scoped |
| **GUE** | Get the Ultimate Edge | Elliott Wave: motive/corrective + 3 rules + setups | wave-rule validators + setup detectors (scoped) |
| **FOME** | Futures & Options | derivatives, options mechanics, Greeks | options layer ✅ (Phase 75) |

## What is built (this phase, D094)

**Candlestick pattern detection — `apps/api/app/marketdata/candles.py`.**
Implements the SMM/PAPA patterns with the material's exact rules:

- Hammer / Shooting Star (wick ≥ 2× body), Doji (body ≤ 10% range).
- Bullish/Bearish Piercing (close beyond the prior body's **median**) vs
  Engulf (close beyond the prior **open**) — kept mutually exclusive, as
  the material treats engulf as the stronger, separate pattern.
- Morning/Evening Star, Three White Soldiers / Three Black Crows, Tweezers.
- **The trend-context rule is enforced**: a reversal shape is a signal only
  at the end of its trend (`requires_trend` + `is_signal_in_context`), so a
  hammer mid-range is not treated as a buy. Detection (shape) and
  confirmation (context) are separate functions.
- `detect_at(bars, i)` reports only patterns that COMPLETE on bar `i`, using
  backward windows — safe for causal replay. 12 tests, ruff/mypy clean.

**Options mechanics — `apps/api/app/options/` (Phase 75, D093).** FOME's
option concepts (premium = intrinsic + time value, ITM/ATM/OTM, delta,
IV, defined-risk structures) are the pricing/structures/selection layer.

## What is specified but not yet wired (scoped next builds)

1. ~~**Candle patterns → intraday signals.**~~ **DONE (Phase 77, D095)** —
   `candle_reversal` is in the `SETUPS` registry, gated on Dow trend context
   AND proximity to a marked level. Measured on 124 real sessions it is
   **significantly negative** (274 trades, t = −3.45); see D095.
2. **Elliott wave rule validators.** The three hard rules (W2 ≤ 100% W1;
   W4 ∉ W1 territory; W3 not shortest) are pure predicates over a labeled
   5-wave count — cheap to implement and test. What is genuinely hard and
   partly discretionary is **producing** the wave count automatically;
   `structure.py`'s swings are the input, but auto-counting is a research
   problem, not a checklist. Honest position: encode the validators and a
   semi-automatic labeler; do not claim full auto-counting.
3. **Elliott setup detectors** (triangle breakout, ending diagonal, 3rd
   wave) — buildable on labeled swings from `structure.py`, per the
   `elliott-setups` checklist rules (entry/stop/target documented there).
4. **PAPA indicators** — Bollinger Bands and ADX/DMI are not in the
   indicator set yet; both are standard and mechanizable.

## Honesty notes

- Two checklist PDFs and four `.docx` in the source set were image-only
  (no extractable text); their prose rules were reconstructed from the
  concept decks, and the wave-label diagrams (A–E, i–v, 1–5) informed the
  setup structures.
- Elliott Wave counting and most price-action reading are discretionary
  skills. This work encodes the **rule-based subset**; it is not a claim
  that the discretionary judgment is automated, nor that any of it is
  profitable. Every setup must be validated on data before use — the same
  discipline the material itself repeats.
