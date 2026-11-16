# Strategy Library — what was measured

Run 2026-11-16 with `python -m scripts.library_report` (Phase 108, D135).
Raw output: `docs/research/library_results.json`. Data: Yahoo adjusted
daily closes, screened (`library/quality.py`). Every strategy ran with its
published default parameters; nothing was tuned.

Conventions: a decision on one close trades on the next; costs per side are
US 3 bps, SG 15, India 15, crypto 40, FX 2; cash earns nothing; Sharpe uses
a zero risk-free rate. **"After publication" is the only period the rule's
author could not have fitted to**, and is what each verdict judges.

## After publication, strategy vs buy-and-hold

| Universe | Strategy | From | Return/yr | Sharpe | Max DD | B&H return/yr | B&H Sharpe | B&H max DD | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| SPY | 200-day trend | 2007 | +8.2% | 0.69 | -27.6% | +10.7% | 0.62 | -55.2% | lower risk |
| SPY | 12m TS momentum | 2012 | +9.5% | 0.72 | -31.5% | +14.2% | 0.88 | -33.7% | no edge |
| SPY | Donchian 55/20 | 2007 | +3.9% | 0.47 | -21.8% | +10.9% | 0.63 | -55.2% | lower risk |
| SPY | RSI(2) pullback | 2008 | +3.3% | 0.52 | -15.0% | +14.1% | 0.81 | -33.7% | lower risk |
| QQQ | 200-day trend | 2007 | +12.6% | 0.78 | -28.6% | +15.8% | 0.77 | -53.4% | lower risk |
| QQQ | 12m TS momentum | 2012 | +12.6% | 0.84 | -24.0% | +18.3% | 0.91 | -35.1% | lower risk |
| QQQ | Donchian 55/20 | 2007 | +7.3% | 0.68 | -19.1% | +15.9% | 0.78 | -53.4% | lower risk |
| QQQ | RSI(2) pullback | 2008 | +4.3% | 0.53 | -13.0% | +19.3% | 0.93 | -35.1% | lower risk |
| US asset classes (5 ETFs) | 200-day trend | 2007 | +4.7% | 0.62 | -17.0% | +7.2% | 0.59 | -38.4% | lower risk |
| US asset classes | 12m TS momentum | 2012 | +4.1% | 0.57 | -20.5% | +7.6% | 0.75 | -22.4% | no edge |
| US asset classes | Momentum rotation (top 2) | 2003 | +11.1% | 0.82 | -22.9% | +8.7% | 0.72 | -38.4% | **beats** |
| SPY / EFA / AGG | Dual momentum | 2013 | +8.4% | 0.60 | -33.7% | +14.5% | 0.89 | -33.7% | no edge |
| US sectors (9 ETFs) | Momentum rotation (top 3) | 1999 | +8.9% | 0.58 | -34.7% | +8.2% | 0.51 | -55.2% | **beats** |
| US sectors | RSI(2) pullback | 2008 | +2.6% | 0.56 | -12.6% | +14.1% | 0.81 | -33.7% | lower risk |
| STI ETF (SG) | 200-day trend | 2008 | +4.9% | 0.53 | -21.9% | +8.4% | 0.62 | -33.8% | lower risk |
| STI ETF | 12m TS momentum | 2012 | +4.3% | 0.51 | -27.2% | +7.4% | 0.63 | -33.8% | no edge |
| STI ETF | Donchian 55/20 | 2008 | +1.7% | 0.24 | -37.5% | +5.9% | 0.45 | -54.7% | lower risk |
| STI ETF | RSI(2) pullback | 2008 | -2.3% | -0.50 | -38.4% | +8.6% | 0.65 | -33.8% | no edge |
| SG blue chips | Momentum rotation | 2000 | +10.4% | 0.59 | -50.7% | +4.8% | 0.38 | -57.7% | survivorship-biased |
| SG blue chips | RSI(2) pullback | 2008 | -1.7% | -0.54 | -31.2% | +8.6% | 0.65 | -33.8% | no edge |
| Nifty 50 ETF (IN) | 200-day trend | 2009 | +7.5% | 0.61 | -30.0% | +11.1% | 0.72 | -36.3% | no edge |
| Nifty 50 ETF | 12m TS momentum | 2012 | +6.2% | 0.54 | -38.3% | +12.4% | 0.88 | -36.3% | no edge |
| Nifty 50 ETF | Donchian 55/20 | 2009 | +4.7% | 0.46 | -22.3% | +13.4% | 0.81 | -36.3% | lower risk |
| Nifty 50 ETF | RSI(2) pullback | 2009 | -1.1% | -0.17 | -26.9% | +11.1% | 0.72 | -36.3% | no edge |
| India large caps | Momentum rotation | 1996 | +30.6% | 1.12 | -59.7% | +13.4% | 0.80 | -36.3% | survivorship-biased |
| India large caps | RSI(2) pullback | 2008 | +0.2% | 0.08 | -16.1% | +13.4% | 0.80 | -36.3% | lower risk |
| Bitcoin | 200-day trend | 2015 | +48.2% | 1.01 | -65.2% | +60.7% | 1.06 | -83.4% | no edge |
| Bitcoin | 12m TS momentum | 2015 | +19.6% | 1.28 | -20.5% | +63.0% | 1.08 | -83.4% | lower risk |
| Bitcoin | Donchian 55/20 | 2014 | +43.3% | 1.09 | -49.4% | +53.7% | 0.98 | -83.4% | lower risk |
| BTC / ETH / SOL | 200-day trend | 2015 | +67.4% | 1.18 | -68.5% | +60.7% | 1.06 | -83.4% | survivorship-biased |
| BTC / ETH / SOL | 12m TS momentum | 2015 | +18.2% | 1.29 | -21.9% | +63.0% | 1.08 | -83.4% | lower risk |
| BTC / ETH / SOL | Donchian 55/20 | 2014 | +58.2% | 1.45 | -39.4% | +53.7% | 0.98 | -83.4% | survivorship-biased |
| FX majors (6 pairs) | 12m TS momentum, long/short | 2012 | +0.9% | 0.20 | -12.8% | n/a | n/a | n/a | no edge |
| FX majors | Donchian 55/20 | 2007 | -0.3% | -0.11 | -10.9% | n/a | n/a | n/a | no edge |
| Gold (GLD) | 200-day trend | 2007 | +7.0% | 0.54 | -39.5% | +9.0% | 0.56 | -45.6% | no edge |
| Gold | 12m TS momentum | 2012 | +3.4% | 0.36 | -28.9% | +5.6% | 0.41 | -42.1% | lower risk |
| Gold | Donchian 55/20 | 2007 | +3.6% | 0.36 | -34.1% | +8.9% | 0.56 | -45.6% | lower risk |

## What this says

1. **Trend rules did what the papers claim and no more.** On SPY and QQQ
   the 200-day filter roughly halved the worst drawdown (-55% to -28% on
   SPY) and gave up two to three points of annual return. That is a risk
   tool, not a source of extra return.
2. **Momentum rotation is the only rule that beat buy-and-hold on clean
   data** - on five US asset-class ETFs (+11.1% vs +8.7%, shallower
   drawdown) and nine US sector ETFs (+8.9% vs +8.2%). Both are small
   lists and one history; the margin on sectors is thin.
3. **Singapore and India index ETFs: nothing beat holding the index.**
   Consistent with the published finding that momentum and trend effects
   are weaker in Asian markets.
4. **The strong-looking stock-list results are not real results.** SG blue
   chips and India large caps are today's index members; the +30.6% a year
   on India is what you get by knowing in 1996 which fifteen companies
   would be the largest in 2026.
5. **RSI(2) has no edge once the entry is a session late**, which is what
   an end-of-day process can actually do. It lost money on the STI ETF and
   the Nifty ETF.
6. **FX majors: nothing**, before financing costs that would make it worse.
7. **Crypto:** trend and breakout rules cut Bitcoin's -83% drawdown to
   between -20% and -65% at a large cost in return. The three-coin results
   are survivor-biased.

## Limits of this evidence

* One vendor, unofficial, end-of-day. India needed bad ticks removed.
* No shorting or financing costs; cash earns nothing; taxes ignored.
* Each row is one path through history. A t-statistic above 2 says the
  mean return was unlikely to be zero in that sample; it does not say the
  next ten years will look the same.
