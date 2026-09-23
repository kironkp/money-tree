# The forex lane is not ready for real money

**Readiness review · 22 September 2026 · MoneyTree v1.57**

Not because a check failed. Because it cannot place a real order at all, and because the two strategies that trade it have already been measured as having no edge.

---

## The three locks

**L1 — There is no forex broker.**
Alpaca does not offer currencies, and nothing else is integrated. `agent.setup()` refuses forex in paper and live for this reason. This is an integration that does not exist, not a switch that is off.

**L2 — No strategy anywhere is qualified.**
Paper and live mode load only strategies marked `qualified`. Across all four lanes, 11 are `unproven` and 2 are `quarantine`. A paper agent started today would load zero strategies and place zero orders.

**L3 — Live arming is off.**
`LIVE_TRADING_ARMED` is unset and `AgentConfig.mode` is `sim`. Both are required, and typing `ARM LIVE` is required on top.

Each lock is independent. Clearing one changes nothing about the others — a test asserts that even with live armed, forex still has no venue to send an order to.

---

## What five days of forex actually shows

The lane made money. Almost all of what it made went to the toll.

| Date (ET) | Trades | Gross | Fees | Slippage | Net |
|---|---:|---:|---:|---:|---:|
| Thu 17 Sep | 17 | +119.17 | 79.32 | 43.39 | −3.54 |
| Fri 18 Sep | 8 | +119.13 | 37.32 | 18.20 | +63.61 |
| Sun 20 Sep | 3 | −59.75 | 14.98 | 11.96 | −86.69 |
| Mon 21 Sep | 16 | +194.65 | 79.11 | 38.40 | +77.14 |
| Tue 22 Sep | 20 | +155.46 | 83.69 | 40.22 | +31.55 |
| **Total** | **64** | **+528.66** | **294.42** | **152.17** | **+82.07** |

Over five days costs took **84%** of gross. Over the current scoring epoch (39 trades): gross +$290.36, costs $268.36, net +$22.00 — **92%**.

Slippage is measured from the gap between decision price and fill on every recorded fill, not assumed.

### What it does not show

- **Five days is not evidence.** Three up days, two down. The lane has never strung together three consecutive up days in its whole history.
- **Lifetime, the lane is down.** 137 trades: gross +$358.36, costs $957.90, **net −$599.54**. The five-day window is the good part of a losing record.
- **The costs are modelled, not observed.** 0.5 bps fee and 0.3 bps slippage are assumptions about a venue that has not been chosen. A real retail forex spread on AUD/USD or NZD/USD is wider. If the true cost is double the model, the lane is negative.
- **No live spread check is possible.** The Yahoo feed gives no bid/ask, so nothing can refuse a trade into a widened spread. That gate has to be built against a real venue.

---

## The finding that matters most

The desk's own promotion rules have already failed both forex strategies that trade. Nothing had acted on that, and both are still enabled and taking positions right now.

| Strategy | Trades | Profit factor | Expectancy | Net | Stored → assessed |
|---|---:|---:|---:|---:|---|
| `ema_momentum` | 39 | 0.65 | −7.48 | −291.69 | `unproven` → **`quarantine`** |
| `vwap_reversion` | 47 | 0.78 | −5.53 | −259.70 | `unproven` → **`quarantine`** |
| `news_catalyst` | 0 | — | — | — | `unproven` → `unproven` |

A profit factor below 1.0 means the losses outweigh the wins. Both are below it on samples large enough for the existing rules to call it. The reviewer now raises this as a critical finding every 15 minutes and emails it — and deliberately does not act on it, because changing a strategy's qualification is a configuration change that review cycles are forbidden from making.

> **This is the direct answer to "forex is the lane that's working."** The *lane* was up $82 over five days. The *strategies* inside it measure as having no edge over their full forward samples. Both statements are true, and the second one is the one that predicts next month.

---

## What was built, and what it proved

| Status | Item | Detail |
|---|---|---|
| **PASS** | Full cost accounting — gross, fees, slippage, net | Slippage was never reported before and is not a line item anywhere; it lives inside the fill price. It is now measured per fill, signed, and tied to the same closed round trips that produce net. A lane can no longer look profitable while handing over most of what it earns. |
| **PASS** | Operational review — 10 checks, after every fill and every 15 minutes | Duplicate orders, acted signals with no order, stuck orders, positions against the venue, open-position / exposure / daily-loss limits, stale data, reconciliation age, cost share, repeated errors, naked positions, and strategies the desk has already failed. |
| **PASS** | A critical finding halts new entries, durably, and clears itself | Demonstrated end to end on the running desk: a duplicate order was injected, the reviewer flagged it, the lane halted, the live agent picked it up within seconds and refused entries, the fault was removed, the check ran clean, the finding auto-resolved and the halt lifted. Open positions keep their stops throughout. |
| **PASS** | Neither review cycle can change what it reviews | Enforced at runtime, not promised in a comment: both cycles run inside a guard that makes strategy settings, risk limits and instruments unwritable, so a reviewer that tries to tune a parameter crashes its own run and files a finding about itself. |
| **PASS** | Findings survive restarts without forgetting or repeating | Deduplicated on a fingerprint built from the problem, not the sighting. A second pass over the same desk opened 0 and repeated 31. A check that crashed — or that declined to look — may not resolve anything it did not confirm. |
| **PASS** | Improvement review — daily, proposes and never applies | A hypothesis with no source is refused outright. Evidence is searched on a train window and the held-out window is read once. A deterministic challenger then tries to reject it, and an optional model second opinion may add objections but never clear one. |
| **PASS** | Audit export — six joined tables, honestly labelled | Signals, orders, fills (partial fills kept separate), trades, cash movements and the reconciliation history, followable from signal to order to fill to the venue's own id. It states its own provenance per mode and explicitly says it is *not* a tax export. |
| **PASS** | A broker conformance contract, enforced on both adapters | Any forex adapter must pass before it may carry money: no double-fill on a resubmitted order id, no position reversal on a duplicate close, partial fills preserved, every fill carrying a fee and a slippage measurement, no invented prices. |
| **FAIL** | Two strategies trading against the desk's own verdict | Described above. This needs a decision from you; the reviewer is forbidden from making it. |
| **NOT VERIFIED** | Fee reconciliation against a venue | The comparison is written and inert: neither existing adapter reports a running fee total, so only a test double exercises it. It is a requirement on the forex adapter, not working machinery. |
| **NOT VERIFIED** | Deposits, withdrawals and currency conversions | The ledger and its idempotency are built and tested; nothing in the application writes to it yet, because nothing moves cash. On a funded margin account, financing charges land nightly and will need a feed. |
| **NOT VERIFIED** | A real paper-trading walkthrough of order flow | Impossible today, for a structural reason rather than an oversight: no strategy is qualified, so a paper agent loads nothing. The control flow was demonstrated on the live simulator instead, and the paper path is proven to be correctly gated. |

---

## The independent review

A reviewer with no prior context was given the work and asked to break it. It found two of the four safety claims false. Both are fixed, with regression tests.

- **A check that declined to look was closing findings it never confirmed.** The scheduled job runs with no broker handle, so the position comparison returned immediately — and that silence resolved the divergence finding the live agent had just raised, then lifted the halt. The lane would have resumed trading against books that still disagreed with the venue.
- **Resolution was not scoped to the lanes under review**, so four agents reviewing themselves continuously erased each other's evidence.
- **The post-fill reviewer never ran in paper or live at all** — it read a list only the simulator keeps. It survived because the conformance suite was only ever pointed at the adapter whose internals it assumed.
- **An epoch reset would have bricked a paper lane permanently**, blocking every entry with a reconciliation error that could never clear.
- **The audit export told an accountant that a live account was simulated.**

Also fixed: a fill landing inside the throttle window was never reviewed; the whole subsystem was pointed only at simulated accounts, making the reconciliation checks unreachable; open positions distorted the cost figures; synchronous email inside the trading tick; one standing fault emailing 96 times a day; and five new tables missing from the nightly backup.

---

## What you have to do by hand before live

In order. Nothing below happens automatically, and nothing in the application can do any of it on its own.

1. **Decide what to do about the two failing strategies.** They are trading now and the desk has already measured them as having no edge. Quarantine them, retune them under the train/test protocol, or accept them knowingly. Leaving them is also a decision.

2. **Choose a forex broker and open the account.** OANDA, IG or Interactive Brokers are the realistic options. Get the API credentials and the published spread schedule for the four pairs traded.

3. **Have the adapter written and pass the conformance suite.** Nine guarantees, already executable. Point `BrokerConformance` at the new adapter against its sandbox. Every test must pass before the lane can be armed.

4. **Re-measure the cost model against that venue's real spreads.** The whole case for the lane rests on 0.8 bps a round trip. Replace the assumption with the venue's number and re-run the five-day and lifetime figures. If costs exceed roughly 1.5 bps, the lane is negative and nothing else on this list matters.

5. **Run it in paper until a strategy qualifies.** The bar is 20 sessions, 30 trades, profit factor 1.3, max drawdown 8%, expectancy ratio 0.7 — plus validated held-out research. Nothing has ever cleared it.

6. **Build the spread and stale-quote gates against the real feed.** The staleness gate exists and works on bar age. A spread gate cannot be written until there is a bid/ask to read.

7. **Wire the cash ledger to the broker's transaction feed.** Deposits, withdrawals, financing and conversions, so reconciliation has something to compare against instead of assuming they never happen.

8. **Choose the account, the funding amount and the risk limits — explicitly.** Then set `LIVE_TRADING_ARMED=1`, switch the mode in Settings, and type `ARM LIVE`. Three separate actions, deliberately.

---

> **One thing not to conclude.** The lane being up five days running is not evidence that any of this is ready. It is 64 trades on simulated costs against a venue that has not been chosen, by two strategies the desk has already failed. The safeguards are real and tested; what they are protecting is not yet proven to be worth protecting.

---

*MoneyTree v1.57 · 434 tests green · 46 test files. All figures from the live desk on 22 Sep 2026. Money is simulated throughout; no real order has ever been submitted.*
