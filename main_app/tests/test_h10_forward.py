"""The H10 forward test: the window must hold, and the spec must be H10's.

Both tests exist because of mistakes already made on this desk. Trades leaked
past a window end when only `act_from` was set — 38% of one "train" window was
test data. And a strategy was measured under parameters that had drifted from the
ones the hypothesis recorded, which makes the measurement about something else.
"""
from datetime import datetime, timezone

from django.test import SimpleTestCase, TestCase

from main_app.management.commands.h10_forward import spread_multiplier
from main_app.services.strategies import make_strategy
from main_app.services.strategies.fx_trend import (H10_HELD_OUT, H10_PAIRS, H10_RISK, H10_SPEC,
                                                   H10_TIMEFRAME)


class TheForwardRunUsesH10sFrozenSpec(SimpleTestCase):
    """The spec lives in ONE place — H10_SPEC in services/strategies/fx_trend.py —
    and the command asserts against it. A run on different numbers is a different
    hypothesis and must not be reported as a forward test of this one."""

    def test_the_spec_is_the_one_recorded_for_hypothesis_10(self):
        self.assertEqual(H10_SPEC, {
            'lookback_h': 480, 'min_move_atr': 1.0, 'stop_atr_mult': 4.0, 'atr_len': 24,
            'cooldown_h': 168, 'allow_short': True, 'hour_from': 7, 'hour_to': 21})
        self.assertEqual(H10_RISK, {'max_hold_minutes': 7200, 'min_reward_to_cost': 0.0})
        self.assertEqual(H10_TIMEFRAME, '1Hour')
        self.assertEqual(set(H10_PAIRS), {'EUR/USD', 'GBP/USD', 'AUD/USD', 'NZD/USD'})
        self.assertEqual(H10_HELD_OUT, ('2026-01-01', '2026-09-07'))

    def test_the_strategy_actually_receives_every_frozen_parameter(self):
        """Strategy.__init__ silently drops any key with no matching Param, which
        is how trade_short sat in a config being read by nothing."""
        s = make_strategy('fx_trend', dict(H10_SPEC))
        for k, v in H10_SPEC.items():
            self.assertIn(k, s.p, f'{k} was dropped — it is not a declared Param')
            self.assertEqual(s.p[k], v, k)

    def test_the_default_strategy_is_unchanged_by_the_new_hour_params(self):
        s = make_strategy('fx_trend', {})
        self.assertEqual((s.p['hour_from'], s.p['hour_to']), (0, 24))


class TheEntryWindowHoldsAtBothEnds(TestCase):
    """act_from stops the engine acting early; the trade filter stops it late.
    Setting only the first is the leak that invalidated a whole train window."""

    def setUp(self):
        from main_app.models import Bar, Instrument
        from main_app.tests.helpers import seed_db
        seed_db(('EUR/USD',), with_bars=False)
        self.inst = Instrument.objects.filter(symbol='EUR/USD').first() or \
            Instrument.objects.create(symbol='EUR/USD', asset_class='forex', market='forex')
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        from datetime import timedelta
        px = 1.10
        for i in range(400):
            px += 0.0004 if (i // 40) % 2 == 0 else -0.0004
            Bar.objects.create(instrument=self.inst, timeframe='1Hour',
                               ts=base + timedelta(hours=i), open=px, high=px + 0.001,
                               low=px - 0.001, close=px, volume=1000)

    def test_no_trade_enters_before_the_window_or_after_it(self):
        from main_app.management.commands.h10_forward import run_window
        from main_app.services.backtest import load_frames
        start = datetime(2026, 1, 8, tzinfo=timezone.utc)
        end = datetime(2026, 1, 12, tzinfo=timezone.utc)
        frames = load_frames(['EUR/USD'], '1Hour', datetime(2026, 1, 1).date(), datetime(2026, 1, 20).date())
        # A short-warmup strategy, so the fixture can actually produce trades.
        params = dict(H10_SPEC, lookback_h=12, atr_len=6, cooldown_h=1, hour_from=0, hour_to=24)
        trades, _ = run_window('fx_trend', params, dict(H10_RISK), frames, start, end)
        for t in trades:
            self.assertGreaterEqual(t.entry_ts, start, 'an entry landed BEFORE the window')
            self.assertLessEqual(t.entry_ts, end, 'an entry landed AFTER the window')


class TheSpreadModelIsShapedByTheClock(SimpleTestCase):
    """A flat toll is the wrong shape for FX and it decided a different
    hypothesis: H4's edge lived entirely in the widest-spread hours."""

    def test_london_and_new_york_are_the_cheap_hours(self):
        for h in (7, 12, 16, 20):
            self.assertEqual(spread_multiplier(h), 1.0)

    def test_the_rollover_window_is_the_expensive_one(self):
        for h in (21, 22, 23, 0, 1):
            self.assertEqual(spread_multiplier(h), 3.0)

    def test_asia_sits_between_them(self):
        self.assertEqual(spread_multiplier(3), 1.5)
