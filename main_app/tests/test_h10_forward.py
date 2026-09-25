"""The H10 forward test: the window must hold, and the spec must be H10's.

Both tests exist because of mistakes already made on this desk. Trades leaked
past a window end when only `act_from` was set — 38% of one "train" window was
test data. And a strategy was measured under parameters that had drifted from the
ones the hypothesis recorded, which makes the measurement about something else.
"""
from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase, TestCase

from main_app.management.commands.h10_forward import (coverage, coverage_gaps,
                                                      spread_multiplier)
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
    Setting only the first is the leak that put 38% of a train window's trades
    inside the test window.

    The first version of this test was vacuous: the fixture had 400 bars against
    FxTrend's 760-bar warm-up constant, so no trade was ever produced and the
    assertion loop ran zero times. It passed, proved nothing, and carried a
    comment claiming the params made warm-up short — they do not, warmup_bars is
    a class constant. Hence the negative control below: if the end filter is
    removed, this test MUST fail.
    """
    WARMUP = 900        # comfortably over FxTrend.warmup_bars (760)

    def setUp(self):
        from datetime import timedelta
        from main_app.models import Bar, Instrument
        self.inst = (Instrument.objects.filter(symbol='EUR/USD').first()
                     or Instrument.objects.create(symbol='EUR/USD', asset_class='forex',
                                                  market='forex'))
        self.base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # A series that actually trends, so the 480-bar lookback clears its ATR
        # threshold and entries fire on both sides of the window boundary.
        px, n = 1.10, self.WARMUP + 900
        for k in range(n):
            leg = (k // 150) % 2
            px += 0.0012 if leg == 0 else -0.0012
            Bar.objects.create(instrument=self.inst, timeframe='1Hour',
                               ts=self.base + timedelta(hours=k), open=px,
                               high=px + 0.0006, low=px - 0.0006, close=px, volume=1000)
        self.start = self.base + timedelta(hours=self.WARMUP)
        self.end = self.start + timedelta(hours=300)

    def _frames(self, end=None):
        """Frames bounded at the window end, as production now supplies them.

        Before MT-A005 these tests handed run_window frames running well past the
        end and relied on the ENTRY filter alone to bound the window. That is no
        longer the contract: frames arrive bounded and the entry filter is the
        second line of defence, not the first. `test_run_window_refuses_frames_...`
        below pins the new half.
        """
        from datetime import timedelta
        from main_app.services.backtest import load_frames
        from main_app.services.research_window import truncate
        frames = load_frames(['EUR/USD'], '1Hour', self.base.date(),
                             (self.base + timedelta(hours=self.WARMUP + 1000)).date())
        return truncate(frames, end or self.end)

    def _params(self):
        # H10's spec but with the hour gate open, so the synthetic clock cannot
        # silently suppress every entry and hand back another empty test.
        return dict(H10_SPEC, hour_from=0, hour_to=24)

    def test_the_fixture_actually_produces_trades(self):
        """Guards the guard. If this ever returns zero, the window assertions
        below are vacuous again and the suite must say so."""
        from main_app.management.commands.h10_forward import run_window
        trades, _ = run_window('fx_trend', self._params(), dict(H10_RISK),
                               self._frames(), self.start, self.end,
                               timeframe='1Hour', pairs=['EUR/USD'])
        self.assertGreater(len(trades), 0,
                           'fixture produced no trades — the window tests would prove nothing')

    def test_no_trade_enters_before_the_window_or_after_it(self):
        from main_app.management.commands.h10_forward import run_window
        trades, _ = run_window('fx_trend', self._params(), dict(H10_RISK),
                               self._frames(), self.start, self.end,
                               timeframe='1Hour', pairs=['EUR/USD'])
        self.assertGreater(len(trades), 0)
        for t in trades:
            self.assertGreaterEqual(t.entry_ts, self.start, 'an entry landed BEFORE the window')
            # Strictly less: the window is half-open [start, end), so an entry AT the
            # end is outside it. The old assertion allowed one and its message called
            # the end inclusive, which is the disagreement MT-A005 had to settle
            # before a shared guard could be written at all.
            self.assertLess(t.entry_ts, self.end, 'an entry landed AT or AFTER the window end')

    def test_run_window_refuses_frames_that_reach_past_the_window_end(self):
        """The MT-A005 contract, at the choke point every research command uses.

        A command that loads its own frames — the mistake made three times — now
        fails loudly here instead of quietly measuring the future.
        """
        from datetime import timedelta
        from main_app.management.commands.h10_forward import run_window
        from main_app.services.backtest import load_frames
        from main_app.services.research_window import WindowLeak
        leaking = load_frames(['EUR/USD'], '1Hour', self.base.date(),
                              (self.base + timedelta(hours=self.WARMUP + 1000)).date())
        with self.assertRaises(WindowLeak) as cm:
            run_window('fx_trend', self._params(), dict(H10_RISK), leaking, self.start, self.end,
                       timeframe='1Hour', pairs=['EUR/USD'])
        self.assertIn('EUR/USD', str(cm.exception))

    def test_negative_control_the_data_does_contain_a_trade_after_the_window_end(self):
        """The end filter is only load-bearing if something would otherwise cross
        it. Run unbounded over the same data and require an entry past `end`; if
        this ever fails, the filter is untested no matter how green the suite is.
        """
        from datetime import timedelta
        from main_app.management.commands.h10_forward import run_window
        far = self.end + timedelta(hours=5000)
        trades, _ = run_window('fx_trend', self._params(), dict(H10_RISK),
                               self._frames(far), self.start, far,
                               timeframe='1Hour', pairs=['EUR/USD'])
        after = [t for t in trades if t.entry_ts > self.end]
        self.assertGreater(len(after), 0,
                           'nothing trades after the window end, so the end filter is unproven')


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


class ABaselineMustReachTheWindowStart(SimpleTestCase):
    """A baseline replayed on a timeframe whose history does not reach the window
    start measured a SHORTER window, and comparing it like for like credits or
    blames the strategy for the calendar.

    This is not hypothetical here. 15Min forex history begins 2026-07-10 and the
    1Hour series reaches back years, so any window opening before July compares a
    full baseline against a partial one — and MT-A001 already shipped one baseline
    measured on the wrong frames entirely.
    """
    START = datetime(2026, 9, 8, tzinfo=timezone.utc)
    END = datetime(2026, 9, 24, tzinfo=timezone.utc)

    def _frames(self, first_offset_h: float, n: int = 400):
        import pandas as pd
        idx = pd.date_range(self.START + timedelta(hours=first_offset_h), periods=n, freq='15min',
                            tz='UTC')
        return {'EUR/USD': pd.DataFrame({'close': [1.1] * n}, index=idx)}

    def test_coverage_reports_the_first_and_last_bar_in_the_window(self):
        cov = coverage(self._frames(0), self.START, self.END)
        self.assertEqual(cov['EUR/USD']['bars'], 400)
        self.assertEqual(cov['EUR/USD']['first'][:16], '2026-09-08T00:00')

    def test_a_series_that_starts_late_is_flagged(self):
        cov = coverage(self._frames(72), self.START, self.END)
        gaps = coverage_gaps(cov, self.START, '15Min')
        self.assertEqual(len(gaps), 1)
        self.assertIn('72.0h after the window opened', gaps[0])

    def test_a_series_with_no_bars_in_the_window_is_flagged(self):
        import pandas as pd
        idx = pd.date_range(self.START - timedelta(days=30), periods=10, freq='15min', tz='UTC')
        cov = coverage({'EUR/USD': pd.DataFrame({'close': [1.1] * 10}, index=idx)},
                       self.START, self.END)
        self.assertEqual(coverage_gaps(cov, self.START, '15Min'),
                         ['EUR/USD: NO bars inside the window at all'])

    def test_one_bar_late_is_a_boundary_and_not_a_gap(self):
        """Without slack every run would warn about its own first bar, and a
        warning that always fires is one nobody reads."""
        self.assertEqual(coverage_gaps(coverage(self._frames(0.25), self.START, self.END),
                                       self.START, '15Min'), [])

    def test_the_slack_scales_with_the_timeframe(self):
        """Two bars of slack means two HOURS on 1Hour frames, not thirty minutes."""
        cov = coverage(self._frames(1.5), self.START, self.END)
        self.assertEqual(coverage_gaps(cov, self.START, '1Hour'), [])
        self.assertEqual(len(coverage_gaps(cov, self.START, '15Min')), 1)
