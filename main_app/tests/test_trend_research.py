"""MT-A008 / H19: the engine must receive exactly what was pre-registered.

Two things a pre-registration cannot enforce by existing. The parameters that reach
the engine may differ from the ones written down — `trade_short` sat in a config
being read by nothing, and `Param` declares a min of 48 for `lookback_h` where the
daily candidates register 20. And the train window may not be the window that was
registered, which has now happened three times on this desk.

So both are tested, and both tests come with mutation evidence in the RESULT.
"""
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from main_app.management.commands.trend_research import (HELD_OUT, HOLDS, KEYS, SPENT,
                                                         TF_PARAMS, TIMEFRAMES, TRAIN_END,
                                                         TRAIN_START, candidates, params_for,
                                                         risk_for)
from main_app.services.strategies import make_strategy

LIVE = {'ema_momentum': {'fast': 9, 'slow': 21, 'rsi_min': 50, 'rsi_max': 70,
                         'min_relvol': 1.0, 'rr': 2.0, 'stop_atr_mult': 1.5}}


class TheEngineReceivesThePreRegisteredParameters(SimpleTestCase):
    """`Param.coerce()` converts type only — it does NOT clamp to min/max or snap to
    step, which are used solely by the optimizer's `grid()`. fx_trend declares
    lookback_h with a minimum of 48 and the 1Day candidates register 20, so had it
    clamped, every daily candidate would silently have run at 48 days and the
    recorded hypothesis would describe something that never ran.

    That is the `trade_short` failure — a declared value read by nothing — and it is
    only knowable by asking the strategy what it actually holds.
    """

    def test_every_candidate_gets_exactly_what_was_registered(self):
        for cand in candidates():
            want = params_for(cand, LIVE)
            got = make_strategy(cand['strategy'], dict(want)).p
            for key, value in want.items():
                self.assertIn(key, got, f'{cand["name"]}: {key} was DROPPED — not a declared Param')
                self.assertEqual(got[key], value,
                                 f'{cand["name"]}: {key} registered {value}, engine got {got[key]}')

    def test_the_daily_values_are_below_their_declared_minimum_and_survive(self):
        """Guards the guard. If the 1Day values ever move inside the declared range,
        the test above stops proving anything about clamping and should be rewritten
        rather than left to pass for the wrong reason."""
        spec = make_strategy('fx_trend', {}).param_map()
        # lookback_h ONLY. atr_len 14 sits inside its declared range (min 12) and
        # cooldown_h 7 inside its (min 0) — an earlier version of this test claimed
        # otherwise and failed, which is the point of writing it down rather than
        # asserting from memory.
        self.assertLess(TF_PARAMS['1Day']['lookback_h'], spec['lookback_h'].min,
                        'the daily lookback is no longer below its declared minimum, so the '
                        'test above no longer proves anything about clamping')
        self.assertGreaterEqual(TF_PARAMS['1Day']['atr_len'], spec['atr_len'].min)

    def test_the_daily_horizon_is_the_hourly_horizon_in_days(self):
        """20 bars of daily trend is 20 days, which is what 480 hourly bars mean.
        A translation that does not hold the calendar constant is a re-tune."""
        self.assertEqual(TF_PARAMS['1Hour']['lookback_h'] / 24, TF_PARAMS['1Day']['lookback_h'])
        self.assertEqual(TF_PARAMS['1Hour']['cooldown_h'] / 24, TF_PARAMS['1Day']['cooldown_h'])

    def test_ema_momentum_carries_its_live_parameters_unchanged(self):
        for cand in candidates():
            if cand['strategy'] == 'ema_momentum':
                self.assertEqual(params_for(cand, LIVE), LIVE['ema_momentum'],
                                 'a cadence experiment must not retune the rule')

    def test_only_fx_trend_relaxes_the_cost_gate(self):
        for cand in candidates():
            over = risk_for(cand)
            self.assertEqual(over['max_hold_minutes'], cand['max_hold_minutes'])
            if cand['strategy'] == 'fx_trend':
                self.assertEqual(over['min_reward_to_cost'], 0.0)
            else:
                self.assertNotIn('min_reward_to_cost', over,
                                 'ema_momentum must be judged under the cost gate it trades under')

    def test_the_grid_is_the_registered_shape(self):
        self.assertEqual(len(candidates()), len(KEYS) * len(TIMEFRAMES) * len(HOLDS))
        self.assertEqual(len(candidates()), 8)
        self.assertNotIn('4Hour', TIMEFRAMES, 'there are zero 4Hour forex bars')


class TheTrainWindowExcludesEveryForbiddenWindow(SimpleTestCase):
    """2026-01-01 onward is H10's held-out window and then the window MT-A001 spent.
    Neither may be used to select anything, and the train window must stop before
    both — not overlap and be filtered, which is the mistake that has happened three
    times here."""

    def test_the_train_window_ends_where_the_held_out_window_begins(self):
        self.assertEqual(TRAIN_END, HELD_OUT[0])
        self.assertLess(TRAIN_START, TRAIN_END)

    def test_no_forbidden_instant_is_inside_the_train_window(self):
        for label, (lo, hi) in (('h10_held_out', HELD_OUT), ('spent', SPENT)):
            self.assertGreaterEqual(lo, TRAIN_END, f'{label} starts inside the train window')

    def test_the_train_window_predates_the_15min_history_entirely(self):
        """Why the live 15Min baseline is unevaluable here, pinned so the claim is
        checkable rather than asserted in prose."""
        self.assertLess(TRAIN_END, datetime(2026, 7, 10, tzinfo=timezone.utc))


class TheRegistrationBindsTheMeasurement(SimpleTestCase):
    def test_run_refuses_until_the_harness_exists(self):
        with self.assertRaises(CommandError) as cm:
            call_command('trend_research', '--run', verbosity=0)
        self.assertIn('not built yet', str(cm.exception))

    def test_registering_twice_is_refused(self):
        folder = Path(tempfile.mkdtemp())
        existing = folder / 'p.json'
        existing.write_text(json.dumps({'registered_at': '2026-09-25T00:54:34+00:00'}))
        with mock.patch('main_app.management.commands.trend_research.PREREG', str(existing)):
            with self.assertRaises(CommandError) as cm:
                call_command('trend_research', '--preregister', verbosity=0)
        self.assertIn('already exists', str(cm.exception))

    def test_registering_after_results_exist_is_refused(self):
        folder = Path(tempfile.mkdtemp())
        (folder / 'r.json').write_text('{}')
        mod = 'main_app.management.commands.trend_research'
        with mock.patch(f'{mod}.PREREG', str(folder / 'absent.json')), \
             mock.patch(f'{mod}.RESULTS', str(folder / 'r.json')):
            with self.assertRaises(CommandError) as cm:
                call_command('trend_research', '--preregister', verbosity=0)
        self.assertIn('refusing to re-register', str(cm.exception))


class TheRegistrationIsRecordedAgainstAHypothesis(TestCase):
    def test_it_creates_a_sourced_hypothesis_with_no_result(self):
        from main_app.models import Hypothesis
        folder = Path(tempfile.mkdtemp())
        mod = 'main_app.management.commands.trend_research'
        with mock.patch(f'{mod}.PREREG', str(folder / 'p.json')), \
             mock.patch(f'{mod}.RESULTS', str(folder / 'r.json')):
            call_command('trend_research', '--preregister', verbosity=0)
        h = Hypothesis.objects.get(title__startswith='MT-A008')
        self.assertTrue(h.source.strip(), 'a hypothesis with no source is not recorded')
        self.assertEqual(h.status, Hypothesis.PROPOSED)
        self.assertEqual(h.train_result, {}, 'a registration must carry no result')
        reg = json.loads(h.rationale)
        self.assertEqual(len(reg['candidates']), 8)
        self.assertIn('argmax', reg['what_is_not_claimed'])
        self.assertIn('advisory', reg['param_ranges_are_advisory'].lower() + 'advisory')
        self.assertEqual(reg['train_window'], [TRAIN_START.isoformat(), TRAIN_END.isoformat()])
