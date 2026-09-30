# FX intraday research — EUR/USD 5-minute, 2024–2025 (Phase 103, D123)

Research only. Nothing here was traded, and nothing here is a
recommendation. Every number below was produced by
`scripts/research_fx.py` on the data described in the next section; rerun
it to reproduce.

## Bottom line

**No setup has a tradable edge on EUR/USD 5-minute bars once a realistic
spread is charged, and the walk-forward selection procedure loses out of
sample.** Pooled out-of-sample over 22 folds: **764 trades, −0.078 R/trade,
t = −1.96, −59.5 R total.** The one setup with a real gross signal
(`sweep_mss`, +0.161 R/trade frictionless, t = +4.70 over 945 trades) is
reduced to +0.022 R (t = +0.66) by a 1-pip round trip and turns clearly
negative at 2 pips. The reason is structural, not a tuning problem: these
setups place stops **4–9 pips** from entry on EUR/USD, so a 1-pip spread is
**0.1–0.25 R per trade before the idea is right or wrong.**

## Data — what it is and what it is not

* **Source: HistData.com free annual 1-minute files**, `DAT_ASCII_EURUSD_M1_2024`
  and `…_2025` (744,463 one-minute bars), downloaded once by hand through
  the site's own download form on 2026-09-29. HistData's FAQ describes the
  data as free with no warranty; its robots.txt disallows only
  `/wp-admin/`. It does not grant redistribution rights, so **the files are
  not in this repository and were not written to `market_data_bars`**; the
  reader (`apps/api/app/marketdata/providers/histdata.py`) takes a local
  path and makes no network call.
* **BID prices, not mid.** HistData builds bars from the bid. Because the
  cost model charges a full spread per round trip on top, the constant
  bid-vs-mid offset cancels in P&L, but stops and targets trigger on bid
  highs/lows: a long's stop is triggered where it really would be (a long
  exits at the bid) and a short's stop fires marginally LATE (a short
  exits at the ask). That flatters shorts by up to one spread on stop-outs;
  it cannot turn the results below positive.
* **Timestamps: EST without DST (fixed UTC−5)**, converted as a fixed
  offset, not as America/New_York.
* **No volume.** HistData's volume column is always 0; it is read as
  `volume=None`.
* **Resampled to 5m (execution) and 15m (higher-timeframe bias)** by
  `marketdata/fx.py::resample_bars`: OHLC over real minutes only, no bucket
  invented for a minute that did not print.
* 149,551 five-minute bars, **519 FX sessions**, 2024-01-02 … 2025-12-31.

### Sources checked and not used

| Source | Finding (2026-09-29) |
| --- | --- |
| **IG** `GET /prices/{epic}` | The venue we would trade at; demo API key **suspended by IG** (`client-suspended`). Not called — every further login extends the suspension. Provider built against the documented API and tested with a stub. |
| **Dukascopy** `datafeed.dukascopy.com` | Answers **HTTP 429** on the first request and points to its data-export page, which now documents access only via a **requester-pays AWS S3 bucket** (needs an AWS account; ~$0.06 for EUR/USD's full history). Not free/anonymous; not used without the operator's own AWS account and approval. |
| **FXCM** `candledata.fxcorporate.com` | Behind a **Cloudflare bot challenge** (HTTP 403, "Just a moment…"). Bypassing bot detection is not something this platform does. |
| Yahoo Finance, Stooq, Alpha Vantage FX intraday | Not attempted: Yahoo's terms forbid automated use; Stooq's bulk intraday needs a captcha; Alpha Vantage FX intraday is a premium endpoint. |

## Method

* **Calendar:** `sessions.FxCalendar` — session 17:00→17:00 New York, named
  by the date it ends on; **17:00–18:00 NY rollover blacked out**; Asia
  (18:00 NY → 08:00 London) is the "pre-market" range; **London open to the
  17:00 NY roll is the traded session** (14 hours, 168 five-minute bars).
  The opening range is the first 15 minutes after the London open, and is
  **hidden from any bar that opens before it is complete** (see D123).
* **Costs:** `FxSpreadCostModel` — mid ± half-spread per fill, in pips.
  Default EUR/USD half-spread **0.5 pips** (1.0-pip round trip: IG advertises
  EUR/USD "from 0.6 pips", and a backtest should not assume the best minute
  of the day all day). Also run at **zero** (to measure what the spread
  takes) and **2×** (1.0 pip/side). No financing: every trade is flat before
  the 17:00 NY roll, so none is owed.
* **Engine:** the existing intraday engine and bracket plan unchanged
  (0.5% risk, TP1 1R / TP2 2R scale-outs, breakeven after TP1, 1.5 ATR trail,
  stop-quality 0.25–4 ATR, −2R daily stop, 3-loss pause, one position at a
  time), higher-timeframe bias from 15m EMA21/50.
* **Walk-forward:** 60 sessions train / 20 test / roll 20, pick the setup
  with the best train expectancy (≥ 10 trades), score it on the next 20
  only. Implemented as one causal full-series replay per setup with trades
  bucketed into folds (equivalent, because every engine control resets
  each session — see the script's docstring).

## Results

### Volume-gated setups refuse on FX

`quiet_pullback`, `volume_climax_reversal` and `vwap_reversion` saw **0
signals** over 519 sessions — volume is `None`, so the volume comparisons
and session VWAP they depend on do not exist, and they return nothing
rather than reading a tick count as volume.

### Full sample (519 sessions), R per trade

| setup | n (1.0-pip RT) | frictionless expR (t) | **1.0-pip RT expR (t)** | 2.0-pip RT expR (t) | median stop (pips) |
| --- | ---: | ---: | ---: | ---: | ---: |
| sweep_mss | 917 | +0.161 (+4.70) | **+0.022 (+0.66)** | −0.180 (−3.66) | 9.2 |
| fib_confluence | 845 | +0.052 (+1.36) | −0.177 (−4.57) | −0.341 (−8.67) | 4.6 |
| orb_failure | 924 | +0.026 (+0.61) | −0.248 (−6.20) | −0.422 (−10.99) | 4.7 |
| bollinger_confluence | 533 | +0.016 (+0.33) | −0.209 (−4.45) | −0.335 (−6.95) | 5.5 |
| gap_fade | 22 | +0.014 (+0.05) | −0.204 (−0.71) | −0.284 (−0.97) | 10.6 |
| ema_reversal | 1,639 | −0.000 (−0.01) | −0.167 (−6.31) | −0.323 (−10.23) | 7.1 |
| order_block_fvg | 2,395 | −0.015 (−0.66) | −0.269 (−11.36) | −0.442 (−17.48) | 4.6 |
| rsi_divergence | 2,029 | −0.016 (−0.63) | −0.277 (−10.46) | −0.489 (−17.96) | 3.8 |
| candle_reversal | 1,926 | −0.024 (−0.89) | −0.319 (−11.78) | −0.526 (−18.58) | 3.7 |
| trend_pullback | 1,799 | −0.043 (−1.75) | −0.207 (−8.05) | −0.365 (−13.79) | 6.4 |

Trade counts differ between cost levels because the engine's own controls
(stop-quality measured from the filled price, the −2R daily stop and the
3-loss pause) respond to costs.

Read with the multiple-comparisons problem in mind: ten setups were
tested. `sweep_mss`'s frictionless t = 4.70 survives a Bonferroni
correction for ten tests, so there is probably a real *gross* asymmetry in
sweeping a prior-day or Asian-range extreme and reclaiming it — but it is
worth about 0.16 R on a 9-pip stop, i.e. about 1.5 pips, which is roughly
one round-trip spread.

### Walk-forward (1.0-pip round trip)

22 folds, 5 distinct setups chosen (`sweep_mss` 14 times,
`bollinger_confluence` 4, `orb_failure` 2, `ema_reversal` 1,
`fib_confluence` 1). **Pooled OOS: 764 trades, −0.078 R, t = −1.96,
−59.5 R.** 7 of 22 folds positive. Selecting on the train window picks
`sweep_mss` most of the time, which is right, but the other picks are noise
winners that lose on test, and `sweep_mss` itself nets ≈ 0 after costs.

### Spread sensitivity for `sweep_mss` alone

Run AFTER seeing the table above, so `sweep_mss` is an in-sample choice
and nothing in this subsection is an out-of-sample test of a selection
procedure. At **IG's advertised minimum EUR/USD spread (0.6-pip round
trip, 0.3/side)**:

| costs | n | expR | t |
| --- | ---: | ---: | ---: |
| frictionless | 945 | +0.161 | +4.70 |
| 0.6-pip RT | 929 | +0.079 | +2.30 |
| 0.9-pip RT | 918 | +0.036 | +1.06 |
| 1.0-pip RT (base) | 917 | +0.022 | +0.66 |
| 2.0-pip RT | 875 | −0.180 | −3.66 |

The same fixed setup rolled through the 22 folds at 0.6 pips: **798 test
trades, +0.052 R, t = +1.42**, 16 of 22 folds positive. That is not
significant, it assumes the minimum spread holds all session, and it
exists only because this setup was picked from ten after looking. The
break-even round trip is roughly 1.1 pips. Whether IG's real London/NY
spread sits under that is exactly what the IG spread series (once the key
works) would answer — this data cannot.

## What this does and does not show

* It does **not** show that EUR/USD cannot be traded intraday. It shows that
  these equity-derived 5-minute reversal setups, with stops of a few pips,
  cannot pay a retail CFD spread.
* The obvious next experiments — wider structural stops (15m/1h execution),
  the Asian session included (`--trade-asian-session`), other pairs — are
  cheap to run with `scripts/research_fx.py` and were **not** run here, so
  that this report cannot be the product of trying variants until one
  looked good.
* Before trusting any FX number at IG, the IG provider must fetch real mid
  bars and its **observed** spread series must replace the 0.5-pip
  default; the rollover-hour spread in particular is unmeasured.
