"""The map. Mostly tests that it cannot lie about the machine it draws."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from main_app.services.agent import Agent
from main_app.services.graph_model import LANES, PAYLOADS, graph


class TheGraphDescribesSomethingReal(TestCase):
    def setUp(self):
        self.g = graph()
        self.ids = {n['id'] for n in self.g['nodes']}

    def test_every_cable_is_plugged_in_at_both_ends(self):
        for e in self.g['edges']:
            self.assertIn(e['from'], self.ids, f'{e} starts nowhere')
            self.assertIn(e['to'], self.ids, f'{e} ends nowhere')

    def test_nothing_is_stranded(self):
        wired = set()
        for e in self.g['edges']:
            wired.add(e['from'])
            wired.add(e['to'])
        self.assertEqual(self.ids - wired, set(), 'a node nothing connects to is a node nobody can find')

    def test_every_node_can_explain_itself(self):
        for n in self.g['nodes']:
            self.assertTrue(n['name'], n['id'])
            self.assertTrue(n['one_liner'], f'{n["id"]} has no plain-English line')
            self.assertTrue(n['what'], f'{n["id"]} has no description')
            self.assertGreater(len(n['what']), 60, f'{n["id"]} is described too thinly to be useful')
            self.assertIn(n['lane'], LANES)
            self.assertIn(n['kind'], ('bot', 'store', 'source', 'venue', 'equation', 'paper', 'gate'))

    def test_every_cable_says_what_flows_along_it(self):
        for e in self.g['edges']:
            self.assertIn(e['payload'], PAYLOADS)

    def test_all_four_lanes_are_on_the_map(self):
        names = {n['id'] for n in self.g['nodes']}
        for lane in ('stocks', 'crypto', 'degen', 'forex'):
            self.assertIn(f'agent.{lane}', names, f'the {lane} bot is missing')

    def test_the_equations_carry_their_arithmetic(self):
        eqs = [n for n in self.g['nodes'] if n['kind'] == 'equation']
        self.assertGreaterEqual(len(eqs), 6)
        for n in eqs:
            self.assertTrue(n['eq'] and n['eq']['html'], f'{n["id"]} has no formula')
            self.assertTrue(n['eq']['subs'], f'{n["id"]} shows a formula with no real numbers in it')

    def test_scheduled_jobs_report_from_evidence_not_from_their_plists(self):
        """A schedule file says what someone intended; a log says what ran.

        The watchdog's plist sat in the repository for weeks looking exactly like
        a job that was working, while nothing was watching anything.
        """
        from main_app.services.graph_state import _job
        dead = _job('nothing-here.log', 12, 'nobody would notice')
        self.assertEqual(dead['status'], 'error')
        self.assertIn('never run', dead['sub'])

    def test_the_supervisor_is_wired_to_every_lane(self):
        wired = {e['to'] for e in self.g['edges'] if e['from'] == 'ops.agents'}
        for lane in ('stocks', 'crypto', 'degen', 'forex'):
            self.assertIn(f'agent.{lane}', wired)


class TheMapRenders(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.user = get_user_model().objects.create_user('mapper', password='x', is_staff=True)
        self.client.force_login(self.user)

    def test_it_renders_with_an_empty_database(self):
        r = self.client.get('/map/')
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn('graph-data', body)
        self.assertIn('graph-state', body)

    def test_the_live_state_endpoint_answers(self):
        r = self.client.get('/api/map/state/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('state', r.json())

    def test_it_needs_a_login(self):
        self.client.logout()
        self.assertIn(self.client.get('/map/').status_code, (301, 302))

    def test_live_state_is_cheap_enough_to_poll(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from main_app.services.graph_state import state
        with CaptureQueriesContext(connection) as q:
            state()
        self.assertLess(len(q), 40, 'the map polls this; an N+1 here becomes a stutter')


class TheFeedDoesNotAdvertiseADisabledStrategy(SimpleTestCase):
    """SymbolState.rules persist until the next bar overwrites them.

    Disable a strategy after the close and the pulse kept naming its trigger
    until the next session — ORB was still shown as "nearest trigger" minutes
    after it was switched off.
    """
    def test_a_rule_from_an_unloaded_strategy_is_ignored(self):
        agent = Agent.__new__(Agent)
        agent.account = SimpleNamespace(pk=1)
        agent.engine = SimpleNamespace(strategies=[SimpleNamespace(key='ema_momentum')])
        rows = [SimpleNamespace(symbol='META', rules=[
            {'strategy': 'orb', 'rule': 'breakout', 'ok': False, 'value': 737.9, 'threshold': 757.1},
            {'strategy': 'ema_momentum', 'rule': 'move', 'ok': False, 'value': 1.0, 'threshold': 2.0},
        ])]
        with patch('main_app.models.SymbolState.objects') as objs:
            objs.filter.return_value = rows
            out = agent._nearest_trigger({'META': 737.9})
        self.assertIn('ema_momentum', out)
        self.assertNotIn('orb', out)


class TheMapSaysWhyAStrategyIsOff(TestCase):
    """"unproven" on something that was measured and stopped reads as
    "not looked at yet" — the opposite of what happened to ORB."""
    def test_a_quarantined_strategy_is_not_called_unproven(self):
        from main_app.models import Strategy
        from main_app.services.graph_state import state
        Strategy.objects.create(key='orb', market='stocks', enabled=False,
                                qualification='quarantine', params={}, symbols=['SPY'])
        s = state()['strat.orb']
        self.assertEqual(s['headline'], '0/1 on')
        self.assertEqual(s['sub'], 'quarantined')
        self.assertIn('stopped', s['detail'])


class GradedMeansAResultNotATimestamp(TestCase):
    """The map counted rows with an outcome TIMESTAMP as graded. 374 rows had
    one; 54 carried an actual outcome. It reported 192 graded verdicts when 54
    existed, and that number was then quoted as the size of the evidence base."""

    def _verdict(self, **kw):
        from main_app.models import NewsItem, NewsSession, NewsVerdict
        from django.utils import timezone
        sess = getattr(self, '_sess', None) or NewsSession.objects.create(started_at=timezone.now())
        self._sess = sess
        n = NewsItem.objects.count()
        item = NewsItem.objects.create(headline='h', url=f'u{n}', external_id=f'x{n}',
                                       published_at=timezone.now())
        return NewsVerdict.objects.create(session=sess, news=item, symbol='AAPL', score=5,
                                          provenance='contemporaneous', **kw)

    def test_a_timestamp_without_a_result_is_not_graded(self):
        from django.utils import timezone
        from main_app.services.graph_state import state
        self._verdict(outcome_at=timezone.now(), outcome_kind='')      # stamped, no result
        self._verdict(outcome_at=timezone.now(), outcome_kind='target')  # actually graded
        s = state()['store.verdicts']
        self.assertEqual(s['sub'], '1 graded')
        self.assertIn('1 have an outcome timestamp and no result', s['detail'])

    def test_the_headline_says_it_is_a_seven_day_window_not_a_total(self):
        from django.utils import timezone
        from main_app.services.graph_state import state
        self._verdict(outcome_at=timezone.now(), outcome_kind='stop')
        self.assertIn('in 7d', state()['store.verdicts']['headline'])
