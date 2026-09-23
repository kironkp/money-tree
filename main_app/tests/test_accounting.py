"""The books: money in, money out, and whether the venue agrees.

Trades explain what the strategies did. They do not explain the balance — a
deposit, a financing charge or a currency conversion moves cash with no trade
behind it, and on a margin forex account that happens nightly. Without a record
of them, reconciliation can only work by assuming they never occur.
"""
import io
import zipfile
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.test import TestCase
from django.utils import timezone

from main_app.models import (Account, AgentConfig, CashMovement, Fill, Instrument, Order,
                             Position, ReconciliationSnapshot, Signal, Trade)
from main_app.services.audit_export import write_bundle
from main_app.services.reconcile import compare, record_movement


class FakeBroker:
    """Only what reconciliation reads. A real adapter must pass the conformance
    suite; this stands in for one whose numbers we control."""
    def __init__(self, positions=None, cash=10000.0, equity=10000.0, fees=None):
        self.positions = {s: SimpleNamespace(qty=q) for s, q in (positions or {}).items()}
        self._cash, self._equity = cash, equity
        if fees is not None:
            self.fees_charged = fees

    def account(self):
        return SimpleNamespace(cash=self._cash, equity=self._equity, buying_power=self._cash)


class AccountingCase(TestCase):
    def setUp(self):
        AgentConfig.get()
        self.inst = Instrument.objects.create(symbol='EUR/USD', asset_class='forex', market='forex')
        self.account = Account.objects.create(mode='sim', market='forex', name='Forex (sim)',
                                              starting_cash=Decimal('10000'), cash=Decimal('10000'),
                                              equity=Decimal('10000'))
        self.now = timezone.now()


class CashMovesAreRecordedAndCannotBeDoubled(AccountingCase):
    def test_a_deposit_is_recorded_with_its_broker_reference(self):
        m = record_movement(self.account, CashMovement.DEPOSIT, 500, broker_ref='TXN-1',
                            source='broker', note='initial funding')
        self.assertEqual(m.amount, Decimal('500'))
        self.assertEqual(m.source, 'broker')

    def test_reading_the_same_statement_line_twice_does_not_double_the_money(self):
        record_movement(self.account, CashMovement.DEPOSIT, 500, broker_ref='TXN-1')
        record_movement(self.account, CashMovement.DEPOSIT, 500, broker_ref='TXN-1')
        self.assertEqual(CashMovement.objects.filter(account=self.account).count(), 1)

    def test_movements_without_a_reference_are_still_separate_events(self):
        """Two genuine manual adjustments must not collapse into one."""
        record_movement(self.account, CashMovement.ADJUSTMENT, -5, note='a')
        record_movement(self.account, CashMovement.ADJUSTMENT, -5, note='b')
        self.assertEqual(CashMovement.objects.count(), 2)

    def test_a_conversion_keeps_both_sides_and_the_rate(self):
        m = record_movement(self.account, CashMovement.CONVERSION, 1080, currency='USD',
                            from_currency='EUR', from_amount=1000, rate='1.08')
        self.assertEqual(m.from_currency, 'EUR')
        self.assertEqual(m.rate, Decimal('1.0800000000'))


class ReconciliationComparesMoreThanPositions(AccountingCase):
    def test_agreement_is_recorded_too_not_only_disagreement(self):
        """A long run of clean checks is the evidence the books can be trusted.
        Only writing the failures leaves nothing to point at."""
        snap = compare(self.account, FakeBroker(cash=10000.0, equity=10000.0), self.now)
        self.assertTrue(snap.ok)
        self.assertEqual(ReconciliationSnapshot.objects.count(), 1)
        self.assertIn('agree', snap.note)

    def test_a_cash_difference_is_caught(self):
        snap = compare(self.account, FakeBroker(cash=9900.0, equity=10000.0), self.now)
        self.assertFalse(snap.ok)
        self.assertEqual(snap.discrepancies[0]['what'], 'cash')
        self.assertAlmostEqual(snap.discrepancies[0]['delta'], 100.0, places=2)

    def test_an_equity_difference_is_caught(self):
        snap = compare(self.account, FakeBroker(cash=10000.0, equity=9500.0), self.now)
        self.assertTrue(any(d['what'] == 'equity' for d in snap.discrepancies))

    def test_a_position_difference_is_caught(self):
        Position.objects.create(account=self.account, instrument=self.inst, strategy_key='ema',
                                qty=Decimal('1000'), avg_price=Decimal('1.08'), opened_at=self.now)
        snap = compare(self.account, FakeBroker(positions={'EUR/USD': 900.0}), self.now)
        self.assertTrue(any('position EUR/USD' in d['what'] for d in snap.discrepancies))

    def test_fees_the_venue_charged_are_compared_with_the_fees_we_modelled(self):
        Order.objects.create(account=self.account, instrument=self.inst, side='buy', qty=1,
                             client_order_id='o1', fees=Decimal('2.50'))
        snap = compare(self.account, FakeBroker(fees=4.00), self.now)
        fee = [d for d in snap.discrepancies if d['what'] == 'fees'][0]
        self.assertAlmostEqual(fee['delta'], -1.50, places=2)

    def test_cash_that_no_trade_or_movement_explains_is_flagged(self):
        """The whole point of the ledger: money arrived and nothing accounts for it."""
        snap = compare(self.account, FakeBroker(cash=10750.0, equity=10750.0), self.now)
        self.assertTrue(any(d['what'] == 'unexplained cash' for d in snap.discrepancies))

    def test_a_recorded_deposit_explains_itself(self):
        record_movement(self.account, CashMovement.DEPOSIT, 750, broker_ref='TXN-9')
        self.account.cash = Decimal('10750')
        self.account.equity = Decimal('10750')
        self.account.save()
        snap = compare(self.account, FakeBroker(cash=10750.0, equity=10750.0), self.now)
        self.assertFalse(any(d['what'] == 'unexplained cash' for d in snap.discrepancies), snap.discrepancies)

    def test_an_open_book_is_not_accused_of_losing_cash(self):
        """An open position holds cash that has left the balance without being
        lost. Comparing it would fire on every tick and teach the operator to
        ignore the alarm."""
        Position.objects.create(account=self.account, instrument=self.inst, strategy_key='ema',
                                qty=Decimal('1000'), avg_price=Decimal('1.08'), opened_at=self.now)
        snap = compare(self.account, FakeBroker(positions={'EUR/USD': 1000.0}, cash=8920.0,
                                                equity=10000.0), self.now)
        self.assertFalse(any(d['what'] == 'unexplained cash' for d in snap.discrepancies))

    def test_a_broker_that_cannot_be_read_is_a_failed_check_not_a_clean_one(self):
        class Dead:
            @property
            def positions(self):
                raise ConnectionError('socket closed')
        snap = compare(self.account, Dead(), self.now)
        self.assertFalse(snap.ok)
        self.assertIn('could not read the broker', snap.note)


class TheAuditExportIsCompleteAndDoesNotOverclaim(AccountingCase):
    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(account=self.account, instrument=self.inst, side='buy',
                                          qty=Decimal('1000'), client_order_id='mt-sim-ema-EURUSD-1-entry',
                                          broker_order_id='BRK-77', fees=Decimal('2'), status='filled')
        Fill.objects.create(order=self.order, ts=self.now, qty=Decimal('600'), price=Decimal('1.08'),
                            fee=Decimal('1.2'), realized_slippage_bps=0.4)
        Fill.objects.create(order=self.order, ts=self.now, qty=Decimal('400'), price=Decimal('1.081'),
                            fee=Decimal('0.8'), realized_slippage_bps=0.5)
        Signal.objects.create(account=self.account, instrument=self.inst, strategy_key='ema',
                              ts=self.now, action='buy', price=Decimal('1.08'), acted=True,
                              order=self.order)
        Trade.objects.create(account=self.account, instrument=self.inst, strategy_key='ema',
                             side='long', qty=1000, entry_ts=self.now, exit_ts=self.now,
                             entry_price=Decimal('1.08'), exit_price=Decimal('1.09'),
                             pnl=Decimal('8'), pnl_pct=Decimal('0.9'), fees=Decimal('2'),
                             bars_held=3, exit_reason='target')
        record_movement(self.account, CashMovement.DEPOSIT, 500, broker_ref='TXN-1')
        compare(self.account, FakeBroker(), self.now)

    def _bundle(self):
        buf = io.BytesIO()
        write_bundle(self.account, buf, self.now)
        return zipfile.ZipFile(io.BytesIO(buf.getvalue()))

    def test_every_table_is_present(self):
        names = set(self._bundle().namelist())
        self.assertEqual(names, {'README.txt', 'signals.csv', 'orders.csv', 'fills.csv',
                                 'trades.csv', 'cash_movements.csv', 'reconciliations.csv'})

    def test_both_halves_of_a_partial_fill_survive(self):
        rows = self._bundle().read('fills.csv').decode().strip().splitlines()
        self.assertEqual(len(rows), 3)                       # header + two fills
        self.assertIn('yes', rows[1])                        # marked partial
        self.assertIn('0.4', rows[1])                        # its slippage measurement

    def test_a_line_can_be_followed_from_signal_to_order_to_fill(self):
        z = self._bundle()
        oid = 'mt-sim-ema-EURUSD-1-entry'
        self.assertIn(oid, z.read('signals.csv').decode())
        self.assertIn(oid, z.read('orders.csv').decode())
        self.assertIn(oid, z.read('fills.csv').decode())
        self.assertIn('BRK-77', z.read('orders.csv').decode())   # and out to the venue

    def test_it_does_not_call_itself_tax_ready(self):
        """Producing a return needs a cost-basis method, wash-sale treatment and
        a §988/§1256 election, none of which has been established here. Naming
        the file tax-ready would invite somebody to file it."""
        readme = self._bundle().read('README.txt').decode().lower()
        self.assertNotIn('tax-ready', readme)
        self.assertNotIn('tax ready', readme)
        self.assertIn('not a tax export', readme)
        self.assertIn('§988', self._bundle().read('README.txt').decode())

    def test_it_says_the_money_was_simulated(self):
        self.assertIn('simulated', self._bundle().read('README.txt').decode().lower())


class AnEpochResetDoesNotBreakTheBooksForever(AccountingCase):
    """reset_epoch rewrites starting_cash and cash and deliberately keeps every
    trade. Summing trades over all time against a reset balance left the
    identity wrong by the whole lifetime P&L, permanently — and on a paper or
    live lane that check runs every tick, blocks new entries and halts the lane
    with a finding that can never clear. The first reset after going to paper
    would have bricked it."""

    def setUp(self):
        super().setUp()
        old = self.now - timedelta(days=30)
        Trade.objects.create(account=self.account, instrument=self.inst, strategy_key='ema',
                             side='long', qty=1000, entry_ts=old, exit_ts=old,
                             entry_price=Decimal('1.10'), exit_price=Decimal('1.09'),
                             pnl=Decimal('-3000'), pnl_pct=Decimal('-1'), fees=Decimal('0'),
                             bars_held=1, exit_reason='stop')
        self.account.epoch_started_at = self.now - timedelta(days=1)
        self.account.starting_cash = Decimal('10000')
        self.account.cash = Decimal('10000')
        self.account.equity = Decimal('10000')
        self.account.save()

    def test_a_reset_account_that_agrees_with_the_broker_reports_clean(self):
        snap = compare(self.account, FakeBroker(cash=10000.0, equity=10000.0), self.now)
        self.assertTrue(snap.ok, snap.discrepancies)

    def test_a_real_gap_after_a_reset_is_still_caught(self):
        snap = compare(self.account, FakeBroker(cash=10400.0, equity=10400.0), self.now)
        d = [x for x in snap.discrepancies if x['what'] == 'unexplained cash']
        self.assertTrue(d)
        self.assertAlmostEqual(d[0]['delta'], 400.0, places=2)

    def test_a_clean_row_says_when_it_could_not_check_provenance(self):
        """A clean row that cannot distinguish 'agreed' from 'not looked at' is
        how a gap hides."""
        Position.objects.create(account=self.account, instrument=self.inst, strategy_key='ema',
                                qty=Decimal('1000'), avg_price=Decimal('1.08'), opened_at=self.now)
        snap = compare(self.account, FakeBroker(positions={'EUR/USD': 1000.0}), self.now)
        self.assertIn('provenance not checked', snap.note)
