# MT-A005 — window-handling audit

Every command in `main_app/management/commands` (29 total) that reaches bars for
research or evaluation, by any route. Report only: no hypothesis is re-decided here.
Leaks and defects found become their own assignments.

**Why this exists.** The same defect has distorted research three times — warm-up
consumed inside a test window, `act_from` set with no `act_until`, and frames loaded
past the boundary their entries were filtered at. Each was fixed where it was found;
the next appeared somewhere else, because the rule lived in three call sites and had to
be remembered at each.

**Two root causes.** `backtest.load_frames` takes an **inclusive end DATE** and expands
it across the ET session day (`date_bounds` returns `end + 1 day at ET midnight`), so a
window ending at an *instant* — which every research window does — cannot be expressed
by it at all. And the three research commands disagreed about whether `end` meant the
last permissible instant or the first forbidden one, which is why no single shared guard
could be written correctly until that was settled. Windows are now half-open everywhere.

## Table

| command | reaches bars via | window handling before MT-A005 | finding |
|---|---|---|---|
| **h10_forward** | `load_frames` ×2 → `run_window` | frames to `end.date()`; entries `<= end`; **each baseline loaded its own timeframe to the same end DATE** | **LEAK.** Unseen: 44 15Min bars past the end. Held-out: 16 1Hour + 64 15Min. Entries were bounded; open positions exited on them. **Fixed here.** |
| **h10_shadow** | `load_frames` → `run_window` | same load pattern; `end` = latest bar in the DB | **No leak in practice** (nothing exists after the latest bar) but unsafe by construction — a `--end` argument or newer data would leak. **Fixed here.** |
| **turnover_research** | `load_frames` ×2 → `run_window` | fixed in MT-A003 correction-1 by `train_frames`, which cut **after** `quality_gate` | **Already found** (112 15Min + 28 1Hour). Now cut before the gate. |
| **auto_research** | `load_frames` → `run_experiment` / `evaluate_fixed_params` | `optimize.slice_frames` bounds every window `[a, b)` — both ends correct | **No future-bar leak.** But **its test slices start cold** — see the defect below. Named exemption; **→ MT-A006**. |
| **optimize** | `run_experiment` | same `slice_frames` `[a, b)` | Same. Named exemption; **→ MT-A006**. |
| **backtest** | `run_backtest_for_model` → `load_frames` | the window **is** the run's `[start, end]`; no train/test split | **Not applicable** — no boundary to enforce. Named exemption. |
| **evaluate** | `dossier.grade` → `news_agent._feed_frame` → `store.covering_frame` | grades shadow verdicts against bars after the verdict; the window is the barrier race the trade implied | **No leak found.** It reads bars *after* a verdict deliberately — that is the outcome being graded, not a peek. Bounded by `max_hold`. |
| **news_agent** | `_feed_frame` → `store.covering_frame` | `news_agent.py:578` loads from `created_at - ATR_WARMUP_DAYS`, i.e. **explicit warm-up before the window** | **No leak found**, and the one place that already did warm-up correctly. |
| **run_agent --replay** | `agent.py:816 load_frame(inst, tf, warm_start, b)` | `warm_start = a - 5d` (intraday) or `- 30d`; end bounded by the session | **No leak found.** Warm-up precedes the window, as it should. |

The other 20 commands do not reach bars for research or evaluation.

## Defect found, not fixed here: cold-start warm-up in the promotion pipeline

`optimize.evaluate_fixed_params` and `run_experiment` build each window with
`slice_frames(frames, win['test_start'], win['test_end'])`. That slice begins **at** the
test window's start with no history before it, and no `act_from` is set — so the
strategy's warm-up is consumed **inside** every walk-forward and validation test window.

That is the same class as the first leak on this desk, in the one pipeline that actually
promotes strategies. Magnitude, computed from bar arithmetic rather than by re-running:

| lane | 20-day test window | warm-up | dead |
|---|---:|---:|---:|
| stocks 5Min | 1,560 bars | 40 | 2.56% |
| forex 15Min | 1,920 bars | 40 | 2.08% |
| **crypto 1Hour** | **480 bars** | **40** | **8.33%** |

Not fixed here because `auto_research` drives nightly promotions and changing its bar
handling would change what gets promoted, which is outside MT-A005's scope. **Queued as
MT-A006**, along with the observation below.

## Open observation: the cleaning step looks forward

`quality_gate`'s outlier check compares each bar against `df['close'].rolling(
OUTLIER_WINDOW, center=True, min_periods=11).median()`. A **centred** window means the
reference for the last bars of a series is computed partly from bars after them, so the
data cleaning is itself mildly forward-looking — in the last place anyone would look.

`research_frames` removes this for research by cutting before the gate runs, and that was
not academic: cutting *after* the gate gave −407.25 for one baseline where cutting
*before* gives −417.87. It is **not** removed for `auto_research`/`optimize`, which load
the full range once and slice afterwards, so each window's tail is still cleaned with
bars from outside it. `OUTLIER_FACTOR` is 10×, so the practical effect should be nil on
FX majors — but "should be nil" is what would have been said about the other three.
**→ MT-A006.**

## Effect on recorded figures

| figure | before | after | cause |
|---|---|---|---|
| H10 unseen (n=8) | −119.35 | **−119.35** | unchanged — the 1Hour series had nothing past the end |
| H10 held-out (n=100) | +1081.84 | **+1081.84** | unchanged — measured, not assumed |
| MT-A003 turnover, all 12 rows | — | **identical** | already fixed in correction-1 |
| unseen, `vwap_reversion` baseline | −415.22 | **−417.87** | 44 leaked 15Min bars |
| held-out, `vwap_reversion` baseline | −2455.15 | **−2471.01** | 64 leaked 15Min bars |
| held-out, `ema_momentum_1hour_h10_risk` | +381.79 | **+393.87** | 16 leaked 1Hour bars |

Recorded under `superseded` in `docs/h10-forward.json` and `docs/h10-forward-heldout.json`.

**Cross-check.** `h10_forward`'s held-out `vwap_reversion` baseline moved −2455.15 →
−2471.01 — *exactly* the contaminated→corrected pair MT-A003 produced independently, in a
different command, with a separately written fix. Two separate leaks of the same shape
converging on the same number.

**No hypothesis is re-decided by any of this.** H10's own rows are unchanged in both
windows, and MT-A003 selected on `turnover_research` train rows, all twelve byte-identical.
The changed rows are context baselines.
