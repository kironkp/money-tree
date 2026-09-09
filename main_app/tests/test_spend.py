"""The spend ledger: pricing, attribution, and an ingest endpoint that servers
can use but strangers cannot."""
import json

from django.test import TestCase, override_settings
from django.urls import reverse

from main_app.models import ApiUsage
from main_app.services import spend
from main_app.tests.helpers import make_user


class PricingIsExplicitAndFailsExpensive(TestCase):
    def test_known_models_priced_per_million(self):
        # 1M input on Opus at $15/M, 1M output at $75/M.
        self.assertAlmostEqual(float(spend.cost_of('claude-opus-5', 1_000_000, 0)), 15.0, places=4)
        self.assertAlmostEqual(float(spend.cost_of('claude-opus-5', 0, 1_000_000)), 75.0, places=4)
        self.assertAlmostEqual(float(spend.cost_of('gpt-4o', 1_000_000, 1_000_000)), 12.50, places=4)

    def test_cached_input_is_a_tenth_of_input(self):
        full = float(spend.cost_of('claude-opus-5', 1_000_000, 0))
        cached = float(spend.cost_of('claude-opus-5', 0, 0, 1_000_000))
        self.assertAlmostEqual(cached, full * spend.CACHE_READ_SHARE, places=4)

    def test_dated_model_suffix_still_prices(self):
        self.assertEqual(spend.price_for('gpt-4o-2024-11-20'), spend.PRICES['gpt-4o'])

    def test_unknown_model_is_assumed_expensive_so_it_stands_out(self):
        self.assertEqual(spend.price_for('some-unreleased-model'), spend.DEFAULT_PRICE)

    def test_realtime_audio_is_priced_as_the_most_expensive_thing(self):
        realtime = float(spend.cost_of('gpt-4o-realtime-preview', 1_000_000, 0))
        self.assertGreater(realtime, float(spend.cost_of('gpt-4o', 1_000_000, 0)) * 10)


class LedgerAttributesSpend(TestCase):
    def test_record_never_raises_and_totals_by_project(self):
        spend.record('gpt-4o', provider='openai', project='findit', purpose='assistant',
                     input_tokens=1_000_000, output_tokens=0)
        spend.record('claude-opus-5', project='moneytree', purpose='coach', input_tokens=1_000_000)
        s = spend.range_spend(30)
        self.assertAlmostEqual(s['total'], 2.50 + 15.0, places=3)
        self.assertEqual(list(s['by_project'])[0], 'moneytree')      # ranked by cost
        self.assertAlmostEqual(s['by_provider']['openai']['cost'], 2.50, places=3)

    def test_a_broken_row_does_not_raise_into_the_caller(self):
        self.assertIsNone(spend.record('x' * 500, project='p' * 500, cost_usd='not-a-number'))


@override_settings(SPEND_INGEST_TOKEN='test-token')
class IngestAcceptsServersAndRefusesStrangers(TestCase):
    def post(self, body, token=None):
        headers = {'HTTP_X_SPEND_TOKEN': token} if token else {}
        return self.client.post(reverse('api-spend-ingest'), data=json.dumps(body),
                                content_type='application/json', **headers)

    def test_good_token_records(self):
        r = self.post({'project': 'findit', 'provider': 'openai', 'model': 'gpt-4o',
                       'input_tokens': 1_000_000, 'output_tokens': 0}, token='test-token')
        self.assertEqual(r.status_code, 200)
        self.assertAlmostEqual(r.json()['cost_usd'], 2.50, places=3)
        self.assertEqual(ApiUsage.objects.get().project, 'findit')

    def test_bad_and_missing_tokens_are_refused(self):
        self.assertEqual(self.post({'model': 'gpt-4o'}, token='wrong').status_code, 403)
        self.assertEqual(self.post({'model': 'gpt-4o'}).status_code, 403)
        self.assertFalse(ApiUsage.objects.exists())

    def test_model_is_required(self):
        self.assertEqual(self.post({'project': 'x'}, token='test-token').status_code, 400)

    @override_settings(SPEND_INGEST_TOKEN='')
    def test_ingest_is_off_without_a_token(self):
        self.assertEqual(self.post({'model': 'gpt-4o'}, token='anything').status_code, 503)

    def test_spend_page_requires_login(self):
        self.assertEqual(self.client.get(reverse('spend')).status_code, 302)
        self.client.force_login(make_user())
        self.assertEqual(self.client.get(reverse('spend')).status_code, 200)


class WindowsAreRealCalendarPeriods(TestCase):
    """Boundaries are local calendar boundaries, not N x 24h ago."""

    def test_day_week_and_month_boundaries(self):
        from datetime import datetime
        from main_app.services.data import calendar as cal
        from main_app.services.spend import spend_window
        # A Wednesday.
        now = datetime(2026, 9, 9, 15, 0, tzinfo=cal.ET)
        d = spend_window('day', 0, now)
        self.assertEqual((d['label'], d['days']), ('Today', 1))
        self.assertEqual(spend_window('day', -1, now)['label'], 'Yesterday')
        w = spend_window('week', 0, now)
        self.assertEqual(w['start'].strftime('%A'), 'Monday')   # weeks start Monday
        self.assertEqual((w['label'], w['days']), ('This week', 7))
        m = spend_window('month', 0, now)
        self.assertEqual((m['start'].day, m['days']), (1, 30))  # September is a real 30-day month
        self.assertEqual(spend_window('month', -1, now)['days'], 31)   # August has 31

    def test_month_arithmetic_crosses_the_year(self):
        from datetime import datetime
        from main_app.services.data import calendar as cal
        from main_app.services.spend import spend_window
        w = spend_window('month', -2, datetime(2026, 1, 15, 12, 0, tzinfo=cal.ET))
        self.assertEqual((w['start'].year, w['start'].month), (2025, 11))

    def test_you_cannot_step_into_the_future(self):
        from main_app.services.spend import spend_window
        self.assertEqual(spend_window('day', 3)['offset'], 0)
        self.assertFalse(spend_window('day', 0)['has_next'])
        self.assertTrue(spend_window('day', -1)['has_next'])

    def test_report_fills_every_day_in_the_window(self):
        from main_app.services.spend import spend_report, spend_window
        rep = spend_report(spend_window('week', 0))
        self.assertEqual(len(rep['daily']), 7)          # gaps are information
        self.assertEqual(spend_report(spend_window('day', 0))['daily'], [])

    def test_unknown_model_marks_the_bucket_estimated(self):
        from main_app.services.spend import record, spend_report, spend_window
        record('a-model-with-no-published-rate', purpose='chat', input_tokens=1000)
        rep = spend_report(spend_window('day', 0))
        self.assertTrue(rep['any_estimated'])
        self.assertTrue(rep['by_model'][0]['estimated'])

    def test_report_endpoint_requires_login_and_returns_a_window(self):
        from django.urls import reverse
        url = reverse('api-spend-report')
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(make_user())
        d = self.client.get(url + '?period=month&offset=-1').json()
        self.assertEqual(d['report']['window']['period'], 'month')
        self.assertEqual(d['report']['window']['offset'], -1)
        self.assertIn('coach', d['kind_labels'])
