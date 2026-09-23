"""Routing macro news to the currency lane.

A census of all 598 shadow verdicts found 2 that concerned any FX instrument,
because the wire tags stories by equity ticker and a Federal Reserve decision
therefore arrives labelled QQQ. These tests pin the routing, the direction
mapping, and — most importantly — that nothing it writes can ever trade.
"""
from django.test import TestCase
from django.utils import timezone

from main_app.models import Market, NewsItem, NewsSession, NewsVerdict
from main_app.services.macro_fx import PAIRS, is_macro, pair_direction, route, usd_direction


class ItRecognisesWhatIsActuallyAboutTheDollar(TestCase):
    def test_policy_and_rates_stories_are_macro(self):
        for h in ('Fed Raises Rates by 0.25%', '10-Year Yield Tops 5%',
                  'Hot Core CPI Data Sends Rate Hike Odds to 80%',
                  'ECB Holds, Sterling Slides'):
            self.assertTrue(is_macro(h), h)

    def test_ordinary_single_stock_news_is_not(self):
        for h in ('Gene Munster Says iPhone 18 Pre-Order Wait Times Are Climbing',
                  'Nvidia Beats on Earnings, Raises Guidance'):
            self.assertFalse(is_macro(h), h)


class TheDollarReadingAbstainsWhenItShould(TestCase):
    def test_tightening_reads_dollar_positive(self):
        for h in ('Fed Raises Rates, Yields Hit 2007 Highs', '10-Year Yield Tops 5%',
                  'Warsh Delivers His Hawkish Show', 'Stickier Inflation Is Here'):
            self.assertEqual(usd_direction(h)[0], 'up', h)

    def test_easing_reads_dollar_negative(self):
        for h in ('Stocks Rise On Dovish Fed Hopes', 'Inflation Cools in August',
                  'Treasury Yields Fall as Growth Slows'):
            self.assertEqual(usd_direction(h)[0], 'down', h)

    def test_a_headline_pulling_both_ways_abstains(self):
        d, why = usd_direction('Yields Surge Then Fall as Dovish Fed Meets Hawkish Data')
        self.assertEqual(d, 'unclear')
        self.assertIn('both readings', why)

    def test_abstaining_is_recorded_rather_than_guessed(self):
        """A rule that answers when it does not know produces a worse sample than
        one that abstains — the story set is the asset, the label can improve later."""
        self.assertEqual(usd_direction('Fed Meets This Week')[0], 'unclear')


class EveryPairIsQuotedAgainstTheDollar(TestCase):
    def test_a_stronger_dollar_sells_all_four(self):
        self.assertEqual(pair_direction('up'), 'sell')

    def test_a_weaker_dollar_buys_all_four(self):
        self.assertEqual(pair_direction('down'), 'buy')

    def test_an_unclear_reading_takes_no_side(self):
        self.assertEqual(pair_direction('unclear'), 'none')


class RoutingWritesShadowRowsAndNothingElse(TestCase):
    def setUp(self):
        self.session = NewsSession.objects.create(started_at=timezone.now())

    def _story(self, headline, n=0):
        return NewsItem.objects.create(headline=headline, url=f'http://x/{n}',
                                       external_id=f'e{n}', published_at=timezone.now())

    def test_one_macro_story_becomes_one_verdict_per_pair(self):
        rows = route(self.session, [self._story('Fed Raises Rates by 0.25%')])
        self.assertEqual(len(rows), len(PAIRS))
        self.assertEqual({r.symbol for r in rows}, set(PAIRS))
        self.assertEqual({r.market for r in rows}, {Market.FOREX})

    def test_the_direction_is_consistent_across_the_four(self):
        route(self.session, [self._story('Fed Raises Rates, Yields Hit New Highs')])
        self.assertEqual(set(NewsVerdict.objects.values_list('direction', flat=True)), {'sell'})

    def test_a_single_stock_story_is_not_routed(self):
        self.assertEqual(route(self.session, [self._story('Nvidia Beats on Earnings')]), [])
        self.assertFalse(NewsVerdict.objects.exists())

    def test_nothing_it_writes_can_trade(self):
        """The whole point is to collect a sample, not to act on one."""
        route(self.session, [self._story('Fed Raises Rates by 0.25%')])
        rows = NewsVerdict.objects.all()
        self.assertTrue(rows.exists())
        self.assertFalse(any(r.tradable for r in rows))
        self.assertTrue(all('shadow' in r.blocked_reason for r in rows))
        self.assertTrue(all(r.score == 0 for r in rows))   # below any action threshold

    def test_it_is_labelled_as_its_own_arm_so_it_never_pools_with_the_headline_arm(self):
        route(self.session, [self._story('Fed Raises Rates by 0.25%')])
        self.assertEqual(set(NewsVerdict.objects.values_list('arm', flat=True)), {'macro_fx'})
