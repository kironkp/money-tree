# MoneyTree — session handoff, 23 Sep 2026

Written to be pasted into a fresh chat. Assumes no prior context.

**What MoneyTree is:** a Django autonomous day-trading agent at `~/code/moneytree`, running on
**fake money** across four lanes (stocks, crypto, degen, forex). Four agent processes run
continuously in sim mode. No real order has ever been placed. Version **1.71**, **461 tests green**,
17 commits today, everything pushed to `github.com/kironkp/money-tree`.

---

## 1. What got built and finished

**Cost accounting that can't hide anything.** `lane_costs()` now reports gross → fees → slippage →
net separately. Slippage was never reported before and isn't a line item anywhere — it lives inside
the fill price. It's now *measured* per fill from `Fill.realized_slippage_bps`, signed, scoped to the
same closed trades that produce net, and it says whether the number was measured or assumed.

**Two review cycles, deliberately separate.**
- *Operational* — 10 checks, after every fill and every 15 minutes: duplicate orders, acted signals
  with no order, stuck orders, positions vs the venue, position/exposure/daily-loss limits, stale
  data, reconciliation age, cost share, repeated errors, naked positions, and strategies the desk has
  already failed. A critical finding **halts new entries** on that lane, durably, per-lane. Open
  positions keep their stops.
- *Improvement* — daily. Sourced hypotheses only, train/held-out discipline, a deterministic
  challenger that tries to reject.
- **Neither can change what it reviews.** Enforced at runtime, not promised: both run inside
  `readonly_config()`, so a reviewer that tries to tune a parameter crashes its own run.

**Accounting.** `CashMovement` (deposits/withdrawals/conversions/financing, idempotent on the
broker's own reference), `ReconciliationSnapshot` (positions *and* cash *and* equity *and* fees, every
check kept, agreements included), and an audit export — six joined tables, followable from signal to
order to fill to the venue's id, which states its own provenance per mode and explicitly says it is
**not** a tax export.

**Broker conformance contract.** Executable tests any forex adapter must pass before its lane can be
armed: no double-fill on a resubmitted order id, no position reversal on duplicate close, partial
fills preserved, every fill carrying a fee and a slippage measurement, no invented prices. Run against
both SimBroker and AlpacaBroker.

**Observability.** `/review` shows both cycles: last run, next run, checks run and crashed, open
findings, actions taken, current blockers, and every hypothesis with its evidence.

**Scheduled:** operational review every 15 min, improvement daily 18:00, plus a **cloud routine**
(`MoneyTree nightly audit`, 02:30 PT daily) that audits the nightly backup snapshot.

**Bugs found and fixed** (the ones that mattered):
- Entry decisions were made per-symbol in alphabetical order, so a slot freed by a late-alphabet
  symbol was invisible to every symbol ahead of it. 6 of 12 stock refusals ever were against an
  already-free slot.
- `trade_short` sat in the stocks config and was read by nothing — the config said "no shorts" while
  the strategy shorted.
- Entry signals never linked to the orders they produced (30 of 33 in one day).
- An epoch reset would have permanently bricked a paper lane.
- The audit export told an accountant a live account was simulated.
- `cooldown_h` was wiped nightly, so every value above 24h behaved as 24h.
- Yahoo daily history was capped at 10 years by *our* code, not the provider — lifting it gave 24
  years of FX data.
- The map/state reported 192 "graded" news verdicts when 54 existed.

---

## 2. The research: 18 hypotheses, one survivor

Tested across FX majors and crosses (24 pairs, 24 years), equities (66 names, 10 years), crypto
(10 coins, 5 years), the news arm, and execution style. Every one used time-ordered training, an
untouched later window, and cost stress at 2× and 3×. All recorded in-app at `/review` so none gets
repeated or cherry-picked.

**15 rejected. 3 in forward testing.**

### The one real candidate: H10

`fx_trend` — time-series momentum on FX majors, entries restricted to 07:00–21:00 UTC, 168h cooldown,
4 ATR stop, held to the week's close.

| | n | net | $/day | gross PF | captured vs cost |
|---|---:|---:|---:|---:|---|
| TRAIN | 176 | +$1,193 | +14.21 | 1.40 | 7.95 vs 1.60 bps |
| **HELD OUT** | **100** | **+$1,082** | **+23.02** | **1.583** | **10.93 vs 1.60 bps** |
| held out, 3× cost | 100 | +$727 | +15.46 | 1.60 | — |
| held out, 5× cost | 100 | +$354 | +7.54 | 1.60 | — |

It **improves out of sample** and captures nearly seven times the toll. It survives a realistic
hour-shaped spread model (1× London/NY, 1.5× Asia, 3× rollover): **held out +$917**. Only **7%** of
its entries fall in the wide-spread rollover window.

**It is not proven.** n=100, per-trade **t = +1.44**. That is a positive result, not a significant
one. Status: forward_testing.

### What was established along the way

- **The signals carry real information.** At zero cost, all four forex candidates are positive out of
  sample (gross PF 1.05–1.14). Cost, not signal, is the binding constraint.
- **Horizon is worth 25–45×** more than any filter or parameter tried.
- **Crypto and degen cannot work intraday at all** — at one hour the typical move doesn't even pay the
  toll. That fully explains the degen lane's −$3,288 and needed no strategy research.
- **Passive limit orders are 9× worse, not cheaper** — adverse selection costs ~42 bps against ~3 bps
  of spread saved.
- **The LLM research arm agrees independently.** It has never taken a position in its life, because
  every dossier forecasts `p_positive_net` of 0.23–0.37 — it won't trade these either.

---

## 3. Mistakes I made, so you can calibrate

Two independent reviewers were run. Both found real defects in my own work.

1. **My harness leaked test data into train.** I set the window start and never the window end — 38%
   of "train" trades were test trades. This caused me to **wrongly reject H10**, the one candidate
   that works. Fixed, and H10 revived.
2. **Earlier, the same bug class in reverse:** warm-up was consumed *inside* the test window,
   discarding 18% of it. That turned a +$675 result into −$311 and produced a different wrong
   rejection.
3. **My significance bar was unreachable.** Requiring a bootstrap lower bound above zero meant
   requiring an annualized Sharpe of 3–4.5; a good live programme runs 0.7–1.2. It was the only bar
   that ever bound, and it killed both candidates that passed everything else.
4. **I overstated H13.** Claimed "t=4.71 is not something a grid search manufactures" — the events are
   correlated pairs firing on the same days; clustered properly t falls to 1.81.
5. **I applied a double standard**, keeping H12 alive at n=48 while rejecting H17 at n=255.
6. **I overstated the conclusion.** "No credible edge exists" was wrong; the correct claim is "no
   strategy demonstrated positive expectancy at 95% confidence on the samples available," which my own
   power analysis said was near-guaranteed either way.
7. **I told you 575 graded news verdicts.** 598 exist; **54** are graded. ~10× overstatement, and it
   was load-bearing.

An implausibly good number was a bug every single time it appeared.

---

## 4. Decisions waiting on you

**1. The forex strategies that are still trading and still losing.** `vwap_reversion` and
`ema_momentum` on forex both assess as `quarantine` under the desk's own promotion rules — measured no
edge — but are stored as `unproven` and are **enabled right now**. The reviewer has flagged this 74
times. `vwap_reversion` lost **$197 in 5 trades** in the 18 hours after I first raised it. Forex this
epoch: 46 trades, gross +$37.32, costs $322.20, **net −$284.88**.
Options: quarantine them, retune them, or knowingly accept it. I'm locked out of changing strategy
qualification by design.

**2. Paper-test H10.** It's the only candidate that's earned one. Paper is where n grows without
risking anything, and the existing gate (20 sessions, 30 trades, PF ≥1.3, max DD ≤8%) is the right
bar. **Note: forex cannot run in paper — there is no forex broker adapter.** Alpaca doesn't offer
currencies. That's the blocker.

**3. Research budget.** Currently **$0.20/day**, which buys ~4.8 dossiers. 64% of attempts are
refused for budget at $0.00 each. At $1/day ($30/month) the news-arm sample reaches a testable size in
13 days instead of 63.

**4. Rotate the exposed keys.** Alpaca paper keys and a Resend key were pasted into chat earlier in
this project's history.

---

## 5. Honest state

**Nothing is ready for real money, and forex structurally cannot be** — no broker adapter exists, no
strategy anywhere is `qualified`, and live arming is off. Those are three independent locks.

**What's genuinely good:** the machinery is real and tested. Cost accounting that can't flatter
itself, review cycles that provably can't alter what they review, durable halts demonstrated live on
the running desk, an audit trail, and a conformance contract. That work stands regardless of whether a
strategy ever qualifies.

**What's genuinely uncertain:** H10 is a positive held-out result that survives realistic costs, at
n=100 with t=1.44. That is worth forward-testing and is not worth funding. The difference matters.

**What I'd stop claiming:** that there's definitely no edge here. Eighteen tests on underpowered
samples can't establish that, and the one candidate that survived a clean test argues against it.

**Full detail:** `docs/forex-research-log.md`, `docs/forex-live-readiness.md`,
`docs/forex-go-no-go-report.md`, and every hypothesis with its evidence at `/review`.
