from datetime import UTC, date, datetime, timedelta

from django.test import TestCase

from main_app.models import Account, AgentConfig, AgentRun, EquitySnapshot, JournalEntry, Order, Position, Signal, Trade
from main_app.services.agent import Agent
from main_app.services.backtest import date_bounds, load_frames, run_backtest, spec_from_models
from main_app.services.broker.sim import SimBroker
from main_app.services.ledger import DBRecorder, hydrate_broker, persist_broker
from main_app.tests.helpers import enable_strategy, seed_db


class SimulatorStateRoundTripsThroughTheDatabase(TestCase):
    def test_persist_then_hydrate(self):
        cfg, instruments = seed_db(('QQQ',), with_bars=False)
        account = Account.for_mode('sim')
        broker = SimBroker(10000, immediate_fills=True, slippage_bps=0, fee_bps={'stock': 0})
        from types import SimpleNamespace
        t0 = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
        broker.on_bar('QQQ', SimpleNamespace(open=100, high=101, low=99, close=100, volume=1e6), t0)
        from main_app.services.broker.base import OrderReq
        broker.submit(OrderReq(id='mt-sim-t-QQQ-1-entry', symbol='QQQ', side='buy', qty=5, leg='entry', strategy_key='t',
                               decision_price=100.0, bar_ts=t0, submitted_ts=t0, stop=98.0, target=104.0))
        rec = DBRecorder(account, instruments)
        for kind, obj, order in broker.drain_events():
            rec.on_order(order)
            rec.on_fill(obj, order)
        persist_broker(account, broker, instruments)
        account.refresh_from_db()
        self.assertEqual(float(account.cash), 9500.0)
        self.assertEqual(Position.objects.count(), 1)
        self.assertEqual(Order.objects.get().status, 'filled')
        fresh = SimBroker(0)
        hydrate_broker(account, fresh)
        self.assertEqual(fresh.cash, 9500.0)
        self.assertEqual(fresh.positions['QQQ'].qty, 5)
        self.assertEqual(fresh.positions['QQQ'].stop, 98.0)


class ReplayMatchesTheBacktest(TestCase):
    """The parity claim: one engine, two drivers, identical trades."""

    def test_replay_and_backtest_agree(self):
        cfg, instruments = seed_db(('QQQ', 'NVDA'))
        row = enable_strategy('orb', {'min_relvol': 0.0, 'range_minutes': 15}, ['QQQ', 'NVDA'])
        day = date(2026, 8, 27)
        agent = Agent(replay_date=day, speed=1e9, quiet=True)
        agent.run()
        replay_trades = list(Trade.objects.filter(account__mode='replay').order_by('entry_ts', 'instrument__symbol'))
        self.assertGreater(len(replay_trades), 0)
        a, b = date_bounds(day, day)
        spec = spec_from_models('orb', row.params, ['QQQ', 'NVDA'], cfg.timeframe, cfg)
        spec.act_from = a
        frames = load_frames(['QQQ', 'NVDA'], cfg.timeframe, day - timedelta(days=5), day)
        result = run_backtest(spec, frames)
        bt = sorted(result.trades, key=lambda t: (t.entry_ts, t.symbol))
        self.assertEqual(len(bt), len(replay_trades))
        for x, y in zip(bt, replay_trades):
            self.assertEqual((x.symbol, x.entry_ts, x.exit_ts, x.exit_reason), (y.instrument.symbol, y.entry_ts, y.exit_ts, y.exit_reason))
            self.assertAlmostEqual(x.pnl, float(y.pnl), places=2)
            self.assertAlmostEqual(x.qty, float(y.qty))
        run = AgentRun.objects.get()
        self.assertEqual(run.status, 'stopped')
        from main_app.models import FeedEvent
        levels = set(FeedEvent.objects.filter(account__mode='replay').values_list('level', flat=True))
        self.assertIn('trade', levels)
        self.assertIn('bar', levels)
        self.assertTrue(FeedEvent.objects.filter(level='signal', text__contains='entry approved').exists())
        from main_app.models import SymbolState, TradeCard
        self.assertTrue(TradeCard.objects.filter(account__mode='replay', status='closed').exists())
        self.assertTrue(SymbolState.objects.filter(account__mode='replay').exists())
        self.assertTrue(EquitySnapshot.objects.filter(account__mode='replay').exists())
        self.assertTrue(JournalEntry.objects.filter(kind='auto_eod').exists())
        self.assertTrue(Signal.objects.filter(account__mode='replay').exists())
        self.assertEqual(Position.objects.filter(account__mode='replay').count(), 0)  # flat at the end
