"""One bounded loader, and nothing may go around it.

The same leak has distorted research on this desk three times — warm-up consumed
inside a test window, `act_from` with no `act_until`, and frames loaded past the
boundary their entries were filtered at. Each was fixed where it was found and the
next appeared somewhere else, because the rule lived in three call sites and had to
be remembered at each.

So these tests guard the rule rather than the instances: research cannot obtain a
frame except through `research_frames`, and nothing reaches the engine carrying a
bar at or after its window end.
"""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from django.test import SimpleTestCase, TestCase

from main_app.services.research_window import (Window, WindowLeak, assert_bounded,
                                               inclusive_through, research_frames, truncate)

START = datetime(2026, 9, 8, tzinfo=timezone.utc)
END = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)
COMMANDS = Path('main_app/management/commands')
# Commands that load bars for research or evaluation. auto_research is excluded
# because it reaches bars through optimize.run_experiment, whose slice_frames
# already bounds every walk-forward window at both ends with [a, b).
RESEARCH_COMMANDS = ('h10_forward.py', 'h10_shadow.py', 'turnover_research.py')


def _frame(first, n, freq='15min'):
    idx = pd.date_range(first, periods=n, freq=freq, tz='UTC')
    return pd.DataFrame({'open': [1.1] * n, 'high': [1.2] * n, 'low': [1.0] * n,
                         'close': [1.1] * n, 'volume': [0] * n}, index=idx)


class AWindowIsHalfOpen(SimpleTestCase):
    """The three commands used to disagree about whether `end` was the last
    permissible instant or the first forbidden one, which is precisely why a single
    shared guard could not be written correctly. Settled: [start, end)."""

    def test_the_end_is_excluded_and_the_start_included(self):
        w = Window(warmup_start=date(2026, 8, 1), start=START, end=END)
        self.assertTrue(w.holds(START))
        self.assertFalse(w.holds(END))
        self.assertTrue(w.holds(END - timedelta(microseconds=1)))

    def test_inclusive_through_keeps_the_last_bar(self):
        """h10_forward's window ends AT its last closed bar, so it needs an
        exclusive bound just past it — not the bar's own timestamp, which would
        silently drop the final bar and change a recorded result."""
        w = Window(warmup_start=date(2026, 8, 1), start=START, end=inclusive_through(END))
        self.assertTrue(w.holds(END))

    def test_a_window_that_ends_before_it_starts_is_refused(self):
        with self.assertRaises(ValueError):
            Window(warmup_start=date(2026, 8, 1), start=END, end=START)

    def test_warmup_may_not_begin_inside_the_window(self):
        """Mistake #1 on this desk: warm-up consumed inside the test window
        discarded 18% of it and turned +$675 into -$311."""
        with self.assertRaises(ValueError) as cm:
            Window(warmup_start=date(2026, 9, 20), start=START, end=END)
        self.assertIn('must precede the window', str(cm.exception))


class NothingReachesTheEngineAfterTheWindowEnd(SimpleTestCase):
    def test_leaking_frames_are_refused_with_the_detail_needed_to_fix_them(self):
        frames = {'EUR/USD': _frame(END - timedelta(hours=2), 20)}
        with self.assertRaises(WindowLeak) as cm:
            assert_bounded(frames, END, 'unit test')
        msg = str(cm.exception)
        self.assertIn('EUR/USD', msg)
        self.assertIn('research_frames', msg, 'the error must say how to fix it')

    def test_bounded_frames_pass(self):
        assert_bounded({'EUR/USD': _frame(END - timedelta(hours=5), 8)}, END)

    def test_a_bar_exactly_at_the_end_is_a_leak(self):
        """Half-open means half-open. A bar AT the end is outside the window, and
        MT-A003's 15Min series has a bar at exactly 00:00 on the boundary."""
        with self.assertRaises(WindowLeak):
            assert_bounded({'EUR/USD': _frame(END, 1)}, END)

    def test_truncate_removes_exactly_the_bars_at_or_after_the_end(self):
        df = _frame(END - timedelta(hours=1), 12)
        cut = truncate({'x': df}, END)['x']
        self.assertEqual(len(cut), 4, 'expected the four bars before the end')
        self.assertTrue((cut.index < END).all())


class ResearchFramesCutsBeforeTheQualityGate(TestCase):
    """quality_gate compares each bar against a CENTRED rolling median, so on an
    untruncated series the reference for the last bars is computed partly from bars
    after them. Cleaning is itself mildly forward-looking, in the last place anyone
    would look. Cutting first removes that, which is why this is not a wrapper
    around backtest.load_frames."""

    WARMUP_FROM = START - timedelta(days=3)

    def setUp(self):
        from main_app.models import Bar, Instrument
        self.inst = Instrument.objects.create(symbol='EUR/USD', asset_class='forex',
                                              market='forex')
        # Spans warm-up, the window, and past its end — so an uncut load leaks and
        # an over-eager cut destroys the warm-up. Both failure modes are reachable.
        n = int((END + timedelta(days=1) - self.WARMUP_FROM).total_seconds() // (15 * 60))
        Bar.objects.bulk_create([
            Bar(instrument=self.inst, timeframe='15Min',
                ts=self.WARMUP_FROM + timedelta(minutes=15 * k),
                open=1.1, high=1.2, low=1.0, close=1.1, volume=0) for k in range(n)])

    def _window(self):
        return Window(warmup_start=self.WARMUP_FROM.date(), start=START, end=END)

    def test_the_fixture_really_does_hold_bars_past_the_end(self):
        from main_app.models import Bar
        self.assertGreater(Bar.objects.filter(ts__gte=END).count(), 0,
                           'fixture has no bars past the end — the test would be vacuous')

    def test_no_bar_at_or_after_the_end_is_returned(self):
        frames = research_frames(['EUR/USD'], '15Min', self._window())
        self.assertGreater(len(frames['EUR/USD']), 0, 'returned nothing at all')
        self.assertTrue((frames['EUR/USD'].index < END).all())

    def test_warmup_history_before_the_window_is_kept(self):
        """Cutting the end must not also cut the warm-up, or every strategy silently
        stops emitting and the silence reads as 'no trades'."""
        frames = research_frames(['EUR/USD'], '15Min', self._window())
        self.assertGreater(int((frames['EUR/USD'].index < START).sum()), 0,
                           'no pre-window warm-up survived the cut')


class ResearchCommandsMayNotLoadTheirOwnFrames(SimpleTestCase):
    """AC1 as an executable rule rather than a convention.

    A future command that calls load_frames directly would re-open the same hole,
    and nothing else in the suite would notice until a reviewer re-derived the
    numbers by hand. This is the cheapest guard that actually holds.
    """

    def test_no_research_command_calls_load_frames_directly(self):
        offenders = []
        for name in RESEARCH_COMMANDS:
            src = (COMMANDS / name).read_text()
            for i, line in enumerate(src.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith('#') or stripped.startswith('*'):
                    continue          # prose in a docstring explaining the bug
                if 'load_frames(' in line:
                    offenders.append(f'{name}:{i}: {stripped}')
        self.assertEqual(offenders, [], 'research commands must obtain bars through '
                                        'research_frames(), which bounds both ends: '
                                        + '; '.join(offenders))

    def test_every_research_command_imports_the_bounded_loader(self):
        missing = [n for n in RESEARCH_COMMANDS
                   if 'research_frames' not in (COMMANDS / n).read_text()]
        self.assertEqual(missing, [], f'{missing} load bars without the bounded loader')

    def test_the_list_of_research_commands_is_not_silently_empty(self):
        for name in RESEARCH_COMMANDS:
            self.assertTrue((COMMANDS / name).exists(), f'{name} has moved — this guard '
                                                        f'would pass while protecting nothing')
