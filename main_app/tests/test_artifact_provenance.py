"""A writer may not delete provenance it did not write.

MT-A005's superseded blocks were added by hand after their runs. The next run — a
verification that changed nothing — rewrote each artifact wholesale and dropped
every field it did not itself produce, taking the superseded blocks and an accepted
run's first_result_at and measured_at with them.

The defect was not that the wrong command was run. It was that provenance survived
only until the next write, so proving a refactor safe destroyed the evidence the
refactor was being judged against. One test per writer, because there are three
writers and the rule has to hold for each.
"""
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from main_app.services.research_window import merge_artifact

SEEDED = [{'window': 'unseen 2026-09-08..09-24', 'what': 'baseline figures only',
           'why': 'MT-A005 leak', 'changed_rows': [{'baseline': 'vwap_reversion',
                                                    'before': -415.22, 'after': -417.87}]}]


class TheMergeKeepsWhatItDidNotWrite(SimpleTestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / 'a.json'

    def _seed(self, **extra):
        doc = {'rows': [1, 2], 'superseded': SEEDED, 'first_result_at': '2026-09-24T23:03:44',
               'hand_added_note': 'written by a human after the run', **extra}
        self.path.write_text(json.dumps(doc))
        return doc

    def test_a_field_the_writer_never_produces_survives(self):
        self._seed()
        out = merge_artifact(str(self.path), {'rows': [1, 2]}, measured=lambda d: d.get('rows'))
        self.assertEqual(out['superseded'], SEEDED)
        self.assertEqual(out['hand_added_note'], 'written by a human after the run')

    def test_an_identical_rerun_keeps_the_accepted_timestamps(self):
        self._seed()
        out = merge_artifact(str(self.path), {'rows': [1, 2], 'first_result_at': '2026-09-25T00:03:45'},
                             measured=lambda d: d.get('rows'), stamps=('first_result_at',))
        self.assertEqual(out['first_result_at'], '2026-09-24T23:03:44',
                         'a verification overwrote the accepted run\'s timestamp')
        self.assertIn('reverified_at', out, 'the re-run left no trace at all')

    def test_a_real_change_does_take_the_new_timestamps(self):
        """The positive control. A merge that never updates anything would pass the
        test above and quietly freeze the artifact."""
        self._seed()
        out = merge_artifact(str(self.path), {'rows': [1, 2, 3], 'first_result_at': '2026-09-25T00:03:45'},
                             measured=lambda d: d.get('rows'), stamps=('first_result_at',))
        self.assertEqual(out['first_result_at'], '2026-09-25T00:03:45')
        self.assertNotIn('reverified_at', out)
        self.assertEqual(out['superseded'], SEEDED, 'a real change still kept the provenance')

    def test_a_writer_may_still_replace_a_field_it_owns(self):
        self._seed()
        out = merge_artifact(str(self.path), {'rows': [9]}, measured=lambda d: d.get('rows'))
        self.assertEqual(out['rows'], [9])

    def test_a_missing_file_is_simply_the_new_payload(self):
        self.assertEqual(merge_artifact(str(self.path), {'rows': [1]}, measured=lambda d: d), {'rows': [1]})


class _WriterCase(TestCase):
    """Shared shape: seed a superseded block, run the writer, require it survives."""

    def seed(self, path: Path, extra=None):
        doc = {'superseded': SEEDED}
        doc.update(extra or {})
        path.write_text(json.dumps(doc))

    def assert_survived(self, path: Path):
        doc = json.loads(path.read_text())
        self.assertEqual(doc.get('superseded'), SEEDED,
                         'the writer deleted a superseded block it did not write')
        return doc


class H10ForwardKeepsItsSupersededBlock(_WriterCase):
    """Reaching the write path is the whole test.

    The first version of this called the command against an empty test database, so
    it exited on "no 1Hour bars" and merge_artifact was called ZERO times. It then
    asserted the seeded block had survived — which it had, because nothing had been
    written at all. Reverting the command to `json.dump(result)` left it green. Same
    vacuous-test failure as MT-A001's 400-bar fixture against a 760-bar warm-up.

    So the collaborators are mocked the way the turnover test does it, and the
    writer's call count is asserted first.
    """
    START = datetime(2026, 9, 8, tzinfo=timezone.utc)
    END = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)

    def _frames(self, *a, **kw):
        import pandas as pd
        from main_app.services.strategies.fx_trend import H10_PAIRS
        # 900 bars before the window start, comfortably over FxTrend's 760-bar
        # warm-up, or the command exits on "warm-up would eat the window" and we
        # are back to a test that never reaches the writer.
        idx = pd.date_range(self.START - timedelta(hours=900), self.END, freq='1h', tz='UTC')
        return {s: pd.DataFrame({'open': [1.1] * len(idx), 'high': [1.1] * len(idx),
                                 'low': [1.1] * len(idx), 'close': [1.1] * len(idx),
                                 'volume': [0] * len(idx)}, index=idx) for s in H10_PAIRS}

    def _run(self, out):
        mod = 'main_app.management.commands.h10_forward'
        with mock.patch(f'{mod}._window_bounds', return_value=(self.START, self.END)), \
             mock.patch(f'{mod}.research_frames', side_effect=self._frames), \
             mock.patch('main_app.services.research_window.run_window',
                        side_effect=lambda *a, **k: ([], 0.3)):
            call_command('h10_forward', out=str(out), verbosity=0)

    def test_the_command_actually_reaches_its_writer(self):
        """Guards the guard. If this is ever 0, the test below proves nothing."""
        folder = Path(tempfile.mkdtemp())
        out = folder / 'h10.json'
        self.seed(out)
        with mock.patch('main_app.management.commands.h10_forward.merge_artifact',
                        wraps=merge_artifact) as spy:
            self._run(out)
        self.assertGreater(spy.call_count, 0, 'the command exited before writing — the '
                                              'provenance test below is vacuous')

    def test_a_run_does_not_drop_a_superseded_block(self):
        folder = Path(tempfile.mkdtemp())
        out = folder / 'h10.json'
        self.seed(out)
        self._run(out)
        doc = self.assert_survived(out)
        self.assertIn('h10', doc, 'the command did not write its own result either')


class TurnoverResearchKeepsTheAcceptedTimestamps(_WriterCase):
    def setUp(self):
        from main_app.models import Hypothesis, Instrument, Strategy
        from main_app.management.commands.turnover_research import PAIRS, STRATEGIES, TRAIN_END
        self.TRAIN_END = TRAIN_END
        for sym in PAIRS:
            Instrument.objects.get_or_create(symbol=sym, defaults={'asset_class': 'forex',
                                                                   'market': 'forex'})
        for key in STRATEGIES:
            Strategy.objects.get_or_create(key=key, market='forex',
                                           defaults={'timeframe': '15Min', 'params': {},
                                                     'enabled': True})
        self.h = Hypothesis.objects.create(market='forex', title='MT-A003 test', claim='x',
                                           source='test')
        self.folder = Path(tempfile.mkdtemp())
        (self.folder / 'p.json').write_text(json.dumps(
            {'registered_at': '2026-09-24T22:44:23+00:00', 'hypothesis_id': self.h.id}))

    def _bars(self, inst, timeframe, a=None, b=None, **kw):
        import pandas as pd
        freq = '15min' if timeframe == '15Min' else '1h'
        idx = pd.date_range(self.TRAIN_END - timedelta(days=12), self.TRAIN_END, freq=freq, tz='UTC')
        return pd.DataFrame({'open': [1.1] * len(idx), 'high': [1.1] * len(idx),
                             'low': [1.1] * len(idx), 'close': [1.1] * len(idx),
                             'volume': [0] * len(idx)}, index=idx)

    def _run(self):
        mod = 'main_app.management.commands.turnover_research'
        with mock.patch(f'{mod}.PREREG', str(self.folder / 'p.json')), \
             mock.patch(f'{mod}.RESULTS', str(self.folder / 'r.json')), \
             mock.patch('main_app.services.data.store.load_frame', side_effect=self._bars), \
             mock.patch('main_app.services.research_window.run_window',
                        side_effect=lambda *a, **k: ([], 0.3)):
            call_command('turnover_research', '--run', verbosity=0)
        return json.loads((self.folder / 'r.json').read_text())

    def test_an_identical_rerun_keeps_first_result_at_and_measured_at(self):
        accepted = self._run()
        again = self._run()
        self.assertEqual(again['first_result_at'], accepted['first_result_at'],
                         'a verification overwrote the accepted run\'s first_result_at')
        self.assertEqual(again['measured_at'], accepted['measured_at'])
        self.assertIn('reverified_at', again, 'the re-run left no trace that it happened')

    def test_a_rerun_does_not_drop_a_superseded_block(self):
        self._run()
        doc = json.loads((self.folder / 'r.json').read_text())
        doc['superseded'] = SEEDED
        (self.folder / 'r.json').write_text(json.dumps(doc))
        self._run()
        self.assert_survived(self.folder / 'r.json')


class H10ShadowKeepsItsRecordAndItsRunLog(_WriterCase):
    def setUp(self):
        from main_app.management.commands.h10_shadow import FORWARD_START
        from main_app.services.strategies.fx_trend import H10_RISK, H10_SPEC
        self.folder = Path(tempfile.mkdtemp())
        self.path = self.folder / 'shadow.json'
        self.base = {'forward_start': FORWARD_START.isoformat(), 'spec': dict(H10_SPEC),
                     'risk': dict(H10_RISK), 'pairs': [], 'timeframe': '1Hour',
                     'trades': {}, 'runs': [{'at': '2026-09-24T02:00:00+00:00',
                                             'status': 'recorded', 'added': 0}]}

    def _run(self):
        call_command('h10_shadow', start='2026-09-25', out=str(self.path), verbosity=0)

    def test_the_command_actually_reaches_its_writer(self):
        """Guards the guard. The first version of these tests ran against an empty
        database, where the command returned on "no bars yet" BEFORE write_record —
        so it was called once over two invocations and every assertion below held
        for the wrong reason."""
        import main_app.management.commands.h10_shadow as mod
        self.seed(self.path, self.base)
        with mock.patch.object(mod, 'write_record', wraps=mod.write_record) as spy:
            self._run()
            self._run()
        self.assertGreaterEqual(spy.call_count, 2,
                                f'write_record reached {spy.call_count} times over two '
                                f'invocations — an exit path skips it')

    def _bars(self, first, n, freq='1h'):
        import pandas as pd
        from main_app.models import Bar, Instrument
        from main_app.services.strategies.fx_trend import H10_PAIRS
        for sym in H10_PAIRS:
            inst, _ = Instrument.objects.get_or_create(
                symbol=sym, defaults={'asset_class': 'forex', 'market': 'forex'})
            Bar.objects.bulk_create([
                Bar(instrument=inst, timeframe='1Hour', ts=ts, open=1.1, high=1.1,
                    low=1.1, close=1.1, volume=0)
                for ts in pd.date_range(first, periods=n, freq=freq, tz='UTC')])

    def _two_runs_log_two_entries(self, expected_status):
        self.seed(self.path, dict(self.base, runs=[]))
        self._run()
        self._run()
        runs = json.loads(self.path.read_text())['runs']
        self.assertEqual(len(runs), 2, f'two invocations logged {len(runs)} entries')
        for entry in runs:
            self.assertIn('at', entry)
            self.assertEqual(entry.get('status'), expected_status,
                             'the entry does not say which exit it came from')

    def test_two_runs_with_no_bars_at_all_log_two_entries(self):
        """The claim I made in a RESULT and had not built. THREE of the four exits
        returned before the log — no bars, no new bars, warm-up too short — which
        are precisely the quiet nights the log exists to distinguish from a nightly
        job that never ran. One test per exit, because a test covering only one of
        them showed nothing when I mutated the others."""
        self._two_runs_log_two_entries('no_bars')

    def test_two_runs_with_no_NEW_bars_log_two_entries(self):
        # Bars exist, all before the forward start, so _window_bounds succeeds and
        # `end <= start` is the exit taken.
        self._bars(datetime(2026, 9, 1, tzinfo=timezone.utc), 48)
        self._two_runs_log_two_entries('no_new_bars')

    def test_two_runs_with_too_little_warmup_log_two_entries(self):
        # Bars either side of the start, but far fewer than FxTrend's 760-bar
        # warm-up, so the "silence would look like no trades" exit is taken.
        self._bars(datetime(2026, 9, 23, tzinfo=timezone.utc), 120)
        self._two_runs_log_two_entries('warmup_short')

    def test_a_quiet_run_does_not_disturb_the_measurement(self):
        """A night that measured nothing must not be able to touch trades or totals."""
        seeded = dict(self.base, trades={'k': {'pnl': 1.0}}, totals={'1x': {'trades': 1}})
        self.seed(self.path, seeded)
        self._run()
        doc = json.loads(self.path.read_text())
        self.assertEqual(doc['trades'], {'k': {'pnl': 1.0}})
        self.assertEqual(doc['totals'], {'1x': {'trades': 1}})

    def test_a_run_does_not_drop_a_superseded_block_or_the_run_log(self):
        self.seed(self.path, self.base)
        self._run()
        doc = self.assert_survived(self.path)
        self.assertEqual(doc['runs'][0]['at'], '2026-09-24T02:00:00+00:00',
                         'an existing run-log entry was rewritten or dropped')

    def test_the_run_log_is_append_only_and_never_truncated(self):
        """`runs` is provenance by nature — it is how a missed nightly run is told
        apart from a night where nothing happened. It used to keep only the last 19
        entries, which silently dropped the older ones."""
        seeded = [{'at': f'2026-0{1 + i // 28}-{1 + i % 28:02d}T02:00:00+00:00',
                   'status': 'recorded', 'added': 0} for i in range(40)]
        self.seed(self.path, dict(self.base, runs=seeded))
        self._run()
        runs = json.loads(self.path.read_text())['runs']
        self.assertEqual(len(runs), 41, f'the log holds {len(runs)}, expected 40 + 1')
        self.assertEqual(runs[:40], seeded, 'existing run-log entries were rewritten')
