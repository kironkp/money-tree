"""Forex vwap_reversion's two re-entry filters, and flatten_all's running-agent guard.

Both filters default off, so a row that does not name them trades exactly as
before. Each is tested on and off, through the engine as well as in isolation,
and the trend guard is proven causal: appending later bars never changes an
earlier bar's value or decision.
"""
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from main_app.models import Account, AgentConfig, AgentRun, Experiment, Strategy
from main_app.services import indicators as ind
from main_app.services.backtest import BacktestSpec, run_backtest
from main_app.services.broker.base import TradeRecord
from main_app.services.optimize import grid_from_schema
from main_app.services.risk import RiskConfig
from main_app.services.strategies.base import Context
from main_app.services.strategies.vwap_reversion import TREND_SLOW, VwapReversion, completed_trend

V3 = {'entry_z': 3.0, 'lookback': 30, 'stop_atr_mult': 1.0, 'min_bar_pos': 3, 'max_bars_held': 36,
      'entry_session': 'london_ny'}
# Loose enough that a random walk trades, stops out and re-signals often.
BUSY = {'entry_z': 1.0, 'lookback': 20, 'stop_atr_mult': 0.5, 'min_bar_pos': 3, 'max_bars_held': 6,
        'entry_session': 'all'}
MONDAY = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)          # 00:00 ET; the forex week is open
STEP = timedelta(minutes=15)


def bars(n=440, seed=5, step=0.0004):
    """A 15Min random walk inside one forex week (Monday 00:00 ET to Friday 14:00 ET)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(MONDAY, periods=n, freq='15min')
    close = 1.10 + np.cumsum(rng.normal(0, step, n))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({'open': open_, 'high': np.maximum(open_, close) + step / 2,
                         'low': np.minimum(open_, close) - step / 2, 'close': close,
                         'volume': np.nan}, index=idx)


def backtest(params, df=None):
    spec = BacktestSpec('vwap_reversion', dict(params), ['EUR/USD'], timeframe='15Min',
                        risk=RiskConfig.from_model(AgentConfig.get(), 'forex'),
                        asset_classes={'EUR/USD': 'forex'}, qty_increments={'EUR/USD': 1.0})
    return run_backtest(spec, {'EUR/USD': bars() if df is None else df}).trades


def session_of(ts):
    return str(ind.session_key(pd.DatetimeIndex([ts]), 'forex').iloc[0])


def bar(z, trend=np.nan, session='2026-10-13'):
    above = z > 0
    return SimpleNamespace(z=z, atr=0.001, svwap=1.10, close=1.103 if above else 1.097, bar_pos=10,
                           vwap_has_volume=False, session=session, trend=trend)


def ctx(ts=datetime(2026, 10, 13, 14, 0, tzinfo=UTC)):
    return Context('EUR/USD', 'forex', '15Min', ts)


def stop(side, at, reason='stop'):
    return TradeRecord('EUR/USD', 'vwap_reversion', side, 1000, at - STEP, at, 1.1, 1.1, -1.0, -0.1, 0.0, 1, reason)


class WithBothFiltersOffNothingChanges(TestCase):
    def test_the_defaults_are_off_and_not_searched_nightly(self):
        self.assertEqual({k: VwapReversion(V3).p[k] for k in ('stop_cooldown', 'trend_guard')},
                         {'stop_cooldown': False, 'trend_guard': False})
        grid = grid_from_schema('vwap_reversion')
        self.assertEqual((grid['stop_cooldown'], grid['trend_guard']), ([False], [False]))

    def test_prepare_adds_nothing_and_a_stop_is_not_remembered(self):
        s = VwapReversion(V3)
        self.assertNotIn('trend', s.prepare(bars(), 'forex', '15Min').columns)
        s.on_trade_closed(stop('short', MONDAY + timedelta(hours=30)))
        self.assertEqual(s.state, {})
        self.assertNotIn('filters', [r.name for r in s.rules(ctx(), bar(3.5))])

    def test_naming_them_off_trades_exactly_like_not_naming_them(self):
        trades = backtest(BUSY)
        self.assertGreater(len(trades), 10)
        self.assertEqual(
            [(t.entry_ts, t.side, t.exit_ts, t.exit_reason) for t in trades],
            [(t.entry_ts, t.side, t.exit_ts, t.exit_reason)
             for t in backtest({**BUSY, 'stop_cooldown': False, 'trend_guard': False})])


class AStopCoolsThatDirectionUntilTheForexDayRolls(SimpleTestCase):
    def setUp(self):
        self.s = VwapReversion({**V3, 'stop_cooldown': True})
        self.stopped_at = datetime(2026, 10, 13, 20, 45, tzinfo=UTC)     # 16:45 ET, the day's last bar

    def test_the_same_direction_waits_and_the_other_does_not(self):
        self.s.on_trade_closed(stop('short', self.stopped_at))
        today = session_of(self.stopped_at)
        self.assertEqual(self.s.on_bar(ctx(), bar(3.5, session=today), None, 0), [])
        self.assertEqual([x.action for x in self.s.on_bar(ctx(), bar(-3.5, session=today), None, 0)], ['buy'])

    def test_the_roll_at_five_eastern_lifts_it(self):
        self.s.on_trade_closed(stop('short', self.stopped_at))
        tomorrow = session_of(self.stopped_at + STEP)                    # 17:00 ET
        self.assertNotEqual(tomorrow, session_of(self.stopped_at))
        self.assertEqual([x.action for x in self.s.on_bar(ctx(), bar(3.5, session=tomorrow), None, 0)], ['sell'])

    def test_only_a_stop_cools(self):
        for reason in ('target', 'signal', 'time', 'kill'):
            self.s.on_trade_closed(stop('short', self.stopped_at, reason))
        self.assertEqual(len(self.s.on_bar(ctx(), bar(3.5, session=session_of(self.stopped_at)), None, 0)), 1)

    def test_through_the_engine_no_same_side_reentry_is_decided_in_a_stopped_session(self):
        def violations(trades):
            out = 0
            for i, t in enumerate(trades):
                for later in trades[i + 1:]:
                    decided = later.entry_ts - STEP          # a backtest stamps the fill bar
                    out += (t.exit_reason == 'stop' and later.side == t.side and later.entry_ts > t.exit_ts
                            and session_of(decided) == session_of(t.exit_ts))
            return out
        off = _plain_backtest(BUSY)
        self.assertGreater(violations(off), 0, 'the test needs re-entries to catch')
        on = _plain_backtest({**BUSY, 'stop_cooldown': True})
        self.assertEqual(violations(on), 0)
        self.assertLess(len(on), len(off))


def _plain_backtest(params):
    """run_backtest without the database: the lane's cost model written out."""
    spec = BacktestSpec('vwap_reversion', dict(params), ['EUR/USD'], timeframe='15Min',
                        risk=RiskConfig(leverage=10.0, min_reward_to_cost=0.0, max_position_pct=500.0,
                                        max_trades_per_day=100),
                        asset_classes={'EUR/USD': 'forex'}, qty_increments={'EUR/USD': 1.0})
    return run_backtest(spec, {'EUR/USD': bars()}).trades


class TheTrendGuardRefusesAFadeAgainstTheHourlyTrend(SimpleTestCase):
    def setUp(self):
        self.s = VwapReversion({**V3, 'trend_guard': True})

    def test_each_side_is_refused_only_against_its_trend(self):
        self.assertEqual(self.s.on_bar(ctx(), bar(3.5, trend=1.0), None, 0), [])
        self.assertEqual(self.s.on_bar(ctx(), bar(-3.5, trend=-1.0), None, 0), [])
        self.assertEqual([x.action for x in self.s.on_bar(ctx(), bar(3.5, trend=-1.0), None, 0)], ['sell'])
        self.assertEqual([x.action for x in self.s.on_bar(ctx(), bar(-3.5, trend=1.0), None, 0)], ['buy'])

    def test_a_trend_that_cannot_be_measured_refuses_both(self):
        self.assertEqual(self.s.on_bar(ctx(), bar(3.5), None, 0), [])
        self.assertEqual(self.s.on_bar(ctx(), bar(-3.5), None, 0), [])
        self.assertIn('not measurable', self.s._held_back(ctx(), bar(3.5), 'short'))

    def test_it_reads_only_completed_hours(self):
        """Bars at :00, :15 and :30 see the previous hour; the :45 bar closes its own."""
        df = bars(300)
        got = completed_trend(df, '15Min', 'forex')
        closes = df['close'][df.index.minute == 45]
        hourly = np.sign(ind.ema(closes, 13) - ind.ema(closes, 27))
        hourly.iloc[:TREND_SLOW] = np.nan
        want = hourly.reindex(df.index, method='ffill')
        pd.testing.assert_series_equal(got, want, check_names=False, check_freq=False)
        self.assertTrue(got.iloc[: 4 * TREND_SLOW].isna().all())
        self.assertTrue(got.iloc[4 * TREND_SLOW + 4:].notna().all())

    def test_appending_future_bars_changes_no_earlier_value_or_decision(self):
        df = bars(420)
        full = completed_trend(df, '15Min', 'forex')
        prepared = self.s.prepare(df, 'forex', '15Min')
        for n in range(4 * TREND_SLOW, len(df), 11):
            pd.testing.assert_series_equal(completed_trend(df.iloc[:n], '15Min', 'forex'), full.iloc[:n],
                                           check_freq=False)
            part = self.s.prepare(df.iloc[:n], 'forex', '15Min')
            for i in range(n - 6, n):
                a = self.s.on_bar(ctx(df.index[i]), part.iloc[i], part, i)
                b = self.s.on_bar(ctx(df.index[i]), prepared.iloc[i], prepared, i)
                self.assertEqual([(x.action, x.stop, x.target) for x in a],
                                 [(x.action, x.stop, x.target) for x in b], f'bar {i} changed when bars were added')

    def test_a_base_coarser_than_an_hour_has_no_trend(self):
        idx = pd.date_range(MONDAY, periods=50, freq='4h')
        df = pd.DataFrame({'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0, 'volume': 0.0}, index=idx)
        self.assertTrue(completed_trend(df, '4Hour', 'crypto').isna().all())

    def test_through_the_engine_no_entry_fades_the_trend(self):
        df = bars()
        trend = completed_trend(df, '15Min', 'forex')

        def against(trades):
            return sum(1 for t in trades
                       if (t.side == 'short' and trend[t.entry_ts - STEP] > 0)
                       or (t.side == 'long' and trend[t.entry_ts - STEP] < 0)
                       or np.isnan(trend[t.entry_ts - STEP]))
        self.assertGreater(against(_plain_backtest(BUSY)), 0, 'the test needs fades to catch')
        self.assertEqual(against(_plain_backtest({**BUSY, 'trend_guard': True})), 0)


@override_settings(RUN_DIR=Path(tempfile.mkdtemp()))
class FixLaneSearchesAFlagOnlyWhenARunAsksForIt(TestCase):
    def setUp(self):
        AgentConfig.get()
        for key in ('vwap_reversion', 'ema_momentum'):
            Strategy.objects.create(key=key, market='forex', name=key, enabled=True, stage='sprout',
                                    symbols=['EUR/USD'])

    def grids(self, *extra):
        with mock.patch('main_app.management.commands.fix_lane.run_experiment'):
            call_command('fix_lane', '--market', 'forex', '--current-regime', '--timeframes', '', *extra,
                         stdout=open('/dev/null', 'w'))
        return {e.strategy_key: e.param_grid for e in Experiment.objects.order_by('-pk')[:2]}

    def test_on_and_off_for_the_named_flags_and_ignored_where_not_declared(self):
        g = self.grids('--search', 'stop_cooldown,trend_guard')
        self.assertEqual((g['vwap_reversion']['stop_cooldown'], g['vwap_reversion']['trend_guard']),
                         ([False, True], [False, True]))
        self.assertNotIn('stop_cooldown', g['ema_momentum'])

    def test_a_misspelt_or_non_bool_name_is_refused_before_anything_runs(self):
        for bad in ('stop_cooldwn', 'entry_z'):
            with self.assertRaises(CommandError) as cm:
                self.grids('--search', bad)
            self.assertIn(f'--search {bad}: not a bool param', str(cm.exception))
        self.assertFalse(Experiment.objects.exists())

    def test_without_the_option_they_stay_off(self):
        g = self.grids()
        self.assertEqual((g['vwap_reversion']['stop_cooldown'], g['vwap_reversion']['trend_guard']),
                         ([False], [False]))


class FlattenAllRefusesWhileAnAgentHoldsTheAccount(TestCase):
    """The agent keeps the positions in its own broker and writes them back."""

    def setUp(self):
        self.account = Account.for_mode('sim', 'forex')

    def flatten(self):
        with mock.patch('main_app.management.commands.flatten_all.flatten_account', return_value=0) as f:
            call_command('flatten_all', '--mode', 'sim', '--market', 'forex', stdout=open('/dev/null', 'w'))
        return f

    def test_a_live_agent_refuses_it(self):
        import os
        AgentRun.objects.create(account=self.account, mode='sim', market='forex', pid=os.getpid(), status='running')
        with self.assertRaises(CommandError) as cm:
            self.flatten()
        self.assertIn('an agent is running on this account', str(cm.exception))

    def test_a_dead_agent_whose_row_still_says_running_does_not(self):
        proc = subprocess.Popen(['true'])
        proc.wait()
        run = AgentRun.objects.create(account=self.account, mode='sim', market='forex', pid=proc.pid,
                                      status='running')
        self.flatten().assert_called_once_with('sim', 'manual', 'forex')
        run.refresh_from_db()
        self.assertEqual(run.status, 'stopped')

    def test_no_agent_flattens(self):
        self.flatten().assert_called_once_with('sim', 'manual', 'forex')
