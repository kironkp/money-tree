from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from django.test import SimpleTestCase

from main_app.services.broker.base import OrderReq
from main_app.services.broker.sim import SimBroker, round_qty

T0 = datetime(2026, 9, 1, 13, 30, tzinfo=UTC)


def bar(o, h, l, c, v=100000):
    return SimpleNamespace(open=o, high=h, low=l, close=c, volume=v)


def entry(qty=10, stop=95.0, target=110.0, side='buy', oid='e1'):
    return OrderReq(id=oid, symbol='X', side=side, qty=qty, leg='entry', strategy_key='t', decision_price=100.0,
                    bar_ts=T0, submitted_ts=T0, stop=stop, target=target)


class SimBrokerFillsAtNextOpenWithSlippage(SimpleTestCase):
    def test_market_entry_fills_at_next_bar_open_plus_slippage(self):
        b = SimBroker(10000, slippage_bps=10, fee_bps={'stock': 0})
        b.on_bar('X', bar(100, 101, 99, 100), T0)
        b.submit(entry())
        self.assertEqual(len(b.open_orders), 1)
        b.on_bar('X', bar(102, 103, 101, 102), T0 + timedelta(minutes=5))
        pos = b.positions['X']
        self.assertAlmostEqual(pos.avg_price, 102 * 1.001)
        self.assertEqual(pos.qty, 10)
        self.assertAlmostEqual(b.cash, 10000 - 10 * 102 * 1.001)
        self.assertAlmostEqual(b.equity, b.cash + 10 * 102)  # marked at close

    def test_immediate_mode_fills_at_last_price(self):
        b = SimBroker(10000, immediate_fills=True, slippage_bps=0, fee_bps={'stock': 0})
        b.on_bar('X', bar(100, 101, 99, 100), T0)
        o = b.submit(entry())
        self.assertEqual(o.status, 'filled')
        self.assertAlmostEqual(o.filled_avg_price, 100.0)


class SimBrokerHonoursStopsAndTargets(SimpleTestCase):
    def _open(self, stop=95.0, target=110.0, side='buy'):
        b = SimBroker(10000, slippage_bps=0, fee_bps={'stock': 0})
        b.on_bar('X', bar(100, 101, 99, 100), T0)
        b.submit(entry(stop=stop, target=target, side=side))
        b.on_bar('X', bar(100, 101, 99, 100), T0 + timedelta(minutes=5))
        return b

    def test_stop_fills_at_stop_when_touched(self):
        b = self._open()
        b.on_bar('X', bar(97, 98, 94, 96), T0 + timedelta(minutes=10))
        self.assertNotIn('X', b.positions)
        t = b.trades[-1]
        self.assertEqual(t.exit_reason, 'stop')
        self.assertAlmostEqual(t.exit_price, 95.0)

    def test_stop_gaps_through_at_the_open(self):
        b = self._open()
        b.on_bar('X', bar(90, 91, 89, 90), T0 + timedelta(minutes=10))
        self.assertAlmostEqual(b.trades[-1].exit_price, 90.0)

    def test_target_fills_at_the_level(self):
        b = self._open()
        b.on_bar('X', bar(105, 112, 104, 108), T0 + timedelta(minutes=10))
        t = b.trades[-1]
        self.assertEqual(t.exit_reason, 'target')
        self.assertAlmostEqual(t.exit_price, 110.0)
        self.assertAlmostEqual(t.pnl, 100.0)

    def test_stop_wins_when_both_touched_in_one_bar(self):
        b = self._open()
        b.on_bar('X', bar(100, 115, 90, 100), T0 + timedelta(minutes=10))
        self.assertEqual(b.trades[-1].exit_reason, 'stop')

    def test_short_round_trip_pnl(self):
        b = self._open(stop=105.0, target=90.0, side='sell')
        self.assertEqual(b.positions['X'].qty, -10)
        b.on_bar('X', bar(95, 96, 88, 92), T0 + timedelta(minutes=10))
        t = b.trades[-1]
        self.assertEqual(t.side, 'short')
        self.assertAlmostEqual(t.pnl, 100.0)
        self.assertAlmostEqual(b.cash, 10100.0)


class SimBrokerKeepsTheBooksStraight(SimpleTestCase):
    def test_fees_reduce_cash_and_trade_pnl(self):
        b = SimBroker(10000, slippage_bps=0, fee_bps={'stock': 10})
        b.on_bar('X', bar(100, 101, 99, 100), T0)
        b.submit(entry())
        b.on_bar('X', bar(100, 101, 99, 100), T0 + timedelta(minutes=5))
        b.close_position('X', 100.0, T0 + timedelta(minutes=10), 'eod', 'x1')
        self.assertAlmostEqual(b.trades[-1].fees, 2.0)  # 1000 notional × 10 bps × 2 legs
        self.assertAlmostEqual(b.trades[-1].pnl, -2.0)
        self.assertAlmostEqual(b.cash, 9998.0)

    def test_liquidity_cap_leaves_a_partial_fill(self):
        b = SimBroker(1_000_000, slippage_bps=0, fee_bps={'stock': 0}, liquidity_cap_pct=1.0)
        b.on_bar('X', bar(100, 101, 99, 100), T0)
        b.submit(entry(qty=5000))
        b.on_bar('X', bar(100, 101, 99, 100, v=100_000), T0 + timedelta(minutes=5))
        self.assertEqual(b.positions['X'].qty, 1000)
        self.assertEqual(b.orders['e1'].status, 'canceled')

    def test_whole_shares_and_crypto_increments(self):
        self.assertEqual(round_qty(12.9, 1), 12)
        self.assertEqual(round_qty(0.012345, 0.0001), 0.0123)
        b = SimBroker(10000, asset_classes={'BTC/USD': 'crypto'}, qty_increments={'BTC/USD': 0.0001})
        b.on_bar('BTC/USD', bar(100, 101, 99, 100), T0)
        o = b.submit(OrderReq(id='c', symbol='BTC/USD', side='buy', qty=0.00005, leg='entry'))
        self.assertEqual(o.status, 'rejected')

    def test_duplicate_client_order_id_is_rejected(self):
        b = SimBroker(10000)
        b.on_bar('X', bar(100, 101, 99, 100), T0)
        b.submit(entry())
        dup = b.submit(entry())
        self.assertEqual(dup.status, 'rejected')
