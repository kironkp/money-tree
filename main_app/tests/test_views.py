from datetime import UTC, date, datetime, timedelta

from django.test import TestCase, override_settings
from django.urls import reverse

from allauth.account.models import EmailAddress

from main_app.models import BacktestRun, Experiment, JournalEntry, SignupInvite
from main_app.services.backtest import run_backtest_for_model
from main_app.tests.helpers import enable_strategy, make_user, seed_db


class PagesRequireLogin(TestCase):
    def test_redirects_to_login(self):
        for name in ('dashboard', 'trades', 'strategy-list', 'backtest-list', 'experiment-list', 'data-index', 'journal-list', 'settings'):
            r = self.client.get(reverse(name))
            self.assertEqual(r.status_code, 302, name)
            self.assertIn('/accounts/login/', r['Location'])


class PagesRenderWithData(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cfg, cls.instruments = seed_db(('QQQ', 'NVDA'))
        cls.row = enable_strategy('orb', {'min_relvol': 0.0}, ['QQQ', 'NVDA'])
        cls.bt = BacktestRun.objects.create(strategy_key='orb', params=cls.row.params, symbols=['QQQ', 'NVDA'],
                                             timeframe='5Min', start=date(2026, 8, 24), end=date(2026, 8, 28))
        run_backtest_for_model(cls.bt)
        cls.exp = Experiment.objects.create(strategy_key='orb', method='grid', param_grid={'rr': [1.5, 2.0]},
                                            symbols=['QQQ'], start=date(2026, 8, 24), end=date(2026, 8, 28),
                                            status='done', best_params={'rr': 2.0},
                                            summary={'ranked': [{'params': {'rr': 2.0}, 'objective': 1.0, 'trades': 5, 'net_pnl': 1.0,
                                                                 'sharpe': 1.0, 'profit_factor': 1.2, 'win_rate': 50.0, 'max_drawdown_pct': -1.0}],
                                                     'stability': {'score': 0.9, 'neighbours': 1}})
        JournalEntry.objects.create(date=date(2026, 8, 28), kind='coach', title='c', body='**bold** line\n- one\n- two',
                                    proposals=[{'title': 'p', 'strategy_key': 'orb', 'method': 'grid', 'param_grid': {'rr': [1.5, 2.5]}, 'rationale': 'r'}])
        cls.user = make_user()

    def setUp(self):
        self.client.force_login(self.user)

    def test_every_page_returns_200(self):
        urls = [reverse('dashboard'), reverse('dashboard-panels'), reverse('api-equity', args=['sim']), reverse('positions'),
                reverse('orders'), reverse('trades'), reverse('trades-csv'), reverse('signals'), reverse('risk-events'),
                reverse('strategy-list'), reverse('strategy-detail', args=['stocks', 'orb']), reverse('backtest-list'),
                reverse('backtest-detail', args=[self.bt.pk]), reverse('api-backtest-equity', args=[self.bt.pk]),
                reverse('backtest-compare') + f'?ids={self.bt.pk}', reverse('experiment-list'),
                reverse('experiment-detail', args=[self.exp.pk]), reverse('experiment-progress', args=[self.exp.pk]),
                reverse('replay'), reverse('replay') + '?market=crypto', reverse('feed'), reverse('api-feed'), reverse('data-index'), reverse('instrument-chart', args=['QQQ']), reverse('api-bars', args=['QQQ']),
                reverse('journal-list'), reverse('settings'), reverse('agent-log'), reverse('sync-log')]
        for url in urls:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)

    def test_settings_lists_forex_accounts(self):
        r = self.client.get(reverse('settings'))
        self.assertContains(r, 'Sprout (sim) · forex')

    def test_backtest_detail_shows_metrics(self):
        r = self.client.get(reverse('backtest-detail', args=[self.bt.pk]))
        self.assertContains(r, 'Profit factor')
        self.assertGreater(self.bt.metrics['trades'], 0)

    def test_promote_installs_params_and_bumps_version(self):
        r = self.client.post(reverse('backtest-promote', args=[self.bt.pk]), {'note': 'test'})
        self.assertEqual(r.status_code, 302)
        self.row.refresh_from_db()
        self.assertEqual(self.row.version, 2)
        self.assertEqual(self.row.history[-1]['source'], f'backtest #{self.bt.pk}')

    def test_walk_forward_cannot_promote_failed_held_out_evidence(self):
        candidate = {**self.row.params, 'rr': 3.0}
        exp = Experiment.objects.create(
            strategy_key='orb', method='walk_forward', param_grid={'rr': [2.0, 3.0]}, symbols=['QQQ'],
            timeframe='5Min', start=date(2026, 6, 1), end=date(2026, 8, 31), status='done',
            min_trades=10, best_params=candidate,
            summary={'oos': {'trades': 40, 'profit_factor': 1.3, 'net_pnl': 100, 'expectancy': 2},
                     'validation': {'candidate': {'trades': 20, 'profit_factor': .94,
                                                  'net_pnl': -10, 'expectancy': -.5}}},
        )
        old_version = self.row.version
        response = self.client.post(reverse('experiment-promote', args=[exp.pk]))
        self.assertEqual(response.status_code, 302)
        self.row.refresh_from_db()
        self.assertEqual(self.row.version, old_version)

    def test_walk_forward_cannot_promote_lucky_final_window_after_losing_pipeline(self):
        candidate = {**self.row.params, 'rr': 3.0}
        exp = Experiment.objects.create(
            strategy_key='orb', method='walk_forward', param_grid={'rr': [2.0, 3.0]}, symbols=['QQQ'],
            timeframe='5Min', start=date(2026, 6, 1), end=date(2026, 8, 31), status='done',
            min_trades=10, best_params=candidate,
            summary={
                'oos': {'trades': 210, 'profit_factor': .51, 'net_pnl': -1961, 'expectancy': -9.34},
                'validation': {'candidate': {'trades': 54, 'profit_factor': 1.1019,
                                             'net_pnl': 76.29, 'expectancy': 1.41}},
            },
        )
        old_version = self.row.version
        response = self.client.post(reverse('experiment-promote', args=[exp.pk]))
        self.assertEqual(response.status_code, 302)
        self.row.refresh_from_db()
        self.assertEqual(self.row.version, old_version)

    def test_walk_forward_promotion_records_only_fixed_validation_metrics(self):
        candidate = {**self.row.params, 'rr': 3.0}
        validation = {'trades': 20, 'profit_factor': 1.4, 'net_pnl': 120, 'expectancy': 6,
                      'max_drawdown_pct': -2, 'sharpe': 1.1}
        exp = Experiment.objects.create(
            strategy_key='orb', method='walk_forward', param_grid={'rr': [2.0, 3.0]}, symbols=['QQQ'],
            timeframe='5Min', start=date(2026, 6, 1), end=date(2026, 8, 31), status='done',
            min_trades=10, best_params=candidate,
            summary={'oos': {'trades': 99, 'profit_factor': 9.9, 'net_pnl': 9999, 'expectancy': 10},
                     'validation': {'candidate': validation}},
        )
        response = self.client.post(reverse('experiment-promote', args=[exp.pk]))
        self.assertEqual(response.status_code, 302)
        self.row.refresh_from_db()
        self.assertEqual(self.row.params, candidate)
        self.assertEqual(self.row.history[-1]['metrics']['profit_factor'], 1.4)
        self.assertEqual(self.row.history[-1]['metrics']['trades'], 20)

    def test_strategy_form_saves_params(self):
        r = self.client.post(reverse('strategy-detail', args=['stocks', 'orb']), {
            'action': 'save', 'range_minutes': '30', 'stop_atr_mult': '1.5', 'rr': '3', 'min_relvol': '0.5',
            'entry_window_minutes': '120', 'symbols': ['QQQ'], 'allocation_pct': '100', 'notes': 'n'})
        self.assertEqual(r.status_code, 302)
        self.row.refresh_from_db()
        self.assertEqual(self.row.params['range_minutes'], 30)
        self.assertNotIn('trade_short', self.row.params)
        self.assertEqual(self.row.symbols, ['QQQ'])

    def test_settings_saves_and_live_mode_needs_the_phrase(self):
        r = self.client.post(reverse('settings'), {
            'trading_enabled': 'on', 'news_enabled': 'on', 'timeframe': '5Min', 'crypto_timeframe': '1Hour', 'starting_cash': '10000', 'risk_per_trade_pct': '0.75',
            'max_position_pct': '20', 'max_open_positions': '3', 'max_daily_loss_pct': '2', 'max_trades_per_day': '10',
            'no_entries_before_close_min': '30', 'flat_before_close_min': '5', 'max_hold_minutes': '240',
            'slippage_bps': '3', 'fee_bps_stock': '0.5', 'fee_bps_crypto': '25', 'liquidity_cap_pct': '1', 'min_reward_to_cost': '3',
            'live_confirm_orders': 'on', 'live_confirm_minutes': '3', 'degen_timeframe': '1Min', 'pulse_seconds': '10',
            'degen_risk_per_trade_pct': '3', 'degen_max_position_pct': '25', 'degen_max_open_positions': '4', 'degen_max_daily_loss_pct': '10',
            'degen_max_trades_per_day': '60', 'degen_max_hold_minutes': '180', 'degen_min_reward_to_cost': '1.5',
            'degen_max_directional_exposure_pct': '50',
            'forex_timeframe': '5Min', 'forex_leverage': '10', 'forex_risk_per_trade_pct': '0.5', 'forex_max_position_pct': '500',
            'forex_max_directional_exposure_pct': '500',
            'forex_max_open_positions': '4', 'forex_max_daily_loss_pct': '2', 'forex_max_trades_per_day': '40',
            'forex_max_hold_minutes': '240', 'forex_min_reward_to_cost': '2', 'forex_slippage_bps': '0.3', 'fee_bps_forex': '0.5'})
        self.assertEqual(r.status_code, 302)
        self.cfg.refresh_from_db()
        self.assertEqual(float(self.cfg.risk_per_trade_pct), 0.75)
        r = self.client.post(reverse('set-mode'), {'mode': 'live', 'confirm': 'nope'})
        self.cfg.refresh_from_db()
        self.assertEqual(self.cfg.mode, 'sim')

    def test_kill_switch_toggles(self):
        self.client.post(reverse('kill-switch'), {'state': 'on'})
        self.cfg.refresh_from_db()
        self.assertTrue(self.cfg.kill_switch)
        self.client.post(reverse('kill-switch'), {'state': 'off'})
        self.cfg.refresh_from_db()
        self.assertFalse(self.cfg.kill_switch)

    def test_backtest_launch_form_runs_inline(self):
        r = self.client.post(reverse('backtest-list'), {'strategy': 'orb', 'start': '2026-08-24', 'end': '2026-08-28',
                                                        'timeframe': '5Min', 'cash': '10000', 'param_orb_min_relvol': '0'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(BacktestRun.objects.count(), 2)


class TemplatesUseSafeComments(TestCase):
    """`{# … #}` comments are single-line only in Django; a multi-line one renders as page text."""

    def test_no_multiline_hash_comments(self):
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parents[1] / 'templates'
        offenders = []
        for path in root.rglob('*.html'):
            text = path.read_text()
            for m in re.finditer(r'\{#', text):
                end = text.find('#}', m.end())
                if end == -1 or '\n' in text[m.end():end]:
                    offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [])


class AuthPagesAndInvites(TestCase):
    def test_login_signup_and_reset_pages_render(self):
        for url in ('/accounts/login/', '/accounts/signup/', '/accounts/password/reset/'):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_signup_is_invite_only(self):
        data = {'email': 'stranger@example.com', 'password1': 'a-long-passw0rd!', 'password2': 'a-long-passw0rd!'}
        r = self.client.post('/accounts/signup/', data)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'invite-only')
        SignupInvite.objects.create(email='stranger@example.com', make_operator=False)
        r = self.client.post('/accounts/signup/', data)
        self.assertEqual(r.status_code, 302)
        from django.contrib.auth import get_user_model
        u = get_user_model().objects.get(email='stranger@example.com')
        self.assertFalse(u.is_staff)  # view-only unless invited as operator
        self.assertIsNotNone(SignupInvite.objects.get(email='stranger@example.com').used_at)

    def test_observer_cannot_change_settings(self):
        u = make_user(staff=False)
        self.client.force_login(u)
        r = self.client.post(reverse('kill-switch'), {'state': 'on'})
        self.assertEqual(r.status_code, 302)
        from main_app.models import AgentConfig
        self.assertFalse(AgentConfig.get().kill_switch)
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)

    def test_password_reset_shows_the_link_when_no_mail_server(self):
        from django.contrib.auth import get_user_model
        u = get_user_model().objects.create_user('owner', 'owner@example.com', 'owner-passw0rd!')
        EmailAddress.objects.create(user=u, email='owner@example.com', verified=True, primary=True)
        with self.settings(EMAIL_HOST='', DEBUG=True):
            r = self.client.post('/accounts/password/reset/', {'email': 'owner@example.com'}, follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, '/accounts/password/reset/key/')

    def test_password_reset_key_logs_in_and_lands_on_the_dashboard(self):
        import re
        from django.contrib.auth import get_user_model
        u = get_user_model().objects.create_user('owner', 'owner@example.com', 'owner-passw0rd!')
        EmailAddress.objects.create(user=u, email='owner@example.com', verified=True, primary=True)
        with self.settings(EMAIL_HOST='', DEBUG=True):
            r = self.client.post('/accounts/password/reset/', {'email': 'owner@example.com'}, follow=True)
            self.assertContains(r, 'Back to log in')
            path = re.search(r'/accounts/password/reset/key/[^\s"<]+', r.content.decode()).group(0)
            r = self.client.get(path, follow=True)  # allauth stores the key in the session and redirects
            r = self.client.post(r.redirect_chain[-1][0] if r.redirect_chain else path,
                                 {'password1': 'brand-new-passw0rd!', 'password2': 'brand-new-passw0rd!'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], reverse('dashboard'))
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)

    def test_email_login_works_for_bootstrapped_owner(self):
        from django.contrib.auth import get_user_model
        u = get_user_model().objects.create_user('owner', 'owner@example.com', 'owner-passw0rd!')
        EmailAddress.objects.create(user=u, email='owner@example.com', verified=True, primary=True)
        r = self.client.post('/accounts/login/', {'login': 'owner@example.com', 'password': 'owner-passw0rd!'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)


class FeedStreamsNarration(TestCase):
    def test_feed_api_returns_events_in_order_and_after_cursor(self):
        from main_app.models import Account, FeedEvent
        cls_user = make_user()
        self.client.force_login(cls_user)
        account = Account.for_mode('sim', 'stocks')
        e1 = FeedEvent.objects.create(account=account, level='system', text='hello')
        e2 = FeedEvent.objects.create(account=account, level='trade', text='CLOSED QQQ', symbol='QQQ')
        r = self.client.get(reverse('api-feed') + '?account=sim&market=stocks')
        data = r.json()
        self.assertEqual([e['id'] for e in data['events']], [e1.id, e2.id])
        r = self.client.get(reverse('api-feed') + f'?account=sim&market=stocks&after={e1.id}')
        self.assertEqual([e['text'] for e in r.json()['events']], ['CLOSED QQQ'])
        r = self.client.get(reverse('api-feed') + '?account=sim&market=stocks&levels=trade')
        self.assertEqual(len(r.json()['events']), 1)


class StatusStripTellsTheTruth(TestCase):
    def setUp(self):
        self.cfg, self.instruments = seed_db(('QQQ',), with_bars=False)
        self.user = make_user()
        self.client.force_login(self.user)

    def test_no_agent_means_not_safe_with_named_blockers(self):
        from main_app.models import Account
        from main_app.services.status import build_status
        account = Account.for_mode('sim', 'stocks')
        st = build_status(account, self.cfg, None)
        self.assertFalse(st['safe'])
        self.assertTrue(any('no agent' in b for b in st['blockers']))
        self.assertTrue(any('no stocks strategy' in b for b in st['blockers']))
        self.cfg.kill_switch = True
        self.cfg.save()
        st = build_status(account, self.cfg, None)
        self.assertTrue(any('kill switch' in b for b in st['blockers']))

    def test_strip_partial_and_alert_ack(self):
        from main_app.models import Account, RiskEvent
        account = Account.for_mode('sim', 'stocks')
        ev = RiskEvent.objects.create(account=account, kind='config_changed', message='changed')
        r = self.client.get(reverse('status-strip') + '?account=sim&market=stocks')
        self.assertContains(r, 'Operational')
        self.assertContains(r, 'Evidence')
        self.assertContains(r, 'Execution')
        self.assertContains(r, 'needs a restart')
        self.client.post(reverse('alert-ack', args=[ev.pk]), {'account': 'sim', 'market': 'stocks'})
        ev.refresh_from_db()
        self.assertIsNotNone(ev.acknowledged_at)

    def test_card_approval_endpoint(self):
        from main_app.models import Account, TradeCard
        account = Account.for_mode('sim', 'stocks')
        card = TradeCard.objects.create(account=account, entry_order_id='mt-x-1', symbol='QQQ', status='awaiting_approval')
        self.client.post(reverse('card-decide', args=[card.pk, 'approve']))
        card.refresh_from_db()
        self.assertEqual(card.status, 'approved')
        self.assertTrue(card.approved_by)

    def test_graduation_is_enforced_unless_overridden(self):
        row = enable_strategy('orb', symbols=['QQQ'], stage='seed')
        r = self.client.post(reverse('strategy-detail', args=['stocks', 'orb']), {'action': 'stage_up'})
        row.refresh_from_db()
        self.assertEqual(row.stage, 'seed')
        r = self.client.post(reverse('strategy-detail', args=['stocks', 'orb']), {'action': 'stage_up', 'override': 'yes', 'override_reason': 'testing'})
        row.refresh_from_db()
        self.assertEqual(row.stage, 'sprout')
        self.assertIn('OVERRIDE', row.history[-1]['source'])

    def test_unqualified_strategy_cannot_override_into_broker_stage(self):
        row = enable_strategy('orb', symbols=['QQQ'], stage='sprout')
        self.client.post(reverse('strategy-detail', args=['stocks', 'orb']),
                         {'action': 'stage_up', 'override': 'yes', 'override_reason': 'skip evidence'})
        row.refresh_from_db()
        self.assertEqual(row.stage, 'sprout')


class PortfolioBacktestRunsAllEnabledStrategies(TestCase):
    def test_portfolio_run_has_per_strategy_breakdown(self):
        # The view backtests [today − days, today], so the fixture has to sit
        # inside that window. Seeding a FIXED calendar week meant this test
        # slowly aged out of it and went red on 2026-09-08; the bars now trail
        # today, with margin on both sides.
        end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        cfg, instruments = seed_db(('QQQ', 'NVDA'), start=end - timedelta(days=15), end=end)
        enable_strategy('orb', {'min_relvol': 0.0}, ['QQQ', 'NVDA'])
        enable_strategy('ema_momentum', {'min_relvol': 0.0}, ['QQQ', 'NVDA'])
        self.client.force_login(make_user())
        r = self.client.post(reverse('portfolio-backtest', args=['stocks']), {'days': 20})
        self.assertEqual(r.status_code, 302)
        run = BacktestRun.objects.get(strategy_key='portfolio')
        self.assertEqual(run.status, 'done')
        self.assertGreaterEqual(len(run.metrics['per_strategy']), 1)


class TheOriginThePhoneUsesIsTrusted(TestCase):
    """A wildcard origin is matched against the netloc INCLUDING the port.

    'https://*.ts.net' does not cover 'https://host.ts.net:8446'. That cost a day
    once, when every POST from the phone through the TLS proxy returned 403 while
    the same page loaded fine. The Tailscale Funnel reintroduces the same trap on
    a different port, which is why FUNNEL_PORT is carried explicitly rather than
    assumed to be 443.
    """

    def test_a_wildcard_without_a_port_does_not_cover_a_ported_origin(self):
        from django.utils.http import is_same_domain
        netloc = 'kironkps-macbook-pro-2.taildfcf4.ts.net:8446'
        self.assertFalse(is_same_domain(netloc, '.ts.net'))
        self.assertTrue(is_same_domain(netloc, '.ts.net:8446'))

    def test_the_funnel_origin_carries_its_port_unless_it_is_443(self):
        from django.conf import settings
        for host, port, expected in (
            ('box.ts.net', 10000, 'https://box.ts.net:10000'),
            ('box.ts.net', 8443, 'https://box.ts.net:8443'),
            ('box.ts.net', 443, 'https://box.ts.net'),
        ):
            origin = f'https://{host}' + ('' if port == 443 else f':{port}')
            self.assertEqual(origin, expected)
        if settings.FUNNEL_HOST:
            self.assertIn(settings.FUNNEL_ORIGIN, settings.CSRF_TRUSTED_ORIGINS,
                          'the address the phone actually uses must be trusted')

    def test_a_post_from_the_configured_funnel_origin_is_accepted(self):
        """A POST from the app's own front door works.

        The origin and the host are fixed here rather than read from settings:
        reading them made the test assert something different on a laptop with a
        FUNNEL_HOST in .env than it did in CI with none, which is how a test
        quietly stops testing anything.
        """
        from django.test import Client
        host = 'box.ts.net:10000'
        origin = f'https://{host}'
        with override_settings(ALLOWED_HOSTS=['box.ts.net'], CSRF_TRUSTED_ORIGINS=[origin]):
            c = Client(enforce_csrf_checks=True, HTTP_HOST=host)
            page = c.get('/accounts/login/', secure=True)
            self.assertEqual(page.status_code, 200)
            token = page.cookies['csrftoken'].value
            r = c.post('/accounts/login/',
                       {'csrfmiddlewaretoken': token, 'login': 'x@example.com',
                        'password': 'wrong'},
                       secure=True, HTTP_ORIGIN=origin,
                       HTTP_REFERER=f'{origin}/accounts/login/')
        self.assertNotEqual(r.status_code, 403, f'CSRF rejected a POST from {origin}')
