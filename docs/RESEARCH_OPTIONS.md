# Options on TQQQ: what a modelled walk-forward can and cannot say

Phase 100 (D119). Every number here comes from
`scripts/optimise_options.py` run on 2026-09-28 against this platform's own
stored TQQQ.US daily bars (**759 closes, 2023-09-18 to 2026-09-25**) and the
one real chain snapshot stored so far. Full report, every fold and every
out-of-sample trade: `docs/research/options_walk_forward_2026-09-28.json`.

## Read this before any number

**Every option price behind these results is MODELLED.** There is no
historical TQQQ option data in this platform — nothing free publishes past
chains, and Longbridge cannot quote options on this account (`301604`).
Each leg is priced with Black-Scholes from the real underlying close, with

    volatility = realised vol (20 daily returns, annualised) x IV/RV multiplier

The multiplier was **measured, once**: on the stored Cboe snapshot of
2026-09-25, the ATM implied vol of the 2026-10-23 expiry was **0.525**
against a 20-day realised vol of **0.4715**, a ratio of **1.113**. One day
is one sample; it sets the level of the model's volatility premium for the
whole three years. The model has no skew, no term structure, no volatility
of volatility, and no earnings/event premium. Costs: half-spread
max($0.02, 3% of mid) per leg, $0.65 per contract per leg to open and
again to close. Of 47 out-of-sample trades, **46 are MODELLED and 1 is
MIXED** (its window-end exit happened to land on the snapshot day and was
marked at the real quote). None is OBSERVED.

## The procedure

Grid: 7 structures (long call, long put, bull call, bear put, bull put,
bear call, iron condor) x target delta {0.20, 0.30, 0.50} x DTE
{7, 14, 30, 45} x profit target {50%, 100%, none} x stop {50%, 100%, none}
= **756 configurations**. Rolling windows of 252 train / 63 test daily
bars, stepped 63 — **8 folds, test windows 2024-10-16 to 2026-09-25**. On
each train window the configuration with the highest t-statistic of return
on risk (mean R / standard error, at least 8 trades) is chosen; it is then
scored, untouched, on the following test window. Positions open at a
window's last bar are closed there, so no train trade sees its test window.
Every configuration is also scored on every test window, to show where the
chosen one ranked.

## Headline (out of sample, modelled)

| | value |
|---|---:|
| trades | 47 |
| win rate | 78.7% |
| expectancy | **+$39.39 per trade** (one structure) |
| mean return on risk | **+0.100 R** |
| t-statistic of R | **1.44** |
| total | +$1,851.19 |
| max drawdown | $790.99 (2.05 R) |
| folds with positive expectancy | 5 of 8 |
| chosen config's OOS rank (share of configs that beat it) | 40% on average |
| TQQQ buy & hold over the same test windows | +8.8% |

What was chosen: a **bull put credit spread** in 5 folds, an **iron
condor** in 2, a **bear call** in 1 — short premium every time. 37 of the
47 trades are bull puts.

The median configuration had **negative** out-of-sample expectancy in all 8
folds (between −0.03 R and −0.12 R). Most of this grid loses money after
costs in the model; the procedure picked better-than-median configurations
in 5 of 8 folds.

## Why this is not evidence of an edge

1. **t = 1.44 is not significant.** 47 trades cannot distinguish +0.10 R
   from zero.
2. **The sign is the multiplier's.** The winners sell premium, and a model
   whose implied vol is set above realised vol pays premium sellers by
   construction. Re-running the whole walk-forward with the multiplier
   changed and nothing else:

   | IV/RV multiplier | trades | mean R | t | total |
   |---:|---:|---:|---:|---:|
   | 1.0 (no premium) | 48 | **−0.173** | −1.96 | **−$2,816.93** |
   | 1.113 (measured, 1 day) | 47 | +0.100 | 1.44 | +$1,851.19 |
   | 1.5 | 59 | +0.084 | 2.21 | +$1,556.51 |

   At IV = RV the same procedure loses. The result is therefore a
   statement about the assumed volatility premium, which is the one input
   this platform has measured on exactly one day.
3. **Selection by raw mean R fails badly.** Choosing on train expectancy
   alone (not its t-statistic) picks long-dated long calls/puts and bull
   call spreads with 8-15 train trades and scores **−0.858 R, −$4,901.14,
   13.5% wins** out of sample (t = −5.36). That is what selecting on small
   samples does; it is why t-statistic selection is the default.
4. **In hindsight, the answer changes.** Over the whole period (in-sample,
   not evidence) the best configurations are long-dated long options and
   bull call spreads (long put 0.20Δ 45 DTE +0.65 R on 22 trades; long call
   0.30Δ 45 DTE with a 50% stop +0.60 R on 48). Share of configurations
   with positive in-sample expectancy per structure: long call 62%, bull
   put 54%, long put 36%, bull call 25%, iron condor 24%, bear put 0%, bear
   call 0% — a 3x ETF that tripled has rewarded owning upside, and a model
   with no skew prices downside too cheaply to sell.
5. **Daily closes only.** Stops and targets are checked at the close; a gap
   fills at the gapped price. An in-the-money leg at expiry is settled at
   intrinsic as if cash-settled; real TQQQ options are physically settled.
   The playbook's 0-3 DTE tactics cannot be tested on daily bars at all.

## What would change the answer

Real chains. The snapshot loop (`OPTION_SNAPSHOT_SCHEDULER_ENABLED`, off by
default) stores ~908 contracts a trading day (~280 KB, ~70 MB a year for
TQQQ). Every day it runs does two things this study lacked: another
sample of the real ATM IV / RV ratio (the multiplier becomes a median over
many days instead of one), and — once enough days exist — trades whose
entry and exit are OBSERVED rather than modelled. Re-running
`scripts/optimise_options.py` then needs no code change; the engine
already prefers stored quotes wherever they exist and labels each trade
accordingly.

Until then: no options configuration here should be promoted to paper
trading on the strength of this study. Nothing was traded to produce it.
