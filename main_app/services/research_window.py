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
