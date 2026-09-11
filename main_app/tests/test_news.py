"""Reading the news: symbol matching, story dedupe, the stand-aside, and the
rule that a backtest must never see a headline the past did not have."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from main_app.models import AgentConfig, Instrument, NewsItem
from main_app.services import news
from main_app.services.risk import RiskConfig, RiskManager
from main_app.services.strategies.base import Context, Signal


class SymbolsAreMatchedToWhatWeCanTrade(TestCase):
    def setUp(self):
        Instrument.objects.create(symbol='AAPL', asset_class='stock', market='stocks')
        Instrument.objects.create(symbol='BTC/USD', asset_class='crypto', market='crypto')
        self.known = news.tradable_map()

    def test_crypto_tags_are_translated(self):
        # Alpaca tags crypto news BTCUSD; the ledger calls it BTC/USD.
        self.assertEqual(news._crypto_symbol('BTCUSD'), 'BTC/USD')
        self.assertEqual(news._crypto_symbol('ETHUSDT'), 'ETH/USD')
        self.assertIsNone(news._crypto_symbol('AAPL'))

    def test_only_tradable_symbols_survive(self):
        hits, market = news.match_symbols(['AAPL', 'TSLA', 'BTCUSD'], self.known)
        self.assertEqual(hits, ['AAPL', 'BTC/USD'])
        self.assertEqual(market, 'stocks')          # mixed story files under stocks

    def test_a_story_about_nothing_we_trade_matches_nothing(self):
        self.assertEqual(news.match_symbols(['NFLX', 'BABA'], self.known)[0], [])


class TheSameStoryIsPaidForOnce(TestCase):
    def test_reworded_headlines_share_a_fingerprint(self):
        a = news.story_key('Apple Beats Earnings Expectations in Q3')
        b = news.story_key('In Q3, Apple beats the earnings expectations')
        self.assertEqual(a, b)

    def test_different_stories_do_not(self):
        self.assertNotEqual(news.story_key('Apple beats earnings'),
                            news.story_key('Tesla recalls 40,000 vehicles'))


class BigConfirmedNewsStandsTheAgentAside(TestCase):
    def setUp(self):
        Instrument.objects.create(symbol='AAPL', asset_class='stock', market='stocks')
        self.cfg = AgentConfig.get()

    def _story(self, **kw):
        base = dict(external_id=f'x{timezone.now().timestamp()}{kw.get("magnitude", 5)}',
                    published_at=timezone.now() - timedelta(minutes=5), headline='Apple halts iPhone sales',
                    symbols=['AAPL'], market='stocks', novel=True, classified_at=timezone.now(),
                    kind='regulatory', direction='bearish', magnitude=5, confidence=5)
        base.update(kw)
        return NewsItem.objects.create(**base)

    def test_a_confirmed_market_moving_story_blocks_an_entry(self):
        self._story()
        self.assertIn('standing aside', news.entry_block('AAPL'))

    def test_rumour_and_routine_coverage_do_not(self):
        self._story(magnitude=5, confidence=2)      # unconfirmed
        self._story(magnitude=2, confidence=5)      # confirmed but routine
        self._story(magnitude=5, confidence=5, direction='neutral')
        self.assertEqual(news.entry_block('AAPL'), '')

    def test_the_block_expires(self):
        self._story(published_at=timezone.now() - timedelta(minutes=news.NEWS_HALT_MINUTES + 10))
        self.assertEqual(news.entry_block('AAPL'), '')

    def test_other_symbols_are_unaffected(self):
        self._story()
        self.assertEqual(news.entry_block('MSFT'), '')

    def test_the_risk_manager_reports_it_as_the_reason(self):
        self._story()
        rm = RiskManager(RiskConfig(), news_aware=True)
        ctx = Context(symbol='AAPL', asset_class='stock', timeframe='5Min', ts=timezone.now())
        sig = Signal('buy', 'AAPL', timezone.now(), 100.0, stop=99.0, target=103.0)
        acct = type('A', (), {'equity': 10000.0, 'buying_power': 10000.0})()
        decision = rm.evaluate(sig, ctx, acct, {}, 'stock')
        self.assertFalse(decision.allowed)
        self.assertIn('standing aside', decision.reason)

    def test_a_backtest_never_sees_the_news(self):
        """The past must not be judged with information the past did not have."""
        self._story()
        rm = RiskManager(RiskConfig())              # news_aware defaults False
        ctx = Context(symbol='AAPL', asset_class='stock', timeframe='5Min', ts=timezone.now())
        sig = Signal('buy', 'AAPL', timezone.now(), 100.0, stop=99.0, target=103.0)
        acct = type('A', (), {'equity': 10000.0, 'buying_power': 10000.0})()
        self.assertTrue(rm.evaluate(sig, ctx, acct, {}, 'stock').allowed)


class EventsAreScoredBeforeTheyAreBelieved(TestCase):
    def test_scoreboard_signs_the_move_by_direction(self):
        Instrument.objects.create(symbol='AAPL', asset_class='stock', market='stocks')
        NewsItem.objects.create(external_id='a', published_at=timezone.now() - timedelta(days=2),
                                headline='up', symbols=['AAPL'], market='stocks',
                                classified_at=timezone.now(), outcome_at=timezone.now(),
                                kind='earnings', direction='bullish', magnitude=4, confidence=4,
                                outcome={'AAPL': {'1d': 2.0}})
        NewsItem.objects.create(external_id='b', published_at=timezone.now() - timedelta(days=2),
                                headline='down', symbols=['AAPL'], market='stocks',
                                classified_at=timezone.now(), outcome_at=timezone.now(),
                                kind='earnings', direction='bearish', magnitude=4, confidence=4,
                                outcome={'AAPL': {'1d': -3.0}})
        board = news.event_scoreboard()
        # A bearish call that was followed by a fall counts as a hit, like a bullish one that rose.
        self.assertEqual({r['direction'] for r in board}, {'bullish', 'bearish'})
        self.assertTrue(all(r['avg_move_pct'] > 0 for r in board))


class RelativeVolumeMeansWhatItSays(TestCase):
    """A threshold of 1.5 must mean 1.5x a typical bar, not 5x."""

    def _series(self, volumes):
        import pandas as pd
        from main_app.services import indicators as ind
        idx = pd.date_range('2026-08-01', periods=len(volumes), freq='15min', tz='UTC')
        df = pd.DataFrame({'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0,
                           'volume': volumes}, index=idx)
        return ind.relative_volume(df, ind.session_key(idx, 'crypto'), 10), df

    def test_a_spiky_history_does_not_depress_the_typical_reading(self):
        # Ninety-six quiet bars of 100, with one 10,000 spike every twenty-fourth.
        vols = [10000 if i % 24 == 0 else 100 for i in range(480)]
        rv, _ = self._series(vols)
        typical = rv.dropna()
        typical = typical[[v == 100 for v in [10000 if i % 24 == 0 else 100
                                              for i in range(480)][-len(typical):]]]
        # A quiet bar against a history of quiet bars is ~1.0, not a fraction of it.
        self.assertGreater(float(typical.median()), 0.8)

    def test_a_genuine_doubling_still_reads_as_two(self):
        vols = [100] * 200 + [200]
        rv, _ = self._series(vols)
        self.assertAlmostEqual(float(rv.iloc[-1]), 2.0, places=1)

    def test_no_volume_is_not_fabricated_as_average(self):
        import math
        rv, _ = self._series([0] * 50)
        self.assertTrue(all(math.isnan(v) for v in rv))
