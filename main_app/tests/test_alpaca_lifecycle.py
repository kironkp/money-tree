"""The Alpaca adapter against a fake venue: brackets, crypto stop orders, the
coordinated close, duplicate closes, divergence detection and partial fills.
The real API is not exercised here; this pins the adapter's behaviour."""
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from django.test import SimpleTestCase

from main_app.services.broker.alpaca import AlpacaBroker
from main_app.services.broker.base import OrderReq
from main_app.tests.test_sim_broker import bar

T0 = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)


class FakeVenue:
    """Just enough of alpaca-py's TradingClient for the adapter."""

    def __init__(self, price=100.0, cash=10000.0, partial_first=False):
        self.price = price
        self.cash = cash
        self.orders: dict[str, SimpleNamespace] = {}
        self.by_client: dict[str, SimpleNamespace] = {}
        self.positions: dict[str, SimpleNamespace] = {}
        self.canceled: list[str] = []
        self.partial_first = partial_first
        self.submitted: list = []

    # account / positions
    def get_account(self):
        equity = self.cash + sum(float(p.qty) * self.price for p in self.positions.values())
        return SimpleNamespace(cash=str(self.cash), equity=str(equity), buying_power=str(self.cash), status='ACTIVE')

    def get_all_positions(self):
        return list(self.positions.values())

    def get_open_position(self, symbol):
        p = self.positions.get(symbol)
        if p is None:
            raise RuntimeError('no position')
        return p

    # orders
    def submit_order(self, req):
        self.submitted.append(req)
        oid = str(uuid4())
        typ = type(req).__name__
        o = SimpleNamespace(id=oid, client_order_id=req.client_order_id, symbol=req.symbol, qty=str(req.qty),
                            side=str(req.side).lower(), status='accepted', filled_qty='0', filled_avg_price=None,
                            filled_at=None, type='market' if 'Market' in typ else 'stop_limit', legs=[],
                            order_class=str(getattr(req, 'order_class', '') or 'simple'))
        self.orders[oid] = o
        self.by_client[req.client_order_id] = o
        if 'Market' in typ:
            self._fill(o, partial=self.partial_first)
            self.partial_first = False
        return o

    def _fill(self, o, partial=False):
        qty = float(o.qty)
        filled = qty / 2 if partial else qty
        o.filled_qty = str(filled)
        o.filled_avg_price = str(self.price)
        o.status = 'partially_filled' if partial else 'filled'
        o.filled_at = T0
        signed = filled if 'buy' in o.side else -filled
        p = self.positions.get(o.symbol)
        new_qty = (float(p.qty) if p else 0.0) + signed
        self.cash -= signed * self.price
        if abs(new_qty) < 1e-9:
            self.positions.pop(o.symbol, None)
        else:
            self.positions[o.symbol] = SimpleNamespace(symbol=o.symbol, qty=str(new_qty), avg_entry_price=str(self.price),
                                                       current_price=str(self.price), side='long' if new_qty > 0 else 'short')

    def complete_partial(self):
        for o in self.orders.values():
            if o.status == 'partially_filled':
                remaining = float(o.qty) - float(o.filled_qty)
                o.status, o.filled_qty = 'filled', o.qty
                signed = remaining if 'buy' in o.side else -remaining
                p = self.positions[o.symbol]
                p.qty = str(float(p.qty) + signed)

    def get_order_by_client_id(self, cid):
        return self.by_client[cid]

    def get_order_by_id(self, oid):
        return self.orders[oid]

    def get_orders(self, req=None):
        want = str(getattr(req, 'status', 'open')).lower().split('.')[-1]
        if want == 'closed':
            return [o for o in self.orders.values() if o.status in ('filled', 'canceled')]
        return [o for o in self.orders.values() if o.status in ('accepted', 'new', 'partially_filled')]

    def cancel_orders(self):
        ids = [oid for oid, o in self.orders.items() if o.status in ('accepted', 'new')]
        for oid in ids:
            self.cancel_order_by_id(oid)
        return ids

    def cancel_order_by_id(self, oid):
        self.canceled.append(oid)
        self.orders[oid].status = 'canceled'


def make_broker(venue, crypto=False):
    ac = {'BTC/USD': 'crypto'} if crypto else {'AAPL': 'stock'}
    b = AlpacaBroker(client=venue, asset_classes=ac, qty_increments={'BTC/USD': 0.0001}, poll_s=0.5)
    b.sync()
    return b


def entry(symbol='AAPL', qty=10, stop=98.0, target=104.0):
    return OrderReq(id=f'mt-t-x-{symbol.replace("/", "")}-1-entry', symbol=symbol, side='buy', qty=qty, leg='entry',
                    strategy_key='x', decision_price=100.0, bar_ts=T0, submitted_ts=T0, stop=stop, target=target)


class StockEntriesCarryBrackets(SimpleTestCase):
    def test_bracket_entry_fills_and_is_protected(self):
        v = FakeVenue()
        b = make_broker(v)
        o = b.submit(entry())
        self.assertEqual(o.status, 'filled')
        self.assertEqual(str(v.submitted[0].order_class).lower().split('.')[-1], 'bracket')
        pos = b.positions['AAPL']
        self.assertEqual(pos.qty, 10)
        self.assertEqual(b.protection_for('AAPL')[0], 'bracket')
        kinds = [e[0] for e in b.drain_events()]
        self.assertEqual(kinds, ['fill'])

    def test_partial_then_full_fill_emits_two_fills(self):
        v = FakeVenue(partial_first=True)
        b = make_broker(v)
        o = b.submit(entry())
        self.assertEqual(o.status, 'partially_filled')
        self.assertEqual(b.positions['AAPL'].qty, 5)
        v.complete_partial()
        b._await_fill(o)
        self.assertEqual(o.status, 'filled')
        self.assertEqual(b.positions['AAPL'].qty, 10)
        self.assertEqual(len([e for e in b.drain_events() if e[0] == 'fill']), 2)


class CryptoGetsAVenueSideStop(SimpleTestCase):
    def test_stop_order_rests_at_the_venue_after_the_fill(self):
        v = FakeVenue(price=100.0)
        b = make_broker(v, crypto=True)
        b.submit(entry('BTC/USD', qty=0.05, stop=95.0, target=110.0))
        kind, oid = b.protection_for('BTC/USD')
        self.assertEqual(kind, 'stop_order')
        self.assertIn(oid, v.orders)
        self.assertEqual(v.orders[oid].type, 'stop_limit')


class CryptoFeesTakenInKind(SimpleTestCase):
    def test_position_adopts_the_post_fee_quantity_and_is_still_protected(self):
        v = FakeVenue(price=100.0)
        orig_fill = v._fill

        def fill_with_fee(o, partial=False):
            orig_fill(o, partial)
            p = v.positions.get(o.symbol)
            if p and 'buy' in o.side:
                p.qty = str(float(p.qty) * (1 - 0.0025))  # 25 bps taken from the coins
        v._fill = fill_with_fee
        b = make_broker(v, crypto=True)
        o = b.submit(entry('BTC/USD', qty=0.02, stop=95.0, target=110.0))
        pos = b.positions['BTC/USD']
        self.assertAlmostEqual(pos.qty, 0.02 * 0.9975)
        self.assertGreater(o.fees, 0)
        kind, oid = b.protection_for('BTC/USD')
        self.assertEqual(kind, 'stop_order')
        self.assertAlmostEqual(float(v.orders[oid].qty), 0.02 * 0.9975)
        self.assertEqual(b.sync()['diverged'], [])


class ClosingIsOneCoordinatedLifecycle(SimpleTestCase):
    def test_close_cancels_protection_confirms_then_closes_remaining(self):
        v = FakeVenue()
        b = make_broker(v, crypto=True)
        b.submit(entry('BTC/USD', qty=0.05, stop=95.0, target=110.0))
        _, stop_id = b.protection_for('BTC/USD')
        b.drain_events()
        v.price = 103.0
        res = b.close_position('BTC/USD', 103.0, T0, 'signal', 'mt-t-x-BTCUSD-2-exit')
        self.assertIsNotNone(res)
        self.assertIn(stop_id, v.canceled)
        self.assertNotIn('BTC/USD', b.positions)
        self.assertNotIn('BTC/USD', v.positions)
        events = b.drain_events()
        self.assertEqual([e[0] for e in events], ['fill', 'trade'])
        self.assertAlmostEqual(events[1][1].pnl, 0.05 * 3.0)

    def test_duplicate_close_and_exit_submit_are_ignored(self):
        v = FakeVenue()
        b = make_broker(v)
        b.submit(entry())
        pos = b.positions['AAPL']
        pos.closing = True   # an exit is in flight
        self.assertIsNone(b.close_position('AAPL', 100.0, T0, 'eod', 'dup-1'))
        dup = b.submit(OrderReq(id='dup-2', symbol='AAPL', side='sell', qty=10, leg='exit'))
        self.assertEqual(dup.status, 'rejected')
        self.assertEqual(len(v.submitted), 1)

    def test_close_when_a_leg_already_closed_it_records_no_new_order(self):
        v = FakeVenue()
        b = make_broker(v)
        b.submit(entry())
        v.positions.pop('AAPL')  # the venue's stop leg took it out
        res = b.close_position('AAPL', 97.0, T0, 'signal', 'mt-t-x-AAPL-3-exit')
        self.assertIsNone(res)
        self.assertNotIn('AAPL', b.positions)
        self.assertEqual(len(v.submitted), 1)


class ReconciliationTrustsTheVenue(SimpleTestCase):
    def test_divergence_is_reported_and_the_venue_quantity_adopted(self):
        v = FakeVenue()
        b = make_broker(v)
        b.submit(entry())
        v.positions['AAPL'].qty = '7'   # someone sold 3 shares by hand
        info = b.sync()
        self.assertEqual(info['diverged'], [('AAPL', 10.0, 7.0)])
        self.assertEqual(b.positions['AAPL'].qty, 7)

    def test_unknown_position_is_adopted_as_external(self):
        v = FakeVenue()
        v.positions['MSFT'] = SimpleNamespace(symbol='MSFT', qty='3', avg_entry_price='400', current_price='401', side='long')
        b = make_broker(v)
        self.assertTrue(b.positions['MSFT'].external)
        self.assertEqual(b.last_sync['adopted'], ['MSFT'])
        self.assertEqual(b.protection_for('MSFT')[0], 'none')

    def test_position_gone_at_the_venue_is_dropped(self):
        v = FakeVenue()
        b = make_broker(v)
        b.submit(entry())
        v.positions.pop('AAPL')
        info = b.sync()
        self.assertEqual(info['closed'], ['AAPL'])
        self.assertNotIn('AAPL', b.positions)


class OrphanProtectionIsCanceled(SimpleTestCase):
    def test_resting_stop_without_a_position_is_canceled_on_sync(self):
        v = FakeVenue()
        o = SimpleNamespace(id='orphan-1', client_order_id='mt-x-entry-stop', symbol='BTCUSD', qty='0.01', side='sell',
                            status='new', filled_qty='0', filled_avg_price=None, filled_at=None, type='stop_limit', legs=[],
                            order_class='simple')
        v.orders['orphan-1'] = o
        b = AlpacaBroker(client=v, asset_classes={'BTC/USD': 'crypto'}, poll_s=0.5)
        info = b.sync()
        self.assertEqual(info['orphans'], ['mt-x-entry-stop'])
        self.assertIn('orphan-1', v.canceled)


class AlpacaHistoryIsRegularSessionOnly(SimpleTestCase):
    def test_extended_hours_bars_are_dropped_and_windows_stay_under_the_cap(self):
        import pandas as pd
        from main_app.services.data.alpaca_data import regular_session_only, windows
        idx = pd.DatetimeIndex([datetime(2026, 9, 1, 8, 0, tzinfo=UTC), datetime(2026, 9, 1, 14, 0, tzinfo=UTC),
                                datetime(2026, 9, 1, 21, 0, tzinfo=UTC)])
        df = pd.DataFrame({'open': [1, 1, 1], 'high': [1, 1, 1], 'low': [1, 1, 1], 'close': [1, 1, 1], 'volume': [1, 1, 1],
                           'vwap': [1, 1, 1], 'trade_count': [1, 1, 1]}, index=idx)
        kept = regular_session_only(df)
        self.assertEqual(list(kept.index), [idx[1]])
        w = list(windows(datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC), '5Min', 'stock'))
        self.assertGreater(len(w), 5)
        self.assertLessEqual((w[0][1] - w[0][0]).days * 16 * 12, 10000)


class ThePaperLaneReportsItsFeesLikeTheSimulator(SimpleTestCase):
    """The two accounts have to answer the same question to be comparable.

    This adapter recorded fees=0.0 and a GROSS pnl where the simulator records
    fees and a NET one. The moment a lane left the simulator, the per-lane report
    built to judge it would have read "fees 0.00 (0.0 bps), net before fees equals
    net" — a confident, silent lie in exactly the artifact used to decide whether
    promoting it had worked.
    """

    def _broker(self):
        from main_app.services.broker.alpaca import AlpacaBroker
        b = AlpacaBroker.__new__(AlpacaBroker)
        b.asset_classes = {'BTC/USD': 'crypto', 'AAPL': 'stock'}
        b.fee_bps = {'stock': 0.0, 'etf': 0.0, 'crypto': 25.0, 'forex': 0.5}
        return b

    def test_crypto_commission_is_charged_not_zeroed(self):
        b = self._broker()
        self.assertAlmostEqual(b._fee('BTC/USD', 10_000, 'buy'), 25.0)

    def test_a_stock_sale_pays_the_regulatory_fee_a_purchase_does_not(self):
        b = self._broker()
        self.assertAlmostEqual(b._fee('AAPL', 10_000, 'buy'), 0.0)
        self.assertAlmostEqual(b._fee('AAPL', 10_000, 'sell'), 0.3)

    def test_a_recorded_trade_carries_both_legs_and_a_net_pnl(self):
        from datetime import UTC, datetime

        from main_app.services.broker.base import OrderReq, Position
        b = self._broker()
        b._positions = {'BTC/USD': Position(symbol='BTC/USD', qty=1.0, avg_price=100.0,
                                            entry_ts=datetime.now(UTC), strategy_key='x',
                                            entry_fees=0.25)}
        b.trades, b._events = [], []
        order = OrderReq(id='x-exit', symbol='BTC/USD', side='sell', qty=1.0, leg='exit',
                         strategy_key='x', exit_reason='target')
        b._record_trade(order, 110.0, 1.0, datetime.now(UTC), exit_fee=0.275)
        tr = b.trades[-1]
        self.assertAlmostEqual(tr.fees, 0.525)
        self.assertAlmostEqual(tr.pnl, 10.0 - 0.525,
                               msg='pnl must be net of fees, as the simulator records it')
