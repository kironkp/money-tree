"""Two brakes that were missing, both found by asking why UNI was missed.

Neither has anything to do with UNI. That is the point: the question "why didn't
we catch that mover" turned out to be about a quarantine that got erased and a
blindness nobody could measure.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from main_app.models import Account, Instrument, Market, NewsItem, Strategy, Trade, UnmatchedSymbol
from main_app.services import promotion


class AVersionBumpCannotEraseALosingRecord(TestCase):
    """`evidence_since` resets on promotion, which is right for judging
    parameters and wrong as the only brake: it let a losing idea run forever,
    thirty trades at a time. burst earned a quarantine at 165 trades and
    -$2,210, an evidence reset erased it, and the lane lost another $1,077."""

    def setUp(self):
        self.inst = Instrument.objects.create(symbol='SOL/USD', asset_class='crypto', market='degen')
        self.account = Account.objects.create(mode='sim', market='degen',
                                              starting_cash=10000, cash=6700)
        self.row = Strategy.objects.create(key='burst', name='Burst', market=Market.DEGEN,
                                           params={}, enabled=True)

    def _trades(self, n, pnl):
        now = timezone.now()
        for i in range(n):
            Trade.objects.create(
                account=self.account, instrument=self.inst, strategy_key='burst', side='long',
                qty=1, entry_ts=now - timedelta(minutes=30), exit_ts=now - timedelta(minutes=i),
                entry_price=Decimal('100'), exit_price=Decimal('99'), pnl=Decimal(str(pnl)),
                pnl_pct=Decimal('-1'), fees=Decimal('0.5'), bars_held=2, exit_reason='stop')

    def test_a_long_losing_record_halts_the_strategy(self):
        self._trades(160, -13)
        verdict = promotion.lifetime_verdict(self.row, self.account)
        self.assertIn('no edge across its whole life', verdict)
        self.assertIn('160 trades', verdict)

    def test_a_short_losing_record_does_not(self):
        """It must be impossible to trip on a bad fortnight; only an operator clears it."""
        self._trades(40, -13)
        self.assertEqual(promotion.lifetime_verdict(self.row, self.account), '')

    def test_resetting_the_evidence_clock_does_not_release_it(self):
        self._trades(160, -13)
        # A promotion moves evidence_since forward, hiding every trade so far.
        self.row.evidence_since = timezone.now()
        self.row.save(update_fields=['evidence_since'])
        self.assertEqual(promotion.live_stats(self.row, self.account)['trades'], 0)
        assessment = promotion.qualification_assessment(self.row, self.account)
        self.assertEqual(assessment['state'], 'quarantine')
        self.assertTrue(assessment['lifetime_halt'])

    def test_the_halt_is_persisted_and_disables_the_strategy(self):
        self._trades(160, -13)
        promotion.refresh_qualification(self.row, self.account)
        self.row.refresh_from_db()
        self.assertTrue(self.row.lifetime_halt)
        self.assertFalse(self.row.enabled)
        self.assertIn('no edge', self.row.lifetime_halt_reason)

    def test_a_profitable_strategy_is_left_alone(self):
        self._trades(200, +5)
        self.assertEqual(promotion.lifetime_verdict(self.row, self.account), '')


class TheDeskRecordsWhatItCannotActon(TestCase):
    """The discard used to be an aggregate in a log line — 'fetched 50, stored 8,
    34 about things we do not trade'. A 70% discard rate with no record of WHAT.
    That is how a mover goes unnoticed: not by decision, but because the question
    'what are we blind to' had nowhere to look."""

    def setUp(self):
        Instrument.objects.create(symbol='AAPL', asset_class='stock', market='stocks')

    def _raw(self, tags, headline='Something happened'):
        return [{'external_id': f'x-{headline}', 'article_id': 'x',
                 'published_at': timezone.now(), 'updated_at': timezone.now(),
                 'headline': headline, 'summary': '', 'content': '',
                 'url': 'https://example.com/a', 'symbols': tags, 'source': 'benzinga'}]

    def _ingest(self, raw):
        from unittest.mock import patch

        from main_app.services import news
        with patch.object(news, 'fetch_alpaca_news', return_value=raw):
            return news.ingest()

    def test_an_untradable_ticker_is_counted_not_forgotten(self):
        self._ingest(self._raw(['UNI', 'AAVE'], 'Uniswap surges on SEC exemption'))
        self.assertEqual(UnmatchedSymbol.objects.count(), 2)
        row = UnmatchedSymbol.objects.get(symbol='UNI')
        self.assertEqual(row.mentions, 1)
        self.assertIn('Uniswap', row.headline)

    def test_repeated_mentions_accumulate(self):
        for i in range(3):
            self._ingest(self._raw(['UNI'], f'Uniswap story {i}'))
        self.assertEqual(UnmatchedSymbol.objects.get(symbol='UNI').mentions, 3)

    def test_the_story_itself_is_still_discarded(self):
        """This is a ledger of blindness, not a back door into the news table."""
        self._ingest(self._raw(['UNI'], 'Uniswap surges'))
        self.assertEqual(NewsItem.objects.count(), 0)

    def test_a_tradable_ticker_is_not_recorded_as_missing(self):
        r = self._ingest(self._raw(['AAPL'], 'Apple ships something'))
        self.assertEqual(r['stored'], 1)
        self.assertEqual(UnmatchedSymbol.objects.count(), 0)
