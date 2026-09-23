# Forex research log — 2026-09-23

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

## Next

Not another signal against the same design. Either get real per-hour spread data
from an actual venue to replace the assumed multipliers, or find a signal whose
moves are large enough that a 3x rollover spread does not decide the outcome.
