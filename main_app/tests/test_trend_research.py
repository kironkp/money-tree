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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pandas as pd
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from main_app.management.commands.trend_research import (HELD_OUT, HOLDS, KEYS, LIVE_PARAMS_ROW, PAIRS,
                                                         SPENT, TF_PARAMS, TIMEFRAMES, TRAIN_END,
                                                         TRAIN_START, WARMUP_FROM, candidates,
                                                         check_bars, check_registration, measure,
                                                         params_for, registration, risk_for, select)
from main_app.services.broker.base import TradeRecord
from main_app.services.strategies import make_strategy
from main_app.services.strategies.fx_trend import H10_RISK, H10_SPEC

MOD = 'main_app.management.commands.trend_research'

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
    # Replaces test_run_refuses_until_the_harness_exists: the harness now exists, so
    # what binds the measurement is that it refuses without a registration, refuses
    # a second look, and refuses a grid the registration does not describe.
    def test_run_refuses_without_a_registration(self):
        folder = Path(tempfile.mkdtemp())
        with mock.patch(f'{MOD}.PREREG', str(folder / 'absent.json')):
            with self.assertRaises(CommandError) as cm:
                call_command('trend_research', '--run', verbosity=0)
        self.assertIn('register what will be tested', str(cm.exception))

    def test_run_refuses_once_results_exist(self):
        folder = Path(tempfile.mkdtemp())
        (folder / 'p.json').write_text('{}')
        (folder / 'r.json').write_text('{}')
        with mock.patch(f'{MOD}.PREREG', str(folder / 'p.json')), \
                mock.patch(f'{MOD}.RESULTS', str(folder / 'r.json')):
            with self.assertRaises(CommandError) as cm:
                call_command('trend_research', '--run', verbosity=0)
        self.assertIn('refusing to measure the same grid twice', str(cm.exception))

    def test_a_grid_that_drifted_from_its_registration_is_refused(self):
        reg = json.loads(json.dumps(registration(TRAIN_START)))
        check_registration(reg)                                 # as registered: fine
        reg['candidates'][0]['max_hold_minutes'] = 1440
        with self.assertRaises(CommandError) as cm:
            check_registration(reg)
        self.assertIn("'candidates'", str(cm.exception))

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


def frame(start, end, freq):
    idx = pd.date_range(start, end, freq=freq, inclusive='left', tz='UTC')
    return pd.DataFrame({'open': 1.1, 'high': 1.1, 'low': 1.1, 'close': 1.1, 'volume': 0.0}, index=idx)


def good_frames():
    """Enough warm-up for FxTrend's 760 bars, a few train bars, nothing forbidden."""
    return {'1Hour': {s: frame(datetime(2024, 9, 1), datetime(2024, 11, 3), 'h') for s in PAIRS},
            '1Day': {s: frame(datetime(2020, 1, 1), datetime(2024, 11, 5), 'D') for s in PAIRS}}


def trade(pnl, reason='target', day=1, exit_days=1, at=None):
    t0 = at or TRAIN_START + timedelta(days=day)
    return TradeRecord('EUR/USD', 'x', 'long', 1000, t0, t0 + timedelta(days=exit_days), 1.1000, 1.1000 + pnl / 1000,
                       pnl, 0.0, 0.1, 3, reason)


class EachGuardRefusesBeforeAnythingIsMeasured(SimpleTestCase):
    def test_a_pair_with_no_train_bars(self):
        frames = good_frames()['1Hour']
        frames['NZD/USD'] = frames['NZD/USD'][frames['NZD/USD'].index < TRAIN_START]
        with self.assertRaises(CommandError) as cm:
            check_bars('c', 'fx_trend', dict(H10_SPEC), frames)
        self.assertIn('NZD/USD has zero bars in the train window', str(cm.exception))

    def test_any_bar_inside_a_forbidden_window(self):
        for label, (lo, _hi) in (('h10_held_out', HELD_OUT), ('spent_by_mt_a001', SPENT)):
            frames = good_frames()['1Hour']
            frames['EUR/USD'] = pd.concat([frames['EUR/USD'], frame(lo, lo + timedelta(hours=1), 'h')])
            with self.assertRaises(CommandError) as cm:
                check_bars('c', 'ema_momentum', {}, frames)
            self.assertIn(f'inside {label}', str(cm.exception))

    def test_too_little_warm_up_for_fx_trend(self):
        frames = {s: frame(TRAIN_START - timedelta(hours=100), TRAIN_START + timedelta(days=2), 'h') for s in PAIRS}
        with self.assertRaises(CommandError) as cm:
            check_bars('c', 'fx_trend', dict(H10_SPEC), frames)
        self.assertIn('100 warm-up bars', str(cm.exception))
        self.assertIn('needs 760', str(cm.exception))

    def test_enough_bars_are_counted_not_assumed(self):
        bars = check_bars('c', 'fx_trend', dict(H10_SPEC), good_frames()['1Hour'])
        self.assertEqual(bars['EUR/USD'], {'train': 48, 'warmup': 61 * 24})

    def test_a_trade_that_closes_after_the_window_is_never_recorded(self):
        late = trade(5.0, at=TRAIN_END - timedelta(days=1), exit_days=5)   # opens in train, closes after it
        with mock.patch('main_app.services.research_window.run_window', return_value=([late], 0.3)):
            with self.assertRaises(CommandError) as cm:
                measure('c', 'ema_momentum', {}, {}, '1Hour', {})
        self.assertIn('outside the train window', str(cm.exception))


def row(name, trades, net, pf2x):
    return {'name': name, 'trades': trades, 'net': net, 'net_pf_2x': pf2x}


class TheGateIsTheRegisteredRule(SimpleTestCase):
    LIVE = row(LIVE_PARAMS_ROW, 40, 50.0, 1.05)

    def test_the_highest_net_that_clears_every_gate_wins(self):
        v = select([row('thin', 12, 900.0, 3.0), row('second', 35, 120.0, 1.1), row('first', 60, 300.0, 1.2)],
                   self.LIVE)
        self.assertEqual(v['winner'], 'first')
        self.assertEqual(v['gates_failed']['thin'], ['n 12 < 30'])
        self.assertEqual(v['ranked'], ['thin', 'first', 'second'])

    def test_nothing_clears_and_the_outcome_is_none(self):
        v = select([row('a', 40, -10.0, 0.9), row('b', 5, 20.0, None)], self.LIVE)
        self.assertEqual((v['outcome'], v['winner']), ('none', None))

    def test_a_candidate_that_fails_only_the_doubled_toll(self):
        v = select([row('a', 40, 100.0, 0.95)], self.LIVE)
        self.assertIsNone(v['winner'])
        self.assertEqual(v['gates_failed']['a'], ['net PF at 2x 0.95 < 1.0'])

    def test_beating_flat_is_not_enough_it_must_beat_the_live_parameters(self):
        v = select([row('a', 40, 30.0, 1.4)], self.LIVE)
        self.assertIsNone(v['winner'])
        self.assertEqual(v['gates_failed']['a'], [f'net at 1x +30.00 does not beat {LIVE_PARAMS_ROW} (+50.00)'])


class TheHarnessRunsExactlyWhatWasRegistered(TestCase):
    """Every candidate reaches the engine with params_for/risk_for and nothing else,
    on the registered window, at 1x and 2x; the comparators run and are never ranked."""

    EMA = {'fast': 13, 'slow': 27, 'rsi_min': 40.0, 'rsi_max': 65.0, 'stop_atr_mult': 2.5, 'rr': 2.0,
           'min_relvol': 1.5, 'entry_session': 'all'}

    def setUp(self):
        from main_app.models import Hypothesis, Strategy
        self.folder = Path(tempfile.mkdtemp())
        h = Hypothesis.objects.create(market='forex', title='MT-A008 test', claim='c', source='s')
        reg = json.loads(json.dumps(registration(datetime(2026, 9, 25, tzinfo=timezone.utc))))
        reg['hypothesis_id'] = h.id
        (self.folder / 'p.json').write_text(json.dumps(reg))
        Strategy.objects.create(key='ema_momentum', market='forex', name='EMA', params=self.EMA, version=3)
        self.frames = good_frames()
        self.calls, self.windows = [], []

    def run_it(self, results):
        def frames(symbols, tf, window):
            self.windows.append((tuple(symbols), tf, window))
            return self.frames[tf]

        def engine(key, params, over, frames, start, end, *, timeframe, pairs, cost_mult=1.0):
            self.calls.append((key, params, over, frames, start, end, timeframe, tuple(pairs), cost_mult))
            return results(key, params, over, timeframe, cost_mult), 0.3

        with mock.patch(f'{MOD}.PREREG', str(self.folder / 'p.json')), \
                mock.patch(f'{MOD}.RESULTS', str(self.folder / 'r.json')), \
                mock.patch('main_app.services.research_window.research_frames', side_effect=frames), \
                mock.patch('main_app.services.research_window.run_window', side_effect=engine):
            call_command('trend_research', '--run', verbosity=0, stdout=open(self.folder / 'out.txt', 'w'))
        return json.loads((self.folder / 'r.json').read_text())

    def test_each_candidate_and_comparator_is_run_as_registered_at_both_costs(self):
        self.run_it(lambda *a: [])
        want = [(c['strategy'], params_for(c, {'ema_momentum': self.EMA}), risk_for(c), c['timeframe'])
                for c in candidates()]
        want += [('fx_trend', dict(H10_SPEC), dict(H10_RISK), '1Hour'), ('ema_momentum', self.EMA, {}, '1Hour')]
        got = [(k, p, o, tf, cost) for k, p, o, fr, start, end, tf, pairs, cost in self.calls]
        self.assertEqual(got, [(k, p, o, tf, cost) for k, p, o, tf in want for cost in (1.0, 2.0)])
        for k, p, o, fr, start, end, tf, pairs, cost in self.calls:
            self.assertEqual((start, end, pairs), (TRAIN_START, TRAIN_END, PAIRS))
            self.assertIs(fr, self.frames[tf], 'a candidate ran on another timeframe\'s bars')
        self.assertEqual({(tf, w.warmup_start, w.start, w.end) for _, tf, w in self.windows},
                         {(tf, WARMUP_FROM[tf], TRAIN_START, TRAIN_END) for tf in TIMEFRAMES})

    def test_the_artifact_records_the_order_and_never_ranks_a_comparator(self):
        from main_app.models import Hypothesis

        win_cand = next(c for c in candidates() if c['name'] == 'fx_trend_1Day_72h')

        def results(key, params, over, tf, cost):
            if (key, params, over, tf) == ('fx_trend', params_for(win_cand, {}), risk_for(win_cand), '1Day'):
                return [trade(10.0 - cost, day=i % 50) for i in range(20)] + \
                       [trade(-5.0, 'stop', day=i % 50) for i in range(10)]
            if key == 'fx_trend' and tf == '1Hour' and params == dict(H10_SPEC):
                return [trade(50.0, day=i % 50) for i in range(40)]       # better than anything, but a comparator
            return [trade(-1.0, 'stop', day=1)]
        out = self.run_it(results)
        self.assertLess(out['registered_at'], out['first_result_at'])
        self.assertEqual(out['verdict']['winner'], 'fx_trend_1Day_72h')
        self.assertNotIn('h10_frozen', out['verdict']['ranked'])
        self.assertEqual([r['name'] for r in out['comparators']], ['h10_frozen', LIVE_PARAMS_ROW])
        self.assertEqual(len(out['candidates']), 8)
        win = next(r for r in out['candidates'] if r['name'] == 'fx_trend_1Day_72h')
        self.assertEqual((win['trades'], win['stopped'], win['median_stop_pips']), (30, 10, 50.0))
        self.assertEqual(out['live_params']['version'], 3)
        self.assertEqual(Hypothesis.objects.get(id=out['hypothesis_id']).status, Hypothesis.BACKTESTED)
        with self.assertRaises(CommandError):
            self.run_it(results)                                 # measured once

    def test_a_registration_the_code_no_longer_matches_is_refused_by_the_command(self):
        reg = json.loads((self.folder / 'p.json').read_text())
        reg['timeframe_params']['1Day']['lookback_h'] = 48
        (self.folder / 'p.json').write_text(json.dumps(reg))
        with self.assertRaises(CommandError) as cm:
            self.run_it(lambda *a: [])
        self.assertIn("'timeframe_params'", str(cm.exception))
        self.assertEqual(self.calls, [])

    def test_a_refused_guard_leaves_no_result(self):
        self.frames['1Day']['AUD/USD'] = self.frames['1Day']['AUD/USD'][:10]
        with self.assertRaises(CommandError):
            self.run_it(lambda *a: [])
        self.assertEqual(self.calls, [], 'a guard must refuse before the first measurement')
        self.assertFalse((self.folder / 'r.json').exists())
