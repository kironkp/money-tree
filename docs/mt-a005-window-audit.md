# MT-A005 — window-handling audit

Every command in `main_app/management/commands` (29 total) that reaches bars for
research or evaluation, by any route. Report only: no hypothesis is re-decided here,
and any leak found becomes its own assignment.

**Why this exists.** The same defect has distorted research three times — warm-up
consumed inside a test window, `act_from` set with no `act_until`, and frames loaded
past the boundary their entries were filtered at. Each was fixed where it was found;
the next appeared somewhere else, because the rule lived in three call sites and had
to be remembered at each.

**The root cause the audit surfaced.** `backtest.load_frames` takes an **inclusive end
DATE** and expands it across the ET session day (`date_bounds` returns
`end + 1 day at ET midnight`). So a window ending at an *instant* — which every research
window does — cannot be expressed by it at all. Asking for `2026-09-08` returns bars to
`2026-09-09 03:45 UTC`. Compounding it, the three commands disagreed about whether `end`
meant the last permissible instant or the first forbidden one, which is why no single
shared guard could be written correctly until that was settled.

## Table

| command | reaches bars via | window handling before MT-A005 | leak found |
|---|---|---|---|
| **h10_forward** | `load_frames` ×2 → `run_window` | frames to `end.date()`; entries filtered `<= end`; **each baseline loaded its own timeframe to the same end DATE** | **YES.** Unseen: 44 15Min bars past the end. Held-out: 16 1Hour + 64 15Min. Entries were bounded, but open positions exited on them. |
| **h10_shadow** | `load_frames` → `run_window` | same load pattern; `end` = latest bar in the DB | **No leak in practice** (nothing exists after the latest bar), but unsafe by construction — a `--end` argument or newer data would have leaked. |
| **turnover_research** | `load_frames` ×2 → `run_window` | fixed in MT-A003 correction-1 by `train_frames`, which cut **after** `quality_gate` | **Already found and fixed** (112 15Min + 28 1Hour). Now cut before the gate. |
| **auto_research** | `load_frames` → `run_experiment` / `evaluate_fixed_params` | `optimize.slice_frames` bounds every walk-forward window `[a, b)` — **both ends, already correct** | **No leak.** See the note below. |
| **optimize** | `run_experiment` | same `slice_frames` `[a, b)` | **No leak.** |
| **backtest** | `run_backtest_for_model` → `load_frames` | the window **is** the run's `[start, end]`; there is no train/test split to leak across | **Not applicable.** |

The other 23 commands do not load bars for research or evaluation.

## Effect on recorded figures

| figure | before | after | cause |
|---|---|---|---|
| H10 unseen (n=8) | −119.35 | **−119.35** | unchanged — the 1Hour series had nothing past the end |
| H10 held-out (n=100) | +1081.84 | **+1081.84** | unchanged — measured explicitly; no trade was open at the boundary |
| MT-A003 turnover, all 12 rows | — | **identical** | already fixed in correction-1 |
| h10_forward unseen, `vwap_reversion` baseline | −415.22 | **−417.87** | 44 leaked 15Min bars |
| h10_forward held-out, `vwap_reversion` baseline | −2455.15 | **−2471.01** | 64 leaked 15Min bars |
| h10_forward held-out, `ema_momentum_1hour_h10_risk` | +381.79 | **+393.87** | 16 leaked 1Hour bars |

**Cross-check worth noting.** `h10_forward`'s held-out `vwap_reversion` baseline moved
−2455.15 → −2471.01 — *exactly* the contaminated→corrected pair MT-A003 produced
independently in a different command. Two separately fixed leaks converging on the same
number is the strongest evidence available that the fix is right.

**No hypothesis is re-decided by any of this.** H10's own rows are unchanged in both
windows, and MT-A003 selected on `turnover_research` train rows, all twelve of which are
byte-identical. The changed rows are context baselines.

## Open observation, not a leak

`quality_gate`'s outlier check compares each bar against `df['close'].rolling(
OUTLIER_WINDOW, center=True, min_periods=11).median()`. A **centred** window means the
reference for the last bars of a series is computed partly from bars that come after
them, so the data-cleaning step is itself mildly forward-looking — in the last place
anyone would look for it.

`research_frames` removes this for research by cutting before the gate runs. It is
**not** removed for `auto_research`/`optimize`, which load the full range once and slice
per walk-forward window afterwards, so each window's tail is still cleaned using bars
from outside it. `OUTLIER_FACTOR` is 10×, so the practical effect is expected to be nil
on FX majors — but "expected to be nil" is what was said about the other three. Flagged
for its own assignment; **not changed here**, because `auto_research` drives nightly
promotions and altering its bar handling is out of scope.
