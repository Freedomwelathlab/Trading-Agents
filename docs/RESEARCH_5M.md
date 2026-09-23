# Reading 5-minute candles: what the data actually says

Phase 85. Every number here comes from this platform's own stored bars —
**24,576 five-minute bars per symbol, 128 sessions, 2026-03-19 to
2026-09-21**, TQQQ.US and QQQ.US — through
`scripts/research_5m_structure.py` and `scripts/backtest_5m_setups.py`.
Nothing is simulated, assumed, or carried over from a book.

The brief asked six questions and then for "a best strategy to catch the
bottoms and tops". The six questions have answers. The strategy does not
exist in this data, and the rest of this document is the evidence.

---

## 0. The measurement error that had to be fixed first

The first run of the structure study reported a large, beautiful edge:
after a swing high, price closed **−0.81 ATR** an hour later and reversed
**66%** of the time; after a swing low, **+0.89 ATR** and 68%.

It was an artefact. A fractal swing is defined by the three bars on each
side of it, so a pivot is not *knowable* until three bars after it prints —
which is exactly what `SwingPoint.confirmed_ts` records. Measuring the
"future" from the pivot bar scores those three defining bars as if they
were unknown. Re-measuring from the confirmation bar:

| after a… | n | mean up | mean down | mean close | reversed |
|---|---:|---:|---:|---:|---:|
| swing high | 566 | +1.49 | −1.69 | **−0.02** | 48% |
| swing low | 564 | +1.47 | −1.70 | **−0.11** | 49% |

The edge was the look-ahead. This is worth stating plainly because the
same error, left in, would have produced three confident strategies and a
backtest that agreed with them.

## 1. Trend (Dow: higher highs **and** higher lows)

Per session, from confirmed swings, TQQQ over 128 sessions:
**34% up · 35% down · 30% sideways · 1% undetermined**. QQQ is the same
within a point. A third of sessions have no Dow trend at all, which is
why every reversal detector in this platform treats "sideways" as a
refusal rather than a coin flip.

## 2–3. Tops and bottoms of rallies, and whether volume confirms

Measured from the confirmation bar, over the next hour (ATR units):

| event | n | mean close | reversed |
|---|---:|---:|---:|
| high, high volume | 302 | +0.09 | 45% |
| high, low volume | 264 | −0.15 | 52% |
| low, high volume | 321 | −0.08 | 50% |
| low, low volume | 243 | −0.14 | 48% |

Volume splits nothing apart. Every cell is a coin flip; the largest
difference between any two is 0.24 ATR, on samples where the standard
deviation of the outcome is several ATR.

## 4. Pullbacks against volume

The one asymmetry with a consistent sign on **both** symbols:

| | n (TQQQ) | mean | continued |
|---|---:|---:|---:|
| pullback on falling volume | 1,479 | +0.06 | 51% |
| pullback on rising volume | 1,422 | −0.09 | 47% |

QQQ: +0.08 / 51% against −0.08 / 49%. Small, but in the direction the
theory predicts, and it is a **continuation** signal — the opposite of
catching a top or a bottom.

## 5. Location (PDH/PDL/premarket/opening range/VWAP ±2σ)

| | n | mean close | reversed |
|---|---:|---:|---:|
| swing high at a marked level | 156 | −0.02 | 44% |
| swing high in open space | 410 | −0.02 | 50% |
| swing low at a marked level | 126 | −0.25 | 44% |
| swing low in open space | 438 | −0.07 | 50% |

A level makes a reversal **less** likely here, not more (44% vs 50%). The
samples are small and the difference is not significant, but there is
certainly no location edge to build on.

## 6. News and corporate actions

This platform has a headline feed but no historical news archive, so an
overnight **gap** is the only visible trace of news and corporate actions
in bar data. A gap ≥ 1 ATR faded **49%** of the time on TQQQ and 46% on
QQQ. Sessions after a large gap moved +0.49 ATR on average — direction
unrelated to the gap's sign.

This is not a news strategy and nothing below claims to read news.

---

## The three strategies, and what they measured

Built in `apps/api/app/backtesting/setups.py`, registered as
`quiet_pullback`, `volume_climax_reversal`, `gap_fade`. Backtested through
the existing intraday bracket engine with **real costs** (1bp fee + 2bps
slippage), full 128 sessions:

**TQQQ.US**

| setup | trades | win% | expectancy R | t | PF | total R |
|---|---:|---:|---:|---:|---:|---:|
| quiet_pullback | 376 | 46.8 | −0.099 | −1.51 | 0.83 | −37.2 |
| gap_fade | 159 | 47.2 | −0.143 | −1.58 | 0.74 | −22.7 |
| volume_climax_reversal | 4 | 25.0 | −0.697 | −2.07 | 0.10 | −2.8 |

**QQQ.US**

| setup | trades | win% | expectancy R | t | PF | total R |
|---|---:|---:|---:|---:|---:|---:|
| quiet_pullback | 342 | 35.7 | −0.384 | **−5.74** | 0.49 | −131.3 |
| gap_fade | 109 | 43.1 | −0.332 | **−3.10** | 0.48 | −36.2 |
| volume_climax_reversal | 11 | 27.3 | −0.384 | −1.00 | 0.49 | −4.2 |

Two further checks, both negative:

- **Raising the score threshold makes it worse.** `quiet_pullback` on
  TQQQ at `min_score ≥ 4`: −0.147 R against −0.099 for every signal. The
  same monotonic degradation D095 found — the score is not ranking
  anything real.
- **`volume_climax_reversal` has no sample.** 4 trades on TQQQ, 11 on
  QQQ over 128 sessions. Its gates (session extreme + 2× average volume +
  at a marked level + rejected by the close) are individually reasonable
  and jointly almost never true. Nothing can be concluded from it, and
  that is the honest reading — not "it needs loosening until it trades."

## The conclusion the brief asked for

**There is no strategy here that catches tops and bottoms.** On 128
sessions of real 5-minute data across two correlated symbols, reversals
at swing extremes are 48–50% with a mean within ±0.15 ATR; volume does
not separate them; location does not separate them; gaps do not fade.
The only asymmetry that survived measurement is a ~0.15 ATR continuation
tilt after a quiet pullback, and once a stop, a target and 3bps of costs
are put around it, it loses money on both symbols.

This is the same answer the platform's earlier intraday research gave
(D095: composite t = −3.32) arrived at from a different direction, which
is itself informative: three studies, three different setup families, no
edge.

What that leaves worth doing:

1. **Treat these three as instrumentation, not signals.** They are
   registered, they draw on the chart (items 6–7), and the Autotrade
   bot's learning loop will demote them from its own live evidence. That
   is a measurement apparatus, not a trading plan.
2. **Look where the effect could be larger than the costs.** A 0.15 ATR
   tilt cannot survive a 3bps round trip on a 5-minute bar. Either the
   horizon has to lengthen (daily bars, where the same tilt is worth more
   than the spread) or the cost has to fall.
3. **Stop adding reversal detectors to this timeframe.** Three studies
   have now said the same thing.

Every figure above is reproducible:

```bash
python scripts/research_5m_structure.py --symbol TQQQ.US --days 130
python scripts/backtest_5m_setups.py --symbol TQQQ.US
```

---

## Phase 88 — the four remaining playbook strategies, and a rolling walk-forward

Four detectors the playbook scoped and nothing had built were added to
`apps/api/app/backtesting/setups.py`, bringing the registry to twelve:
`rsi_divergence` (strategy 3), `order_block_fvg` (strategy 9),
`fib_confluence` (strategy 10) and `bollinger_confluence` (strategy 6).

Measured the same way as the eight before them — this platform's own
stored 5-minute bars, 129 sessions, `min_score=0`, real costs (1bp fee +
2bps slippage), no parameter search:

| setup | TQQQ trades / expR / t | QQQ trades / expR / t |
|---|---|---|
| `rsi_divergence` | 249 / **−0.279** / −3.68 | 240 / **−0.569** / −7.95 |
| `order_block_fvg` | 360 / **−0.127** / −2.16 | 314 / **−0.317** / −4.98 |
| `fib_confluence` | 121 / −0.089 / −0.88 | 121 / **−0.346** / −3.36 |
| `bollinger_confluence` | 66 / −0.061 / −0.40 | 60 / −0.222 / −1.36 |

Nothing here changes the conclusion the earlier phases reached. Two of the
four are negative with a t-statistic that is not ambiguous on either
symbol, and `rsi_divergence` is the worst result this study has produced
from any setup: taking the far side of a new extreme because the
oscillator disagreed with it lost 0.28R per trade on TQQQ and 0.57R on
QQQ. That is a finding, not a failure of the implementation — the
divergence is detected correctly, against confirmed swings only, with RSI
recomputed as of the comparison swing rather than read off the present
bar, and it still loses.

`fib_confluence` and `bollinger_confluence` are negative but small and
statistically indistinguishable from zero on TQQQ. They fire rarely (121
and 66 trades over 129 sessions), which is what requiring independent
confluence is for, and it also means neither has a sample worth drawing a
conclusion from. Neither was loosened to produce one.

### The rolling walk-forward (§22)

`scripts/walk_forward_5m.py` automates the procedure §22 describes and
that this engine had only ever had one hand-made 60/40 split of. Each fold
selects the best setup by expectancy on a train window of sessions, then
scores ONLY that setup on the immediately following, non-overlapping test
window. Folds are sliced by session date, never by bar count, so a
boundary never lands mid-session.

40 train / 20 test, stepping 20, over the same 129 sessions:

| symbol | folds | distinct picks | pooled OOS trades | pooled OOS expR | t |
|---|---|---|---|---|---|
| TQQQ.US | 4 | 3 | 55 | **−0.070** | −0.54 |
| QQQ.US | 4 | 2 | 41 | **−0.087** | −0.46 |

The per-fold tables matter more than the pooled number. On TQQQ the
procedure picked a different setup in three of four folds, and in three of
four the setup that looked best in training lost in testing — including
`sweep_mss` at +0.18 expR in training and −0.16 in testing, twice. That is
the signature of selecting on noise: the training figure is real, and it
does not survive contact with the next twenty sessions.

**The conclusion is unchanged and now has a procedure behind it rather
than a per-setup table.** Selecting the best-performing intraday setup on
recent history and trading it forward would have lost money over this
window, on both symbols. Nothing in this study has earned a paper
deployment, let alone a live one.

### What was deliberately NOT done

* No parameter search on any of the four new detectors. Each was written
  once, to the playbook's description, and measured once.
* No loosening of `fib_confluence`/`bollinger_confluence` to manufacture a
  sample. `require_confluence` stays on by default.
* No retuning of the walk-forward windows after seeing the result. 40/20
  was chosen before the first run because it gives four folds on 129
  sessions; the negative pooled figure is not a reason to search for a
  train/test split that produces a positive one, and doing so would be the
  overfitting this procedure exists to detect.
