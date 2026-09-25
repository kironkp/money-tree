"""The only way research obtains bars, with both ends of the window enforced.

A window has two ends and the code has only ever enforced the one being thought
about. That has now distorted research three times on this desk:

  1. Warm-up consumed INSIDE a test window — 18% of it discarded, +$675 became
     -$311, and two hypotheses were wrongly rejected.
  2. `act_from` set with no `act_until` — 38% of a "train" window's trades were
     test trades, which wrongly rejected H10, the one candidate that survives a
     clean test.
  3. Frames loaded past the boundary their ENTRIES were filtered at (MT-A003).
     `load_frames` takes an INCLUSIVE end date and expands it across the ET
     session day, so asking for 2026-09-08 returns bars to 2026-09-09 03:45 UTC.
     Entries were bounded; open positions still exited on spent bars.

Each was fixed where it was found, and the next one appeared somewhere else. So
the fix is not another filter: it is that research cannot obtain a frame except
through `research_frames`, which cannot return a bar at or after the end, and
`run_window`, which refuses frames that contain one.

Truncation happens BEFORE `quality_gate`, not after. That gate compares each bar
against a CENTRED rolling median, so on an untruncated series the reference for
the last few bars is computed partly from bars that come after them — the data
cleaning is itself mildly forward-looking, in the last place anyone would look
for it. Cutting first removes that too.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd


class WindowLeak(AssertionError):
    """Raised when a bar at or after a window's end reaches the engine."""


@dataclass(frozen=True)
class Window:
    """A research window. `end` is EXCLUSIVE — that is the whole point.

    `warmup_start` is where history may begin so the strategy has enough bars to
    form its indicators. It is deliberately separate from `start`: warm-up must
    come from BEFORE the window, and consuming it inside was mistake #1 above.
    """
    warmup_start: date
    start: datetime          # first instant an entry may be taken
    end: datetime            # no bar at or after this may reach the engine

    def __post_init__(self):
        if self.end <= self.start:
            raise ValueError(f'window end {self.end} is not after its start {self.start}')
        if self.warmup_start > self.start.date():
            raise ValueError(f'warm-up starts {self.warmup_start}, after the window opens '
                             f'{self.start.date()} — warm-up must precede the window')

    def holds(self, ts: datetime) -> bool:
        return self.start <= ts < self.end


def inclusive_through(last_bar: datetime) -> datetime:
    """The exclusive end of a window that INCLUDES `last_bar`.

    Every window in research is now half-open, [start, end), because the three
    commands previously disagreed: h10_forward treated `end` as the last
    permissible entry instant while turnover_research treated it as the first
    forbidden one. That disagreement is what made a single shared guard impossible
    to write correctly, so it is settled here rather than at each call site.
    """
    return last_bar + timedelta(microseconds=1)


def truncate(frames: dict, end: datetime) -> dict:
    return {sym: df[df.index < end] for sym, df in frames.items()}


def assert_bounded(frames: dict, end: datetime, what: str = 'frames') -> None:
    """Refuse frames carrying a bar at or after `end`.

    This is the choke point. Anything that reaches the engine passes through it,
    so a future call site that loads its own frames fails loudly instead of
    quietly measuring the future.
    """
    for sym, df in frames.items():
        late = df.index[df.index >= end]
        if len(late):
            raise WindowLeak(
                f'{what}: {sym} carries {len(late)} bar(s) at or after the window end {end} '
                f'(first {late[0]}, last {late[-1]}). Load frames with research_frames(), '
                f'which cuts them before quality_gate ever sees them.')


def research_frames(symbols, timeframe: str, window: Window) -> dict[str, pd.DataFrame]:
    """Bars for a research window: warm-up included, nothing at or after the end.

    Deliberately NOT a wrapper around `backtest.load_frames`. That function runs
    `quality_gate` over whatever range it was given, and the gate's centred median
    looks forward — so wrapping it would clean the tail of the window using bars
    from outside it, then hand back a truncated result that still carried their
    influence. The order here is load, cut, then clean.
    """
    from main_app.models import Instrument

    from .backtest import date_bounds
    from .data.store import load_frame, quality_gate

    a, _ = date_bounds(window.warmup_start, window.warmup_start)
    # Ask the store for everything up to the end DATE, then cut to the instant.
    # The store's own bounds are date-shaped; the window's are not.
    _, b = date_bounds(window.end.date(), window.end.date())
    frames = {}
    for inst in Instrument.objects.filter(symbol__in=list(symbols)):
        df = load_frame(inst, timeframe, a, b)
        df = df[df.index < window.end]                 # BEFORE the gate, not after
        df, _ = quality_gate(df, timeframe, inst.asset_class)
        frames[inst.symbol] = df
    assert_bounded(frames, window.end, f'research_frames({timeframe})')
    return frames


def run_window(key: str, params: dict, risk_over: dict, frames, start, end, *,
               timeframe: str, pairs, cost_mult: float = 1.0) -> tuple[list, float]:
    """Replay one strategy over the half-open window [start, end).

    Lives here rather than in a command because it is the choke point: every
    research command reaches the engine through it, so this is the one place that
    can refuse frames carrying a bar at or after the end. It was in h10_forward,
    which meant that command had to reference `run_backtest` directly — and a rule
    saying "no command may touch a raw loader" then had to exempt the very file
    holding the guard.

    `timeframe` and `pairs` are REQUIRED and keyword-only. They used to default to
    H10's, and that default is what let MT-A001 replay the live 15Min strategies on
    H10's 1Hour frames under H10's risk settings and label the result "live params"
    — a configuration that has never existed. A baseline has to be run as it
    actually lives, so the caller states it.
    """
    from dataclasses import replace

    from main_app.models import AgentConfig

    from .backtest import run_backtest, spec_from_models

    spec = spec_from_models(key, params, list(pairs), timeframe, AgentConfig.get())
    if risk_over:
        spec.risk = replace(spec.risk, **risk_over)
    if cost_mult != 1.0:
        spec.fee_bps = {k: v * cost_mult for k, v in spec.fee_bps.items()}
        spec.risk = replace(spec.risk, slippage_bps=spec.risk.slippage_bps * cost_mult)
    assert_bounded(frames, end, f'run_window({key})')
    spec.act_from = start
    res = run_backtest(spec, frames)
    # act_from stops the engine acting early; this stops it acting late. Both are
    # required — setting only the first is the leak that invalidated a whole train
    # window, and bounding entries while the FRAMES ran past the boundary is the
    # same mistake one layer out.
    return [t for t in res.trades if start <= t.entry_ts < end], spec.risk.slippage_bps
