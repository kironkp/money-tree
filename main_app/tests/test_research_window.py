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
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

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


@contextmanager
def _temp_command(name: str, source: str):
    """A throwaway command file in a throwaway directory.

    Written to a temp dir rather than into main_app/management/commands, so a
    crashed test cannot leave a broken command behind for Django to import.
    """
    with TemporaryDirectory() as folder:
        path = Path(folder)
        (path / f'{name}.py').write_text(source)
        yield path


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


# Anything that can put bars in front of a strategy. Matched by AST, so an alias
# (`load_frames as lf`), a module-qualified call (`store.load_frame`) and an indirect
# driver (`run_experiment`) are all caught.
BAR_REACHING = {'load_frames', 'load_frame', 'covering_frame', 'run_backtest',
                'run_backtest_for_model', 'run_experiment', 'evaluate_fixed_params',
                'slice_frames'}

# Commands allowed to reach bars without research_frames, each with the reason.
# A NAMED exemption, not an omission: the previous version of this guard was a
# hardcoded tuple of three filenames matched against one literal string, so
# auto_research called load_frames directly and the test said nothing. A guard that
# protects only what someone remembered to list is the failure this whole module
# exists to remove.
EXEMPT = {
    'auto_research.py': (
        'Promotion pipeline. Reaches bars via optimize.run_experiment and '
        'evaluate_fixed_params, whose slice_frames bounds every walk-forward window '
        '[a, b) at both ends; no future-bar read found. Routing it through '
        'research_frames would change what the nightly job promotes, which is out of '
        'MT-A005 scope. Its cold-start warm-up is a real defect, queued as MT-A006.'),
    'optimize.py': (
        'Drives the same walk-forward machinery by hand. slice_frames bounds every '
        'train and test window [a, b) at both ends and no future-bar read was found, '
        'but its test slices start cold so warm-up is consumed inside each test '
        'window — the same class as the first leak on this desk. Excluded here '
        'because a fix changes promotion behaviour. Queued as MT-A006.'),
    'backtest.py': (
        'Runs one BacktestRun whose window IS its [start, end]. There is no train/test '
        'split for data to leak across, so there is no boundary to enforce.'),
}


class NoCommandReachesBarsWithoutTheBoundedLoader(SimpleTestCase):
    """AC1 as a discovered rule, not a remembered list.

    Every command is parsed. Any that can put bars in front of a strategy must
    either use research_frames or carry a written exemption. A new command, or an
    aliased import inside an existing one, fails this without anyone updating it.
    """

    @staticmethod
    def bar_reaching(path: Path) -> set:
        import ast
        hits = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                # `a.name` is the SOURCE name, so `import load_frames as lf` is caught.
                hits |= {a.name for a in node.names if a.name in BAR_REACHING}
            elif isinstance(node, ast.Attribute) and node.attr in BAR_REACHING:
                hits.add(node.attr)
            elif isinstance(node, ast.Name) and node.id in BAR_REACHING:
                hits.add(node.id)
        return hits

    def offenders(self, folder=COMMANDS) -> list:
        out = []
        for path in sorted(folder.glob('*.py')):
            if path.name == '__init__.py' or path.name in EXEMPT:
                continue
            hits = self.bar_reaching(path)
            if hits and 'research_frames' not in path.read_text():
                out.append(f'{path.name} reaches bars via {sorted(hits)}')
        return out

    def test_every_command_that_reaches_bars_uses_the_bounded_loader(self):
        self.assertEqual(self.offenders(), [],
                         'these obtain bars without research_frames and are not on the '
                         'exemption list: ' + '; '.join(self.offenders()))

    def test_the_scan_actually_finds_something(self):
        """Guards the guard. If the AST walk silently matched nothing — a renamed
        helper, a parse that failed open — the rule above would pass vacuously."""
        found = {p.name for p in COMMANDS.glob('*.py') if self.bar_reaching(p)}
        self.assertTrue(found, 'the scan flagged no command at all, so it is proving nothing')
        self.assertIn('auto_research.py', found)

    def test_every_exemption_is_still_a_real_file_with_a_real_reason(self):
        """An exemption for a deleted file is a hole nobody can see."""
        for name, reason in EXEMPT.items():
            self.assertTrue((COMMANDS / name).exists(), f'{name} is exempt but does not exist')
            self.assertGreater(len(reason), 80, f'{name} needs a reason, not a label')
            self.assertTrue(self.bar_reaching(COMMANDS / name),
                            f'{name} no longer reaches bars — drop the exemption')

    def test_an_aliased_import_does_not_slip_past(self):
        with _temp_command('aliased_leak', 'from main_app.services.backtest import '
                                           'load_frames as lf\n\n\ndef go():\n    return lf\n') as folder:
            self.assertTrue(any('aliased_leak' in o for o in self.offenders(folder)),
                            'an aliased import was not detected')

    def test_a_module_qualified_call_does_not_slip_past(self):
        with _temp_command('qualified_leak', 'from main_app.services.data import store\n\n\n'
                                             'def go(i):\n    return store.load_frame(i)\n') as folder:
            self.assertTrue(any('qualified_leak' in o for o in self.offenders(folder)),
                            'a module-qualified call was not detected')

    def test_a_brand_new_command_is_caught_without_updating_any_list(self):
        with _temp_command('brand_new', 'from main_app.services.backtest import load_frames\n\n\n'
                                        'def go():\n    return load_frames\n') as folder:
            self.assertTrue(any('brand_new' in o for o in self.offenders(folder)),
                            'a new command file was not discovered')

    def test_a_new_command_that_uses_the_bounded_loader_is_accepted(self):
        """The positive control: the rule must pass what it is meant to allow, or it
        is just a ban on writing commands."""
        with _temp_command('good_new', 'from main_app.services.backtest import load_frames\n'
                                       'from main_app.services.research_window import '
                                       'research_frames\n\n\ndef go(s, tf, w):\n'
                                       '    return research_frames(s, tf, w)\n') as folder:
            self.assertEqual([o for o in self.offenders(folder) if 'good_new' in o], [])
