"""Phase 3: the preregistered gate.

The most important test here is the one that asserts a NEGATIVE — that a policy
with no edge at all does not get promoted, however often the job looks. Optional
stopping is the single most likely way this project produces a confident wrong
answer, and it is invisible in any individual run.
"""
import random
from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from main_app.models import Evaluation, SymbolDossier
from main_app.services import evaluation as ev
from main_app.services import preregistration as prereg


class TheArithmeticIsRight(SimpleTestCase):
    def test_inverse_normal_matches_the_table(self):
        self.assertAlmostEqual(ev._z(0.95), 1.6449, places=3)
        self.assertAlmostEqual(ev._z(0.975), 1.9600, places=3)
        self.assertAlmostEqual(ev._z(0.80), 0.8416, places=3)

    def test_alpha_is_spent_late_not_early(self):
        spends = [ev.obrien_fleming(k, 5, 0.05) for k in range(1, 6)]
        self.assertLess(spends[0], 0.001, 'an early look must be nearly free')
        self.assertAlmostEqual(spends[-1], 0.05, places=6)
        self.assertEqual(spends, sorted(spends))

    def test_power_is_in_the_sample_size(self):
        """Alpha alone understates what is needed; beta is not optional."""
        with_power = ev.required_days(0.9, 0.15, 0.05, 0.20)
        alpha_only = ev.required_days(0.9, 0.15, 0.05, 0.50)
        self.assertGreater(with_power, alpha_only)

    def test_dependence_makes_the_variance_larger_not_smaller(self):
        """A naive standard error on autocorrelated days is optimistic."""
        rng = random.Random(7)
        series, prev = [], 0.0
        for _ in range(200):                       # strongly autocorrelated
            prev = 0.8 * prev + rng.gauss(0, 1)
            series.append(prev)
        import statistics
        naive = statistics.pstdev(series)
        self.assertGreater(ev.batch_means_sigma(series), naive)

    def test_holm_is_stricter_than_taking_each_claim_alone(self):
        passed = ev.holm({'primary': 0.03, 'ablation': 0.04}, alpha=0.05)
        self.assertFalse(passed['primary'], '0.03 clears 0.05 alone but not 0.05/2')

    def test_holm_passes_a_claim_that_earns_it(self):
        passed = ev.holm({'primary': 0.001, 'ablation': 0.30}, alpha=0.05)
        self.assertTrue(passed['primary'])
        self.assertFalse(passed['ablation'])


class ForecastsAreScoredProbabilitiesAreNotRatings(SimpleTestCase):
    def test_a_confident_correct_forecaster_beats_the_base_rate(self):
        pairs = [(0.9, True)] * 20 + [(0.1, False)] * 20
        out = ev.brier(pairs)
        self.assertLess(out['brier'], 0.05)
        self.assertGreater(out['skill'], 0.8)

    def test_a_forecaster_who_always_says_the_base_rate_has_no_skill(self):
        pairs = [(0.5, True)] * 20 + [(0.5, False)] * 20
        self.assertAlmostEqual(ev.brier(pairs)['skill'], 0.0, places=6)

    def test_reliability_shows_where_a_forecast_drifts(self):
        pairs = [(0.9, False)] * 10 + [(0.1, False)] * 10
        rows = {round(r['from'], 1): r for r in ev.reliability(pairs)}
        self.assertAlmostEqual(rows[0.8]['forecast'], 0.9)
        self.assertAlmostEqual(rows[0.8]['realised'], 0.0, msg='confident and wrong')

    def test_too_few_observations_produce_no_score_rather_than_a_bad_one(self):
        self.assertIsNone(ev.brier([(0.5, True)] * 3))


class NoiseIsNotPromoted(TestCase):
    """The optional-stopping regression test.

    A policy with exactly zero edge is evaluated at every preregistered
    checkpoint, repeatedly, across many simulated histories. If the gate is
    sound it promotes at most about alpha of the time. If someone later replaces
    the checkpoint logic with "check the interval every night and promote when it
    passes", this test fails loudly — which is the entire reason it exists.
    """

    def _run(self, seed: int, mean: float = 0.0) -> str:
        rng = random.Random(seed)
        evaluation = Evaluation.objects.create(
            identifier=f'null-{seed}', fingerprint='x', delta_min=0.15, alpha=0.05, beta=0.20,
            checkpoints=[20, 40, 60, 90, 120])
        series = []
        for day in range(1, 121):
            series.append(rng.gauss(mean, 1.0))
            rows = [{'diff': v} for v in series]
            fake = {'days': rows, 'n_days': len(series)}

            def assess(_e, _n=None, _rows=rows, _ev=evaluation):
                diffs = [r['diff'] for r in _rows]
                sigma = ev.batch_means_sigma(diffs)
                done = len(_ev.checkpoints_done or [])
                spent = ev.obrien_fleming(done + 1, len(_ev.checkpoints), _ev.alpha)
                boot = (ev.circular_block_bootstrap(diffs, spent, draws=600, seed=seed)
                        if len(diffs) >= ev.MIN_DAYS_FOR_STATS else None)
                return {'evaluation': _ev, 'days': _rows, 'n_days': len(diffs), 'sigma_lr': sigma,
                        'required_days': None, 'alpha_spent_next': spent,
                        'next_checkpoint': _ev.next_checkpoint, 'primary': boot,
                        'ablation': None, 'delta_min': _ev.delta_min, 'forecasts': {},
                        'outcomes': {}}

            from unittest.mock import patch
            with patch.object(ev, 'assess', assess):
                out = ev.decide(evaluation, apply=True)
            if out['decision'] in ('promote', 'demote'):
                return out['decision']
        return 'collecting'

    def test_a_policy_with_no_edge_is_almost_never_promoted(self):
        runs = [self._run(seed) for seed in range(40)]
        promoted = runs.count('promote')
        self.assertLessEqual(promoted, 4,
                             f'{promoted}/40 null histories promoted — the gate is leaking')

    def test_a_real_edge_is_eventually_promoted(self):
        """The gate must not be so strict that nothing can ever pass it."""
        runs = [self._run(seed, mean=0.9) for seed in range(8)]
        self.assertGreater(runs.count('promote'), 0,
                           'a large true edge must be detectable, or the gate is theatre')


class ADecisionOnlyHappensAtACheckpoint(TestCase):
    def setUp(self):
        self.ev = Evaluation.objects.create(identifier='cp-1', fingerprint='x', delta_min=0.15,
                                            alpha=0.05, checkpoints=[20, 40])

    def test_being_near_a_checkpoint_is_not_a_look(self):
        from unittest.mock import patch
        with patch.object(ev, 'assess', return_value={'n_days': 19, 'primary': None,
                                                      'alpha_spent_next': 0.01,
                                                      'next_checkpoint': 20}):
            out = ev.decide(self.ev)
        self.assertEqual(out['decision'], 'collecting')
        self.assertIn('19 of 20', out['reason'])
        self.assertEqual(self.ev.checkpoints_done, [])

    def test_a_closed_evaluation_decides_nothing_further(self):
        self.ev.status = 'promoted'
        self.ev.save()
        self.assertEqual(ev.decide(self.ev)['decision'], 'promoted')


class TheExperimentIsFrozen(TestCase):
    def test_the_same_rules_keep_the_same_evaluation(self):
        first = prereg.current()
        self.assertEqual(prereg.current().pk, first.pk)

    def test_changing_a_rule_supersedes_it_rather_than_inheriting_its_data(self):
        from main_app.services import dossier as dz
        first = prereg.current()
        from unittest.mock import patch
        with patch.object(dz, 'MAX_SEARCHES', 9):
            second = prereg.current()
        self.assertNotEqual(second.pk, first.pk)
        first.refresh_from_db()
        self.assertEqual(first.status, 'superseded')
        self.assertEqual(second.predecessor_id, first.pk)

    def test_the_prompt_text_is_part_of_the_fingerprint(self):
        from main_app.services import dossier as dz
        before, _ = prereg.current_fingerprint()
        from unittest.mock import patch
        with patch.object(dz, 'SYSTEM', dz.SYSTEM + '\nOne more instruction.'):
            after, _ = prereg.current_fingerprint()
        self.assertNotEqual(before, after, 'a prompt edit is a new experiment')

    def test_the_research_arm_cannot_trade_while_it_is_in_shadow(self):
        from main_app.services.news_agent import ACT_SOURCES
        self.assertNotIn('catalyst', ACT_SOURCES)
        self.assertEqual(ACT_SOURCES, ('headline',))


class DaysWithNoTradeStillCount(TestCase):
    def test_a_quiet_day_is_an_observation_not_a_gap(self):
        evaluation = prereg.current()
        now = timezone.now()
        SymbolDossier.objects.create(symbol='AAPL', market='stocks', as_of=now,
                                     direction='none', error='')
        rows = ev.daily_series(evaluation, now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['research'], 0.0)
        self.assertEqual(rows[0]['diff'], 0.0)

    def test_a_veto_is_a_decision_with_a_result_of_zero(self):
        evaluation = prereg.current()
        now = timezone.now()
        SymbolDossier.objects.create(
            symbol='AAPL', market='stocks', as_of=now, direction='buy',
            veto_reason='estimates being cut', size_multiplier=0.5,
            net_atr_catalyst_only=2.0, net_atr_combined=0.0,
            outcome_kind='target', outcome_at=now, error='')
        rows = ev.daily_series(evaluation, now)
        self.assertEqual(rows[0]['research'], 0.0)
        self.assertEqual(rows[0]['catalyst_only'], 2.0,
                         'the ablation still sees what the catalyst alone would have earned')


class TheScoreboardIsReadableBeforeThereIsAnything(TestCase):
    """The page has to be honest when it has nothing, which is most of the time."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        self.user = get_user_model().objects.create_user('kiron-test', password='x', is_staff=True)
        self.client.force_login(self.user)

    def test_it_renders_with_no_data_and_says_so(self):
        prereg.current()
        r = self.client.get('/news-agent/scoreboard/')
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn('Not enough days yet', body)
        self.assertIn('circular-block-bootstrap', body,
                      'the page must name the method it used, not just its conclusion')

    def test_it_shows_the_hurdle_and_the_sample_size_next_to_the_numbers(self):
        prereg.current()
        body = self.client.get('/news-agent/scoreboard/').content.decode()
        self.assertIn('trading days collected', body)
        self.assertIn('days needed for 80% power', body)
        self.assertIn('0.150', body, 'the hurdle must be on the page, not in a docstring')

    def test_it_requires_a_login(self):
        self.client.logout()
        r = self.client.get('/news-agent/scoreboard/')
        self.assertIn(r.status_code, (302, 301))

    def test_rebuilt_history_is_shown_apart_from_the_experiment(self):
        prereg.current()
        body = self.client.get('/news-agent/scoreboard/').content.decode()
        self.assertIn('rebuilt afterwards is not a forecast recorded before the fact', body)
