from datetime import UTC, date, datetime
from decimal import Decimal

from django.test import TestCase

from main_app.models import Account, Strategy, Trade
from main_app.services.journal import day_summary, drift_check, write_eod_journal
from main_app.services.promotion import graduation_checklist, promote
from main_app.tests.helpers import enable_strategy, seed_db


class JournalSummarisesTheDayAndCatchesDrift(TestCase):
    def setUp(self):
        self.cfg, self.instruments = seed_db(('QQQ',), with_bars=False)
        self.account = Account.for_mode('sim')
        self.row = enable_strategy('orb', symbols=['QQQ'])
        promote(self.row, self.row.params, 'backtest #1', metrics={'expectancy': 5.0, 'trades': 40, 'profit_factor': 1.5})
        t = datetime(2026, 8, 28, 15, 0, tzinfo=UTC)
        for i in range(25):
            Trade.objects.create(account=self.account, instrument=self.instruments['QQQ'], strategy_key='orb', side='long',
                                 qty=1, entry_ts=t, exit_ts=t, entry_price=100, exit_price=99, pnl=Decimal('-2.00'),
                                 pnl_pct=Decimal('-1'), exit_reason='stop')

    def test_day_summary_counts_trades(self):
        s = day_summary(self.account, date(2026, 8, 28))
        self.assertEqual(s['trades'], 25)
        self.assertEqual(s['net_pnl'], -50.0)
        self.assertEqual(s['exit_reasons'], {'stop': 25})

    def test_drift_auto_disables_the_strategy(self):
        dc = drift_check(self.account, self.row)
        self.assertTrue(dc['drift'])
        entry = write_eod_journal(self.account, date(2026, 8, 28))
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
