# Forex Live-Readiness — Go/No-Go

**Status: NO-GO.** The MoneyTree forex lane cannot trade real money.
**As of:** 2026-09-22 · MoneyTree v1.57 · 434 tests green
**Scope:** the `forex` lane (EUR/USD, GBP/USD, AUD/USD, NZD/USD), simulated throughout. No real order has ever been submitted by this system.

> **For a planning agent reading this file.** This is a source document, not a plan. It states verified facts and the work implied by them. Every figure came from the live database on the date above. Do not treat the five-day profit as evidence of readiness — see §3. Do not plan any step that enables live trading; that decision is the owner's alone and is listed as the final manual gate. Task IDs (`T1`–`T8`) are stable; reference them rather than restating.

---

## 1. Why it is a NO-GO

Three independent blockers. Clearing any one of them changes nothing about the others.

| ID | Blocker | Evidence |
|----|---------|----------|
| **L1** | **No forex broker adapter exists.** Alpaca does not offer currency trading and nothing else is integrated. | `main_app/services/agent.py` `setup()` raises `RuntimeError` for `market=forex` in `paper` and `live`. Tests: `main_app/tests/test_broker_conformance.py::NoForexAdapterExistsYet` |
| **L2** | **No strategy anywhere is qualified.** Paper and live load only strategies with `qualification='qualified'`. Desk-wide: 11 `unproven`, 2 `quarantine`, 0 `qualified`. | `eligible_strategy_rows()` in `agent.py`. A paper agent started today loads zero strategies and places zero orders. |
| **L3** | **Live arming is off.** | `LIVE_TRADING_ARMED` unset; `AgentConfig.mode = 'sim'`. Both required, plus typing `ARM LIVE`. |

L1 is an integration that does not exist, not a switch that is off. It is the long pole.

---

## 2. The finding that matters most

The desk's own promotion rules have **already failed both forex strategies that trade**, and nothing acted on it. Both are still enabled and taking positions.

| Strategy | Trades | Profit factor | Expectancy | Net | Stored → Assessed | Enabled |
|---|---:|---:|---:|---:|---|---|
| `ema_momentum` | 39 | **0.65** | −7.48 | −$291.69 | `unproven` → **`quarantine`** | **yes** |
| `vwap_reversion` | 47 | **0.78** | −5.53 | −$259.70 | `unproven` → **`quarantine`** | **yes** |
| `news_catalyst` | 0 | — | — | — | `unproven` → `unproven` | yes |

A profit factor below 1.0 means losses outweigh wins. `qualification_assessment()` returns `quarantine` for both; the stored value was never updated because nothing compared the verdict against the stored state.

This is now raised as a **critical finding every 15 minutes** by the operational reviewer (`check_key='missed_quarantine'`). The reviewer deliberately does **not** act on it: changing a strategy's qualification is a configuration change that review cycles are forbidden from making.

**Implication for planning:** any plan that treats the forex lane as "working and nearly ready" is starting from a false premise. Task **T1** must be resolved before T2–T8 are worth doing.

---

## 3. What the data does and does not show

### Last five trading days (forex, simulated)

| Date (ET) | Trades | Gross | Fees | Slippage | Net |
|---|---:|---:|---:|---:|---:|
| Thu 17 Sep | 17 | +119.17 | 79.32 | 43.39 | −3.54 |
| Fri 18 Sep | 8 | +119.13 | 37.32 | 18.20 | +63.61 |
| Sun 20 Sep | 3 | −59.75 | 14.98 | 11.96 | −86.69 |
| Mon 21 Sep | 16 | +194.65 | 79.11 | 38.40 | +77.14 |
| Tue 22 Sep | 20 | +155.46 | 83.69 | 40.22 | +31.55 |
| **Total** | **64** | **+528.66** | **294.42** | **152.17** | **+82.07** |

**Costs took 84% of gross.** Over the current scoring epoch (39 trades): gross +$290.36, costs $268.36, net +$22.00 — **92%**.

### What it does NOT show

- **Five days is not evidence.** Three up days, two down. The lane has never strung together three consecutive up days in its entire history.
- **Lifetime the lane is down.** 137 trades: gross +$358.36, costs $957.90, **net −$599.54**. The five-day window is the good part of a losing record.
- **The costs are modelled, not observed.** 0.5 bps fee + 0.3 bps slippage are assumptions about a venue that has not been chosen. Real retail spreads on AUD/USD and NZD/USD are wider. **If true round-trip cost exceeds ~1.5 bps, the lane is net negative and nothing else here matters.**
- **No live spread check is possible.** The Yahoo feed provides no bid/ask, so nothing can refuse a trade into a widened spread.

### Definitions (use these exactly)

```
net    = SUM(trade.pnl)                                   -- already NET of fees
fees   = SUM(trade.fees)
slip   = SUM(fill.realized_slippage_bps/1e4 * qty * price) -- SIGNED against the account
gross  = net + fees + slip
```
Slippage is not a separate charge — it lives inside the fill price. Count only fills belonging to **closed** trades; a fill from an open position has no P&L beside it and inflates gross.

---

## 4. What is built and verified

| Area | State | Where |
|---|---|---|
| Gross / fees / slippage / net accounting | **PASS** | `services/report.py::lane_costs` |
| Operational review — 10 checks, post-fill + every 15 min | **PASS** | `services/review/operational.py` |
| Critical finding halts new entries, durably, per lane; clears itself | **PASS** (demonstrated end-to-end on the running desk) | `Account.review_halt` → `agent.poll_controls` → `risk.blocks` |
| Neither review cycle can modify strategy or risk config | **PASS** (runtime-enforced, not documented) | `services/review/guard.py::readonly_config` |
| Findings dedupe across restarts; resolve only when re-checked clean | **PASS** | `services/review/findings.py` |
| Improvement review — sourced hypotheses, held-out test, adversarial challenger | **PASS** | `services/review/improvement.py` |
| Audit export — 6 joined tables, provenance per mode, explicitly not tax-ready | **PASS** | `services/audit_export.py` |
| Broker conformance contract, enforced on Sim **and** Alpaca adapters | **PASS** | `tests/test_broker_conformance.py` |
| Stale-bar gate refuses entries at decision time | **PASS** | `risk.py::max_bar_age_bars` |
| Two failing strategies still trading | **FAIL** | see §2 → **T1** |
| Fee reconciliation against a venue | **NOT VERIFIED** — built and inert; no adapter reports a running fee total | `services/reconcile.py` |
| Cash movements (deposits / withdrawals / financing / conversions) | **NOT VERIFIED** — ledger built and tested, no production writer | `models.CashMovement` |
| Real paper-trading walkthrough of order flow | **NOT VERIFIED** — structurally impossible while L2 holds | — |

### Independent review already performed
A fresh-context reviewer found **two of four safety claims false**; both fixed with regression tests (v1.57). Notable: a check that *declined to look* was closing findings it never confirmed and lifting halts; resolution was not scoped per lane; the post-fill reviewer never ran in paper/live at all; an epoch reset would have permanently bricked a paper lane. **Do not re-plan this review — it is done.** Plan the *next* one only after T3 lands.

---

## 5. Manual steps required before live activation

Ordered. Nothing below happens automatically; nothing in the application can do any of it unaided.

| ID | Task | Acceptance criteria | Depends on |
|----|------|---------------------|-----------|
| **T1** | Decide what to do about `ema_momentum` and `vwap_reversion` on forex | Each is quarantined, retuned under a preregistered train/test protocol, or knowingly accepted with the decision recorded. The `missed_quarantine` finding closes. | — |
| **T2** | Choose a forex broker and open the account | Credentials issued; published spread schedule obtained for all four pairs. Candidates: OANDA, IG, Interactive Brokers. | — |
| **T3** | Write the forex broker adapter | Passes every test in `BrokerConformance` against the venue's sandbox: no double-fill on a resubmitted `client_order_id`, no position reversal on duplicate close, partial fills preserved, every fill carries fee + slippage, no invented prices, `fills_seen` increments, `account()` exposes cash/equity/buying power. | T2 |
| **T4** | Re-measure the cost model against that venue's real spreads | Replace the 0.5/0.3 bps assumptions with observed figures; re-run the five-day and lifetime tables in §3. **Abort criterion:** if round-trip cost > ~1.5 bps, stop — the lane is negative. | T2, T3 |
| **T5** | Run in paper until a strategy qualifies | Bar: 20 sessions, 30 trades, profit factor ≥ 1.3, max drawdown ≤ 8%, expectancy ratio ≥ 0.7, plus validated held-out research. Nothing has ever cleared it. | T1, T3, T4 |
| **T6** | Build spread and stale-quote gates against the real feed | An entry into an abnormally wide spread is refused and recorded. (The bar-age gate already exists.) | T3 |
| **T7** | Wire the cash ledger to the broker's transaction feed | Deposits, withdrawals, financing and conversions land in `CashMovement` with the venue's own reference; `reconcile.compare()` reports no unexplained cash on a flat book. | T3 |
| **T8** | Owner explicitly chooses account, funding amount and risk limits, then arms | Three separate deliberate actions: set `LIVE_TRADING_ARMED=1`, switch mode in Settings, type `ARM LIVE`. **Owner-only. Never plan an agent to perform this.** | all of T1–T7 |

---

## 6. Constraints binding on any plan derived from this document

- **Never enable live trading, raise a risk limit, or change a strategy's qualification autonomously.** These are owner decisions. Review cycles are runtime-blocked from all three.
- **No manual per-trade approval may be introduced.** The desk is fully autonomous by design; adding a human gate per trade is a regression, not a safeguard.
- **Do not reset the scoring epoch to make results look better.** A reset moves the starting line and has previously erased an earned quarantine.
- **Report gross, every cost, and net separately, always.** A positive gross must never be allowed to conceal an expensive strategy.
- **Held-out data is read once.** A candidate tuned until the held-out window agrees is not a finding.
- Money is simulated throughout. Treat every cost figure as a modelled assumption until T4 replaces it with an observed one.

---

## 7. Provenance

Generated from the live MoneyTree database on 2026-09-22 at v1.57.
Companion artifact (same content, formatted for reading): the Forex Go/No-Go report.
Reproduce the figures with `lane_costs()` in `main_app/services/report.py` and `qualification_assessment()` in `main_app/services/promotion.py`.
