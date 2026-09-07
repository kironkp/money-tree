# MoneyTree Logbook

This is the running, evidence-first record of MoneyTree releases. Each phase
records the observed problem, its cause, what changed, how it was verified,
and what remains unproven. A green test suite means the software behaves as
specified; it does **not** mean a trading strategy is profitable.

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
