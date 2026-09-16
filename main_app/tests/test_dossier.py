"""Phase 2: dossiers in shadow.

The theme is that structure is not truth. A strict schema will accept a fabricated
number in the right shape as happily as a real one, so the tests here are mostly
about what gets thrown away.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from main_app.models import Instrument, NewsItem, NewsVerdict, SymbolDossier
from main_app.services import dossier as dz


class OnlyCheckableNumbersSurvive(SimpleTestCase):
    def _one(self, **kw):
        base = {'label': 'duo_units', 'value': 6e6, 'unit': 'units', 'period': '2026',
                'quote': 'Counterpoint estimates Apple could sell about six million units.',
                'url': 'https://reuters.com/x'}
        base.update(kw)
        return base

    def test_a_sourced_quoted_number_is_kept(self):
        kept, rejected = dz.validate_evidence([self._one()], set())
        self.assertEqual(len(kept), 1)
        self.assertEqual(rejected, [])
        self.assertEqual(kept[0]['tier'], 'searched')

    def test_a_number_with_no_url_is_an_assertion(self):
        kept, rejected = dz.validate_evidence([self._one(url='')], set())
        self.assertEqual(kept, [])
        self.assertIn('no usable source URL', rejected[0])

    def test_a_url_with_no_quote_is_a_gesture_at_a_page(self):
        kept, rejected = dz.validate_evidence([self._one(quote='see link')], set())
        self.assertEqual(kept, [])
        self.assertIn('verbatim quote', rejected[0])

    def test_a_claim_with_no_number_is_not_evidence(self):
        kept, _ = dz.validate_evidence([self._one(value=None)], set())
        self.assertEqual(kept, [])

    def test_it_may_not_relaunder_a_supplied_fact_as_its_own_finding(self):
        kept, rejected = dz.validate_evidence([self._one(label='trailing_pe')], {'trailing_pe'})
        self.assertEqual(kept, [])
        self.assertIn('restates a supplied fact', rejected[0])


class TheForecastMustBeADistribution(SimpleTestCase):
    def test_three_outcomes_summing_to_one_are_accepted(self):
        out, problem = dz.validate_forecast({'p_target_first': 0.3, 'p_stop_first': 0.6,
                                             'p_timeout': 0.1, 'p_positive_net': 0.31,
                                             'p_low': 0.2, 'p_high': 0.4})
        self.assertEqual(problem, '')
        self.assertAlmostEqual(out['p_target_first'], 0.3)

    def test_a_small_residual_is_renormalised(self):
        out, problem = dz.validate_forecast({'p_target_first': 0.30, 'p_stop_first': 0.60,
                                             'p_timeout': 0.11, 'p_positive_net': 0.3,
                                             'p_low': 0.2, 'p_high': 0.4})
        self.assertEqual(problem, '')
        self.assertAlmostEqual(out['p_target_first'] + out['p_stop_first'] + out['p_timeout'], 1.0)

    def test_numbers_that_are_not_a_distribution_are_refused(self):
        _, problem = dz.validate_forecast({'p_target_first': 0.7, 'p_stop_first': 0.7,
                                           'p_timeout': 0.1, 'p_positive_net': 0.5,
                                           'p_low': 0.6, 'p_high': 0.8})
        self.assertIn('sums to', problem)

    def test_a_probability_outside_zero_to_one_is_refused(self):
        _, problem = dz.validate_forecast({'p_target_first': 1.4, 'p_stop_first': 0.1,
                                           'p_timeout': 0.1, 'p_positive_net': 0.5,
                                           'p_low': 0.1, 'p_high': 0.2})
        self.assertIn('missing or out of range', problem)


class ACatalystMustBeDated(TestCase):
    def setUp(self):
        Instrument.objects.create(symbol='AAPL', asset_class='stock', market='stocks')
        self.now = timezone.now()

    def _apply(self, catalyst, direction='buy'):
        d = SymbolDossier(symbol='AAPL', market='stocks', as_of=self.now)
        dz._apply(d, {
            'narrative': 'n', 'catalyst': catalyst, 'bull': [], 'bear': [], 'evidence': [],
            'scores': {'catalyst': 8, 'context': 5, 'thesis': 6}, 'direction': direction,
            'forecast': {'p_target_first': 0.4, 'p_stop_first': 0.5, 'p_timeout': 0.1,
                         'p_positive_net': 0.4, 'p_low': 0.3, 'p_high': 0.5},
            'size_multiplier': 1.0, 'veto_reason': '', 'triggers': [],
        }, supplied=[], supplied_labels=set(), now=self.now, started=0.0)
        return d

    def test_a_fresh_dated_event_is_a_catalyst(self):
        d = self._apply({'present': True, 'headline': 'Apple cuts production',
                         'url': 'https://reuters.com/a',
                         'happened_at': (self.now - timedelta(hours=2)).isoformat(), 'why': 'x'})
        self.assertTrue(d.has_catalyst)
        self.assertEqual(d.direction, 'buy')

    def test_an_undated_event_is_demoted_and_cannot_trade(self):
        d = self._apply({'present': True, 'headline': 'Apple is doing well',
                         'url': 'https://x.com/a', 'happened_at': '', 'why': 'x'})
        self.assertFalse(d.has_catalyst)
        self.assertEqual(d.direction, 'none')
        self.assertIn('undated or stale', d.refused_reason)

    def test_a_week_old_event_is_context_not_a_catalyst(self):
        d = self._apply({'present': True, 'headline': 'Apple reported last week',
                         'url': 'https://x.com/a',
                         'happened_at': (self.now - timedelta(days=7)).isoformat(), 'why': 'x'})
        self.assertFalse(d.has_catalyst)
        self.assertEqual(d.direction, 'none')

    def test_no_catalyst_means_no_direction_however_high_the_thesis(self):
        d = self._apply({'present': False, 'headline': '', 'url': '', 'happened_at': '', 'why': ''},
                        direction='buy')
        self.assertEqual(d.direction, 'none')
        self.assertEqual(d.score_thesis, 6, 'the thesis is still recorded, it just cannot trade')


class ConvictionOnlyShrinks(TestCase):
    def _size(self, value):
        d = SymbolDossier(symbol='AAPL', as_of=timezone.now())
        dz._apply(d, {
            'narrative': '', 'catalyst': {'present': False, 'headline': '', 'url': '',
                                          'happened_at': '', 'why': ''},
            'bull': [], 'bear': [], 'evidence': [],
            'scores': {'catalyst': 1, 'context': 1, 'thesis': 1}, 'direction': 'none',
            'forecast': {'p_target_first': 0.33, 'p_stop_first': 0.34, 'p_timeout': 0.33,
                         'p_positive_net': 0.3, 'p_low': 0.2, 'p_high': 0.4},
            'size_multiplier': value, 'veto_reason': '', 'triggers': [],
        }, supplied=[], supplied_labels=set(), now=timezone.now(), started=0.0)
        return d.size_multiplier

    def test_it_cannot_ask_for_a_bigger_position(self):
        self.assertEqual(self._size(3.0), 1.0)

    def test_it_cannot_shrink_to_nothing(self):
        self.assertEqual(self._size(0.0), 0.25)

    def test_nonsense_falls_back_to_full_size_not_to_leverage(self):
        self.assertEqual(self._size('lots'), 1.0)


class ItFailsClosed(TestCase):
    def setUp(self):
        Instrument.objects.create(symbol='AAPL', asset_class='stock', market='stocks')

    def test_an_exhausted_budget_spends_nothing(self):
        with patch.object(dz, 'budget_left', return_value=Decimal('0.001')):
            with patch('openai.OpenAI') as client:
                d = dz.build('AAPL')
        self.assertIn('budget used up', d.error)
        client.assert_not_called()

    def test_an_unreadable_ledger_is_treated_as_no_headroom(self):
        """A budget check that reads a dropped row as $0 spent is worse than none."""
        from main_app.services.spend import BudgetError
        with patch.object(dz, 'budget_left', side_effect=BudgetError('database is locked')):
            with patch('openai.OpenAI') as client:
                d = dz.build('AAPL')
        self.assertIn('refusing to spend', d.error)
        client.assert_not_called()

    def test_a_name_we_do_not_research_is_refused_before_any_work(self):
        d = dz.build('EUR/USD')
        self.assertIn('not a researched name', d.error)


class ShadowTouchesNothing(TestCase):
    def test_building_a_dossier_issues_no_instruction(self):
        Instrument.objects.create(symbol='AAPL', asset_class='stock', market='stocks')
        with patch.object(dz, 'budget_left', return_value=Decimal('0.0')):
            dz.build('AAPL')
        self.assertEqual(NewsVerdict.objects.count(), 0,
                         'a shadow dossier writes no instruction and touches no order')


class CandidatesAreRankedNotThresholded(TestCase):
    def setUp(self):
        for sym in ('AAPL', 'NVDA', 'TSLA'):
            Instrument.objects.create(symbol=sym, asset_class='stock', market='stocks')

    def _stories(self, symbol, n):
        for i in range(n):
            NewsItem.objects.create(
                external_id=f'{symbol}-{i}', published_at=timezone.now() - timedelta(hours=1),
                first_public_at=timezone.now() - timedelta(hours=1), headline=f'{symbol} news {i}',
                symbols=[symbol], market='stocks', novel=True, tradable=True)

    def test_the_busiest_names_win_the_budget(self):
        self._stories('NVDA', 6)
        self._stories('AAPL', 3)
        self._stories('TSLA', 1)
        self.assertEqual(dz.candidates(limit=2), ['NVDA', 'AAPL'])

    def test_a_quiet_day_costs_the_same_as_a_loud_one(self):
        self._stories('NVDA', 40)
        self.assertEqual(len(dz.candidates(limit=4)), 1, 'volume does not buy extra dossiers')

    def test_names_we_do_not_research_are_never_candidates(self):
        NewsItem.objects.create(external_id='spy-1', published_at=timezone.now(),
                                first_public_at=timezone.now(), headline='SPY moves',
                                symbols=['SPY'], market='stocks', novel=True, tradable=True)
        self.assertEqual(dz.candidates(), [])
