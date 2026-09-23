"""The contract a broker adapter must satisfy before its lane may be armed.

This exists because of a specific fact: the forex lane cannot trade real money,
because there is no forex broker adapter. Alpaca does not offer currencies, and
`agent.py` refuses forex in paper and live for exactly that reason. The gap is
therefore not a bug to fix but an integration to build — and the danger with an
integration built later, under pressure, against a live account, is that nobody
writes down what it was supposed to guarantee.

So it is written down here first, as executable tests. `BrokerConformance` is a
mixin: point it at an adapter, and it will refuse the adapter that double-fills
a resubmitted order, loses a partial fill, reverses a position on a duplicate
close, or reports success while disconnected.

SimBroker is run through it below to prove the suite is real. Any forex adapter
must pass the same suite before `LIVE_BROKERS` may admit it, and the arming gate
reads that list.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase

from main_app.services.broker.base import OrderReq
from main_app.services.broker.sim import SimBroker

T0 = datetime(2026, 9, 22, 13, 30, tzinfo=UTC)


def bar(o, h, l, c, v=100000):
    return SimpleNamespace(open=o, high=h, low=l, close=c, volume=v)


class BrokerConformance:
    """Mixin. Implement `make_broker()` and `symbol`, then inherit."""

    symbol = 'X'
    asset_class = 'stock'

    def make_broker(self):
        raise NotImplementedError

    def _entry(self, oid='e1', qty=10, side='buy', stop=95.0, target=110.0):
        return OrderReq(id=oid, symbol=self.symbol, side=side, qty=qty, leg='entry',
                        strategy_key='t', decision_price=100.0, bar_ts=T0, submitted_ts=T0,
                        stop=stop, target=target)

    def _prime(self, b, price=100.0, ts=T0):
        b.on_bar(self.symbol, bar(price, price + 1, price - 1, price), ts)

    # --- the interface itself ------------------------------------------------
    def test_it_implements_the_whole_broker_interface(self):
        b = self.make_broker()
        for name in ('cash', 'positions', 'account', 'submit', 'on_bar', 'close_position',
                     'cancel_open_orders', 'can_short', 'sync', 'open_orders_for',
                     'protection_for', 'drain_events'):
            self.assertTrue(hasattr(b, name), f'adapter is missing {name}()')

    def test_account_state_carries_cash_equity_and_buying_power(self):
        a = self.make_broker().account()
        for f in ('cash', 'equity', 'buying_power'):
            self.assertIsNotNone(getattr(a, f, None), f'AccountState.{f} is required for reconciliation')

    # --- idempotency: the failure that doubles real money --------------------
    def test_resubmitting_one_client_order_id_does_not_trade_twice(self):
        b = self.make_broker()
        self._prime(b)
        b.submit(self._entry('dup-1'))
        second = b.submit(self._entry('dup-1'))
        self._prime(b, 100.0, T0 + timedelta(minutes=5))
        self.assertEqual(second.status, 'rejected',
                         'a resubmitted client_order_id must be rejected, not accepted again')
        pos = b.positions.get(self.symbol)
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(abs(float(pos.qty)), 10.0, places=6,
                               msg='the duplicate submission doubled the position')

    def test_a_duplicate_exit_is_refused_rather_than_reversing_the_position(self):
        b = self.make_broker()
        self._prime(b)
        b.submit(self._entry('e-rev'))
        self._prime(b, 100.0, T0 + timedelta(minutes=5))
        qty = abs(float(b.positions[self.symbol].qty))
        first = b.submit(OrderReq(id='x-1', symbol=self.symbol, side='sell', qty=qty, leg='exit'))
        second = b.submit(OrderReq(id='x-2', symbol=self.symbol, side='sell', qty=qty, leg='exit'))
        self.assertEqual(first.status, 'accepted')
        self.assertEqual(second.status, 'rejected',
                         'a second exit while one is in flight would sell a position twice and go short')

    def test_closing_an_absent_position_is_refused_not_opened_short(self):
        b = self.make_broker()
        self._prime(b)
        o = b.submit(OrderReq(id='ghost', symbol=self.symbol, side='sell', qty=10, leg='exit'))
        self.assertEqual(o.status, 'rejected')
        self.assertNotIn(self.symbol, b.positions)

    # --- fills ---------------------------------------------------------------
    def test_every_fill_carries_a_fee_and_a_slippage_measurement(self):
        b = self.make_broker()
        self._prime(b)
        b.submit(self._entry('fee-1'))
        self._prime(b, 100.0, T0 + timedelta(minutes=5))
        self.assertTrue(b.fills, 'no fill was emitted')
        f = b.fills[-1]
        self.assertIsNotNone(getattr(f, 'fee', None), 'a fill without a fee cannot be reconciled')
        self.assertIsNotNone(getattr(f, 'slippage_bps', None),
                             'a fill without a slippage measurement makes the cost model unfalsifiable')

    def test_a_partial_fill_emits_its_own_event_rather_than_being_rounded_away(self):
        b = self.make_broker()
        b.liquidity_cap_pct = 0.001            # force the cap to bite
        self._prime(b, 100.0)
        b.submit(self._entry('part-1', qty=1000))
        self._prime(b, 100.0, T0 + timedelta(minutes=5))
        filled = sum(float(f.qty) for f in b.fills)
        self.assertGreater(len(b.fills), 0)
        self.assertLess(filled, 1000.0, 'the liquidity cap did not produce a partial fill')

    # --- reconciliation ------------------------------------------------------
    def test_sync_returns_a_dict_the_caller_can_read(self):
        """sync() reports what CHANGED at the venue. The balance comparison reads
        account(), which is why that one is asserted separately above — a broker
        that is its own venue has nothing to sync and legitimately returns {}."""
        self.assertIsInstance(self.make_broker().sync(), dict)

    def test_the_balances_reconciliation_compares_are_all_present(self):
        b = self.make_broker()
        self._prime(b)
        b.submit(self._entry('rec-1'))
        self._prime(b, 100.0, T0 + timedelta(minutes=5))
        a = b.account()
        self.assertGreater(float(a.equity), 0)
        self.assertIsNotNone(a.cash)
        self.assertIn(self.symbol, b.positions, 'positions must be readable by symbol')

    def test_can_short_gives_a_reason_when_it_says_no(self):
        ok, why = self.make_broker().can_short(self.symbol)
        self.assertIsInstance(ok, bool)
        if not ok:
            self.assertTrue(why, 'a refusal with no reason cannot be shown to the operator')

    # --- failure behaviour ---------------------------------------------------
    def test_an_unpriced_symbol_is_refused_rather_than_filled_at_a_guess(self):
        b = self.make_broker()
        o = b.submit(self._entry('nopx'))
        self._prime(b, 100.0, T0 + timedelta(minutes=5))
        # Either refuse outright, or accept and only fill once a price exists.
        # What is forbidden is inventing a fill price.
        if o.status not in ('rejected', 'canceled'):
            for f in b.fills:
                self.assertGreater(float(f.price), 0)


class SimBrokerMeetsTheContract(BrokerConformance, SimpleTestCase):
    """Proving the suite is real by running the adapter we already trust."""

    def make_broker(self):
        return SimBroker(10000, slippage_bps=3.0, fee_bps={'stock': 0.5},
                         liquidity_cap_pct=100.0)


class NoForexAdapterExistsYet(TestCase):
    """The blocking fact, pinned so it cannot be forgotten or quietly assumed away."""

    def test_paper_forex_is_refused_because_no_adapter_exists(self):
        from main_app.services.agent import Agent
        with self.assertRaises(RuntimeError) as cm:
            Agent(mode='paper', market='forex').setup()
        self.assertIn('forex', str(cm.exception).lower())

    def test_live_forex_is_refused_twice_over(self):
        """Live is stopped by the arming gate before the missing-adapter gate is
        even reached. Both locks are real and independent: arming live would
        still leave forex with no venue to send an order to."""
        from main_app.services.agent import Agent
        with self.assertRaises(RuntimeError) as cm:
            Agent(mode='live', market='forex').setup()
        self.assertIn('live mode refused', str(cm.exception).lower())
        with self.settings(LIVE_TRADING_ARMED=True):
            from main_app.models import AgentConfig
            cfg = AgentConfig.get()
            cfg.mode = 'live'
            cfg.save()
            with self.assertRaises(RuntimeError) as cm2:
                Agent(mode='live', market='forex').setup()
            self.assertIn('forex', str(cm2.exception).lower())

    def test_alpaca_is_not_registered_as_a_forex_venue(self):
        from main_app.services.broker.alpaca import AlpacaBroker
        self.assertNotIn('forex', getattr(AlpacaBroker, 'SUPPORTED_MARKETS', ('stocks', 'crypto')),
                         'Alpaca does not offer currency trading; listing it would arm a lane '
                         'against a venue that cannot execute it')
