# MoneyTree Logbook

This is the running, evidence-first record of MoneyTree releases. Each phase
records the observed problem, its cause, what changed, how it was verified,
and what remains unproven. A green test suite means the software behaves as
specified; it does **not** mean a trading strategy is profitable.

## Release 1.30 — Trust the evidence before risking money

Status: in progress

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

Changes and verification will be filled in when the phase is complete.

### Phase 2 — Comparable held-out research

Goal: distinguish adaptive walk-forward diagnostics from the fixed candidate
that can actually be installed, and compare that candidate with the current
champion on the same untouched validation window.

Changes and verification will be filled in when the phase is complete.

### Next phases

1. Strategy qualification and automatic quarantine for measured no-edge.
2. Honest unavailable-volume handling and venue-aware execution costs.
3. Three-part screen status: operational health, statistical qualification,
   and execution authorization.
4. Solana executable-quote shadow wallet, with no private-key signing.
5. Separate long/short perpetual simulator with funding and liquidation.
6. Optional practice-only Forex broker adapter after the evidence gates work.
