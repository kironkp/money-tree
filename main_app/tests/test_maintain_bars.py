"""Staleness has to distinguish a dead feed from an unread one.

The 1Hour forex series H10 is measured on stopped for 17 days and nothing noticed,
because freshness was only ever checked for series something was actively trading.
The fix is to check every series — but 32 of them are 1Min and 5Min feeds abandoned
two versions ago, and a report that cries "32 stale" every night is a report that
gets ignored, which is the same failure wearing different clothes.
"""
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from main_app.models import Bar, Instrument, Strategy


class StalenessIsJudgedAgainstWhoReadsTheSeries(TestCase):
    def setUp(self):
        self.inst = Instrument.objects.create(symbol='EUR/USD', asset_class='forex',
                                              market='forex')
        self.old = timezone.now() - timedelta(days=9)

    def _bar(self, timeframe):
        Bar.objects.create(instrument=self.inst, timeframe=timeframe, ts=self.old,
                           open=1.1, high=1.1, low=1.1, close=1.1, volume=0)

    def _report(self):
        out = StringIO()
        call_command('maintain_bars', '--no-sync', stdout=out, stderr=StringIO())
        return out.getvalue()

    def test_a_stale_series_a_live_strategy_trades_is_counted_stale(self):
        Strategy.objects.create(key='ema_momentum', market='forex', timeframe='15Min',
                                enabled=True, params={})
        self._bar('15Min')
        report = self._report()
        self.assertIn('STALE', report)
        self.assertIn('1 series STALE that something actually trades', report)

    def test_a_stale_series_nothing_trades_is_shown_but_not_counted(self):
        """Still printed — a feed that quietly stopped is worth seeing — but it
        must not inflate the number that is supposed to mean 'act on this'."""
        Strategy.objects.filter(market='forex').update(enabled=False)
        self._bar('1Min')          # no enabled strategy runs 1Min
        report = self._report()
        self.assertIn('stale, unused', report)
        self.assertIn('stale but unread', report)
        self.assertIn('every series an enabled strategy or research job reads is current',
                      report)

    def test_disabling_the_strategy_downgrades_its_series_to_unread(self):
        """The two cases differ only by who is reading, so prove the switch moves
        one series between them rather than trusting two separate fixtures."""
        s = Strategy.objects.create(key='ema_momentum', market='forex', timeframe='15Min',
                                    enabled=True, params={})
        self._bar('15Min')
        self.assertIn('1 series STALE that something actually trades', self._report())
        s.enabled = False
        s.save()
        self.assertIn('stale but unread', self._report())

    def test_the_research_series_counts_as_read_even_with_no_strategy(self):
        """H10's 1Hour forex frames are traded by nothing and are exactly the feed
        whose death went unnoticed. RESEARCH_SERIES is what keeps them watched."""
        Strategy.objects.filter(market='forex').update(enabled=False)
        self._bar('1Hour')
        report = self._report()
        self.assertIn('STALE', report)
        self.assertIn('1 series STALE that something actually trades', report)
