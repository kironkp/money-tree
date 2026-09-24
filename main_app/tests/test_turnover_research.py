"""MT-A003: the pre-registration must bind, and the rule must be applied as written.

Two separate risks. A harness that will produce a number without a registration
lets the selection rule be chosen after the results, which is how a grid search
gets reported as a discovery. And a selection rule implemented loosely — "near
enough" on a gate — is the same failure one layer down, where nobody looks.
"""
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from main_app.management.commands.turnover_research import (CANDIDATES, MIN_NET_PF_AT_2X,
                                                            MIN_TRAIN_TRADES, STRATEGIES,
                                                            TRAIN_END, _select)


class TheRegistrationBindsTheMeasurement(SimpleTestCase):
    def test_run_refuses_without_a_registration(self):
        with mock.patch('main_app.management.commands.turnover_research.PREREG',
                        str(Path(tempfile.mkdtemp()) / 'absent.json')):
            with self.assertRaises(CommandError) as cm:
                call_command('turnover_research', '--run', verbosity=0)
        self.assertIn('before measuring it', str(cm.exception))

    def test_registering_twice_is_refused(self):
        folder = Path(tempfile.mkdtemp())
        existing = folder / 'prereg.json'
        existing.write_text(json.dumps({'registered_at': '2026-09-24T22:44:23+00:00'}))
        with mock.patch('main_app.management.commands.turnover_research.PREREG', str(existing)):
            with self.assertRaises(CommandError) as cm:
                call_command('turnover_research', '--preregister', verbosity=0)
        self.assertIn('already exists', str(cm.exception))

    def test_registering_after_results_exist_is_refused(self):
        """The one that actually matters: re-registering once a number is known is
        exactly the thing pre-registration exists to prevent."""
        folder = Path(tempfile.mkdtemp())
        results = folder / 'results.json'
        results.write_text('{}')
        with mock.patch('main_app.management.commands.turnover_research.PREREG',
                        str(folder / 'absent.json')), \
             mock.patch('main_app.management.commands.turnover_research.RESULTS', str(results)):
            with self.assertRaises(CommandError) as cm:
                call_command('turnover_research', '--preregister', verbosity=0)
        self.assertIn('refusing to re-register', str(cm.exception))

    def test_exactly_one_mode_must_be_chosen(self):
        for args in (['--preregister', '--run'], []):
            with self.assertRaises(CommandError):
                call_command('turnover_research', *args, verbosity=0)

    def test_selection_never_touches_the_spent_window(self):
        self.assertEqual(TRAIN_END, datetime(2026, 9, 8, tzinfo=timezone.utc),
                         'the train window must stop before the window MT-A001 spent')

    def test_the_candidate_set_is_small_and_changes_one_thing_at_a_time(self):
        self.assertLessEqual(len(CANDIDATES), 6)
        names = [c[0] for c in CANDIDATES]
        self.assertEqual(names[0], 'baseline', 'the baseline must be in the table it is judged against')
        self.assertEqual(CANDIDATES[0][2], {}, 'the baseline must carry no override')


def _row(strategy, candidate, net, trades, pf2x, net_2x=0.0):
    return {'strategy': strategy, 'candidate': candidate, 'net': net, 'trades': trades,
            'net_pf_2x': pf2x, 'net_2x': net_2x}


class ThePreRegisteredRuleIsAppliedExactly(SimpleTestCase):
    """Each gate is proven load-bearing by failing exactly one at a time."""
    S = 'ema_momentum'

    def _rows(self, **over):
        cand = dict(net=500.0, trades=MIN_TRAIN_TRADES, pf2x=MIN_NET_PF_AT_2X)
        cand.update(over)
        return [_row(self.S, 'baseline', -100.0, 200, 0.5),
                _row(self.S, 'gate_4x', cand['net'], cand['trades'], cand['pf2x'])]

    def test_a_candidate_clearing_every_gate_is_frozen(self):
        v = _select(self.S, self._rows())
        self.assertTrue(v['frozen'])
        self.assertEqual(v['winner'], 'gate_4x')
        self.assertEqual(v['forward_confirmation_start'], '2026-09-25T00:00:00+00:00')

    def test_the_gates_are_inclusive_at_their_boundary(self):
        """n >= 30 and PF >= 1.0 as registered, not > — a rule that quietly moves
        its own boundary is not the rule that was registered."""
        self.assertTrue(_select(self.S, self._rows())['frozen'])

    def test_too_few_trades_blocks_rather_than_selecting(self):
        v = _select(self.S, self._rows(trades=MIN_TRAIN_TRADES - 1))
        self.assertFalse(v['frozen'])
        self.assertEqual(v['outcome'], 'BLOCKED: insufficient train data')
        self.assertIn(str(MIN_TRAIN_TRADES - 1), ' '.join(v['why']), 'the number must be stated')

    def test_failing_the_doubled_cost_gate_is_not_frozen(self):
        v = _select(self.S, self._rows(pf2x=MIN_NET_PF_AT_2X - 0.01))
        self.assertFalse(v['frozen'])
        self.assertEqual(v['outcome'], 'none beat baseline')

    def test_a_candidate_that_beat_the_baseline_but_failed_a_gate_is_named(self):
        """The headline label is the registered one, so the near-miss has to be
        spelled out or the table reads as if nothing helped at all."""
        v = _select(self.S, self._rows(pf2x=0.89))
        why = ' '.join(v['why'])
        self.assertIn('did beat the baseline on net', why)
        self.assertIn('net PF at 2x', why)

    def test_not_beating_the_baseline_is_not_frozen_however_good_it_looks(self):
        rows = [_row(self.S, 'baseline', 900.0, 200, 2.0),
                _row(self.S, 'gate_4x', 800.0, 100, 3.0)]
        v = _select(self.S, rows)
        self.assertFalse(v['frozen'])
        self.assertNotIn('did beat the baseline', ' '.join(v['why']))

    def test_each_strategy_is_selected_independently(self):
        rows = self._rows() + [_row('vwap_reversion', 'baseline', -100.0, 200, 0.5),
                               _row('vwap_reversion', 'gate_4x', -500.0, 200, 0.4)]
        self.assertTrue(_select(self.S, rows)['frozen'])
        self.assertFalse(_select('vwap_reversion', rows)['frozen'])


class TheResearchIsRecordedAgainstAHypothesis(TestCase):
    def test_preregistering_creates_a_hypothesis_with_a_source(self):
        from main_app.models import Hypothesis
        folder = Path(tempfile.mkdtemp())
        with mock.patch('main_app.management.commands.turnover_research.PREREG',
                        str(folder / 'p.json')), \
             mock.patch('main_app.management.commands.turnover_research.RESULTS',
                        str(folder / 'r.json')):
            call_command('turnover_research', '--preregister', verbosity=0)
        h = Hypothesis.objects.get(title__startswith='MT-A003')
        self.assertTrue(h.source.strip(), 'a hypothesis with no source is not recorded')
        self.assertEqual(h.status, Hypothesis.PROPOSED, 'status must not anticipate a result')
        self.assertEqual(h.train_result, {}, 'a registration must carry no result')
        reg = json.loads(h.rationale)
        self.assertEqual(reg['forward_confirmation_start'], '2026-09-25T00:00:00+00:00')
        self.assertIn('argmax', reg['what_is_not_claimed'])
        self.assertEqual(len(reg['candidates']), len(CANDIDATES))
        self.assertEqual(set(reg['strategies']), set(STRATEGIES))
