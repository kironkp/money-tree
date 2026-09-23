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

## Next

1. Re-read every equity result in this session against a benchmark; several were
   evaluated against zero.
2. Get real per-hour spread data from an actual venue to replace the assumed
   1x/1.5x/3x multipliers, which currently decide the forex answer.
3. The measurement points at equities at multi-day horizons. The honest version
   of that test is excess return over buy-and-hold, on a universe wide enough to
   be cross-sectional rather than nine names that all went up together.
