"""A strategy's own timeframe inside a lane (v1.73).

The lane polls one feed at its base timeframe; a strategy may run on a whole
multiple, on bars resampled causally from the base. These tests pin the four
promises: it cannot see the future, it matches native bars, two timeframes can
share a lane, and a bad setting falls back to the base with an alert.
"""
import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
from django.test import SimpleTestCase, TestCase

from main_app.services.backtest import BacktestSpec, run_backtest
from main_app.services.broker.sim import SimBroker
from main_app.services.data.resample import resample_complete
from main_app.services.engine import Engine, EngineConfig, MemoryRecorder
from main_app.services.risk import RiskConfig
from main_app.services.strategies import make_strategy

T0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


def walk(n, step_minutes=15, seed=11, start=T0, vol=0.004):
    """A continuous (24/7) random walk of OHLCV bars, stamped at bar start."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq=f'{step_minutes}min', tz=UTC)
    close = 100 * np.exp(np.cumsum(rng.normal(0, vol, n)))
    open_ = np.r_[100.0, close[:-1]]
    high = np.maximum(open_, close) * (1 + rng.uniform(0, vol, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, vol, n))
    vol_ = rng.uniform(50, 150, n)
    return pd.DataFrame({'open': open_, 'high': high, 'low': low, 'close': close, 'volume': vol_}, index=idx)


def native_hourly(df):
    """An independent resample (pandas), for the parity check."""
    return df.resample('60min', label='left', closed='left').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}).dropna(subset=['open'])


class ACoarseBarCannotSeeTheFuture(SimpleTestCase):
    def test_a_spike_in_the_last_quarter_is_invisible_until_the_hour_completes(self):
        df = walk(8)                                        # 00:00 .. 01:45, two complete hours
        df.iloc[3, df.columns.get_loc('high')] = 999.0      # 00:45, the hour's last quarter
        coarse, at = resample_complete(df, '15Min', '1Hour', 'crypto')
        self.assertEqual(at[:4], [None, None, None, 0])     # nothing of hour 0 before 00:45 closes it
        self.assertEqual(coarse['high'].iloc[0], 999.0)     # and then the spike is in it
        # Live: a window ending mid-hour has no bar for that hour at all.
        coarse_live, at_live = resample_complete(df.iloc[:6], '15Min', '1Hour', 'crypto')
        self.assertEqual(len(coarse_live), 1)
        self.assertEqual(at_live[4:], [None, None])

    def test_a_missing_closing_bar_emits_one_bar_late_never_early(self):
        df = walk(8).drop(index=walk(8).index[3])           # 00:45 never arrived
        coarse, at = resample_complete(df, '15Min', '1Hour', 'crypto')
        self.assertEqual(at[:4], [None, None, None, 0])     # emitted at 01:00, the first bar after
        self.assertEqual(coarse.index[0], T0)

    def test_missing_volume_stays_missing(self):
        df = walk(8)
        df.iloc[1, df.columns.get_loc('volume')] = math.nan
        coarse, _ = resample_complete(df, '15Min', '1Hour', 'crypto')
        self.assertTrue(math.isnan(coarse['volume'].iloc[0]))
        self.assertFalse(math.isnan(coarse['volume'].iloc[1]))

    def test_a_coarse_strategy_acts_only_when_its_bar_completes(self):
        seen = []
        strat = make_strategy('ema_momentum', {'min_relvol': 0.0})
        strat.timeframe = '1Hour'
        strat.warmup_bars = 0
        strat.on_bar = lambda ctx, row, frame, j: seen.append((ctx.ts, row.Index, j)) or []
        broker = SimBroker(10_000, asset_classes={'X/USD': 'crypto'})
        engine = Engine([strat], broker, EngineConfig(timeframe='15Min', asset_classes={'X/USD': 'crypto'}),
                        MemoryRecorder())
        engine.run_frames({'X/USD': walk(16)})
        self.assertEqual([j for *_, j in seen], [0, 1, 2, 3])
        for ts, own, _ in seen:                               # decided on the base bar that closes the hour
            self.assertEqual(ts, own.to_pydatetime() + timedelta(minutes=45))


class OneHourOnResampledBarsMatchesNativeBars(SimpleTestCase):
    def test_entries_match_native_hourly_bars(self):
        base = walk(24 * 4 * 20, seed=5)                     # 20 days of 15Min
        ac = {'X/USD': 'crypto'}
        params = {'min_relvol': 0.0}
        native = run_backtest(BacktestSpec('ema_momentum', params, ['X/USD'], '1Hour', 10000.0, RiskConfig(),
                                           asset_classes=ac), {'X/USD': native_hourly(base)})
        resampled = run_backtest(BacktestSpec('ema_momentum', params, ['X/USD'], '15Min', 10000.0, RiskConfig(),
                                              asset_classes=ac, strategy_timeframe='1Hour'), {'X/USD': base})
        a = [t.entry_ts for t in native.trades]
        b = [t.entry_ts for t in resampled.trades]
        self.assertGreater(len(a), 5, 'the fixture must trade or parity proves nothing')
        # Same signal bars, same fill (the next base bar's open is the next hour's open).
        self.assertEqual(a[0], b[0])
        self.assertEqual([round(t.entry_price, 6) for t in native.trades[:1]],
                         [round(t.entry_price, 6) for t in resampled.trades[:1]])
        # Exits are checked on finer bars, so later entries can shift once an exit
        # does; the bulk must still coincide.
        shared = len(set(a) & set(b))
        self.assertGreaterEqual(shared / max(len(a), len(b)), 0.8)


class TwoTimeframesShareALane(SimpleTestCase):
    def test_hourly_ema_and_quarter_hourly_vwap_both_trade_in_one_backtest(self):
        base = walk(24 * 4 * 20, seed=7)
        spec = BacktestSpec('portfolio', {'strategies': [
            {'key': 'ema_momentum', 'params': {'min_relvol': 0.0}, 'timeframe': '1Hour'},
            {'key': 'vwap_reversion', 'params': {'entry_z': 1.0}, 'timeframe': ''},
        ]}, ['X/USD'], '15Min', 10000.0, RiskConfig(max_open_positions=4), asset_classes={'X/USD': 'crypto'})
        result = run_backtest(spec, {'X/USD': base})
        keys = {t.strategy_key for t in result.trades}
        self.assertIn('ema_momentum', keys)
        self.assertIn('vwap_reversion', keys)


class ABadTimeframeFallsBackWithAnAlert(SimpleTestCase):
    def _engine(self, tf, base='15Min', ac='crypto'):
        strat = make_strategy('ema_momentum')
        strat.timeframe = tf
        rec = MemoryRecorder()
        engine = Engine([strat], SimBroker(10_000), EngineConfig(timeframe=base, asset_classes={'X': ac}), rec)
        return engine, rec

    def test_finer_or_non_multiple_runs_on_the_base_and_says_so(self):
        for tf in ('5Min', '30Min_x', '1Min'):
            engine, rec = self._engine(tf)
            self.assertEqual(engine.timeframes['ema_momentum'], '15Min')
            self.assertEqual(rec.risk_events[-1][0], 'timeframe')
            self.assertIn('not a whole multiple', rec.risk_events[-1][1])

    def test_blank_base_and_multiples_are_accepted_silently(self):
        for tf, want in (('', '15Min'), ('15Min', '15Min'), ('1Hour', '1Hour'), ('4Hour', '4Hour')):
            engine, rec = self._engine(tf)
            self.assertEqual(engine.timeframes['ema_momentum'], want)
            self.assertEqual(rec.risk_events, [])

    def test_bars_held_counts_the_strategys_own_bars(self):
        from main_app.services.broker.base import Position
        engine, _ = self._engine('1Hour')
        engine.broker.hydrate(10_000, [Position('X', 10, 100.0, T0, strategy_key='ema_momentum', bars_held=9)])
        self.assertEqual(engine.position_view('X', 'ema_momentum').bars_held, 2)    # 9 quarters = 2 hours


class AStocksLaneResamplesOnlyUpToAnHour(SimpleTestCase):
    """Above an hour, UTC-clock buckets would straddle the 09:30 open and 16:00 close."""

    def test_up_to_an_hour_is_fine_and_above_falls_back_with_a_reason(self):
        engine, rec = ABadTimeframeFallsBackWithAnAlert._engine(None, '1Hour', base='5Min', ac='stock')
        self.assertEqual(engine.timeframes['ema_momentum'], '1Hour')
        self.assertEqual(rec.risk_events, [])
        engine, rec = ABadTimeframeFallsBackWithAnAlert._engine(None, '4Hour', base='5Min', ac='stock')
        self.assertEqual(engine.timeframes['ema_momentum'], '5Min')
        self.assertEqual(rec.risk_events[-1][0], 'timeframe')
        self.assertIn('above 1Hour', rec.risk_events[-1][1])


class ABlankTimeframeFollowsTheLane(SimpleTestCase):
    """'' moves with the lane's base; an explicit coarse choice survives a base
    change if it is still a coarser multiple, and otherwise falls back loudly."""

    def _tf(self, tf, base):
        engine, rec = ABadTimeframeFallsBackWithAnAlert._engine(None, tf, base=base)
        return engine.timeframes['ema_momentum'], [e[0] for e in rec.risk_events]

    def test_blank_moves_with_every_base(self):
        for base in ('15Min', '1Hour', '4Hour'):
            self.assertEqual(self._tf('', base), (base, []))

    def test_an_explicit_coarse_choice_survives_or_falls_back_with_an_alert(self):
        self.assertEqual(self._tf('1Hour', '15Min'), ('1Hour', []))
        self.assertEqual(self._tf('1Hour', '1Hour'), ('1Hour', []))          # now equal to the base
        self.assertEqual(self._tf('1Hour', '4Hour'), ('4Hour', ['timeframe']))  # finer than the new base


class MigrationBlanksRowsThatMatchTheirLane(TestCase):
    def test_only_rows_equal_to_their_lane_base_are_blanked(self):
        import importlib

        from django.apps import apps

        from main_app.models import AgentConfig, Strategy
        cfg = AgentConfig.get()
        cfg.forex_timeframe = '15Min'
        cfg.save()
        follows = Strategy.objects.create(key='vwap_reversion', name='v', market='forex', timeframe='15Min')
        coarse = Strategy.objects.create(key='ema_momentum', name='e', market='forex', timeframe='1Hour')
        importlib.import_module('main_app.migrations.0033_strategy_timeframe_follows_lane').follow_lane(apps, None)
        follows.refresh_from_db()
        coarse.refresh_from_db()
        self.assertEqual(follows.timeframe, '')
        self.assertEqual(coarse.timeframe, '1Hour')


class H10BaselinesReplayRowsAsTheLaneRunsThem(TestCase):
    """h10_forward read row.timeframe raw; a '' row would have asked for '' bars."""

    def test_a_blank_row_resolves_to_the_lanes_base(self):
        from main_app.models import Strategy
        from main_app.services.fix_loop import own_timeframe
        self.assertEqual(own_timeframe(Strategy(timeframe=''), '15Min'), '')
        self.assertEqual(own_timeframe(Strategy(timeframe='15Min'), '15Min'), '')
        self.assertEqual(own_timeframe(Strategy(timeframe='1Hour'), '15Min'), '1Hour')

    def test_run_window_hands_the_strategy_timeframe_to_the_spec(self):
        from unittest import mock

        from main_app.services import backtest as bt
        from main_app.services import research_window as rw
        with mock.patch.object(bt, 'spec_from_models', wraps=bt.spec_from_models) as spec, \
                mock.patch.object(bt, 'run_backtest', return_value=mock.Mock(trades=[], metrics={})):
            try:
                rw.run_window('ema_momentum', {}, {}, {}, datetime(2026, 9, 1, tzinfo=UTC),
                              datetime(2026, 9, 2, tzinfo=UTC), timeframe='15Min', pairs=['EUR/USD'],
                              strategy_timeframe='1Hour')
            except Exception:
                pass                     # empty frames may stop it later; the spec call is what is pinned
        self.assertEqual(spec.call_args.kwargs['strategy_timeframe'], '1Hour')


class EmaSkipsASessionsFirstBarsOnlyIntraday(SimpleTestCase):
    """On daily bars every bar is its own session, so `bar_pos < 2` refused every
    entry and a 1Day walk-forward could never trade (item 10.1, 2026-10-06)."""

    V3 = {'fast': 13, 'slow': 27, 'rsi_min': 40.0, 'rsi_max': 65.0, 'stop_atr_mult': 2.5, 'rr': 2.0,
          'min_relvol': 1.5, 'entry_session': 'all'}

    def _signals(self, timeframe, bar_pos, asset_class='forex', params=None):
        from types import SimpleNamespace

        from main_app.services.strategies.base import Context
        strat = make_strategy('ema_momentum', params or self.V3)
        bar = SimpleNamespace(ema_diff=0.001, ema_diff_prev=-0.001, atr=0.002, rsi=55.0, relvol=float('nan'),
                              bar_pos=bar_pos, close=1.1)
        ctx = Context(symbol='EUR/USD', asset_class=asset_class, timeframe=timeframe, ts=T0, bar_pos=bar_pos)
        return strat.on_bar(ctx, bar, None, 50)

    def test_daily_bars_can_enter_on_any_bar(self):
        self.assertEqual([s.action for s in self._signals('1Day', 0)], ['buy'])
        crypto = self._signals('1Day', 0, asset_class='crypto', params={'min_relvol': 0.0})
        self.assertEqual([s.action for s in crypto], ['buy'])

    def test_the_intraday_gate_is_unchanged_including_forex_v3_on_1Hour(self):
        for tf in ('15Min', '1Hour'):
            self.assertEqual(self._signals(tf, 0), [], tf)
            self.assertEqual(self._signals(tf, 1), [], tf)
            self.assertEqual([s.action for s in self._signals(tf, 2)], ['buy'], tf)


class AStrategyMayHoldLongerThanItsLane(SimpleTestCase):
    """Degen ema@4Hour holds a day; burst in the same lane keeps the lane's 180 minutes."""

    def test_ema_keeps_its_own_hold_while_burst_keeps_the_lanes(self):
        from main_app.services.broker.base import Position
        from main_app.services.risk import RiskManager
        from main_app.services.strategies.base import set_own_hold
        ema, burst = make_strategy('ema_momentum'), make_strategy('burst')
        self.assertTrue(set_own_hold(ema, 1440))
        self.assertTrue(set_own_hold(burst, None))              # None leaves the lane's
        ac = {'ADA/USD': 'crypto', 'SOL/USD': 'crypto'}
        risk = RiskConfig(max_hold_minutes=180)
        broker = SimBroker(10_000, immediate_fills=True, slippage_bps=0, asset_classes=ac)
        broker.hydrate(10_000, [Position('ADA/USD', 100, 1.0, T0, strategy_key='ema_momentum', last_price=1.0),
                                Position('SOL/USD', 10, 10.0, T0, strategy_key='burst', last_price=10.0)])
        engine = Engine([ema, burst], broker, EngineConfig(timeframe='15Min', asset_classes=ac, risk=risk),
                        MemoryRecorder(), RiskManager(risk))
        bar = type('Bar', (), {'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0, 'volume': 0})()
        for sym in ac:
            engine._time_exits(sym, T0 + timedelta(minutes=181), bar, None)
        self.assertNotIn('SOL/USD', broker.positions)           # burst: the lane's 180
        self.assertIn('ADA/USD', broker.positions)              # ema: its own 1440
        engine._time_exits('ADA/USD', T0 + timedelta(minutes=1441), bar, None)
        self.assertNotIn('ADA/USD', broker.positions)

    def test_news_catalyst_keeps_its_graded_hold(self):
        from main_app.services.strategies.base import set_own_hold
        news = make_strategy('news_catalyst')
        news.market = 'degen'
        self.assertFalse(set_own_hold(news, 1440))
        self.assertEqual(news.max_hold_minutes, 180)

    def test_research_and_backtests_carry_the_rows_hold(self):
        from main_app.models import Strategy
        from main_app.services.fix_loop import row_risk
        self.assertEqual(row_risk(Strategy(max_hold_minutes=1440)), {'max_hold_minutes': 1440})
        self.assertEqual(row_risk(Strategy()), {})
        frames = {'X/USD': walk(24 * 4 * 20, seed=5)}

        def time_exits(hold):
            spec = BacktestSpec('ema_momentum', {'min_relvol': 0.0}, ['X/USD'], '15Min', 10000.0,
                                RiskConfig(max_hold_minutes=180), asset_classes={'X/USD': 'crypto'},
                                strategy_max_hold=hold)
            return [(t.exit_ts - t.entry_ts).total_seconds() / 60
                    for t in run_backtest(spec, frames).trades if t.exit_reason == 'time']

        lane = time_exits(None)
        self.assertTrue(lane and max(lane) <= 195, 'the fixture must hit the lane hold or this proves nothing')
        self.assertTrue(all(m >= 1440 for m in time_exits(1440)))


class ChangingAHoldOnARunningLaneNeedsARestart(TestCase):
    """The strategy is loaded once with its own hold; poll_controls raises
    config_changed whenever this snapshot differs from the one taken at startup."""

    def test_max_hold_is_part_of_the_strategy_snapshot(self):
        from main_app.models import Mode, Strategy
        from main_app.services.agent import Agent
        row = Strategy.objects.create(key='ema_momentum', name='e', market='degen', enabled=True, stage='sprout')
        for mode in (Mode.SIM, Mode.PAPER):
            agent = Agent.__new__(Agent)
            agent.market, agent.mode = 'degen', mode
            before = agent._strategy_snapshot()
            row.max_hold_minutes = 1440 if row.max_hold_minutes is None else None
            row.save(update_fields=['max_hold_minutes'])
            self.assertNotEqual(agent._strategy_snapshot(), before, mode)
