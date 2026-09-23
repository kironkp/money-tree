# Research log — 2026-09-23

Six hypotheses, none survived. Recorded so none is repeated or cherry-picked.
Every row is in the app as a Hypothesis with its evidence; `/review` shows them.

## Method, after correction

- 1Hour bars, EUR/GBP/AUD/NZD-USD. TRAIN 2024-09-08..2025-12-31, HELD OUT
  2026-01-01..2026-09-07. Warm-up is taken from bars BEFORE the window via
  `act_from`, never from inside it.
- Costs priced **by hour**, not flat: 1x London/NY (07-20 UTC), 1.5x Asia
  (02-06), 3x rollover and Asia open (21-01). The flat model was wrong in shape
  and flattered exactly the strategy that traded the wide hours.
- Gross profit factor includes fees AND slippage. Slippage is charged on the
  entry leg always and on the exit leg only when it is not a limit fill.

## Results

| # | hypothesis | held-out result | why it failed |
|---|---|---|---|
| 1 | VWAP repair: longer lookback + hold | gross PF 0.99 | edge did not survive the split |
| 2 | News verdicts predict forex | — | 2 of 598 verdicts touch any FX instrument, and the corpus post-dates the test window entirely |
| 3 | Fewer, better trades: session + volatility filters | gross PF 0.90 | worse than no filter |
| 4 | Time-series momentum, multi-day, wide stop | **flat-cost net +$675, real-cost net −$303** | 67% of entries in the 21-01 UTC rollover window; the profit was the cost model being wrong |
| 5 | The edge is in stocks not forex | later window PF 1.77 | the GOOD window was the later one and the BAD one earlier — a kind period, not an edge. 209 trades, net −$24 |
| 6 | Trend restricted to the liquid session | gross PF 1.10, real-cost net −$227 | small gross edge, entirely eaten by the true toll |

## Two mistakes in this work, both caught by an independent reviewer

**The harness burned its warm-up inside the test window.** `fx_trend` needs 760
bars; the test window has ~4,200, so 18% of it — all of January — was discarded
and the strategy scored on the remainder. That alone turned +$675 into −$311 and
a passing candidate into a rejected one. `BacktestSpec.act_from` exists for
precisely this and was not set. **H4's first rejection was wrong and has been
withdrawn**; it is rejected now for the cost-shape reason instead.

**The profit factor left slippage out**, understating every figure by 2.5-7%
while being compared against a bar that assumed a true gross number.

## What is actually established

- Gross edges here are real but small: profit factor 1.05-1.15 before costs.
- The true toll is larger than modelled and is not flat across the clock.
- Cost is the binding constraint, and it is worse than the flat model said, not
  better. Nothing found so far clears it.
- The experiment is also underpowered: per-trade mean +$3.10 against a standard
  deviation of $77, so detecting the observed effect at 95%/80% needs ~4,850
  trades and two years of four-pair hourly data yields ~375.

## Where the opportunity structurally is

Median absolute move at each horizon, divided by that lane's round-trip toll —
how many tolls a typical move is worth:

| lane | toll | 1h | 4h | 24h | 120h | 480h |
|---|---:|---:|---:|---:|---:|---:|
| forex | 1.6b | 3.2 | 6.7 | 19.0 | 44.5 | 87.9 |
| **stocks** | 7.0b | **4.2** | **11.0** | **31.0** | **69.0** | **187.1** |
| crypto | 56.0b | 0.5 | 1.0 | 2.7 | 6.4 | 15.7 |
| degen | 56.0b | 0.7 | 1.3 | 3.9 | 8.8 | 17.2 |

Two things fall out. Crypto and degen cannot work intraday at all — at one hour
the typical move does not even pay the toll — which is the entire explanation for
the degen lane's -$3,288 and needed no strategy research to see. And the horizon
effect is worth 25-45x in every lane, more than any filter or parameter has ever
been worth here.

## H7 — and the hole it exposed

Multi-day reversal on nine megacaps, on 22,599 daily bars synced for the purpose
(10 years). It looked outstanding: gross profit factor 5.08, +$7,675 on a $10,000
account.

Buy-and-hold on the same basket over the same window returned **+389.9%**. The
strategy returned **+76.7%** — one fifth of simply owning the assets. Longs made
+$8,664 and shorts lost -$990 during the largest megacap bull market on record.
Momentum, the OPPOSITE signal, also showed gross PF 1.91 on the same data: when
both directions look profitable, the measurement is picking up exposure, not
information.

**No test in this session had a benchmark until this one.** Net > 0 is a fair bar
in forex, where a currency pair has no long-run drift, and a trivially low one in
equities, where the assets rose 390% by themselves. `REQUIRE_BENCHMARK` is now
one of the challenger's rules, with tests.

Also visible in H7: 18 of 55 exits were the daily-loss kill switch. A third of the
trades ended because the risk overlay flattened the account, so what was measured
was the overlay as much as the strategy.

## H8 — cross-sectional, dollar-neutral, 66 names

Built to remove the drift that made H7 meaningless: long the 5 weakest, short the
5 strongest, equal dollars. 143,127 daily bars synced for it (66 US large caps,
10 years). On train the lookback=5 block was positive in all six (hold, k) cells
while 10/21/63 were mostly negative — a coherent block, and short-term
cross-sectional reversal is the most documented equity anomaly there is.

Held out: −6.23% annualised, and the whole block mostly negative
(−7.38, −0.87, −6.23, +1.66, −7.52, +0.56). Rejected.

## The one that matters: is it COST or SIGNAL?

Every candidate re-run at ZERO cost on the held-out window:

| candidate | net/day | gross PF |
|---|---:|---:|
| fx_trend | **+10.89** | 1.138 |
| ema_momentum | +7.97 | 1.135 |
| vwap lb=150 | +3.00 | 1.074 |
| vwap + session | +2.71 | 1.049 |

**All four are positive out of sample with a free broker.** The signals carry
real information. Cost, not signal, is the binding constraint — which is the
original day-one diagnosis, now confirmed properly on held-out data across four
independent strategies.

## The strongest candidate, and the single fact that decides it

`fx_trend`, held out, n=340, at flat cost per side:

| bps/side | 0.0 | 0.2 | 0.4 | 0.6 | **0.8 (the desk's model)** | 1.2 |
|---|---:|---:|---:|---:|---:|---:|
| net | +1209 | +1070 | +939 | +819 | **+675** | +413 |

Net positive out of sample at every flat cost tested, including 1.5x the desk's
own model. But 76% of its trades open in the 21:00-01:00 UTC rollover window,
where real spreads are worst.

**Breakeven rollover spread: 2.91x the London/NY rate.** Below that this is
profitable out of sample; above it, it is not. Everything else about the
strategy is settled; that one broker fact decides it, and it is not in hand.

Decomposition by hour, held out, at a uniform London-rate toll:

| entry window | n | gross | net |
|---|---:|---:|---:|
| rollover 21-01 | 260 | +441 | **−31** |
| liquid 07-20 | 58 | +458 | **+345** |
| Asia mid 02-06 | 22 | +310 | +268 |

The rollover trades are churn. H9 tried to confine the strategy to the liquid
window and re-searched its parameters there: 0 of 9 cells cleared on train, so
the held-out window was not spent. The 58-trade liquid slice is a favourable CUT
of a run, not a strategy — blocking rollover entries frees slots and cooldowns,
so the restricted version takes 700+ different trades and loses.

## H11 — the first candidate to survive out of sample

Everything above died on cost. So the design was changed to one where cost cannot
decide the answer: rebalance MONTHLY, not hourly. Twelve round trips a year
against three hundred.

Also fixed the other binding constraint — data. 60,265 daily FX bars synced
across 24 pairs over 10 years, roughly 25x what four pairs of hourly bars over
two years provided. And a daily bar is struck at the New York close, so the
unknown 21:00-01:00 rollover spread that decided H4 stops mattering entirely.

Momentum first, as the null: uniformly negative across 12 well-powered cells
(7,536 positions each). A consistent negative sign is evidence for the opposite,
so the sign was flipped.

**Held out 2023-01-01..2026-09-22, lookback 30, hold 21, one look:**

| round-trip cost | annualised | Sharpe | hit |
|---|---:|---:|---:|
| 3 bps (assumed) | **+1.18%** | 0.56 | 53.5% |
| 6 bps (doubled) | **+0.81%** | 0.38 | 53.5% |
| 9 bps (tripled) | **+0.45%** | 0.21 | 51.2% |
| 15 bps (5x) | −0.27% | −0.13 | 46.5% |

Robust in the ways that matter: four of five neighbouring lookbacks are also
positive out of sample, and dropping the three widest-spread pairs (USD/MXN,
USD/NOK, USD/SEK) makes it BETTER, not worse — +1.44% at 3 bps, Sharpe 0.69. So
it is not an execution artifact. lookback=30 was picked deliberately over the
argmax at 20.

It was not significant — t = 1.05 against the 2.02 needed at n=43. So rather than
defer that, the evidence was extended.

## H11 confirmed dead on 12 years never previously loaded

The Yahoo daily history cap turned out to be **ours, not the provider's**: a
hardcoded 3650 days in `data/yahoo.py` silently truncated every request for more.
The intraday caps there are real and were kept; the daily one was a default
pretending to be a limit. Lifting it loaded FX dailies back to 2002 — 6,419 bars
a pair instead of 2,600.

Parameters frozen on 2016-2022, nothing searched, applied to 2003-2015:

| window | periods | annualised | Sharpe | t |
|---|---:|---:|---:|---:|
| 2003-2015 (never seen) | 138 | **−0.95%** | −0.21 | −0.71 |
| at 2x cost | 138 | −1.31% | −0.29 | −0.98 |
| at 3x cost | 138 | −1.66% | −0.37 | −1.25 |
| 2023-2026 (the earlier look) | 43 | +1.18% | +0.56 | +1.05 |
| **pooled out-of-sample** | **181** | **−0.45%** | **−0.11** | **−0.43** |

The +1.18% was noise. n=43 could not tell; n=181 can. A well-powered negative is
worth more than an underpowered positive, and this is the first result all
session with enough sample to settle anything.

## Where twelve hypotheses leave it

No candidate has credible positive net expectancy after costs. What is
established rather than guessed:

- **Signals carry real information.** At zero cost, all four hourly forex
  candidates are positive out of sample (gross PF 1.05-1.14).
- **Cost is the binding constraint**, confirmed on held-out data across four
  independent strategies.
- **Horizon is worth 25-45x** more than any filter or parameter tried.
- **Crypto and degen cannot work intraday at all** — at one hour the typical move
  does not pay the toll. That explains the degen lane's -$3,288 without any
  strategy research.
- **The monthly FX reversion that survived one window does not survive two.**

## H12 — the news verdicts, tested properly

H1 established that the verdicts do not cover forex and stopped there. That was
too early: it answered "do they cover forex" and not "do they predict anything",
which is the question actually asked.

The grader had completed 54 rows, and that is not the constraint — the outcome
can be computed straight from bars for every verdict that named a symbol and took
a side. Entry at the next bar OPEN after `created_at`, so nothing is used that did
not exist at decision time. 131 such verdicts; 48 with bars covering the horizon.

Raw forward returns were negative at 1h and 4h. Subtracting the market over the
identical window (SPY for stocks, BTC/USD for crypto) flips them positive:

| horizon | n | market-adjusted | t |
|---|---:|---:|---:|
| 1h | 48 | **+9.98 bps** | 0.87 |
| 4h | 48 | **+13.05 bps** | 1.28 |
| 1d | 48 | **+23.26 bps** | 1.06 |

An eight-day rally had been hiding the signal, and the earlier score-vs-outcome
table that looked uniformly bad was reading market drift.

**The score ranks it backwards.** Market-adjusted 1d return by score:

| score | n | return |
|---|---:|---:|
| 3 | 18 | **+48.13 bps** |
| 4 | 16 | +21.01 |
| 5 | 8 | −19.36 |
| 6 | 4 | **−58.53** |

Correlation −0.269, monotone across all four levels. **The agent acts at score ≥ 5
— exactly the half with negative market-adjusted returns.**

Caveats that matter: n=48 over eight calendar days, no held-out window exists
because the corpus is eight days old, and no t-statistic reaches 1.3. The short
side is negative market-adjusted (−75 bps) and the long side carries everything.

Recorded as `forward_testing`, the first hypothesis this session not rejected.
The actionable part is a REDUCTION rather than an edge: the acting threshold
selects the losing half. Changing it is the owner's call.

## H13 — the clearest result of the thirteen

Conditional rather than always-on: act only when a pair has already moved past
2.5 standard deviations in a day, then take the other side for three days. 24
pairs, 24 years, 140,128 daily bars. Net of 3 bps throughout.

**Train 2002-2014**, and the structure is exactly what the mechanism predicts —
bigger overreaction, bigger reversal:

| z > | hold 1 | hold 3 |
|---|---:|---:|
| 2.0 | +7.85 bps, t=4.38 | +9.66, t=3.71 |
| 2.5 | +16.81, t=4.71 | +19.79, t=3.99 |
| 3.0 | +28.91, t=4.39 | +28.89, t=3.20 |

t above 4 on thousands of events, after costs.

**Held out 2015-2026**, same parameters, one look:

| cost | events | mean | t |
|---|---:|---:|---:|
| 3 bps | 1,609 | **−2.76 bps** | −0.84 |
| 6 bps | 1,609 | −5.76 | −1.75 |
| 9 bps | 1,609 | −8.76 | −2.66 |

Gone. And the opposite sign loses too (−3.24 bps), so there is no direction left
to take — the toll simply exceeds whatever remains.

**This one is not noise-fitting.** t=4.71 on 1,536 events is not a fluke that a
grid search manufactured; the effect was real in 2002-2014 and is absent in
2015-2026. That is decay, and it is the most economically coherent result in the
whole log: short-horizon FX overreaction was tradeable before algorithmic
market-making became ubiquitous, and it has been competed away.

## What thirteen hypotheses establish

No credible positive net expectancy is available to this desk from price-based
strategies on liquid instruments. That is not a failure to search — it is the
answer, and H13 shows the mechanism: the effects that were real have decayed.

The one live thread is H12, and it is live precisely because it is NOT
price-based: the news verdicts carry positive market-adjusted returns and the
score ranks them backwards. Its sample grows on its own.

## Next

1. **Re-run H12 when the corpus reaches a few hundred verdicts.** The news agent
   and the new macro_fx channel reach that in weeks, and it is the only line of
   enquiry that has produced a positive market-adjusted number.
2. Re-read every equity result here against a benchmark; several were scored
   against zero before `REQUIRE_BENCHMARK` existed.
2. The equity universe now has 66 names and 10 years but was only tested at
   daily-and-longer horizons on the cross-section. The move/toll table says
   equities at multi-day horizons is the best ratio available; H7 and H8 tested
   that badly (no benchmark, and a survivorship-biased universe) and it deserves
   one honest attempt.
3. Nothing here should reach paper until a candidate clears a window it has not
   been fitted to. None has.
