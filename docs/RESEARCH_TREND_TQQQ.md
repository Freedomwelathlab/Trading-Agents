# Riding the trend on TQQQ: rises, pullbacks, and the break

Phase 101, decision D120. Every number below comes from this platform's own
stored bars - **10,296 regular-hours five-minute bars, 132 sessions,
2026-03-19 to 2026-09-25**, and **759 daily bars, 2023-09-18 to
2026-09-25** - through two scripts anyone can re-run:

```bash
python scripts/trend_anatomy_tqqq.py --symbol TQQQ.US \
    --json docs/research/trend_anatomy_2026-09-28.json            # the study
python scripts/trend_walk_forward_tqqq.py --symbol TQQQ.US \
    --out docs/research/trend_walk_forward_2026-09-28.json        # the optimiser
```

The brief: *analyse and backtest the rise and pullback of the price during
the trend until trend reversal; find the pattern, the reaction of price, the
trend strength and the breaking point of the reversal; enter and exit at the
right time; do not exit in between unless the rules break; use a trailing
take-profit if the trend keeps going.*

**The answer, first.** The anatomy is measurable and parts of it are real:
pullbacks have a typical shape, trend strength does predict whether a new
high follows, and two warning signs genuinely raise the odds of a break.
**But no version of "buy the confirmed pullback, hold to the structure
break" made money out of sample on this data after costs.** The rolling
walk-forward over 576 parameter combinations lost **-0.518R per trade
(t = -2.96, 43 trades)** flat by the bell and **-0.200R (t = -0.53, 27
trades)** held overnight. Every one of the eight fold winners lost in the
fold that followed it. Section 2 shows why: it is arithmetic about how late
confirmed structure arrives, not bad luck.

Nothing here has earned a paper deployment, let alone a live one.

---

## 1. How a trend is defined here (and why it cannot peek)

* Swings are fractal pivots (`find_swings`, strength 3 unless stated). A
  swing enters the analysis on its **confirmation bar**, never on the bar
  it printed. `docs/RESEARCH_5M.md` §0 shows what happens otherwise: a fake
  66% "reversal edge" that was pure look-ahead.
* **Uptrend** begins on the bar that completes a higher high AND a higher
  low (Dow). **Downtrend** mirrors it.
* **The break** - the "breaking point of reversal" - is the first **close**
  below the most recent confirmed higher low (above the most recent lower
  high in a downtrend): the market-structure break. A new trend may only be
  established from at least one swing confirmed after the previous break.
* The five-minute tape is the regular session joined across days, so a
  trend can span an overnight gap. Levels (EMA9/21, session VWAP, the prior
  swing, Fibonacci 38.2/50/61.8 of the impulse) are measured at the pivot
  bar from bars up to it. The 15-minute bias is read only from 15m bars
  that had CLOSED by the 5m bar's close; the daily bias only from earlier
  sessions.

## 2. The rise: trend legs, and the arithmetic that sinks the ride

Five-minute, strength 3:

| measure | up (168 trends) | down (165 trends) |
|---|---:|---:|
| leg size origin to peak, median | **2.93%** (6.4 ATR) | 2.56% (5.8 ATR) |
| leg size p75 / p90 | 4.70% / 7.89% | 4.37% / 6.48% |
| bars origin to peak, median | 24 (2 hours) | 18 |
| bars origin to break, median | 35 | 31 |
| pullbacks per trend, median (mean) | 1 (1.8) | 1 (1.5) |
| share of the leg still AHEAD when the trend is confirmed, median | **39%** | 50% |
| confirm-to-break move as a fraction of the leg, median | **-12%** | -15% |

The last two rows are the most important numbers in this document. By the
time confirmed swings prove an uptrend exists, a median 61% of the leg has
already happened. By the time a close below the last higher low proves it
has ended, price has given back more than the 39% that was left. **A rider
who enters on confirmation and leaves on the break loses on the median
trend - before costs.** Waiting for the market to prove a bottom costs the
bottom; waiting for it to prove a top costs the top; on this instrument and
timeframe the two lags together are larger than what is left in between.

Other facts about the rise:

* **The break is a good description of the end of a trend.** After a
  break, the next trend established ran the other way **78%** of the time
  (84% at strength 2). It is correct; it is just late.
* **52% of trends span at least one overnight gap** (38% at strength 2).
  The house flat-by-the-bell rule cuts about half of all trends short,
  which is why both variants were optimised below.
* **Daily bars, strength 2, three years: 35 trends.** Up legs median
  **29.6%** over 13 sessions origin-to-peak (p90 76.7%). The same shape
  exactly: 41% of the leg left at confirmation, confirm-to-break median
  -23% of the leg.

## 3. The pullback: depth, duration, where it holds

563 pullbacks inside trends (each a confirmed higher low in an uptrend or
lower high in a downtrend), five-minute strength 3:

| measure | p25 | median | mean | p75 | p90 |
|---|---:|---:|---:|---:|---:|
| depth, fraction of the prior impulse | 0.40 | **0.59** | 0.59 | 0.77 | 0.90 |
| depth, ATR | 1.64 | **2.14** | 2.38 | 2.85 | 3.57 |
| pullback duration, bars | 2 | **4** | 5 | 6 | 9 |
| impulse duration, bars | 4 | 7 | 9 | 12 | 17 |
| impulse size, ATR | 2.94 | 4.06 | 4.69 | 5.51 | 7.40 |
| pullback / impulse average volume | 0.69 | 0.85 | 0.98 | 1.08 | 1.50 |

**The typical five-minute TQQQ pullback retraces ~60% of the impulse,
~2 ATR, over ~4 bars (20 minutes), on lighter volume than the impulse.**
Daily is the same shape: median 0.63 of the impulse, 1.9 ATR, 2 sessions.

**Where they held** (nearest level within 0.25 ATR of the pivot). "Ride R"
is the trade the brief describes: enter on the close of the bar that
confirms the pullback, stop 0.3 ATR beyond it, hold to the structure break,
net of 6bps round-trip costs.

| held at | n | new extreme followed | ride R > 0 | mean ride R, net | t |
|---|---:|---:|---:|---:|---:|
| open space (no level) | 214 | 57% | 25% | -0.306 | -1.54 |
| Fibonacci 61.8% | 77 | 68% | 31% | +0.610 | +1.17 |
| EMA21 | 75 | 61% | 23% | +0.128 | +0.20 |
| Fibonacci 50% | 52 | 69% | 23% | -1.625 | -2.15 |
| Fibonacci 38.2% | 47 | 72% | 30% | -0.044 | -0.10 |
| EMA9 | 46 | 85% | 17% | -0.788 | -2.09 |
| session VWAP | 29 | 52% | 24% | +0.006 | +0.01 |
| prior swing (old resistance) | 23 | 61% | 13% | -1.292 | -1.94 |

**38% of pullbacks held at no identifiable level.** EMA21 and the 61.8%
retracement are the most-touched levels (22% and 19% of pullbacks). A
pullback that held at a level was followed by a new extreme more often than
one in open space (61-85% vs 57%), but that did not become a reliable
profit: the one positive bucket with any size (61.8%, +0.61R) has t = 1.17,
and at strength 2 it is +0.31R, t = 0.76. Nothing in this table survives a
correction for having looked at eight buckets.

**Depth against outcome** (mean net ride R; strength 2 alongside):

| depth of impulse | n | new extreme followed | ride R (t) | strength 2: ride R (t) |
|---|---:|---:|---:|---:|
| < 0.236 | 40 | 85% | -0.03 (-0.06) | +1.29 (+1.47) |
| 0.236-0.382 | 89 | 72% | -0.38 (-1.16) | -0.58 (-2.43) |
| 0.382-0.5 | 83 | 69% | -0.60 (-1.94) | -0.33 (-1.01) |
| 0.5-0.618 | 93 | 63% | -0.38 (-0.59) | +0.20 (+0.48) |
| 0.618-0.786 | 126 | 56% | +0.32 (+0.80) | +0.19 (+0.61) |
| > 0.786 | 132 | 55% | **-0.62 (-3.37)** | **-0.67 (-2.38)** |

The only robust depth finding is negative: **a pullback that gives back
more than 78.6% of the impulse is a bad entry** (t = -3.4 and -2.4 on the
two strengths). The shallower the pullback, the more often a new high
follows (85% down to 55%) - but the ride's profit does not rise with it.

## 4. Trend strength at the entry

Terciles of each feature at the pullback entry, strength 3 [strength 2]:

| feature | weakest third: ride R (t) | strongest third: ride R (t) | new extreme followed, weakest to strongest |
|---|---|---|---|
| ADX(14) | **-0.66 (-2.20)** [-0.62 (-2.14)] | +0.01 (+0.02) [+0.26 (+1.08)] | 59% to 74% |
| EMA9-EMA21 spread / ATR | **-0.39 (-2.21)** [-0.62 (-2.50)] | -0.15 (-0.47) [+0.15 (+0.63)] | 45% to 80% |
| EMA21 slope over 6 bars / ATR | -0.32 (-1.72) [-0.60 (-2.35)] | -0.27 (-0.84) [+0.46 (+1.75)] | 45% to 81% |

**Trend strength is real as a description of price**: in the strongest
third of trends a new high follows a pullback 74-81% of the time, against
45-59% in the weakest. **It is not enough to make the ride pay**: the
strong-trend rides are indistinguishable from zero, and only the weak-trend
side is reliably negative. The usable rule is a filter, not a signal: *do
not buy pullbacks in a weak trend (ADX under ~20, EMAs flat or crossed).*

Higher-timeframe agreement did **not** help. With the 15-minute bias
agreeing, rides averaged -0.39R against -0.03R without; with the daily bias
agreeing, -0.55R (t = -2.70) against -0.03R. That is not evidence that
disagreement is good; it is evidence the alignment filter is not where an
edge lives on this timeframe.

Direction: longs -0.18R (t = -0.72) [strength 2: +0.01R], shorts -0.41R
(t = -2.18) [-0.37R, t = -2.25]. Shorting TQQQ's pullbacks lost in both
cuts over a window in which the ETF rose 77%.

## 5. What precedes the break

Each with-trend swing (a high in an uptrend, a low in a downtrend) was
classified by what it looked like when confirmed, and by whether the NEXT
structural event was a continuation (another higher low) or the break.
Base rate: 30% of 587 were followed by the break.

| feature at the swing | n | next event = break | without it | strength 2 |
|---|---:|---:|---:|---|
| **failed extreme** (a lower high inside an uptrend) | 201 | **40%** | 25% | 41% vs 23% |
| **ADX lower than at the previous extreme** | 329 | **36%** | 23% | 34% vs 24% |
| RSI divergence (higher high, lower RSI) | 159 | 27% | 32% | 24% vs 31% |
| volume >= 1.5x its 20-bar mean | 114 | 29% | 31% | 28% vs 30% |

Two warning signs are real and replicate at both swing strengths: **a
failed higher high raises the chance the next event is the break from ~25%
to ~40%, and fading ADX from ~23% to ~35%.** Both are still more likely
than not to be followed by continuation, so they are reasons to tighten,
not to reverse. **RSI divergence does not predict the break** - if anything
the opposite - consistent with Phase 88's `rsi_divergence` losing 0.28R per
trade. Climactic volume at the high predicts nothing. On daily bars none of
the four separates (n = 56).

The median ride reached **+0.83R** at its best (p75 +2.15R) before the
break took it back to a loss (only 26% of rides finished positive, while
63% saw a new extreme): most pullback entries DO go the right way first.
**The exit, not the entry, is where the money leaks** - which is exactly
what the arithmetic in section 2 predicts.

## 6. The setup, the exit, and the optimisation loop

Built from the study:

* **`trend_pullback`** in `SETUPS` (`apps/api/app/backtesting/setups.py`).
  Fires on exactly one bar per pullback - the bar the higher low's
  `confirmed_ts` arrives - inside at least `min_trend_legs` consecutive
  HH+HL from confirmed swings, with the retracement inside a depth band and
  optionally held at a support (EMA9, EMA21, VWAP, Fibonacci, prior swing).
  Stop: the confirmed low less a buffer in ATR. Short mirrored.
* **The structure exit**: `BracketPlan(structure_trail=True)` in
  `brackets.py`. No fixed target. The stop ratchets to each new CONFIRMED
  higher low - only on the bar after that swing's confirmation bar, never
  loosened - and the position leaves on that stop, on a confirmed lower
  low, on an optional ATR trailing take-profit
  (`structure_atr_trail_multiple`), or at the time stop. Opt-ins:
  `structure_break_on_close` (a wick through the higher low does not end
  the ride) and `hold_overnight` (carry the ride across sessions; a gap
  through the stop fills at the open). A default `BracketPlan()` is
  unchanged.

**The loop** (`scripts/trend_walk_forward_tqqq.py`), fixed before its first
run: 576 combinations of swing strength {2, 3} x trend legs {1, 2} x depth
band {any, 0.236-0.618, 0.5-0.786} x support {any, EMA9, EMA21, Fibonacci}
x stop buffer {0.1, 0.3} ATR x direction {long, short, both} x exit
{pure structure, structure + 2 ATR trailing take-profit}. Each combination
is backtested once through the house intraday engine at real costs (1bp
fee + 2bps slippage per side), 0.5% risk per trade, the 10%-of-equity
notional cap, the engine's daily loss limit and one position at a time.
Rolling folds of **40 train sessions then the next 20 test sessions**,
stepping 20: four folds, 80 out-of-sample sessions. The best TRAIN
expectancy (minimum 15 train trades) is picked; only it is scored on TEST.
In the overnight mode a train trade that exits inside the test window is
dropped from the train score, so selection never sees a test-window price.

### Flat by the bell (the house rule)

| fold | train window | picked | train | test | buy & hold, test |
|---|---|---|---|---|---:|
| 1 | 03-19..05-14 | s2, legs1, 0.236-0.618, fib, buf 0.1, **short**, trail 2 | 17 tr, +0.629R | 15 tr, **-0.148R** | +2.4% |
| 2 | 04-17..06-12 | s2, legs1, 0.5-0.786, fib, buf 0.1, long, trail 2 | 20 tr, +0.799R | 16 tr, **-0.797R** (t -6.66) | -9.5% |
| 3 | 05-15..07-14 | s3, legs1, any depth, EMA21, buf 0.1, both, structure | 16 tr, +0.750R | 6 tr, **-0.646R** | -4.2% |
| 4 | 06-15..08-11 | s3, legs1, 0.5-0.786, any, buf 0.3, **short**, structure | 18 tr, +0.105R | 6 tr, **-0.569R** | -5.4% |

**Pooled out of sample: 43 trades, win rate 12%, expectancy -0.518R,
t = -2.96, total -22.3R, max drawdown -26.0R, -0.96% of equity.**

### Held overnight until the structure breaks (the brief, literally)

| fold | picked | train | test | buy & hold, test |
|---|---|---|---|---:|
| 1 | s2, legs1, any depth, fib, buf 0.1, long, structure | 36 tr, +1.197R | 15 tr, **-0.070R** | +2.4% |
| 2 | s2, legs2, any depth, fib, buf 0.1, long, structure | 20 tr, +1.126R | 8 tr, **-0.214R** | -9.5% |
| 3 | s2, legs2, 0.5-0.786, any, buf 0.1, long, trail 2 | 16 tr, +0.718R | 3 tr, **-0.513R** | -4.2% |
| 4 | s2, legs1, 0.5-0.786, EMA21, buf 0.1, both, structure | 15 tr, +0.831R | 1 tr, **-1.105R** | -5.4% |

**Pooled out of sample: 27 trades, win rate 19%, expectancy -0.200R,
t = -0.53, total -5.4R, max drawdown -12.0R, +0.07% of equity.**

### How to read it

* **Eight folds, eight picks that lost in the next window.** Training
  expectancies of +0.1R to +1.2R became -0.07R to -1.1R. That is the
  signature of selecting on noise.
* **The grid's train ranking does not predict its test ranking.** Spearman
  rank correlation between train and test expectancy across all eligible
  combinations: -0.18, -0.14, -0.02, -0.02 (flat by the bell) and
  +0.01, +0.22, +0.03, +0.24 (overnight). Around zero; nowhere near the
  level at which choosing the best-looking combination would help.
* **Even hindsight barely finds anything.** The single best combination
  over all 132 sessions, chosen by looking at the answer, is +0.062R
  (t = 0.28, 56 trades) flat by the bell; overnight, +1.139R (t = 1.07,
  34 trades) - a handful of large winners, not a distribution. 45 of 576
  combinations (8%) are positive over the whole period flat by the bell,
  109 (19%) overnight.
* **The setup's own defaults, with no selection at all**, over every
  session: **-0.310R, t = -4.09, 254 trades** flat by the bell (-0.322R,
  t = -3.55, 154 trades on the 80 out-of-sample sessions) and **-0.390R,
  t = -2.99, 253 trades** held overnight (-0.496R, t = -3.71 out of
  sample). This is the clearest single result: the plain "buy every
  confirmed higher low, sell every confirmed lower high, ride to the break"
  rule loses about a third of its risk per trade, with a t-statistic that
  is not ambiguous.
* **Buy and hold over the same 80 out-of-sample sessions lost 16.0%**
  (the full 132-session window gained 77%). The strategy's equity loss was
  smaller only because it was flat most of the time at a 10% notional cap;
  in its own unit of risk it lost money in both modes. Being out of the
  market during a drawdown is not the same thing as an edge.
* **Overnight holding was less bad than flat-by-the-bell** (-0.20R vs
  -0.52R), consistent with half of all trends spanning a gap - but its
  sample is 27 trades with t = -0.53. It is not distinguishable from zero,
  and certainly not from a loss.

### Caveats

* One instrument, one 132-session window (a single regime: a large rally,
  then a 16% slide in the out-of-sample stretch), four folds, and
  out-of-sample samples of 43 and 27 trades. A negative result on this
  sample is informative; a positive one would not have been.
* Fills are the house pessimistic conventions: a bar touching the stop
  fills at the stop (or the open, if it gapped through), costs on both
  legs. The fixed 6bps round trip is ~0.09-0.11R per trade at these stop
  distances (median) - a material share of any edge this small.
* Daily bars (three years) show the same trend geometry but only 66
  pullbacks; no daily optimisation was run, because the intraday engine is
  session-based and 66 trades cannot support a 576-cell search.
* The last 12 sessions (09-10..09-25) fall outside the final fold, as in
  `walk_forward_5m.py`; the windows were not re-cut to include them.

## 7. What this means for the operator's question

* **Catching the exact bottom and top** is not something confirmed
  structure can do: by construction it confirms after the fact. Section 2
  quantifies the cost - on the median trend it is larger than the part of
  the trend that remains.
* **Where pullbacks hold**: ~60% of the impulse, ~2 ATR, ~20 minutes; most
  often near EMA21 or the 61.8% level; 38% at no level at all. Deeper than
  78.6% is a bad sign.
* **Trend strength**: ADX and EMA spread/slope genuinely separate trends
  that make a new high (74-81%) from those that do not (45-59%). Use them
  to *avoid* weak trends; they do not make strong-trend rides profitable.
* **The breaking point**: a close below the last confirmed higher low. It
  is followed by an opposite trend 78% of the time. A failed higher high and
  fading ADX are the two early warnings that actually raise its odds; RSI
  divergence and climactic volume are not.
* **Trailing take-profit**: a 2 ATR trail layered on the structure stop was
  in the grid. It was picked in 3 of 8 folds and lost in all three.

`trend_pullback` and the structure exit stay in the codebase as measured
instrumentation, like the setups before them. They are not a strategy, and
nothing here should be armed on paper or live on the strength of this
study.
