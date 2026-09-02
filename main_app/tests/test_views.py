from datetime import date

from django.test import TestCase
from django.urls import reverse

from main_app.models import BacktestRun, Experiment, JournalEntry
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
                reverse('strategy-list'), reverse('strategy-detail', args=['orb']), reverse('backtest-list'),
                reverse('backtest-detail', args=[self.bt.pk]), reverse('api-backtest-equity', args=[self.bt.pk]),
                reverse('backtest-compare') + f'?ids={self.bt.pk}', reverse('experiment-list'),
                reverse('experiment-detail', args=[self.exp.pk]), reverse('experiment-progress', args=[self.exp.pk]),
                reverse('replay'), reverse('data-index'), reverse('instrument-chart', args=['QQQ']), reverse('api-bars', args=['QQQ']),
                reverse('journal-list'), reverse('settings'), reverse('agent-log'), reverse('sync-log')]
        for url in urls:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)

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

    def test_strategy_form_saves_params(self):
        r = self.client.post(reverse('strategy-detail', args=['orb']), {
            'action': 'save', 'range_minutes': '30', 'stop_atr_mult': '1.5', 'rr': '3', 'min_relvol': '0.5',
            'entry_window_minutes': '120', 'symbols': ['QQQ'], 'allocation_pct': '100', 'notes': 'n'})
        self.assertEqual(r.status_code, 302)
        self.row.refresh_from_db()
        self.assertEqual(self.row.params['range_minutes'], 30)
        self.assertEqual(self.row.params['trade_short'], False)
        self.assertEqual(self.row.symbols, ['QQQ'])

    def test_settings_saves_and_live_mode_needs_the_phrase(self):
        r = self.client.post(reverse('settings'), {
            'trading_enabled': 'on', 'timeframe': '5Min', 'starting_cash': '10000', 'risk_per_trade_pct': '0.75',
            'max_position_pct': '20', 'max_open_positions': '3', 'max_daily_loss_pct': '2', 'max_trades_per_day': '10',
            'no_entries_before_close_min': '30', 'flat_before_close_min': '5', 'max_hold_minutes': '240',
            'slippage_bps': '3', 'fee_bps_stock': '0.5', 'fee_bps_crypto': '25', 'liquidity_cap_pct': '1'})
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
