"""The sim fix loop: a failing strategy is repaired and retried, never stopped.

Also the two knobs the fix searches that did not exist before: a COARSE entry
window (three presets, never a free per-hour list) and lane-regime risk values
an experiment can run under without touching the live config.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from main_app.models import Account, Experiment, Instrument, Qualification, Strategy, Trade
from main_app.services import fix_loop
from main_app.services.backtest import BacktestSpec, run_backtest
from main_app.services.data import calendar as cal
from main_app.services.optimize import grid_from_schema, with_risk_overrides
from main_app.services.risk import RiskConfig
from main_app.services.strategies import make_strategy
from main_app.tests.helpers import frames_for


class TheEntryWindowIsCoarseAndOnlyFiltersEntries(SimpleTestCase):
    def _at(self, hour):
        return datetime(2026, 10, 6, hour, 15, tzinfo=cal.ET)

    def test_the_three_presets(self):
        s = make_strategy('vwap_reversion', {'entry_session': 'skip_asia'})
        self.assertFalse(s.entry_allowed(self._at(22)))
        self.assertFalse(s.entry_allowed(self._at(2)))
        self.assertTrue(s.entry_allowed(self._at(3)))
        self.assertTrue(s.entry_allowed(self._at(18)))
        s = make_strategy('vwap_reversion', {'entry_session': 'london_ny'})
        self.assertTrue(s.entry_allowed(self._at(3)))
        self.assertTrue(s.entry_allowed(self._at(11)))
        self.assertFalse(s.entry_allowed(self._at(12)))
        self.assertFalse(s.entry_allowed(self._at(2)))
        self.assertTrue(make_strategy('vwap_reversion').entry_allowed(self._at(22)))   # default 'all'

    def test_nightly_grids_do_not_multiply(self):
        """search=False: the optimizer keeps the default unless asked."""
        self.assertEqual(grid_from_schema('ema_momentum')['entry_session'], ['all'])
        asked = grid_from_schema('ema_momentum', {'entry_session': ['all', 'skip_asia', 'london_ny']})
        self.assertEqual(asked['entry_session'], ['all', 'skip_asia', 'london_ny'])

    def test_the_engine_drops_entries_outside_the_window(self):
        frames = frames_for(('QQQ',))
        ac = {'QQQ': 'stock'}

        def run(session):
            spec = BacktestSpec('ema_momentum', {'min_relvol': 0.0, 'entry_session': session}, ['QQQ'], '5Min',
                                10000.0, RiskConfig(), asset_classes=ac)
            return run_backtest(spec, frames).trades

        late = lambda trades: [t for t in trades if t.entry_ts.astimezone(cal.ET).hour >= 12]
        everything = run('all')
        self.assertTrue(late(everything), 'the fixture must trade after noon or this proves nothing')
        filtered = run('london_ny')
        # A signal on the 11:55 bar fills at the 12:00 open; nothing later.
        self.assertFalse([t for t in late(filtered)
                          if t.entry_ts.astimezone(cal.ET) > t.entry_ts.astimezone(cal.ET).replace(hour=12, minute=0)])


class AnExperimentCanRunUnderAnotherRegime(SimpleTestCase):
    def test_overrides_replace_only_the_named_risk_values(self):
        spec = BacktestSpec('ema_momentum', {}, ['EUR/USD'], risk=RiskConfig(min_reward_to_cost=2.0))
        out = with_risk_overrides(spec, {'min_reward_to_cost': 4.0, 'max_hold_minutes': 1440})
        self.assertEqual(out.risk.min_reward_to_cost, 4.0)
        self.assertEqual(out.risk.max_hold_minutes, 1440)
        self.assertEqual(spec.risk.min_reward_to_cost, 2.0)          # the original is untouched
        self.assertIs(with_risk_overrides(spec, {}), spec)

    def test_a_typo_is_an_error_not_a_silent_no_op(self):
        spec = BacktestSpec('ema_momentum', {}, ['EUR/USD'])
        with self.assertRaises(ValueError):
            with_risk_overrides(spec, {'min_reward_to_cosst': 4.0})


class TheLaneMovesOnlyForABetterCombinedResult(SimpleTestCase):
    def test_choose_regime(self):
        cur = fix_loop.regime_key('15Min', 2.0, 240)
        hourly = fix_loop.regime_key('1Hour', 2.0, 1440)
        self.assertEqual(fix_loop.choose_regime({cur: -100.0, hourly: 50.0}, cur)[0], hourly)
        self.assertEqual(fix_loop.choose_regime({cur: 10.0, hourly: -50.0}, cur)[0], cur)
        self.assertEqual(fix_loop.choose_regime({cur: 10.0, hourly: 10.0}, cur)[0], cur)   # a tie stays
        with self.assertRaises(ValueError):
            fix_loop.choose_regime({hourly: 1.0}, cur)


class AFailingSimStrategyIsFixedNotStopped(TestCase):
    def setUp(self):
        self.inst = Instrument.objects.create(symbol='EUR/USD', asset_class='forex', market='forex')
        self.account = Account.objects.create(mode='sim', market='forex', starting_cash=10000, cash=10000)
        self.row = Strategy.objects.create(key='vwap_reversion', name='VWAP', market='forex', enabled=True,
                                           stage='sprout', qualification=Qualification.QUARANTINED,
                                           params={'entry_z': 2.0}, symbols=['EUR/USD'],
                                           evidence_since=timezone.now() - timedelta(days=2))
        self.exp = Experiment.objects.create(strategy_key='vwap_reversion', method='walk_forward',
                                             symbols=['EUR/USD'], timeframe='15Min',
                                             start=datetime(2026, 8, 9).date(), end=datetime(2026, 10, 6).date(),
                                             windows={'train_days': 21, 'test_days': 7},
                                             best_params={'entry_z': 2.5, 'entry_session': 'london_ny'})

    def _trades(self, n):
        ts = timezone.now() - timedelta(days=1)
        for _ in range(n):
            Trade.objects.create(account=self.account, instrument=self.inst, strategy_key='vwap_reversion',
                                 qty=1000, entry_ts=ts, exit_ts=ts, entry_price=Decimal('1.1'),
                                 exit_price=Decimal('1.1'), pnl=Decimal('-1'), pnl_pct=Decimal('-0.01'))

    def test_a_young_retry_version_is_not_replaced(self):
        self._trades(5)
        due, why = fix_loop.retry_due(self.row, self.account)
        self.assertFalse(due)
        self.assertIn('too young', why)

    def test_thirty_trades_or_fourteen_days_makes_it_due(self):
        self._trades(30)
        self.assertTrue(fix_loop.retry_due(self.row, self.account)[0])
        Trade.objects.all().delete()
        self.row.evidence_since = timezone.now() - timedelta(days=15)
        self.assertTrue(fix_loop.retry_due(self.row, self.account)[0])

    def test_a_strategy_that_is_not_failing_is_not_due(self):
        self.row.qualification = Qualification.UNPROVEN
        self.assertFalse(fix_loop.retry_due(self.row, self.account)[0])

    def _compare(self, beats):
        return mock.patch.object(fix_loop, 'compare_on_oos', return_value={
            'beats': beats, 'windows': 5,
            'current': {'net_pnl': -100.0, 'profit_factor': 0.7, 'trades': 40},
            'procedure': {'net_pnl': 20.0 if beats else -150.0, 'profit_factor': 1.05 if beats else 0.6,
                          'trades': 30}})

    def test_no_better_candidate_keeps_the_current_version_trading(self):
        with self._compare(False):
            verdict, _ = fix_loop.install_if_better(self.row, self.exp, cfg=None)
        self.row.refresh_from_db()
        self.assertIn('no fix found', verdict)
        self.assertEqual(self.row.version, 1)
        self.assertTrue(self.row.enabled)

    def test_a_better_candidate_becomes_a_new_unproven_sim_version(self):
        before = self.row.evidence_since
        with self._compare(True):
            verdict, _ = fix_loop.install_if_better(self.row, self.exp, cfg=None)
        self.row.refresh_from_db()
        self.assertIn('INSTALLED v2 in sim', verdict)
        self.assertEqual(self.row.version, 2)
        self.assertEqual(self.row.params, {'entry_z': 2.5, 'entry_session': 'london_ny'})
        self.assertEqual(self.row.qualification, Qualification.UNPROVEN)
        self.assertGreater(self.row.evidence_since, before)               # the reset: evidence restarts at n=0
        self.assertEqual(self.row.history[-1]['evidence']['kind'], 'sim_fix')
        self.assertTrue(self.row.enabled)

    def test_the_install_test_is_the_procedures_clean_oos_not_a_replay_of_its_pick(self):
        """The latest pick trained on data that overlaps the earlier test windows,
        so replaying it over them is partly in-sample. The procedure's adaptive OOS
        is clean, and the current params are out of sample on every window."""
        rows = [{'n': 1}, {'n': '2', 'skipped': 'no positive training edge'}, {'n': 3}, {'n': 4}, {'n': 5}]
        self.exp.summary = {'oos': {'net_pnl': 96.63, 'profit_factor': 1.06, 'trades': 70}, 'windows': rows}
        current = {'net_pnl': -334.81, 'profit_factor': 0.93, 'trades': 90}
        with mock.patch.object(fix_loop, 'evaluate_fixed_params', return_value={'metrics': current}) as ev, \
                mock.patch.object(fix_loop, 'load_frames', return_value={}), \
                mock.patch.object(fix_loop, 'spec_from_models', return_value=mock.Mock()), \
                mock.patch.object(fix_loop, 'with_risk_overrides', side_effect=lambda s, o: s):
            cmp = fix_loop.compare_on_oos(self.exp, {'entry_z': 2.0}, cfg=None)
        self.assertTrue(cmp['beats'])
        self.assertEqual(ev.call_count, 1)                       # only the CURRENT params are replayed
        self.assertEqual(ev.call_args.args[3], {'entry_z': 2.0})
        self.assertEqual(cmp['procedure']['net_pnl'], 96.63)
        # Like for like: current is replayed only on the windows the procedure traded.
        self.assertEqual([w['n'] for w in ev.call_args.args[2]], [1, 3, 4, 5])
        self.assertEqual(cmp['windows'], 4)
        self.exp.summary = {'oos': {'net_pnl': 96.63, 'profit_factor': 0.9, 'trades': 70}, 'windows': rows}
        with mock.patch.object(fix_loop, 'evaluate_fixed_params', return_value={'metrics': current}), \
                mock.patch.object(fix_loop, 'load_frames', return_value={}), \
                mock.patch.object(fix_loop, 'spec_from_models', return_value=mock.Mock()), \
                mock.patch.object(fix_loop, 'with_risk_overrides', side_effect=lambda s, o: s):
            self.assertFalse(fix_loop.compare_on_oos(self.exp, {'entry_z': 2.0}, cfg=None)['beats'])   # PF too

    def test_a_procedure_that_traded_no_window_cannot_win(self):
        self.exp.summary = {'oos': {}, 'windows': [{'n': '1', 'skipped': 'no combo reached min_trades'}]}
        with mock.patch.object(fix_loop, 'evaluate_fixed_params') as ev:
            cmp = fix_loop.compare_on_oos(self.exp, {'entry_z': 2.0}, cfg=None)
        self.assertFalse(cmp['beats'])
        self.assertEqual(cmp['windows'], 0)
        ev.assert_not_called()

    def test_without_a_gated_winner_the_final_windows_best_is_the_sim_candidate(self):
        self.exp.best_params = {}
        self.exp.summary = {'windows': [{'params': {'entry_z': 1.5}},
                                        {'skipped': 'no positive training edge', 'params': {'entry_z': 3.0}}]}
        self.assertEqual(fix_loop.sim_candidate(self.exp), {'entry_z': 3.0})
        self.exp.summary = {'windows': [{'skipped': 'no combo reached min_trades'}]}
        self.assertEqual(fix_loop.sim_candidate(self.exp), {})


class ALaneRegimeChangeDoesNotMoveTheNewsArmsHorizon(TestCase):
    """Forex moved to a 1440-minute hold on 2026-10-06. News grading read the
    lane's hold, so the preregistered evaluation would silently have been scored
    on a 24 h race instead of 4 h — a break the fingerprint could not see."""

    def setUp(self):
        from main_app.models import AgentConfig
        self.cfg = AgentConfig.get()
        self.cfg.forex_max_hold_minutes = 1440
        self.cfg.degen_max_hold_minutes = 1440
        self.cfg.save()

    def test_grading_ignores_the_lane_hold(self):
        from main_app.services.news_agent import _hold_minutes
        self.assertEqual(_hold_minutes('forex'), 240)
        self.assertEqual(_hold_minutes('degen'), 180)
        self.assertEqual(_hold_minutes('stocks'), 240)

    def test_the_fingerprint_does_not_follow_lane_holds(self):
        from main_app.services.preregistration import current_fingerprint
        before = current_fingerprint()[0]
        self.cfg.forex_max_hold_minutes = 240
        self.cfg.save()
        self.assertEqual(current_fingerprint()[0], before)

    def test_a_news_trade_keeps_its_graded_hold_while_the_lane_holds_longer(self):
        from main_app.services.broker.base import Position
        from main_app.services.broker.sim import SimBroker
        from main_app.services.engine import Engine, EngineConfig, MemoryRecorder
        from main_app.services.risk import RiskManager
        news = make_strategy('news_catalyst')
        news.market = 'forex'
        ema = make_strategy('ema_momentum')
        ema.market = 'forex'
        self.assertEqual(news.max_hold_minutes, 240)
        self.assertIsNone(ema.max_hold_minutes)                      # ema follows the lane: 1440
        risk = RiskConfig(max_hold_minutes=1440)
        broker = SimBroker(10_000, immediate_fills=True, slippage_bps=0, asset_classes={'EUR/USD': 'forex',
                                                                                         'GBP/USD': 'forex'})
        t0 = datetime(2026, 10, 7, 13, 0, tzinfo=UTC)
        broker.hydrate(10_000, [Position('EUR/USD', 1000, 1.1, t0, strategy_key='news_catalyst', last_price=1.1),
                                Position('GBP/USD', 1000, 1.3, t0, strategy_key='ema_momentum', last_price=1.3)])
        engine = Engine([news, ema], broker, EngineConfig(timeframe='15Min', asset_classes=broker.asset_classes,
                                                          risk=risk), MemoryRecorder(), RiskManager(risk))
        bar = type('Bar', (), {'open': 1.1, 'high': 1.1, 'low': 1.1, 'close': 1.1, 'volume': 0})()
        later = t0 + timedelta(minutes=241)
        engine._time_exits('EUR/USD', later, bar, None)
        engine._time_exits('GBP/USD', later, bar, None)
        self.assertNotIn('EUR/USD', broker.positions)                # closed at its 240-minute race
        self.assertIn('GBP/USD', broker.positions)                   # the lane's 1440 still applies


class FixLaneRefusesLanesThatShareFields(TestCase):
    """Crypto and stocks share min_reward_to_cost and max_hold_minutes; moving one
    lane's regime would silently move the other's."""

    def test_stocks_and_crypto_apply_refuse_and_change_nothing(self):
        from django.core.management import CommandError, call_command
        from main_app.models import AgentConfig
        cfg = AgentConfig.get()
        before = {f.name: getattr(cfg, f.name) for f in AgentConfig._meta.concrete_fields}
        row = Strategy.objects.create(key='ema_momentum', name='EMA', market='stocks', enabled=True,
                                      stage='sprout', params={}, symbols=['SPY'],
                                      evidence_since=timezone.now() - timedelta(days=5))
        since = row.evidence_since
        for market, other in (('stocks', 'crypto'), ('crypto', 'stocks')):
            with self.assertRaisesMessage(CommandError, f'lane {market} shares fields with {other}; not supported'):
                call_command('fix_lane', '--market', market, '--apply')
        cfg.refresh_from_db()
        self.assertEqual({f.name: getattr(cfg, f.name) for f in AgentConfig._meta.concrete_fields}, before)
        row.refresh_from_db()
        self.assertEqual(row.evidence_since, since)

    def test_every_mapped_field_exists(self):
        from main_app.management.commands.fix_lane import lane_fields
        self.assertEqual(lane_fields('forex')['max_hold_minutes'], 'forex_max_hold_minutes')
        self.assertEqual(lane_fields('degen')['timeframe'], 'degen_timeframe')


class NightlyResearchKeepsTheRowsOwnEntryWindow(TestCase):
    def _row(self, market='forex', qualification=Qualification.UNPROVEN, session='london_ny'):
        return Strategy(key='vwap_reversion', name='VWAP', market=market, enabled=True, stage='sprout',
                        qualification=qualification, params={'entry_session': session})

    def test_a_healthy_row_is_researched_at_its_own_window(self):
        from main_app.management.commands.auto_research import research_grid
        self.assertEqual(research_grid(self._row())['entry_session'], ['london_ny'])
        self.assertEqual(research_grid(self._row(session='all'))['entry_session'], ['all'])

    def test_only_a_failing_forex_row_searches_the_presets(self):
        from main_app.management.commands.auto_research import research_grid
        self.assertEqual(research_grid(self._row(qualification=Qualification.QUARANTINED))['entry_session'],
                         ['all', 'skip_asia', 'london_ny'])
        self.assertEqual(research_grid(self._row(market='degen', qualification=Qualification.QUARANTINED,
                                                 session='all'))['entry_session'], ['all'])
