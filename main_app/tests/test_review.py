"""The two review cycles.

The operational cycle asks whether the machine did what it was told. The
improvement cycle asks whether what it was told is any good. They are separate
on purpose: one reviewer that did both would retune a strategy in response to a
plumbing fault.

What these tests are actually for: a reviewer is only worth having if it fires
on a real fault, stays quiet on a healthy desk, survives a restart without
either forgetting or repeating itself, notices when the fault goes away, and
cannot quietly change the thing it is reviewing.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from main_app.models import (Account, AgentConfig, Hypothesis, Instrument, Order, Position,
                             ReviewFinding, ReviewRun, RiskEvent, Signal, Strategy, Trade)
from main_app.services.review import improvement as imp
from main_app.services.review import operational as ops
from main_app.services.review.guard import ReviewerWroteToConfig, readonly_config
from main_app.services.review.runner import cycle_status, run_improvement, run_operational


class ReviewCase(TestCase):
    def setUp(self):
        AgentConfig.get()
        self.inst = Instrument.objects.create(symbol='EUR/USD', asset_class='forex', market='forex')
        self.account = Account.objects.create(mode='sim', market='forex', name='Forex (sim)',
                                              starting_cash=10000, cash=10000, equity=10000,
                                              day_start_equity=10000)
        self.now = timezone.now()
        # A lane with no price history is genuinely unhealthy and the stale-data
        # check says so, so the baseline has to carry a fresh bar or every test
        # here starts from a critical finding it did not mean to create.
        self.fresh_bar()

    def fresh_bar(self, inst=None, minutes_ago=1, timeframe='15Min'):
        from main_app.models import Bar
        return Bar.objects.create(instrument=inst or self.inst, timeframe=timeframe,
                                  ts=self.now - timedelta(minutes=minutes_ago),
                                  open=1, high=1, low=1, close=1, volume=1)

    def run_ops(self, **kw):
        return run_operational(accounts=[self.account], notify=False, now=self.now, **kw)

    def findings(self, check_key=None, status=ReviewFinding.OPEN):
        q = ReviewFinding.objects.filter(status=status)
        return q.filter(check_key=check_key) if check_key else q


class AHealthyDeskProducesNoFindings(ReviewCase):
    def test_a_clean_lane_is_silent(self):
        run = self.run_ops()
        self.assertEqual(run.status, 'ok')
        self.assertEqual(run.checks_failed, 0)
        self.assertEqual(run.findings_opened, 0)
        self.assertFalse(Account.objects.get(pk=self.account.pk).review_halt)


class ASimulatedBadTradeIsCaughtAndStopsTheDesk(ReviewCase):
    """The headline case: something goes wrong at the moment of a trade."""

    def _order(self, oid, leg='entry', bar_ts=None, status='filled'):
        return Order.objects.create(
            account=self.account, instrument=self.inst, strategy_key='ema_momentum', side='buy',
            qty=Decimal('1000'), client_order_id=oid, leg=leg, status=status,
            bar_ts=bar_ts or self.now, submitted_at=self.now)

    def test_two_orders_for_one_bar_are_critical_and_halt_new_entries(self):
        """The four-QQQ-shorts shape: one instruction becoming several orders."""
        self._order('mt-sim-ema-EURUSD-1-entry')
        self._order('mt-sim-ema-EURUSD-1-entry-dup')
        run = self.run_ops()
        f = self.findings('duplicate_order').get()
        self.assertEqual(f.severity, ReviewFinding.CRITICAL)
        self.assertIn('2 entry orders', f.title)
        self.account.refresh_from_db()
        self.assertTrue(self.account.review_halt)
        self.assertIn('halted new entries', ' '.join(run.actions))

    def test_an_open_position_with_no_stop_is_critical(self):
        Position.objects.create(account=self.account, instrument=self.inst, strategy_key='ema_momentum',
                                qty=Decimal('1000'), avg_price=Decimal('1.08'), opened_at=self.now)
        self.run_ops()
        f = self.findings('position_without_stop').get()
        self.assertEqual(f.severity, ReviewFinding.CRITICAL)
        self.assertTrue(Account.objects.get(pk=self.account.pk).review_halt)

    def test_more_positions_than_the_limit_allows_is_critical(self):
        cfg = AgentConfig.get()
        cfg.forex_max_open_positions = 1
        cfg.save()
        for sym in ('EUR/USD', 'GBP/USD'):
            inst = Instrument.objects.get_or_create(symbol=sym, defaults={'asset_class': 'forex',
                                                                          'market': 'forex'})[0]
            Position.objects.create(account=self.account, instrument=inst, strategy_key='ema_momentum',
                                    qty=Decimal('1000'), avg_price=Decimal('1.08'),
                                    stop_price=Decimal('1.07'), opened_at=self.now)
        self.run_ops()
        f = self.findings('limit_open_positions').get()
        self.assertIn('2 positions open, limit is 1', f.title)

    def test_a_breached_daily_loss_that_did_not_halt_is_a_control_that_failed(self):
        self.account.equity = Decimal('9500')      # -5% against a 2% limit
        self.account.day_halted = False
        self.account.save()
        self.run_ops()
        self.assertTrue(self.findings('limit_daily_loss_not_halted').exists())

    def test_an_acted_signal_with_no_order_at_all_is_critical_but_a_mere_missing_link_is_not(self):
        """Two faults wear one shape; grading both critical would halt the desk
        for a bookkeeping gap."""
        Signal.objects.create(account=self.account, instrument=self.inst, strategy_key='ema_momentum',
                              ts=self.now, action='buy', price=Decimal('1.08'), acted=True)
        self.run_ops()
        self.assertEqual(self.findings('intent_without_order').count(), 1)

        ReviewFinding.objects.all().delete()
        Account.objects.filter(pk=self.account.pk).update(review_halt=False)
        self._order('mt-sim-ema-EURUSD-2-entry', bar_ts=self.now)    # the order does exist
        self.run_ops()
        self.assertFalse(self.findings('intent_without_order').exists())
        link = self.findings('signal_order_unlinked').get()
        self.assertEqual(link.severity, ReviewFinding.WARN)
        self.assertFalse(Account.objects.get(pk=self.account.pk).review_halt)


class AFailedReviewJobIsLoudAndDoesNotCloseAnything(ReviewCase):
    """A review that dies must never be mistaken for a review that found nothing."""

    def test_a_crashing_check_marks_the_run_failed_and_files_a_finding(self):
        def boom(ctx, rec):
            raise RuntimeError('the venue returned something unparseable')
        with self.settings():
            original = ops.CHECKS['limits']
            ops.CHECKS['limits'] = boom
            try:
                run = self.run_ops()
            finally:
                ops.CHECKS['limits'] = original
        self.assertEqual(run.status, 'failed')
        self.assertEqual(run.checks_failed, 1)
        f = self.findings('check_crashed').get()
        self.assertEqual(f.severity, ReviewFinding.CRITICAL)
        self.assertIn('unparseable', f.detail)
        self.assertTrue(Account.objects.get(pk=self.account.pk).review_halt)

    def test_a_crashing_check_may_not_resolve_the_findings_it_failed_to_confirm(self):
        """The dangerous version of this bug: a check crashes, raises nothing,
        and its silence is read as 'the problem is fixed'."""
        Position.objects.create(account=self.account, instrument=self.inst, strategy_key='ema_momentum',
                                qty=Decimal('1000'), avg_price=Decimal('1.08'), opened_at=self.now)
        self.run_ops()
        self.assertTrue(self.findings('position_without_stop').exists())

        def boom(ctx, rec):
            raise RuntimeError('still broken')
        original = ops.CHECKS['protection']
        ops.CHECKS['protection'] = boom
        try:
            self.run_ops()
        finally:
            ops.CHECKS['protection'] = original
        self.assertTrue(self.findings('position_without_stop').exists())   # still open

    def test_the_command_exits_nonzero_when_the_runner_itself_fails(self):
        from django.core.management import call_command
        from main_app.services.review import runner as R
        original = R.run_operational

        def boom(*a, **k):
            raise RuntimeError('database gone')
        R.run_operational = boom
        try:
            with self.assertRaises(SystemExit):
                call_command('review', 'operational', '--no-email')
        finally:
            R.run_operational = original
        row = ReviewRun.objects.filter(status='failed').first()
        self.assertIsNotNone(row)
        self.assertIn('database gone', row.error)


class FindingsSurviveRestartsWithoutRepeatingOrForgetting(ReviewCase):
    def setUp(self):
        super().setUp()
        Position.objects.create(account=self.account, instrument=self.inst, strategy_key='ema_momentum',
                                qty=Decimal('1000'), avg_price=Decimal('1.08'), opened_at=self.now)

    def test_the_same_fault_seen_twice_is_one_row_with_a_counter(self):
        self.run_ops()
        self.run_ops()
        self.run_ops()
        f = self.findings('position_without_stop').get()      # .get() asserts exactly one
        self.assertEqual(f.seen_count, 3)

    def test_a_fault_that_goes_away_is_resolved_by_the_check_that_found_it(self):
        self.run_ops()
        self.assertTrue(self.findings('position_without_stop').exists())
        Position.objects.all().update(stop_price=Decimal('1.07'))
        self.run_ops()
        self.assertFalse(self.findings('position_without_stop').exists())
        closed = ReviewFinding.objects.get(check_key='position_without_stop')
        self.assertEqual(closed.status, ReviewFinding.RESOLVED)
        self.assertIn('ran clean', closed.resolution)

    def test_a_fault_that_comes_back_reopens_the_row_it_already_had(self):
        self.run_ops()
        Position.objects.all().update(stop_price=Decimal('1.07'))
        self.run_ops()
        Position.objects.all().update(stop_price=None)
        self.run_ops()
        f = ReviewFinding.objects.get(check_key='position_without_stop')
        self.assertEqual(f.status, ReviewFinding.OPEN)
        self.assertIn('reopened', f.resolution)
        self.assertEqual(ReviewFinding.objects.filter(check_key='position_without_stop').count(), 1)

    def test_the_halt_clears_itself_once_the_fault_is_gone(self):
        self.run_ops()
        self.assertTrue(Account.objects.get(pk=self.account.pk).review_halt)
        Position.objects.all().update(stop_price=Decimal('1.07'))
        self.run_ops()
        self.assertFalse(Account.objects.get(pk=self.account.pk).review_halt)


class ChronicProblemsAlertButDoNotFreezeTheLane(ReviewCase):
    def test_expensive_trading_is_critical_and_still_does_not_halt(self):
        """Costs eating the gross will be just as true tomorrow. Halting on it
        would freeze the lane while destroying the only thing that could resolve
        it, which is more evidence."""
        for i in range(12):
            Trade.objects.create(account=self.account, instrument=self.inst, strategy_key='ema_momentum',
                                 side='long', qty=1000, entry_ts=self.now, exit_ts=self.now,
                                 entry_price=Decimal('1.00'), exit_price=Decimal('1.001'),
                                 pnl=Decimal('1'), pnl_pct=Decimal('0.1'), fees=Decimal('20'),
                                 bars_held=2, exit_reason='target')
        self.run_ops()
        f = self.findings('abnormal_costs').get()
        self.assertEqual(f.severity, ReviewFinding.CRITICAL)
        self.assertNotIn('abnormal_costs', ops.HALTING_CHECKS)
        self.assertFalse(Account.objects.get(pk=self.account.pk).review_halt)


class NeitherCycleMayChangeWhatItReviews(ReviewCase):
    """The owner's rule, enforced at runtime rather than promised in a comment."""

    def test_the_guard_refuses_a_strategy_write(self):
        Strategy.objects.create(key='ema_momentum', market='forex', enabled=True, params={}, symbols=[])
        with self.assertRaises(ReviewerWroteToConfig):
            with readonly_config():
                Strategy.objects.filter(key='ema_momentum').update(enabled=False)
        self.assertTrue(Strategy.objects.get(key='ema_momentum').enabled)

    def test_the_guard_refuses_a_risk_limit_write(self):
        with self.assertRaises(ReviewerWroteToConfig):
            with readonly_config():
                c = AgentConfig.get()
                c.forex_max_daily_loss_pct = Decimal('99')
                c.save()
        self.assertNotEqual(AgentConfig.get().forex_max_daily_loss_pct, Decimal('99'))

    def test_a_check_that_tries_to_tune_a_parameter_crashes_its_own_run(self):
        def sneaky(ctx, rec):
            AgentConfig.objects.all().update(forex_risk_per_trade_pct=Decimal('5'))
        original = ops.CHECKS['costs']
        ops.CHECKS['costs'] = sneaky
        try:
            run = self.run_ops()
        finally:
            ops.CHECKS['costs'] = original
        self.assertEqual(run.status, 'failed')
        self.assertTrue(self.findings('check_crashed').exists())
        self.assertNotEqual(AgentConfig.get().forex_risk_per_trade_pct, Decimal('5'))

    def test_the_improvement_cycle_is_under_the_same_guard(self):
        def sneaky(account, rec, now=None):
            Strategy.objects.all().update(enabled=True)
            return {}
        original = imp.analyse
        imp.analyse = sneaky
        try:
            run = run_improvement(accounts=[self.account], notify=False)
        finally:
            imp.analyse = original
        self.assertEqual(run.status, 'failed')

    def test_the_guard_still_lets_the_reviewer_record_what_it_found(self):
        with readonly_config():
            ReviewFinding.objects.create(cycle='operational', check_key='x', fingerprint='y', title='z')
        self.assertTrue(ReviewFinding.objects.filter(check_key='x').exists())

    def test_the_guard_is_released_even_when_the_body_raises(self):
        with self.assertRaises(ValueError):
            with readonly_config():
                raise ValueError('boom')
        AgentConfig.get().save()          # would raise if the guard leaked


class TheImprovementCycleProposesAndNeverApplies(ReviewCase):
    def test_a_hypothesis_without_a_source_is_refused(self):
        with self.assertRaises(ValueError):
            imp.propose('tighter stops', 'claim', source='   ')

    def test_a_sourced_hypothesis_is_recorded(self):
        h = imp.propose('wider stops on high-ATR days', 'claim',
                        source='https://example.org/paper — Zarattini & Aziz 2024, table 3')
        self.assertEqual(h.status, Hypothesis.PROPOSED)
        self.assertIn('Zarattini', h.source)

    def _evaluated(self, train, test):
        h = imp.propose('t', 'c', source='in-app measurement 2026-09-22')
        return imp.evaluate(h, lambda w: {'train': train, 'test': test}[w], 'train', 'test')

    def test_the_challenger_rejects_a_thin_out_of_sample(self):
        h = self._evaluated({'trades': 400, 'net': 900, 'per_day': 4.0, 'ci_low': 2.0, 'ci_high': 6.0},
                            {'trades': 8, 'net': 40, 'per_day': 3.0, 'ci_low': 1.0, 'ci_high': 5.0})
        v = imp.challenge(h)
        self.assertTrue(v['rejected'])
        self.assertTrue(any('out-of-sample trades' in r for r in v['reasons']))
        self.assertEqual(Hypothesis.objects.get(pk=h.pk).status, Hypothesis.REJECTED)

    def test_the_challenger_rejects_an_edge_that_evaporated_out_of_sample(self):
        """ORB's exact failure: the search half looked wonderful."""
        h = self._evaluated({'trades': 1200, 'net': 900, 'per_day': 5.0, 'ci_low': 3.0, 'ci_high': 7.0},
                            {'trades': 400, 'net': 20, 'per_day': 0.2, 'ci_low': 0.05, 'ci_high': 0.4})
        v = imp.challenge(h)
        self.assertTrue(v['rejected'])
        self.assertTrue(any('disappeared out of sample' in r for r in v['reasons']))

    def test_the_challenger_rejects_a_lower_bound_that_straddles_zero(self):
        h = self._evaluated({'trades': 900, 'net': 500, 'per_day': 3.0, 'ci_low': 1.0, 'ci_high': 5.0},
                            {'trades': 300, 'net': 200, 'per_day': 2.6, 'ci_low': -0.9, 'ci_high': 6.0})
        v = imp.challenge(h)
        self.assertTrue(v['rejected'])
        self.assertTrue(any('lower bound' in r for r in v['reasons']))

    def test_a_survivor_reaches_paper_forward_testing_and_not_live(self):
        h = self._evaluated({'trades': 900, 'net': 500, 'per_day': 3.0, 'ci_low': 1.0, 'ci_high': 5.0},
                            {'trades': 300, 'net': 260, 'per_day': 2.4, 'ci_low': 0.9, 'ci_high': 4.1})
        v = imp.challenge(h)
        self.assertFalse(v['rejected'], v['reasons'])
        h.refresh_from_db()
        self.assertEqual(h.status, Hypothesis.FORWARD)
        self.assertIn('not for live', h.decision_note)
        self.assertIsNone(h.applied_at)

    def test_a_second_opinion_may_object_and_may_not_clear(self):
        h = self._evaluated({'trades': 900, 'net': 500, 'per_day': 3.0, 'ci_low': 1.0, 'ci_high': 5.0},
                            {'trades': 300, 'net': 260, 'per_day': 2.4, 'ci_low': 0.9, 'ci_high': 4.1})
        v = imp.challenge(h, llm_second_opinion=lambda _h: {'objections': ['the period is one regime']})
        self.assertTrue(v['rejected'])

        h2 = self._evaluated({'trades': 900, 'net': 500, 'per_day': 3.0, 'ci_low': 1.0, 'ci_high': 5.0},
                             {'trades': 5, 'net': 9, 'per_day': 1.0, 'ci_low': 0.5, 'ci_high': 2.0})
        v2 = imp.challenge(h2, llm_second_opinion=lambda _h: {'objections': [], 'verdict': 'looks great'})
        self.assertTrue(v2['rejected'])      # arithmetic wins over enthusiasm

    def test_a_broken_second_opinion_cannot_take_the_challenge_down_with_it(self):
        h = self._evaluated({'trades': 900, 'net': 500, 'per_day': 3.0, 'ci_low': 1.0, 'ci_high': 5.0},
                            {'trades': 300, 'net': 260, 'per_day': 2.4, 'ci_low': 0.9, 'ci_high': 4.1})
        def explode(_h):
            raise RuntimeError('no api key')
        v = imp.challenge(h, llm_second_opinion=explode)
        self.assertFalse(v['rejected'])
        self.assertIn('error', v['second_opinion'])


class TheCyclesAreVisibleInTheApp(ReviewCase):
    def test_status_reports_last_run_next_run_findings_and_blockers(self):
        Position.objects.create(account=self.account, instrument=self.inst, strategy_key='ema_momentum',
                                qty=Decimal('1000'), avg_price=Decimal('1.08'), opened_at=self.now)
        due = self.now + timedelta(minutes=15)
        self.run_ops(next_due_at=due)
        s = cycle_status(ReviewRun.OPERATIONAL)
        self.assertEqual(s['last_status'], 'ok')
        self.assertEqual(s['next_due_at'], due)
        self.assertGreaterEqual(s['open_critical'], 1)
        self.assertEqual(s['blockers'][0][0], 'forex')
        self.assertTrue(s['actions'])

    def test_a_cycle_that_never_ran_says_so_rather_than_looking_healthy(self):
        s = cycle_status(ReviewRun.IMPROVEMENT)
        self.assertEqual(s['last_status'], 'never run')
        self.assertIsNone(s['last_at'])


class StalenessIsJudgedInBarsNotMinutes(ReviewCase):
    def test_a_four_hour_lane_is_not_stale_after_thirty_minutes(self):
        """The first version of this check halted the crypto lane for a bar that
        was perfectly on time."""
        from main_app.models import Bar
        crypto_inst = Instrument.objects.create(symbol='BTC/USD', asset_class='crypto', market='crypto')
        crypto = Account.objects.create(mode='sim', market='crypto', name='Crypto (sim)',
                                        starting_cash=10000, cash=10000, equity=10000)
        Bar.objects.create(instrument=crypto_inst, timeframe='4Hour', ts=self.now - timedelta(minutes=45),
                           open=1, high=1, low=1, close=1, volume=1)
        run_operational(accounts=[crypto], notify=False, now=self.now)
        self.assertFalse(ReviewFinding.objects.filter(check_key='stale_data',
                                                      status=ReviewFinding.OPEN).exists())


class TheAuditTrailJoinsIntentToExecution(ReviewCase):
    """ledger.on_signal only linked a signal to its order if the order row
    already existed, and entries pass through a card, so the order is created
    afterwards and every entry signal was stored with a null link."""

    def _recorder(self):
        from main_app.services.ledger import DBRecorder
        return DBRecorder(self.account, {'EUR/USD': self.inst})

    def _order_req(self, leg='entry', oid='mt-sim-ema-EURUSD-1-entry'):
        from main_app.services.broker.base import OrderReq
        return OrderReq(id=oid, symbol='EUR/USD', side='buy', qty=1000, leg=leg,
                        strategy_key='ema_momentum', decision_price=1.08, bar_ts=self.now,
                        submitted_ts=self.now, status='filled')

    def test_an_entry_signal_written_before_its_order_is_joined_when_the_order_lands(self):
        rec = self._recorder()
        sig = type('S', (), {'symbol': 'EUR/USD', 'ts': self.now, 'price': 1.08, 'stop': 1.07,
                             'target': 1.10, 'reason': 'x', 'action': 'buy', 'strength': 1.0})()
        rec.on_signal(sig, 'ema_momentum', type('D', (), {'allowed': True, 'reason': ''})(), None)
        row = Signal.objects.get()
        self.assertIsNone(row.order_id)                       # the order does not exist yet
        rec.on_order(self._order_req())
        row.refresh_from_db()
        self.assertIsNotNone(row.order_id)
        self.assertEqual(row.order.client_order_id, 'mt-sim-ema-EURUSD-1-entry')

    def test_a_protective_stop_does_not_get_joined_to_an_entry_signal(self):
        rec = self._recorder()
        sig = type('S', (), {'symbol': 'EUR/USD', 'ts': self.now, 'price': 1.08, 'stop': 1.07,
                             'target': 1.10, 'reason': 'x', 'action': 'buy', 'strength': 1.0})()
        rec.on_signal(sig, 'ema_momentum', type('D', (), {'allowed': True, 'reason': ''})(), None)
        rec.on_order(self._order_req(leg='exit', oid='mt-sim-ema-EURUSD-1-entry-stop-2'))
        self.assertIsNone(Signal.objects.get().order_id)

    def test_a_stop_sharing_the_bar_cannot_vouch_for_an_entry_that_never_went_out(self):
        """The reviewer's own version of the same trap: matching any leg would
        downgrade a genuinely lost order to a bookkeeping note."""
        Signal.objects.create(account=self.account, instrument=self.inst, strategy_key='ema_momentum',
                              ts=self.now, action='buy', price=Decimal('1.08'), acted=True)
        Order.objects.create(account=self.account, instrument=self.inst, strategy_key='ema_momentum',
                             side='sell', qty=Decimal('1000'), client_order_id='stop-1', leg='exit',
                             bar_ts=self.now, status='filled', submitted_at=self.now)
        self.run_ops()
        self.assertTrue(self.findings('intent_without_order').exists())
        self.assertFalse(self.findings('signal_order_unlinked').exists())


class AStrategyTheDeskHasAlreadyFailedIsNotLeftTrading(ReviewCase):
    """qualification_assessment is consulted when a page is opened and when the
    nightly job runs, so a strategy can sit at "measured no edge" for days while
    still taking positions, because nothing compares the verdict with the stored
    state. Both forex strategies were in exactly that position."""

    def _strategy(self, enabled=True, qualification='unproven'):
        return Strategy.objects.create(key='ema_momentum', market='forex', enabled=enabled,
                                       qualification=qualification, params={}, symbols=['EUR/USD'])

    def _assess_as(self, state, reason='measured no edge after 39 trades: PF 0.65'):
        from main_app.services.review import operational as O
        import main_app.services.promotion as P
        self._orig = P.qualification_assessment
        P.qualification_assessment = lambda row, acct: {
            'state': state, 'reason': reason,
            'stats': {'trades': 39, 'profit_factor': 0.65, 'expectancy': -7.48, 'net_pnl': -292.0}}
        self.addCleanup(lambda: setattr(P, 'qualification_assessment', self._orig))

    def test_a_failed_strategy_still_enabled_is_critical(self):
        self._strategy()
        self._assess_as('quarantine')
        self.run_ops()
        f = self.findings('missed_quarantine').get()
        self.assertEqual(f.severity, ReviewFinding.CRITICAL)
        self.assertIn('ENABLED and trading', f.detail)
        self.assertEqual(f.evidence['stats']['profit_factor'], 0.65)

    def test_a_failed_strategy_already_disabled_is_only_a_warning(self):
        self._strategy(enabled=False)
        self._assess_as('quarantine')
        self.run_ops()
        self.assertEqual(self.findings('missed_quarantine').get().severity, ReviewFinding.WARN)

    def test_a_strategy_already_quarantined_raises_nothing(self):
        self._strategy(qualification='quarantine', enabled=False)
        self._assess_as('quarantine')
        self.run_ops()
        self.assertFalse(self.findings('missed_quarantine').exists())

    def test_the_reviewer_reports_it_and_does_not_quarantine_it_itself(self):
        """Changing qualification is a configuration change. The guard would
        crash the run if the check tried, and the strategy must come out
        untouched."""
        row = self._strategy()
        self._assess_as('quarantine')
        run = self.run_ops()
        self.assertEqual(run.status, 'ok')
        row.refresh_from_db()
        self.assertEqual(row.qualification, 'unproven')
        self.assertTrue(row.enabled)


class ACheckThatDidNotLookMayNotCloseAnything(ReviewCase):
    """The worst bug in the first version of this module.

    The scheduled job runs out of process with no broker handle, so
    position_vs_broker returns immediately. That silence was read as a clean
    pass: it closed the divergence finding the in-process reviewer had just
    raised — with a resolution line claiming the check "ran clean" — and then
    lifted the halt. The lane resumed entering trades against books that still
    disagreed with the venue.
    """
    class Broker:
        def __init__(self, qty):
            self.positions = {'EUR/USD': SimpleNamespaceQty(qty)}

    def setUp(self):
        super().setUp()
        Position.objects.create(account=self.account, instrument=self.inst, strategy_key='ema',
                                qty=Decimal('1000'), avg_price=Decimal('1.08'),
                                stop_price=Decimal('1.07'), opened_at=self.now)

    def test_a_divergence_found_with_a_broker_survives_a_review_run_without_one(self):
        self.run_ops(broker=self.Broker(900.0))
        self.assertTrue(self.findings('position_divergence').exists())
        self.assertTrue(Account.objects.get(pk=self.account.pk).review_halt)

        self.run_ops()                       # the scheduled job: broker=None
        self.assertTrue(self.findings('position_divergence').exists(),
                        'a check that never looked closed the finding it never confirmed')
        self.assertTrue(Account.objects.get(pk=self.account.pk).review_halt,
                        'and then lifted the halt, letting the lane trade against wrong books')

    def test_the_run_records_which_checks_declined_to_look(self):
        run = self.run_ops()
        self.assertIn('position_vs_broker', run.summary['skipped'])

    def test_a_real_agreement_still_closes_it(self):
        self.run_ops(broker=self.Broker(900.0))
        self.run_ops(broker=self.Broker(1000.0))          # now they agree
        self.assertFalse(self.findings('position_divergence').exists())
        self.assertFalse(Account.objects.get(pk=self.account.pk).review_halt)


class OneLanesReviewDoesNotEraseAnothersEvidence(ReviewCase):
    """The in-process reviewer passes exactly one account. Without account
    scoping, every lane's review closed the other three lanes' open findings,
    so four agents reviewing themselves every five minutes meant the desk
    continuously deleted its own evidence."""

    def setUp(self):
        super().setUp()
        self.other_inst = Instrument.objects.create(symbol='BTC/USD', asset_class='crypto',
                                                    market='crypto')
        self.other = Account.objects.create(mode='sim', market='crypto', name='Crypto (sim)',
                                            starting_cash=10000, cash=10000, equity=10000)
        self.fresh_bar(inst=self.other_inst, timeframe='4Hour')
        Position.objects.create(account=self.other, instrument=self.other_inst, strategy_key='ema',
                                qty=Decimal('1'), avg_price=Decimal('80000'), opened_at=self.now)

    def test_reviewing_forex_leaves_cryptos_finding_and_halt_alone(self):
        run_operational(accounts=[self.other], notify=False, now=self.now)
        self.assertTrue(self.findings('position_without_stop').exists())
        self.assertTrue(Account.objects.get(pk=self.other.pk).review_halt)

        self.run_ops()                       # forex only
        self.assertTrue(self.findings('position_without_stop').exists(),
                        "forex's review closed crypto's finding")
        self.assertTrue(Account.objects.get(pk=self.other.pk).review_halt)
        self.assertEqual(Position.objects.filter(account=self.other).exclude(qty=0).count(), 1)


class SimpleNamespaceQty:
    def __init__(self, qty):
        self.qty = qty


class TheGuardCoversEveryOrmPathAChecKCouldReachFor(ReviewCase):
    def test_bulk_create_is_blocked(self):
        with self.assertRaises(ReviewerWroteToConfig):
            with readonly_config():
                Strategy.objects.bulk_create([Strategy(key='x', market='forex', params={}, symbols=[])])
        self.assertFalse(Strategy.objects.filter(key='x').exists())

    def test_get_or_create_and_update_or_create_are_blocked(self):
        for fn in ('get_or_create', 'update_or_create'):
            with self.assertRaises(ReviewerWroteToConfig):
                with readonly_config():
                    getattr(Strategy.objects, fn)(key='y', market='forex',
                                                  defaults={'params': {}, 'symbols': []})
        self.assertFalse(Strategy.objects.filter(key='y').exists())

    def test_an_f_expression_update_is_blocked(self):
        from django.db.models import F
        AgentConfig.get()
        with self.assertRaises(ReviewerWroteToConfig):
            with readonly_config():
                AgentConfig.objects.update(forex_max_open_positions=F('forex_max_open_positions') + 10)

    def test_a_failure_while_patching_does_not_leave_models_blocked(self):
        """Patching used to run before the try, so a raise there left every
        model already patched blocked for the life of the process."""
        import main_app.services.review.guard as G
        orig = G.PROTECTED
        G.PROTECTED = ('Strategy', 'NoSuchModel')
        try:
            with self.assertRaises(AttributeError):
                with readonly_config():
                    pass
        finally:
            G.PROTECTED = orig
        Strategy.objects.create(key='z', market='forex', params={}, symbols=[])   # must not raise
        self.assertTrue(Strategy.objects.filter(key='z').exists())
