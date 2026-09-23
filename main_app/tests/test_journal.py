from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from main_app.services.data import calendar as cal

from main_app.models import Account, Qualification, Strategy, Trade
from main_app.services.journal import day_summary, drift_check, write_eod_journal
from main_app.services.promotion import (graduation_checklist, promote, qualification_assessment,
                                         refresh_qualification)
from main_app.tests.helpers import enable_strategy, seed_db


class JournalSummarisesTheDayAndCatchesDrift(TestCase):
    def setUp(self):
        self.cfg, self.instruments = seed_db(('QQQ',), with_bars=False)
        self.account = Account.for_mode('sim')
        self.row = enable_strategy('orb', symbols=['QQQ'])
        promote(self.row, self.row.params, 'backtest #1', metrics={'expectancy': 5.0, 'trades': 40, 'profit_factor': 1.5})
        # Promoted a month ago, traded since: forward evidence has to post-date the
        # parameters it judges. Relative to today so the window never ages out.
        self.row.evidence_since = timezone.now() - timedelta(days=30)
        self.row.save(update_fields=['evidence_since'])
        t = (timezone.now() - timedelta(days=5)).replace(hour=15, minute=0, second=0, microsecond=0)
        self.day = t.astimezone(cal.ET).date()
        for i in range(25):
            Trade.objects.create(account=self.account, instrument=self.instruments['QQQ'], strategy_key='orb', side='long',
                                 qty=1, entry_ts=t, exit_ts=t, entry_price=100, exit_price=99, pnl=Decimal('-2.00'),
                                 pnl_pct=Decimal('-1'), exit_reason='stop')

    def test_day_summary_counts_trades(self):
        s = day_summary(self.account, self.day)
        self.assertEqual(s['trades'], 25)
        self.assertEqual(s['net_pnl'], -50.0)
        self.assertEqual(s['exit_reasons'], {'stop': 25})

    def test_drift_auto_disables_the_strategy(self):
        dc = drift_check(self.account, self.row)
        self.assertTrue(dc['drift'])
        entry = write_eod_journal(self.account, self.day)
        self.row.refresh_from_db()
        self.assertFalse(self.row.enabled)
        self.assertIn('DRIFT', entry.body)
        self.assertEqual(entry.kind, 'auto_eod')

    def test_graduation_checklist_reports_gaps(self):
        self.row.stage = 'sprout'
        self.row.save()
        check = graduation_checklist(self.row, self.account, self.cfg)
        names = {i['name']: i['ok'] for i in check['items']}
        self.assertFalse(names['Closed trades'])      # 25 < 30
        self.assertFalse(names['Profit factor'])
        self.assertEqual(check['next'], 'sapling')
        self.assertFalse(check['ready'])

    def test_sufficient_losing_forward_sample_is_quarantined_and_disabled(self):
        t = (timezone.now() - timedelta(days=4)).replace(hour=15, minute=0, second=0, microsecond=0)
        for _ in range(5):
            Trade.objects.create(account=self.account, instrument=self.instruments['QQQ'], strategy_key='orb', side='long',
                                 qty=1, entry_ts=t, exit_ts=t, entry_price=100, exit_price=99, pnl=Decimal('-2.00'),
                                 pnl_pct=Decimal('-1'), exit_reason='stop')
        assessment, changed = refresh_qualification(self.row, self.account)
        self.row.refresh_from_db()
        self.assertTrue(changed)
        self.assertTrue(assessment['no_edge'])
        self.assertEqual(self.row.qualification, Qualification.QUARANTINED)
        self.assertFalse(self.row.enabled)


class ProfitableEvidenceCanQualify(TestCase):
    def test_requires_held_out_provenance_and_forward_sessions(self):
        cfg, instruments = seed_db(('QQQ',), with_bars=False)
        account = Account.for_mode('sim')
        row = enable_strategy('orb', symbols=['QQQ'])
        promote(row, row.params, 'experiment #7',
                metrics={'expectancy': 4.0, 'net_pnl': 160, 'trades': 40, 'profit_factor': 1.5},
                evidence={'kind': 'held_out_validation', 'same_bars': True,
                          'window': {'test_start': '2026-06-01', 'test_end': '2026-07-31'}})
        # Twenty distinct sessions of forward trading AFTER the promotion.
        row.evidence_since = timezone.now() - timedelta(days=25)
        row.save(update_fields=['evidence_since'])
        for i in range(30):
            t = (timezone.now() - timedelta(days=(i % 20) + 1)).replace(hour=15, minute=0, second=0, microsecond=0)
            pnl = Decimal('3.00') if i % 4 else Decimal('-1.00')
            Trade.objects.create(account=account, instrument=instruments['QQQ'], strategy_key='orb', side='long',
                                 qty=1, entry_ts=t, exit_ts=t, entry_price=100, exit_price=101, pnl=pnl,
                                 pnl_pct=Decimal('1'), exit_reason='target' if pnl > 0 else 'stop')
        assessment = qualification_assessment(row, account)
        self.assertTrue(assessment['research_ok'])
        self.assertTrue(assessment['ready'])
        persisted, changed = refresh_qualification(row, account)
        row.refresh_from_db()
        self.assertTrue(changed)
        self.assertEqual(persisted['state'], Qualification.QUALIFIED)
        self.assertEqual(row.qualification, Qualification.QUALIFIED)


class EachBotCarriesItsOwnFees(TestCase):
    """One desk-wide fee number hides the only thing that matters here.

    Degen and crypto pay 50 bps a round trip; stocks and forex pay 1. Averaging
    them makes every lane look like the same problem, when in fact one lane can be
    ready to promote while another bleeds tolls in the simulator.
    """

    def setUp(self):
        from decimal import Decimal

        from main_app.models import Account, Instrument, Trade
        from django.utils import timezone
        self.inst = Instrument.objects.create(symbol='EUR/USD', asset_class='forex', market='forex')
        self.account = Account.objects.create(mode='sim', market='forex',
                                              starting_cash=10000, cash=10000)
        now = timezone.now()
        for pnl, fee in ((Decimal('40'), Decimal('12')), (Decimal('-30'), Decimal('12'))):
            Trade.objects.create(
                account=self.account, instrument=self.inst, strategy_key='ema_momentum',
                side='long', qty=1000, entry_ts=now, exit_ts=now,
                entry_price=Decimal('1.05'), exit_price=Decimal('1.06'), pnl=pnl,
                pnl_pct=Decimal('0.9'), fees=fee, bars_held=3, exit_reason='target')

    def test_it_reports_what_the_lane_moved_not_what_it_holds(self):
        from main_app.services.report import lane_costs
        c = lane_costs(self.account)
        self.assertEqual(c['trades'], 2)
        self.assertAlmostEqual(c['moved'], 2100.0)      # 2 x 1000 x 1.05
        self.assertAlmostEqual(c['fees'], 24.0)

    def test_the_fee_rate_is_measured_against_what_moved(self):
        from main_app.services.report import lane_costs
        c = lane_costs(self.account)
        self.assertAlmostEqual(c['fee_bps'], 24.0 / 2100.0 * 10000, places=4)

    def test_it_separates_the_trading_from_the_tolls(self):
        """+10 after fees, +34 before them. That gap is the whole point."""
        from main_app.services.report import lane_costs
        c = lane_costs(self.account)
        self.assertAlmostEqual(c['net'], 10.0)
        self.assertAlmostEqual(c['net_before_fees'], 34.0)

    def test_a_lane_that_only_loses_to_fees_is_identifiable(self):
        from decimal import Decimal

        from main_app.models import Trade
        from django.utils import timezone
        Trade.objects.filter(account=self.account).delete()
        now = timezone.now()
        Trade.objects.create(
            account=self.account, instrument=self.inst, strategy_key='x', side='long', qty=1000,
            entry_ts=now, exit_ts=now, entry_price=Decimal('1.05'), exit_price=Decimal('1.05'),
            pnl=Decimal('-5'), pnl_pct=Decimal('0'), fees=Decimal('8'), bars_held=1,
            exit_reason='target')
        from main_app.services.report import lane_costs
        c = lane_costs(self.account)
        self.assertLess(c['net'], 0)
        self.assertGreater(c['net_before_fees'], 0, 'profitable before fees')

    def test_a_lane_with_no_trades_reports_zeroes_not_an_error(self):
        from main_app.models import Account, Trade
        from main_app.services.report import lane_costs
        Trade.objects.filter(account=self.account).delete()
        c = lane_costs(self.account)
        self.assertEqual(c['trades'], 0)
        self.assertEqual(c['fee_bps'], 0.0)


class AResetMovesTheStartingLineNotTheMemory(TestCase):
    """The money resets; the record does not.

    Deleting trades would do exactly what `evidence_since` did — erase a
    strategy's earned quarantine and let a losing idea run again. That mistake
    cost $1,077.52 the first time. A reset must never be able to buy a failed
    strategy a second life.
    """

    def setUp(self):
        from decimal import Decimal

        from django.utils import timezone

        from main_app.models import Account, Instrument, Trade
        self.inst = Instrument.objects.create(symbol='SOL/USD', asset_class='crypto', market='degen')
        self.account = Account.objects.create(mode='sim', market='degen',
                                              starting_cash=10000, cash=6000, equity=6000)
        now = timezone.now()
        for i in range(160):
            Trade.objects.create(
                account=self.account, instrument=self.inst, strategy_key='burst', side='long',
                qty=1, entry_ts=now, exit_ts=now, entry_price=Decimal('100'),
                exit_price=Decimal('99'), pnl=Decimal('-25'), pnl_pct=Decimal('-1'),
                fees=Decimal('1'), bars_held=2, exit_reason='stop')

    def _reset(self):
        from django.core.management import call_command
        call_command('reset_epoch', '--apply', '--markets', 'degen', '--note', 'test', verbosity=0)
        self.account.refresh_from_db()

    def test_the_balance_returns_to_seed_capital(self):
        self._reset()
        self.assertEqual(self.account.equity, self.account.starting_cash)
        self.assertIsNotNone(self.account.epoch_started_at)

    def test_not_one_trade_is_deleted(self):
        from main_app.models import Trade
        self._reset()
        self.assertEqual(Trade.objects.count(), 160)

    def test_the_lifetime_brake_still_sees_everything(self):
        from main_app.models import Strategy
        from main_app.services.promotion import lifetime_verdict
        row = Strategy.objects.create(key='burst', name='Burst', market='degen', params={})
        self._reset()
        verdict = lifetime_verdict(row, self.account)
        self.assertIn('no edge across its whole life', verdict,
                      'a reset must not buy a failed strategy a second life')
        self.assertIn('160 trades', verdict)

    def test_the_report_counts_only_the_new_epoch(self):
        from main_app.services.report import lane_costs
        self._reset()
        self.assertEqual(lane_costs(self.account)['trades'], 0)
        self.assertEqual(lane_costs(self.account, all_time=True)['trades'], 160,
                         'all_time must still see the whole record')

    def test_it_refuses_while_a_position_is_open(self):
        from decimal import Decimal

        from django.core.management import call_command
        from django.utils import timezone

        from main_app.models import Position
        Position.objects.create(account=self.account, instrument=self.inst, qty=Decimal('1'),
                                avg_price=Decimal('100'), opened_at=timezone.now())
        before = self.account.equity
        call_command('reset_epoch', '--apply', '--markets', 'degen', verbosity=0)
        self.account.refresh_from_db()
        self.assertEqual(self.account.equity, before,
                         'resetting mid-trade books the rest of it against a balance it never '
                         'opened from')


class SlippageIsACostAndIsReportedAsOne(TestCase):
    """Fees came out of pnl; slippage never did — it is inside the fill price.

    Reporting only fees let the forex lane describe itself as paying 0.5 bps a
    side while the measured gap between decision and fill cost it more than the
    commission did. Gross, every cost, and net now travel together.
    """
    def setUp(self):
        from main_app.models import Account, Fill, Instrument, Order, Trade
        self.inst = Instrument.objects.create(symbol='EUR/USD', asset_class='forex', market='forex')
        self.account = Account.objects.create(mode='sim', market='forex', starting_cash=10000, cash=10000)
        now = timezone.now()
        Trade.objects.create(account=self.account, instrument=self.inst, strategy_key='ema_momentum',
                             side='long', qty=1000, entry_ts=now, exit_ts=now,
                             entry_price=Decimal('1.00'), exit_price=Decimal('1.02'),
                             pnl=Decimal('18'), pnl_pct=Decimal('1.8'), fees=Decimal('2'),
                             bars_held=3, exit_reason='target')
        self.order = Order.objects.create(
            account=self.account, instrument=self.inst, side='buy', qty=1000,
            strategy_key='ema_momentum', client_order_id='mt-sim-ema-EURUSD-1-entry',
            status='filled')
        self.now = now

    def _fill(self, bps):
        from main_app.models import Fill
        return Fill.objects.create(order=self.order, ts=self.now, qty=1000, price=Decimal('1.00'),
                                   fee=Decimal('1'), realized_slippage_bps=bps)

    def test_an_adverse_fill_is_added_to_the_cost_stack(self):
        from main_app.services.report import lane_costs
        self._fill(10.0)                                   # 10 bps of 1000 x 1.00 = $1.00
        c = lane_costs(self.account)
        self.assertTrue(c['slippage_measured'])
        self.assertAlmostEqual(c['slippage'], 1.0, places=6)
        self.assertAlmostEqual(c['costs'], 3.0, places=6)   # $2 fees + $1 slippage
        self.assertAlmostEqual(c['net'], 18.0, places=6)
        self.assertAlmostEqual(c['gross'], 21.0, places=6)  # net + every cost

    def test_a_favourable_fill_is_not_counted_as_a_cost(self):
        """The field is signed against us. abs() would bill the desk for a
        fill that came in better than it asked for."""
        from main_app.services.report import lane_costs
        self._fill(-10.0)
        c = lane_costs(self.account)
        self.assertAlmostEqual(c['slippage'], -1.0, places=6)
        self.assertAlmostEqual(c['gross'], 19.0, places=6)

    def test_slippage_follows_the_trades_it_is_reported_beside(self):
        """Scoping both by date looked equivalent and is not. A fill belonging to
        a position that has not closed has no trade in `net`, so its cost joined
        the stack while its P&L did not and gross silently inflated — enough, with
        one large open position, to push a lane whose closed trades cost 25% of
        gross to a reported 98%, over the line that raises a critical finding."""
        from main_app.services.report import lane_costs
        stray = self._fill(100.0)                       # a fill outside every trade's span
        stray.ts = self.now - timedelta(days=30)
        stray.save(update_fields=['ts'])
        self._fill(10.0)                                # this one sits inside the trade
        c = lane_costs(self.account)
        self.assertAlmostEqual(c['slippage'], 1.0, places=6)
        self.assertTrue(c['slippage_measured'])

    def test_a_trade_missing_a_leg_measurement_is_reported_as_incomplete(self):
        """Two fills per round trip is the shape. Short of that the number is a
        floor, and saying so is the difference between a measurement and a
        guess wearing a measurement's clothes."""
        from main_app.services.report import lane_costs
        self._fill(10.0)                                # one leg only
        c = lane_costs(self.account)
        self.assertTrue(c['slippage_measured'])
        self.assertFalse(c['slippage_complete'])
        self._fill(10.0)
        self.assertTrue(lane_costs(self.account)['slippage_complete'])

    def test_an_assumed_cost_is_never_reported_as_a_measured_one(self):
        from main_app.services.report import lane_costs
        c = lane_costs(self.account)                                   # no fills at all
        self.assertFalse(c['slippage_measured'])
        self.assertGreater(c['slippage'], 0.0)

    def test_cost_share_works_when_the_lane_is_up(self):
        """fee_share only ever spoke when a lane was losing, so a profitable lane
        handing over most of what it earned reported a clean 0."""
        from main_app.services.report import lane_costs
        self._fill(10.0)
        c = lane_costs(self.account)
        self.assertEqual(c['fee_share'], 0.0)                           # up, so the old number is silent
        self.assertAlmostEqual(c['cost_share'], 3.0 / 21.0 * 100, places=4)
