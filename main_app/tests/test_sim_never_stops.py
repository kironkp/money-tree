"""In sim, failing never means stopping (owner's policy, 2026-10-06).

A failing verdict in the simulator sends a strategy to the nightly fix loop and
keeps it trading. The verdict itself is kept, so it still keeps the strategy off
paper and live: real-money safety is exactly what it was.
"""
import importlib
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.apps import apps
from django.test import TestCase
from django.utils import timezone

from main_app.models import (Account, Instrument, JournalEntry, Mode, Qualification, RiskEvent, Strategy,
                             Trade)
from main_app.services import promotion
from main_app.services.agent import Agent, eligible_strategy_rows
from main_app.services.data import calendar as cal
from main_app.services.journal import write_eod_journal


class LosingInSimKeepsTradingAndWaitsForAFix(TestCase):
    def setUp(self):
        self.inst = Instrument.objects.create(symbol='EUR/USD', asset_class='forex', market='forex')
        self.row = Strategy.objects.create(key='vwap_reversion', name='VWAP', market='forex', params={},
                                           symbols=['EUR/USD'], enabled=True, stage='sprout',
                                           evidence_since=timezone.now() - timedelta(days=30))

    def _account(self, mode):
        return Account.objects.create(mode=mode, market='forex', starting_cash=10000, cash=10000, equity=10000)

    def _losses(self, account, n):
        start = timezone.now() - timedelta(days=20)
        for i in range(n):
            ts = start + timedelta(hours=i)
            Trade.objects.create(account=account, instrument=self.inst, strategy_key='vwap_reversion',
                                 side='long', qty=1000, entry_ts=ts - timedelta(minutes=30), exit_ts=ts,
                                 entry_price=Decimal('1.1'), exit_price=Decimal('1.09'), pnl=Decimal('-8.6'),
                                 pnl_pct=Decimal('-0.1'), fees=Decimal('1'), exit_reason='stop')

    def test_thirty_losing_trades_in_sim_stay_enabled_and_flag_a_retry(self):
        sim = self._account('sim')
        self._losses(sim, 35)
        assessment, changed = promotion.refresh_qualification(self.row, sim)
        self.row.refresh_from_db()
        self.assertTrue(changed)
        self.assertTrue(assessment['no_edge'])
        self.assertEqual(self.row.qualification, Qualification.QUARANTINED)   # the verdict is kept
        self.assertTrue(self.row.enabled)
        self.assertTrue(self.row.retry_pending)
        self.assertTrue(RiskEvent.objects.filter(account=sim, kind='retry', message__contains='retry pending').exists())
        self.assertFalse(promotion.graduation_checklist(self.row, sim)['ready'])

    def test_the_same_record_on_paper_stops_it_and_it_cannot_graduate(self):
        paper = self._account('paper')
        self.row.stage = 'sapling'
        self.row.save(update_fields=['stage'])
        self._losses(paper, 35)
        promotion.refresh_qualification(self.row, paper)
        self.row.refresh_from_db()
        self.assertFalse(self.row.enabled)
        self.assertFalse(self.row.retry_pending)
        self.assertFalse(promotion.graduation_checklist(self.row, paper)['ready'])
        self.assertFalse(RiskEvent.objects.filter(kind='retry').exists())

    def test_the_lifetime_verdict_does_not_disable_in_sim(self):
        sim = self._account('sim')
        self._losses(sim, 160)
        assessment, _ = promotion.refresh_qualification(self.row, sim)
        self.row.refresh_from_db()
        self.assertTrue(assessment['lifetime_halt'])
        self.assertIn('no edge across its whole life', self.row.qualification_reason)
        self.assertTrue(self.row.enabled)
        self.assertFalse(self.row.lifetime_halt)          # not persisted in sim: a fix can earn its way out
        self.assertEqual(self.row.qualification, Qualification.QUARANTINED)

    def test_drift_does_not_disable_in_sim(self):
        sim = self._account('sim')
        promotion.promote(self.row, self.row.params, 'backtest #1',
                          metrics={'expectancy': 5.0, 'trades': 40, 'profit_factor': 1.5})
        self.row.evidence_since = timezone.now() - timedelta(days=30)
        self.row.save(update_fields=['evidence_since'])
        self._losses(sim, 25)
        entry = write_eod_journal(sim, cal.session_date(timezone.now()))
        self.row.refresh_from_db()
        self.assertTrue(self.row.enabled)
        self.assertIn('DRIFT', entry.body)
        self.assertNotIn('auto-disabled', entry.body)

    def test_sim_loads_quarantined_rows_and_broker_modes_do_not(self):
        self.row.qualification = Qualification.QUARANTINED
        self.row.save(update_fields=['qualification'])
        self.assertIn(self.row, eligible_strategy_rows(Mode.SIM, 'forex'))
        self.row.stage = 'sapling'
        self.row.save(update_fields=['stage'])
        self.assertNotIn(self.row, eligible_strategy_rows(Mode.PAPER, 'forex'))

    def test_a_verdict_change_does_not_block_sim_entries(self):
        """Any change to the strategy snapshot blocks entries until a restart.
        In sim a verdict written at the end-of-day journal must not."""
        agent = Agent.__new__(Agent)
        agent.market = 'forex'
        agent.mode = Mode.SIM
        before = agent._strategy_snapshot()
        self.row.qualification = Qualification.QUARANTINED
        self.row.save(update_fields=['qualification'])
        self.assertEqual(agent._strategy_snapshot(), before)
        agent.mode = Mode.PAPER
        paper_before = agent._strategy_snapshot()
        self.row.qualification = Qualification.UNPROVEN
        self.row.save(update_fields=['qualification'])
        self.assertNotEqual(agent._strategy_snapshot(), paper_before)


class JournalsSurviveTheNightlyResearchRun(TestCase):
    """Auto-research wrote its rows as kind='auto_eod' at 02:10 and then restarted
    every lane. Each lane booted believing today's journal was done, so the 24/7
    lanes never journalled — and never refreshed qualification — for three weeks."""

    def setUp(self):
        self.account = Account.objects.create(mode='sim', market='forex', starting_cash=10000, cash=10000,
                                              equity=10000)

    def _agent(self):
        agent = Agent.__new__(Agent)
        agent.account = self.account
        agent.lane_asset_class = 'forex'
        agent.last_tick = timezone.now()
        agent.journal_done = None
        agent.eod_done = None
        return agent

    def test_a_lane_restarted_after_research_still_journals_at_its_next_midnight(self):
        today = cal.session_date(timezone.now())
        JournalEntry.objects.create(date=today, kind='research', account=self.account,
                                    title='Auto-research vwap_reversion (forex): kept current params')
        agent = self._agent()
        self.assertFalse(agent._eod_already_written(today))
        boot = datetime.combine(today, time(6, 30), tzinfo=cal.ET)
        with mock.patch.object(Agent, 'end_of_day') as eod:
            agent._daily_roll(boot, True)
            eod.assert_not_called()
            after_midnight = datetime.combine(today + timedelta(days=1), time(0, 1), tzinfo=cal.ET)
            agent._daily_roll(after_midnight, True)
            eod.assert_called_once()
            self.assertEqual(eod.call_args.args[1], today)
            self.assertEqual(eod.call_args.kwargs.get('flatten'), False)

    def test_the_end_of_day_journal_no_longer_meets_research_rows(self):
        d = cal.session_date(timezone.now())
        for verdict in ('kept current params', 'PROMOTED v2'):
            JournalEntry.objects.create(date=d, kind='auto_eod', account=self.account,
                                        title=f'Auto-research ema_momentum (forex): {verdict}')
        with self.assertRaises(JournalEntry.MultipleObjectsReturned):
            write_eod_journal(self.account, d)
        relabel = importlib.import_module('main_app.migrations.0030_research_journal_kind').relabel
        relabel(apps, None)
        entry = write_eod_journal(self.account, d)
        self.assertEqual(entry.kind, 'auto_eod')
        self.assertEqual(JournalEntry.objects.filter(account=self.account, kind='auto_eod', date=d).count(), 1)
        self.assertEqual(JournalEntry.objects.filter(account=self.account, kind='research', date=d).count(), 2)
