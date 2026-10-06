# MoneyTree Logbook

This is the running, evidence-first record of MoneyTree releases. Each phase
records the observed problem, its cause, what changed, how it was verified,
and what remains unproven. A green test suite means the software behaves as
specified; it does **not** mean a trading strategy is profitable.

## Release 1.72 (2026-10-06) — Fix the losers, reset their numbers, keep them trading

Status: forex applied; degen measured, no fix found. Nothing is qualified by
this, and every installed version is unproven.

### What was wrong (lifetime sim trades)

- **Forex vwap_reversion** (144 trades): net −$1,040.54, gross −$430.22 (PF 0.86
  gross, 0.70 net). It loses before costs too. 54% of exits are stops (−$2,902).
  Median R is −1.03, yet 53% of trades reached +1R MFE. Longs lost $1,137 while
  shorts made $97. Asian-session entries lose most (22:00 ET: −$440).
- **Forex ema_momentum** (107 trades): net −$594.61, gross −$110.61 (PF 0.95
  gross). Costs ($484, about 1 bp a trade on ~$47k notional) are what sink it.
  25% of entries never reach 0.25R MFE. Entries at 17:00 and 20:00 ET lose most.
- **Degen ema_momentum** (35 dust trades): −60.5 bps a trade, which is −10.5 bps
  gross less 50 bps of costs. Time exits (29%) pay a full round trip for a flat
  move.

### What changed in the machinery

- `entry_session` (ema_momentum, vwap_reversion): `all`, `skip_asia` (no entries
  19:00–02:59 ET) or `london_ny` (entries 03:00–11:59 ET only). It is coarse on
  purpose: a free per-hour subset would fit the noise. The engine filters entries
  only, never exits. `Param.search=False` keeps nightly grids at `all` unless an
  experiment asks for the presets.
- `Experiment.risk_overrides` (migration 0031): a walk-forward can run under
  another lane regime (`min_reward_to_cost`, `max_hold_minutes`) without touching
  the live config.
- `services/fix_loop.py` and nightly auto-research: a `retry_pending` sim
  strategy that has run 30 trades or 14 days on its version gets its fix. It is
  promoted normally if the gate passes. Otherwise its latest pick is installed as
  a new sim version (`evidence.kind='sim_fix'`), but only if the selection
  procedure's clean adaptive OOS beats the current params, run fixed under the
  same regime on the same test windows, on both net and PF. Otherwise "no fix
  found". Nothing is ever disabled. The installed params are the procedure's
  latest pick, not separately validated. Replaying that pick fixed over every
  window is not used, because its training data overlaps the earlier test
  windows (forex vwap: +$596 fixed vs +$97 adaptive).
- `manage.py fix_lane --market X [--random N]` measures 8 lane regimes
  (timeframe {15Min, 1Hour} × min_reward_to_cost {current, 2×} × max_hold
  {current, 1440}), and `--apply` chooses one by combined adaptive-OOS net. A lane
  moves only if the new regime beats its current one.

### Forex (window 2026-08-09..10-06, train 21 d / test 7 d, 30 random combos)

Adaptive OOS net (ema / vwap / combined): current 15Min·2·240 −535.26 / −10.11 /
−545.37; **15Min·2·1440 −240.73 / +96.63 / −144.10 (chosen)**; 1Hour·2·1440
+465.36 / −618.90 / −153.54. Every regime is negative combined. ema works only at
1Hour, and vwap only at 15Min with a long hold. That is the case for
per-strategy timeframes, planned separately.

Applied: `forex_max_hold_minutes` 240 → 1440. Both strategies are v2 with
`entry_session=london_ny`. The comparison is like for like: the v1 params were
replayed only on the test windows the procedure traded (3–5 of 5). ema: the
procedure scored −240.73 (PF 0.81, 48 trades) against −915.45 (PF 0.65, 96
trades) for v1. vwap: +96.63 (PF 1.06, 70 trades) against −216.57 (PF 0.93, 134
trades). Neither passed the gate. Evidence restarts at n=0, and no trade was
deleted.

### Degen (window 2026-06-08..10-06, train 30 d / test 10 d, 9 windows, 5 random combos)

Measured on full-size fills (after the dust fix). In every regime, for both ema_momentum
and burst, nearly every training window's best combination lost after costs,
so the procedure abstained (best training PF: burst 0.34–0.59, ema 0.11–0.92,
all net negative). Combined adaptive OOS: current 15Min·3·180 0.00; 15Min·3·1440
−150.98; 15Min·6·180 −22.15; 15Min·6·1440 −133.01; every 1Hour regime 0.00.
Burst made no sense on 1Hour bars either. Result: regime kept, "no fix found"
for both, nothing installed and nothing disabled. Both keep trading at full size
under the never-stop policy, and the nightly loop retries them as their guards
come due. The search was thin (5 combos a window, because of machine load), but
with every window's best PF below 1 it is unlikely a wider one would differ.

**Holdout used:** this selection ran over 2026-09-08..09-24, the window MT-A003
had kept untouched. It is logged in both `docs/mt-a003-*.json` files
(`selection_use_log`). No later claim may treat that window as untouched.
Promotion to paper or live still needs fresh forward evidence under the existing
gates.

**Side effect, caught and fixed the same day:** news-verdict grading read the
lane's `max_hold`, so the 1440 change would have silently moved the
preregistered evaluation's forex horizon from 4 h to 24 h, and the fingerprint
couldn't see it. The grading horizon is now the constant
`news_risk.GRADE_HOLD_MINUTES` (stocks/crypto/forex 240, degen 180). It is
deliberately left out of the fingerprint, which is unchanged at
`dc5de471d4f6`. news_catalyst's own trades keep that hold through a strategy-level
time exit. No forex verdict had been graded on the 1440 horizon, so none needed
regrading.

## Release 1.72 (2026-10-06) — In sim, failing never means stopping

Status: policy changed by the owner ("fix it and then reset their numbers and try
again. Stopping altogether is never an option. It's not real money right now.").
Paper and live gating is unchanged.

### Why the quarantine never fired

Forex vwap_reversion (108 trades, PF 0.66, −$932.11), forex ema_momentum (92
trades, PF 0.70, −$654.89) and degen ema_momentum (35 trades, PF 0.13) all
assessed as quarantine, but their stored rows still said `unproven`. The
operational review had raised `missed_quarantine` 925 times. Its cause was two
bugs sharing one key:

- `refresh_qualification` runs only from `write_eod_journal`, and the 24/7 lanes
  had not journalled since 2026-09-15. At startup, an agent treats today as
  already journalled if a `JournalEntry(kind='auto_eod', date=today)` exists.
  Nightly auto-research wrote its "Auto-research …" rows under that same kind at
  02:10 and then restarted every lane. Each lane booted believing its journal was
  done. A 24/7 lane journals only when `journal_done < yesterday`, so a process
  had to survive two ET midnights, and it was restarted every morning before the
  second. Stocks was hit the same way through `eod_done`.
- From 09-09 to 09-15, when a journal did run, `update_or_create(date,
  kind='auto_eod', account)` raised `MultipleObjectsReturned` on the research
  rows (forex had 29 such dates, degen 28).

Fix: auto-research writes `kind='research'`, and migration `0030` relabels the
366 existing rows by their "Auto-research" title. Nothing is deleted. The agent's
startup check is now `Agent._eod_already_written`, which counts only real EOD
rows. Nightly auto-research also refreshes every Sprout strategy's verdict, so a
missed journal can't hide a failing strategy again.

### The new policy

- Sim and replay: a failing verdict (no edge after 30+ trades, or a lifetime
  record of 150+ trades with PF < 1) is still stored, but it no longer sets
  `enabled=False` and the lifetime halt is not persisted. The strategy keeps
  trading, `Strategy.retry_pending` is true, and a `RiskEvent(kind='retry')`
  records it. Drift no longer disables in sim either.
- The sim agent loads quarantined rows. A qualification change no longer counts
  as a configuration change in sim (in paper/live it still blocks entries until
  a restart), so a verdict written at the EOD journal can't stop the lane.
- Paper and live: unchanged. They still require `qualified`, quarantine still
  disables, the lifetime halt is still persisted, and a failing record still
  blocks graduation.
- The operational review reports a sim strategy that should be failing but isn't
  marked yet as a WARN, "failing — retry pending". It no longer raises a CRITICAL
  every 15 minutes.
- Re-enabled in sim, because automation had disabled them: degen burst (lifetime
  brake; the halt record stays), stocks orb (quarantine 2026-09-17), and stocks
  ema_momentum (drift auto-disable 2026-10-05). Left off, because it was disabled
  deliberately: stocks vwap_reversion ("DISABLED — it would only drain the seed",
  2026-09-01).

## Release 1.72 (2026-10-06) — Crypto fills were dust; research on them is suspect

Status: simulator fix landed; every crypto and degen research result before
it is suspect and has not been re-run

### Evidence

The degen lane read $0.00 because its fills were dust. The cards planned the
right size, and the simulator then filled a sliver of it:

- card 646 ADA: planned 7,450.63 (~$2,000), filled 0.646 (~$0.17)
- card 623 LINK 139.5 → 0.94; card 622 ADA 7,982 → 4.21; card 602 AVAX
  180.8 → 0.049

Sim TradeCards from the last 30 days, `filled_qty / qty`:

| lane | cards | Σfilled/Σplanned | median | under 10% |
|---|---|---|---|---|
| degen | 138 | 0.82 | 1.00 | 61 |
| crypto | 5 | 0.18 | 0.24 | 0 |
| forex | 252 | 1.00 | 1.00 | 0 |
| stocks | 86 | 1.00 | 1.00 | 0 |

The degen median hides a step change. From 09-16 onward, all 37 degen cards
filled less than 10% of their size. SOL and XRP never filled more than 2%.

### Cause

`SimBroker` limited each fill to `liquidity_cap_pct` (1%) of the bar's volume,
cut the order to that, and held the dust that remained. Alpaca's crypto venue
volume is a small sliver of the market's trades, which is also why pulse
quotes the mid instead of using trades (v1.3). Its volume does not measure the
liquidity actually available.

### What changed

- `broker/sim.py`: the cap is set per asset class through
  `LIQUIDITY_CAP_BY_CLASS`, and crypto is `0` (no cap). This covers both the
  crypto and degen lanes. Forex is `0` as well: spot FX has no centralized
  volume, and all 271,712 stored Yahoo forex bars report 0, so in production
  this changes nothing. It does stop a fixture or a future tick-volume feed
  from turning forex entries into dust. Size is still bounded by
  `max_position_pct`. The h10 forward/shadow test fixtures (EUR/USD at
  volume 1000) had been passing on 10-unit dust fills; they now fill at
  full size. Every
  place that builds a `SimBroker` (the sim agent, replay, backtests and
  walk-forwards) inherits this default.
- An entry that the cap would cut below `min_fill_pct` of its size (10%, the
  same value as `RiskConfig.min_entry_size_pct` from v1.32) is now canceled
  whole as `liquidity: would be dust`. Before, it was partially filled.
- Backtest `config_snapshot` now records `liquidity_cap_by_class`.

### What is now suspect

Every crypto and degen backtest, experiment and walk-forward run before this
change filled through the same 1% cap: 630 crypto and 590 degen backtests,
and 91 crypto and 80 degen experiments. Their P&L, trade counts and profit
factors may describe dust positions, not the strategies. No result from those
lanes should be relied on until it is re-run. Stocks and forex fills were
unaffected (ratio 1.00).

## Release 1.32 — Stop the bleed before searching for edge

Status: engineering safeguards complete; profitability remains unproven

Safety boundary: no strategy was promoted, no account was reset, no real or
paper order was submitted, and losing history remains intact as evidence.

### Evidence

The 2026-09-07 ledger snapshot contained 213 closed simulator trades:

- Degen: −$2,210.42 over 165 trades, including $1,698.07 in modeled fees;
  Burst was already quarantined by v1.30.
- Forex: −$140.55 over 13 closed trades plus four open cards. Two meaningful
  same-direction positions consumed about the full 10× buying-power ceiling;
  the other two were dust orders created from leftover capacity. Two restart
  exits contributed −$31.85 and were not strategy decisions.
- Stocks: −$0.44 over 33 trades. ORB was +$33.38 while EMA was −$33.82; both
  samples remain too small and unproven.
- Crypto: −$1.78 over two trades, far too little evidence to judge.

All 127 pre-change tests passed. The application was operational; the red
accounts reflected weak strategies plus missing portfolio/restart guardrails,
not a single crashing code path.

### What changed

- Untouched default Forex strategies are parked at Seed and disabled because
  corrected walk-forward research found no valid held-out candidate. Fresh
  installs no longer auto-enable Forex or Degen defaults.
- A 5×-equity directional cap now aggregates every USD-quoted Forex position.
  Total leverage may still reach 10× only when opposing USD directions offset.
- Pending entries reserve slots and exposure, and a symbol cannot stack a
  duplicate entry while one is waiting. Orders below 10% of their
  risk/allocation-planned size are blocked rather than recorded as dust.
- Operator Stop still flattens as advertised. Infrastructure restart signals
  preserve simulator/paper positions for hydration, so deploys no longer
  manufacture manual exits, spread, or slippage.
- Dashboard risk uses the selected lane's daily-loss settings, exposes the
  dominant one-way exposure/cap, and Settings now lists Forex accounts.
- GitHub Actions supplies the repository's previously missing CI baseline.

### Verification

- Regression coverage exercises same-direction Forex aggregation, pending
  reservations, dust rejection, lane-specific risk display, safe seed
  defaults, Forex account visibility, and restart-versus-operator shutdown.
- `python manage.py test`: **137 tests passed**.
- Django system/schema checks, bytecode compilation, a clean-database
  migration, and migration of the restored 2026-09-07 ledger all passed.

### What this does not claim

These changes can prevent avoidable and oversized losses; they cannot turn a
negative-expectancy strategy profitable. The next strategy work must begin
with cost-adjusted, non-overlapping walk-forward evidence and must leave failed
candidates disabled.

## Release 1.31 — No lucky-window promotions

Status: complete; Forex remains sim-only and profitability remains unproven

Safety boundary: no strategy was promoted, no live/paper mode was enabled,
and no real order or wallet transaction was submitted.

### Evidence that exposed the remaining flaw

Corrected v1.30 research was run without promotion:

- Stock EMA final holdout: 9 trades, PF 1.70, +$21.57. It was correctly not
  confirmed because the sample missed the 10-trade minimum.
- Stock ORB candidate: 106 trades, PF 0.68, −$240.57. The installed champion
  produced 164 trades, PF 1.16, +$121.89 on the identical bars, so the
  candidate was rejected. ORB remains unproven rather than promoted.
- Forex EMA candidate/champion: PF 0.61/0.62. Both were losing after costs.
- Forex VWAP experiment #27 appeared to qualify on only the final seven-day
  holdout: 54 trades, PF 1.102, +$76.29 versus v1 at PF 0.84, −$173.03.
  However, four of five adaptive OOS windows lost. The adaptive selection
  pipeline totaled 210 trades, PF 0.51, −$1,961.62, and the proposed fixed
  parameters lost $680.84 across the diagnostic replay. The selected params
  had also lost $607.25 in their own training window. Promoting this would
  have rewarded a lucky week and a least-bad losing training result.

### What changed

- The best-ranked training combo is now viable only with the experiment's
  minimum trades, PF above 1.0, positive net P&L, and positive expectancy
  after modeled costs.
- Only the final chronological training window may nominate the parameters
  for its following holdout. When that training window has no viable edge,
  the experiment returns no recommendation instead of falling back to an
  older configuration.
- Promotion/confirmation now requires both the final fixed-candidate holdout
  and the aggregate adaptive walk-forward pipeline. The pipeline needs at
  least 30 trades, PF ≥ 1.10, positive net P&L, and positive expectancy.
- The research screen labels the adaptive pipeline and final validation as
  separate pass/fail gates. The web action and nightly command share the same
  enforcement.

### Verification

- A regression test reproduces experiment #27's lucky-final-window shape and
  proves neither automation nor the web endpoint can promote it.
- Tests prove a losing best-ranked training combo is not viable.
- `pipenv run python manage.py test`: **127 tests passed**.
- Corrected Forex experiments #28 (EMA) and #29 (VWAP) both reported
  `no valid held-out candidate`. Forex strategies stayed at v1, enabled only
  for simulator observation, with `qualification=unproven`.

### Next decision

Keep gathering forward-simulation evidence while research focuses on robust
edges that survive multiple regimes and symbols. Do not add real capital to
Forex, day trading, or crypto until a version becomes `qualified` under the
full research and forward-evidence contract.

## Release 1.30 — Trust the evidence before risking money

Status: phases 1–4 complete; profitability remains unproven

Safety boundary: this release does not enable live trading, submit real
orders, sign wallet transactions, or claim that any strategy is profitable.

### Baseline observed before changes

- Forex v1.5 already exists and must be preserved: four USD-quoted majors,
  Yahoo 15-minute bars, Sunday 17:00 through Friday 17:00 ET, long/short
  signals, 10× simulated margin, lane-aware costs, and simulator-only broker
  enforcement.
- The Forex simulator was healthy with 46,000+ stored bars across its cached
  timeframes, but it had completed zero forward trades. Historical evidence
  documented in `CLAUDE.md` remained below break-even after costs.
- The default `pipenv run python manage.py test` reached all 108 tests but
  ended with seven view errors because a production manifest static backend
  was selected from `.env` without a collected test manifest. With `DEBUG=1`,
  all 108 tests passed.
- Nightly research compared an adaptive, per-window parameter policy against
  the current parameters over the full date range. Those results covered
  different bars and could not support a promotion decision.
- Re-selecting the current parameters was called “confirmed” even when their
  out-of-sample profit factor was below the configured evidence threshold.
- Before this phase started, the worktree already contained uncommitted edits
  to `data/store.py`, `engine.py`, `risk.py`, and `views/data.py`. They are
  preserved as pre-existing work and are not part of the claims below.

### Phase 1 — Deterministic engineering baseline

Goal: make the documented test command mean the same thing regardless of the
developer's `.env`, while retaining production manifest storage outside tests.

What changed:

- Unit tests now select ordinary static-file storage even when local `.env`
  uses production manifest storage.
- Production continues to use compressed manifest storage.
- A duplicate test-environment flag left by concurrent work was consolidated.

Verification:

- `pipenv run python manage.py test`: **124 tests passed** without overriding
  `DEBUG`.
- `pipenv run python manage.py check`: no issues.
- `pipenv run python manage.py makemigrations --check --dry-run`: no changes.

### Phase 2 — Comparable held-out research

Goal: distinguish adaptive walk-forward diagnostics from the fixed candidate
that can actually be installed, and compare that candidate with the current
champion on the same untouched validation window.

What changed:

- Adaptive OOS performance is labeled as an adaptive policy, because each
  window may trade different parameters.
- The winner of the most recent training window is replayed as one immutable
  candidate over the recorded test windows for diagnostic context.
- The final test window is retained as promotion evidence. Nightly research
  reruns the current champion on those exact bars.
- Confirmation and promotion require the minimum held-out trades, PF ≥ 1.10,
  positive net P&L, and positive expectancy. Promotion additionally requires
  improvement over the champion's PF and net P&L.
- The web promotion endpoint rejects failed or arbitrary walk-forward params,
  and records final-validation metrics rather than adaptive metrics.

Verification:

- Regression tests prove PF 0.94 cannot be called confirmed.
- Regression tests prove one fixed config is replayed over exact persisted
  test boundaries.
- View tests prove failed held-out evidence cannot change a strategy version.

### Phase 3 — Qualification and quarantine

What changed:

- Migration `0009_strategy_qualification` adds explicit `unproven`,
  `qualified`, and sticky `quarantine` state with a human-readable reason.
- Sim/replay can observe unproven ideas. Paper/live agents load only qualified
  immutable versions. The broker-stage gate cannot be overridden.
- Thirty forward trades with PF below 1, non-positive expectancy, or
  non-positive net P&L quarantine and disable a strategy. Parameter changes
  reset it to unproven.
- The status strip now separates operational health, statistical evidence,
  and execution authorization.
- `audit_qualifications` provides a reproducible dry-run and `--apply` mode.
- An online SQLite backup was written to
  `backups/db-pre-v1.30.sqlite3`, then migration `0009` was applied.

Observed result:

- Degen Burst was quarantined and disabled at 165 closed trades, PF 0.23,
  net −$2,210.42. Its simulator was stopped gracefully after its remaining
  BONK position reached the existing max-hold exit. No real money was involved.
- Stock ORB remained unproven despite PF 1.79 and +$33.38 because 14 forward
  trades is below the evidence minimum.
- Forex remained running in simulator mode. Its first two closed trades were
  losing, so both Forex strategies correctly remain unproven.

### Phase 4 — Honest unavailable data

What changed:

- Relative volume no longer converts missing data or the first causal session
  to a fictional 1.0×.
- Required crypto/stock volume blocks a setup when unavailable. A zero
  threshold explicitly says the filter is disabled.
- Spot FX preserves its price-only approach but says centralized volume is
  unavailable. VWAP reversion calls its no-volume anchor an equal-weighted
  session mean.

Verification:

- Indicator, strategy, backtest, Forex, qualification, and view regression
  tests are included in the 124-test green suite.

### Next phases

1. Run new walk-forward research under the corrected evidence contract; do
   not reuse earlier experiment verdicts as proof.
2. Add venue-aware executable quotes and costs, beginning with a Solana
   shadow wallet that never signs transactions.
3. Add a separate long/short perpetual simulator with funding and liquidation.
4. Add uncertainty intervals and regime/symbol stability to qualification.
5. Consider a practice-only Forex adapter after a Forex strategy demonstrates
   an edge in the simulator.
