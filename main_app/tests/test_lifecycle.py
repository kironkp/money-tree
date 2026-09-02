"""Trade lifecycle invariants: one coordinated close, no reversals, strategies
own their positions, cards move through their states, approvals expire."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from django.test import SimpleTestCase

from main_app.services.broker.base import OrderReq
from main_app.services.broker.sim import SimBroker
from main_app.services.engine import Engine, EngineConfig, MemoryRecorder
from main_app.services.risk import RiskConfig, RiskManager
from main_app.services.strategies.base import Context, Signal, Strategy
from main_app.tests.test_sim_broker import bar, entry

T0 = datetime(2026, 9, 1, 13, 30, tzinfo=UTC)


def T(minutes):
    return T0 + timedelta(minutes=minutes)


class NoReversalOnDoubleExit(SimpleTestCase):
    def test_signal_exit_pending_then_stop_hits_does_not_reverse(self):
        b = SimBroker(10000, slippage_bps=0, fee_bps={'stock': 0})
        b.on_bar('X', bar(100, 101, 99, 100), T(0))
        b.submit(entry(stop=95.0, target=110.0))
        b.on_bar('X', bar(100, 101, 99, 100), T(5))          # entry fills
        b.submit(OrderReq(id='exit-1', symbol='X', side='sell', qty=10, leg='exit', exit_reason='signal'))
        self.assertTrue(b.positions['X'].closing)
        b.on_bar('X', bar(90, 91, 89, 90), T(10))            # pending exit fills at the open; stop is NOT evaluated on top
        self.assertNotIn('X', b.positions)
        self.assertEqual(len(b.trades), 1)
        self.assertEqual(b.trades[0].exit_reason, 'signal')

    def test_duplicate_exit_is_rejected_and_close_position_supersedes_pending_exit(self):
        b = SimBroker(10000, slippage_bps=0, fee_bps={'stock': 0})
        b.on_bar('X', bar(100, 101, 99, 100), T(0))
        b.submit(entry())
        b.on_bar('X', bar(100, 101, 99, 100), T(5))
        first = b.submit(OrderReq(id='exit-1', symbol='X', side='sell', qty=10, leg='exit'))
        dup = b.submit(OrderReq(id='exit-2', symbol='X', side='sell', qty=10, leg='exit'))
        self.assertEqual(first.status, 'accepted')
        self.assertEqual(dup.status, 'rejected')
        b.close_position('X', 100.0, T(6), 'eod', 'eod-1')   # immediate close cancels the pending exit first
        self.assertEqual(b.orders['exit-1'].status, 'canceled')
        self.assertNotIn('X', b.positions)
        b.on_bar('X', bar(100, 101, 99, 100), T(10))
        self.assertNotIn('X', b.positions)                    # nothing left to fill → no reversal
        self.assertEqual(len(b.trades), 1)

    def test_exit_without_position_is_canceled_not_filled(self):
        b = SimBroker(10000)
        b.on_bar('X', bar(100, 101, 99, 100), T(0))
        o = b.submit(OrderReq(id='exit-x', symbol='X', side='sell', qty=10, leg='exit'))
        self.assertEqual(o.status, 'rejected')
        self.assertNotIn('X', b.positions)


class Owner(Strategy):
    key = 'owner'
    name = 'Owner'
    warmup_bars = 0

    def prepare(self, df, asset_class='stock', timeframe='5Min'):
        return df

    def on_bar(self, ctx, bar, df, i):
        if ctx.position is None and i == 0:
            return [Signal('buy', ctx.symbol, ctx.ts, float(bar.close), float(bar.close) - 2, float(bar.close) + 4)]
        return []


class Intruder(Strategy):
    key = 'intruder'
    name = 'Intruder'
    warmup_bars = 0

    def prepare(self, df, asset_class='stock', timeframe='5Min'):
        return df

    def on_bar(self, ctx, bar, df, i):
        # Sees no position (it isn't its own) and still tries to close it.
        return [Signal('close', ctx.symbol, ctx.ts, float(bar.close), reason='intruder')] if i >= 1 else []


def run_engine(strategies, cfg=None, bars=None):
    broker = SimBroker(10000, immediate_fills=True, slippage_bps=0, fee_bps={'stock': 0})
    rec = MemoryRecorder()
    engine = Engine(strategies, broker, cfg or EngineConfig(mode='t'), rec, RiskManager(RiskConfig()))
    rows = bars or [bar(100, 101, 99, 100), bar(100, 101, 99, 100.5), bar(100.5, 101, 99.5, 101)]
    for i, r in enumerate(rows):
        r.bar_pos = i
        engine.process_bar('X', T(5 * i), r, {s.key: r for s in strategies}, {s.key: None for s in strategies}, i, 200.0)
    return engine, broker, rec


class StrategiesOwnTheirPositions(SimpleTestCase):
    def test_other_strategy_cannot_close_the_position(self):
        engine, broker, rec = run_engine([Owner(), Intruder()])
        self.assertIn('X', broker.positions)
        self.assertEqual(broker.positions['X'].strategy_key, 'owner')
        refused = [s for s in rec.signals if s[1] == 'intruder' and s[2] is not None and not s[2].allowed]
        self.assertTrue(refused)
        self.assertIn('belongs to owner', refused[0][2].reason)

    def test_allocation_cap_limits_the_strategy(self):
        cfg = EngineConfig(mode='t', allocations={'owner': 5.0}, risk=RiskConfig(risk_per_trade_pct=5, max_position_pct=50))
        engine, broker, rec = run_engine([Owner()], cfg)
        self.assertLessEqual(broker.positions['X'].qty * 100, 10000 * 0.05 + 1)


class CardsFollowTheTrade(SimpleTestCase):
    def test_card_goes_approved_to_protected_to_closed(self):
        class Exiter(Owner):
            def on_bar(self, ctx, bar, df, i):
                if i == 2 and ctx.position is not None:
                    return [Signal('close', ctx.symbol, ctx.ts, float(bar.close), reason='done')]
                return super().on_bar(ctx, bar, df, i)
        engine, broker, rec = run_engine([Exiter()])
        self.assertEqual(len(rec.cards), 1)
        card = next(iter(rec.cards.values()))
        self.assertEqual(card['status'], 'closed')
        self.assertEqual(card['protection'], 'engine')
        self.assertEqual(card['exit_reason'], 'signal')
        self.assertIsNotNone(card['net_pnl'])
        self.assertGreater(card['risk_dollars'], 0)
        self.assertAlmostEqual(card['reward_risk'], 2.0)

    def test_approval_mode_holds_the_entry_then_expires(self):
        cfg = EngineConfig(mode='live', confirm_entries=True, confirm_minutes=3)
        engine, broker, rec = run_engine([Owner()], cfg, bars=[bar(100, 101, 99, 100)])
        self.assertNotIn('X', broker.positions)
        card = next(iter(engine.cards.values()))
        self.assertEqual(card.status, 'awaiting_approval')
        self.assertEqual(engine.expire_cards(T(4)), 1)
        self.assertEqual(card.status, 'expired')

    def test_approved_card_submits_and_fills(self):
        cfg = EngineConfig(mode='live', confirm_entries=True, confirm_minutes=3)
        engine, broker, rec = run_engine([Owner()], cfg, bars=[bar(100, 101, 99, 100)])
        card = next(iter(engine.cards.values()))
        engine.submit_card(card.id, T(1))
        self.assertEqual(card.status, 'protected')
        self.assertIn('X', broker.positions)
