"""Staleness has to distinguish a dead feed from an unread one.

The 1Hour forex series H10 is measured on stopped for 17 days and nothing noticed,
because freshness was only ever checked for series something was actively trading.
The fix is to check every series — but 32 of them are 1Min and 5Min feeds abandoned
two versions ago, and a report that cries "32 stale" every night is a report that
gets ignored, which is the same failure wearing different clothes.
"""
from datetime import UTC, datetime, timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from main_app.management.commands import maintain_bars
from main_app.models import Bar, Instrument, Strategy

WEDNESDAY = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)       # 11:00 ET: stocks and forex both open


class ReportCase(TestCase):
    """The report on a pinned clock. On the real one every case that needs an
    open market failed whenever the suite ran at a weekend, which is how CI went
    red on two Sundays running."""
    NOW = WEDNESDAY

    def setUp(self):
        clock = mock.patch.object(timezone, 'now', return_value=self.NOW)
        clock.start()
        self.addCleanup(clock.stop)
        # The standing research series is present and current unless a case says
        # otherwise. One with no bars at all is rightly STALE, so without this
        # every case would start from four findings it did not mean to create.
        for pair in maintain_bars.H10_PAIRS:
            inst = Instrument.objects.create(symbol=pair, asset_class='forex', market='forex')
            self.bar(inst, maintain_bars.H10_TIMEFRAME, self.NOW - timedelta(hours=1))

    def bar(self, inst, timeframe, ts, source=''):
        Bar.objects.create(instrument=inst, timeframe=timeframe, ts=ts, source=source,
                           open=1.1, high=1.1, low=1.1, close=1.1, volume=0)

    def _report(self):
        out = StringIO()
        call_command('maintain_bars', '--no-sync', stdout=out, stderr=StringIO())
        return out.getvalue()

    def line(self, report, symbol, timeframe):
        return next(ln for ln in report.splitlines() if ln.split()[:2] == [symbol, timeframe])


class StalenessIsJudgedAgainstWhoReadsTheSeries(ReportCase):
    def setUp(self):
        super().setUp()
        self.inst = Instrument.objects.get(symbol='EUR/USD')
        self.old = self.NOW - timedelta(days=9)

    def _bar(self, timeframe):
        self.bar(self.inst, timeframe, self.old)

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
        Bar.objects.filter(instrument=self.inst, timeframe='1Hour').delete()
        self._bar('1Hour')
        report = self._report()
        self.assertIn('STALE', report)
        self.assertIn('1 series STALE that something actually trades', report)


class AnEnabledStrategyReadsItsLanesBaseBars(ReportCase):
    """Since v1.73 `Strategy.timeframe` is '' (follow the lane) or a coarse
    choice that is resampled from the lane's base bars. Read literally it named
    no lane base at all, so a dead live feed would have been "stale, unused"."""

    def setUp(self):
        super().setUp()
        self.old = self.NOW - timedelta(days=9)

    def test_a_strategy_that_follows_its_lane_reads_the_lanes_base_series(self):
        eur = Instrument.objects.get(symbol='EUR/USD')
        Strategy.objects.create(key='vwap_reversion', market='forex', timeframe='', enabled=True, params={})
        self.bar(eur, '15Min', self.old)
        report = self._report()
        self.assertIn('STALE', self.line(report, 'EUR/USD', '15Min'))
        self.assertIn('1 series STALE that something actually trades', report)
        self.assertNotIn('stale, unused', report)

    def test_a_coarse_strategy_reads_the_base_it_is_resampled_from_and_not_native_coarse_bars(self):
        sol = Instrument.objects.create(symbol='SOL/USD', asset_class='crypto', market='degen')
        Strategy.objects.create(key='ema_momentum', market='degen', timeframe='4Hour', enabled=True, params={})
        self.bar(sol, '15Min', self.old)
        self.bar(sol, '4Hour', self.old)
        report = self._report()
        self.assertIn('STALE', self.line(report, 'SOL/USD', '15Min'))
        self.assertIn('stale, unused', self.line(report, 'SOL/USD', '4Hour'))
        self.assertIn('1 series STALE that something actually trades', report)


class AResearchSeriesIsJudgedOnTheCopyResearchReads(ReportCase):
    """Stock research backtests on SIP; the live loop writes IEX. Summed across
    feeds the series looked current while its SIP copy had stopped on 2026-09-17,
    and the nightly run never sees the stock market open to judge it anyway."""
    NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)          # Tuesday 02:00 PT, the nightly run: stocks shut
    MONDAY_LAST_BAR = datetime(2026, 10, 12, 19, 55, tzinfo=UTC)

    def setUp(self):
        super().setUp()
        self.spy = Instrument.objects.create(symbol='SPY', asset_class='etf', market='stocks')
        Strategy.objects.create(key='orb', market='stocks', enabled=True, params={}, symbols=['SPY'])

    def test_a_best_source_five_sessions_behind_is_stale_behind_a_fresh_second_source(self):
        self.bar(self.spy, '5Min', datetime(2026, 10, 5, 19, 55, tzinfo=UTC), 'alpaca:sip:split')
        self.bar(self.spy, '5Min', self.MONDAY_LAST_BAR, 'alpaca:iex')
        report = self._report()
        row = self.line(report, 'SPY', '5Min')
        self.assertIn('STALE (stops 7.0 d before the last close)', row)
        self.assertIn('research reads alpaca:sip:split', row)
        self.assertIn('2026-10-05 19:55', row)               # the copy research loads, not the newest of any
        self.assertIn('1 series STALE that something actually trades', report)

    def test_a_best_source_that_reaches_the_last_close_is_current(self):
        self.bar(self.spy, '5Min', self.MONDAY_LAST_BAR, 'alpaca:sip:split')
        self.bar(self.spy, '5Min', datetime(2026, 10, 5, 19, 55, tzinfo=UTC), 'alpaca:iex')
        report = self._report()
        self.assertIn('ok', self.line(report, 'SPY', '5Min'))
        self.assertNotIn('STALE', report)
        self.assertIn('every series an enabled strategy or research job reads is current', report)

    def test_a_copy_refreshed_last_night_is_not_stale_at_midday(self):
        """Research never reads past the last completed session, so a nightly
        copy is judged against that close and not against the wall clock."""
        self.bar(self.spy, '5Min', self.MONDAY_LAST_BAR, 'alpaca:sip:split')
        with mock.patch.object(timezone, 'now', return_value=datetime(2026, 10, 13, 16, 0, tzinfo=UTC)):
            self.assertNotIn('STALE', self._report())


class TheStockSeriesResearchReadsIsRefreshedNightly(ReportCase):
    """Nothing wrote SIP: the live loop writes IEX and RESEARCH_SERIES held only
    the H10 forex frames."""

    def setUp(self):
        super().setUp()
        for sym in ('SPY', 'QQQ', 'AAPL', 'TSLA'):
            Instrument.objects.create(symbol=sym, asset_class='stock', market='stocks')
        Strategy.objects.create(key='orb', market='stocks', enabled=True, params={}, symbols=['SPY', 'QQQ'])
        Strategy.objects.create(key='vwap_reversion', market='stocks', enabled=True, params={},
                                symbols=['QQQ', 'AAPL'])
        Strategy.objects.create(key='ema_momentum', market='stocks', enabled=False, params={}, symbols=['TSLA'])
        Strategy.objects.create(key='ema_momentum', market='crypto', enabled=True, params={},
                                symbols=['BTC/USD'])

    def test_the_symbols_come_from_every_stock_strategy_research_walks_enabled_or_not(self):
        """auto_research walks every row unless told otherwise, and can un-park
        a strategy on the result, so a parked row's symbols are read too."""
        stocks = [s for s in maintain_bars.research_series() if s[2] == 'stocks']
        self.assertEqual([(s[0], s[1]) for s in stocks], [(['AAPL', 'QQQ', 'SPY', 'TSLA'], '5Min')])
        self.assertIn(maintain_bars.RESEARCH_SERIES[0], maintain_bars.research_series())

    def test_the_sync_asks_alpaca_for_them_so_it_is_sip_that_gets_refreshed(self):
        with mock.patch.object(maintain_bars, 'call_command') as sync:
            call_command('maintain_bars', stdout=StringIO(), stderr=StringIO())
        sync.assert_any_call('sync_bars', timeframe='5Min', days=14, symbols='AAPL,QQQ,SPY,TSLA',
                             provider='alpaca')
        sync.assert_any_call('sync_bars', timeframe=maintain_bars.H10_TIMEFRAME, days=14,
                             symbols=','.join(maintain_bars.H10_PAIRS), provider='yahoo')


class AResearchSeriesWithNoBarsAtAllIsNotSilence(ReportCase):
    """The report is built from the bars that exist, so a series with none had
    no row and the summary said everything was current: a symbol added to a
    stocks strategy whose first sync failed looked exactly like a healthy one."""
    NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)

    def test_it_gets_a_row_and_counts_as_stale(self):
        spy = Instrument.objects.create(symbol='SPY', asset_class='etf', market='stocks')
        Strategy.objects.create(key='orb', market='stocks', enabled=True, params={}, symbols=['SPY', 'QQQ'])
        self.bar(spy, '5Min', datetime(2026, 10, 12, 19, 55, tzinfo=UTC), 'alpaca:sip:split')
        report = self._report()
        self.assertIn('ok', self.line(report, 'SPY', '5Min'))
        self.assertIn('STALE (no bars at all)', self.line(report, 'QQQ', '5Min'))
        self.assertIn('1 series STALE that something actually trades', report)
