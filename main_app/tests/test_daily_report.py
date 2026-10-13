import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from main_app.models import Account, Fill, Order, Qualification, Trade
from main_app.tests.helpers import enable_strategy, seed_db

NOW = datetime(2026, 10, 14, 22, 0, tzinfo=UTC)      # a Wednesday evening


@override_settings(EMAIL_HOST='smtp.test', REPORT_EMAIL='owner@example.com')
class ReportCase(TestCase):
    """The evening report on a pinned clock, mailed to the test outbox."""

    def setUp(self):
        clock = mock.patch.object(timezone, 'now', return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.cfg, self.instruments = seed_db(('QQQ',), with_bars=False)
        self.sim = Account.for_mode('sim')

    def trades(self, row, account, pnls):
        """One closed trade a day, newest yesterday."""
        for i, pnl in enumerate(pnls):
            ts = NOW - timedelta(days=i + 1)
            Trade.objects.create(account=account, instrument=self.instruments['QQQ'], strategy_key=row.key,
                                 side='long', qty=1, entry_ts=ts, exit_ts=ts, entry_price=100, exit_price=101,
                                 pnl=Decimal(pnl), pnl_pct=Decimal('1'), exit_reason='target')

    def passes_every_check(self, row, account):
        """30 trades over 30 sessions at PF 4, measured slippage, and a qualified version."""
        self.trades(row, account, ['-1.00' if i % 3 == 0 else '2.00' for i in range(30)])
        order = Order.objects.create(account=account, instrument=self.instruments['QQQ'], strategy_key=row.key,
                                     side='buy', qty=1, client_order_id=f'mt-test-{row.market}-{row.key}')
        Fill.objects.create(order=order, ts=NOW - timedelta(days=1), qty=1, price=100, realized_slippage_bps=1.0)
        row.qualification = Qualification.QUALIFIED
        row.save(update_fields=['qualification'])

    def send(self):
        call_command('daily_report', '--no-journal', stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(len(mail.outbox), 1)
        return mail.outbox[0]

    def line(self, msg, starts):
        """The readiness line of the text body; it must be in the HTML too."""
        found = [ln for ln in msg.body.splitlines() if ln.startswith(starts)]
        self.assertEqual(len(found), 1, msg.body[:400])
        self.assertIn(found[0], msg.alternatives[0][0])
        return found[0]


class WithNothingReadyTheReportNamesTheClosestRow(ReportCase):
    ELEVEN = ['2.00'] * 5 + ['-2.00'] * 6            # PF 0.83

    def test_the_line_gives_its_trades_its_profit_factor_and_the_step_it_is_short_of(self):
        """The line opens "Real money"; a Sprout row is short of paper, a stage before that."""
        row = enable_strategy('ema_momentum', symbols=['QQQ'])
        self.trades(row, self.sim, self.ELEVEN)
        msg = self.send()
        self.assertEqual(self.line(msg, 'Real money:'),
                         'Real money: nothing ready. Closest: stocks ema_momentum v1 (sprout, unproven): '
                         '11 of 30 trades, PF 0.83 against the 1.3 it needs for Alpaca paper.')
        self.assertTrue(msg.subject.startswith('MoneyTree Oct 14:'), msg.subject)

    def test_a_forex_row_is_short_of_a_paper_stage_no_broker_can_host_yet(self):
        fx = enable_strategy('ema_momentum', symbols=['QQQ'], market='forex')
        self.trades(fx, Account.for_mode('sim', 'forex'), self.ELEVEN)
        self.assertTrue(self.line(self.send(), 'Real money:').endswith(
            'PF 0.83 against the 1.3 it needs for paper (no forex broker yet).'))

    def test_one_early_winner_does_not_outrank_a_row_with_a_real_sample(self):
        self.trades(enable_strategy('orb', symbols=['QQQ']), self.sim, ['54.00'])
        self.trades(enable_strategy('ema_momentum', symbols=['QQQ']), self.sim, self.ELEVEN)
        self.assertIn('Closest: stocks ema_momentum v1', self.line(self.send(), 'Real money:'))

    def test_a_thin_sample_is_closest_only_when_no_row_has_ten_trades(self):
        self.trades(enable_strategy('orb', symbols=['QQQ']), self.sim, ['54.00'])
        self.trades(enable_strategy('ema_momentum', symbols=['QQQ']), self.sim, self.ELEVEN[2:])   # 9 trades
        self.assertIn('Closest: stocks orb v1', self.line(self.send(), 'Real money:'))

    def test_a_disabled_row_is_labelled_not_hidden(self):
        row = enable_strategy('ema_momentum', symbols=['QQQ'])
        self.trades(row, self.sim, self.ELEVEN)
        row.enabled = False
        row.save(update_fields=['enabled'])
        self.assertIn('Closest: stocks ema_momentum v1 (sprout, unproven, parked):',
                      self.line(self.send(), 'Real money:'))

    def test_closest_is_the_best_record_not_the_most_boxes_ticked(self):
        """A quarantined row passes the trade-count check because it lost for long enough."""
        loser = enable_strategy('orb', symbols=['QQQ'])
        self.trades(loser, self.sim, ['1.00'] * 10 + ['-2.00'] * 26)
        loser.qualification = Qualification.QUARANTINED
        loser.save(update_fields=['qualification'])
        self.trades(enable_strategy('ema_momentum', symbols=['QQQ']), self.sim, self.ELEVEN)
        self.assertIn('Closest: stocks ema_momentum v1', self.line(self.send(), 'Real money:'))

    def test_a_row_that_has_lost_nothing_is_not_given_a_dollar_figure_as_its_profit_factor(self):
        """A scratch trade is not a loss: wins and one at zero is still nothing lost."""
        self.trades(enable_strategy('ema_momentum', symbols=['QQQ']), self.sim, ['54.00', '0.00'])
        self.assertIn('2 of 30 trades, no losing trade yet on the way to Alpaca paper.',
                      self.line(self.send(), 'Real money:'))

    def test_a_row_of_nothing_but_scratch_trades_has_not_won_either(self):
        """It prints its PF of 0.00, and a row with a real record still outranks it."""
        self.trades(enable_strategy('orb', symbols=['QQQ']), self.sim, ['0.00'] * 12)
        self.assertIn('Closest: stocks orb v1 (sprout, unproven): 12 of 30 trades, PF 0.00 against the 1.3',
                      self.line(self.send(), 'Real money:'))
        mail.outbox.clear()
        self.trades(enable_strategy('ema_momentum', symbols=['QQQ']), self.sim, self.ELEVEN)
        self.assertIn('Closest: stocks ema_momentum v1', self.line(self.send(), 'Real money:'))

    def test_before_any_row_has_traded_it_says_so(self):
        enable_strategy('ema_momentum', symbols=['QQQ'])
        self.assertEqual(self.line(self.send(), 'Real money:'),
                         'Real money: nothing ready. No Sprout or Sapling strategy has closed a trade yet.')


class ASproutRowThatPassesEveryCheckLeadsTheReportForPaper(ReportCase):
    def test_subject_and_body_say_alpaca_paper(self):
        self.passes_every_check(enable_strategy('ema_momentum', symbols=['QQQ']), self.sim)
        msg = self.send()
        self.assertTrue(msg.subject.startswith('READY for Alpaca paper: stocks ema_momentum · MoneyTree Oct 14:'),
                        msg.subject)
        self.assertEqual(self.line(msg, 'READY'),
                         'READY for Alpaca paper: stocks ema_momentum v1. Every graduation check passes; '
                         'promoting is still a manual step on the strategy page.')

    def test_a_disabled_row_that_passes_is_announced_and_labelled(self):
        row = enable_strategy('ema_momentum', symbols=['QQQ'])
        self.passes_every_check(row, self.sim)
        row.enabled = False
        row.save(update_fields=['enabled'])
        msg = self.send()
        self.assertTrue(msg.subject.startswith('READY for Alpaca paper: stocks ema_momentum (parked) · MoneyTree'),
                        msg.subject)
        self.assertTrue(self.line(msg, 'READY').startswith('READY for Alpaca paper: stocks ema_momentum v1 (parked).'))

    def test_it_says_so_again_the_next_evening_and_changes_nothing(self):
        row = enable_strategy('ema_momentum', symbols=['QQQ'])
        self.passes_every_check(row, self.sim)
        first = self.send().subject
        mail.outbox.clear()
        self.assertEqual(self.send().subject, first)
        row.refresh_from_db()
        self.assertEqual((row.stage, row.version), ('sprout', 1))

    def test_a_forex_row_is_not_promised_a_broker_that_does_not_exist(self):
        """And its wording does not leak onto the stocks row walked after it."""
        fx = enable_strategy('ema_momentum', symbols=['QQQ'], market='forex')
        self.passes_every_check(fx, Account.for_mode('sim', 'forex'))
        self.passes_every_check(enable_strategy('ema_momentum', symbols=['QQQ']), self.sim)
        msg = self.send()
        self.assertTrue(msg.subject.startswith(
            'READY for paper (no forex broker yet): forex ema_momentum · '
            'READY for Alpaca paper: stocks ema_momentum · MoneyTree'), msg.subject)


class ASaplingRowThatPassesEveryCheckLeadsTheReportForRealMoney(ReportCase):
    def setUp(self):
        super().setUp()
        self.row = enable_strategy('ema_momentum', symbols=['QQQ'], stage='sapling')

    def test_subject_and_body_say_real_money(self):
        self.passes_every_check(self.row, Account.for_mode('paper'))
        msg = self.send()
        self.assertTrue(msg.subject.startswith('READY for real money: stocks ema_momentum · MoneyTree Oct 14:'),
                        msg.subject)
        self.assertTrue(self.line(msg, 'READY').startswith('READY for real money: stocks ema_momentum v1.'))

    def test_it_is_judged_on_its_paper_record_and_the_walk_opens_no_account(self):
        """The same record on the simulator is the wrong stage's evidence."""
        self.passes_every_check(self.row, self.sim)
        msg = self.send()
        self.assertTrue(msg.subject.startswith('MoneyTree Oct 14:'), msg.subject)
        self.line(msg, 'Real money: nothing ready.')
        self.assertFalse(Account.objects.filter(mode='paper').exists())


class AReadinessCheckThatFailsDoesNotStopTheReport(ReportCase):
    def test_the_report_is_still_sent_and_says_the_status_is_unavailable(self):
        self.passes_every_check(enable_strategy('ema_momentum', symbols=['QQQ']), self.sim)
        with mock.patch('main_app.services.report.readiness', side_effect=RuntimeError('boom')), \
                self.assertLogs('moneytree.report', level='ERROR'):
            msg = self.send()
        self.assertTrue(self.line(msg, 'Real money:').startswith('Real money: status unavailable'))
        self.assertTrue(msg.subject.startswith('MoneyTree Oct 14:'), msg.subject)
