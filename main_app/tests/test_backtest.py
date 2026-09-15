from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from main_app.services.backtest import BacktestSpec, run_backtest
from main_app.services.metrics import compute_metrics, downsample_equity
from main_app.services.risk import RiskConfig
from main_app.services.strategies import STRATEGIES
from main_app.tests.helpers import frames_for

ET = ZoneInfo('America/New_York')


class BacktestsAreIntradayAndBalanced(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.frames = frames_for(['QQQ', 'NVDA', 'BTC/USD'])
        cls.ac = {'QQQ': 'etf', 'NVDA': 'stock', 'BTC/USD': 'crypto'}

    def _run(self, key, params=None, risk=None):
        spec = BacktestSpec(key, params or {}, list(self.frames), '5Min', 10000.0, risk or RiskConfig(),
                            asset_classes=self.ac)
        return run_backtest(spec, self.frames)

    # news_catalyst acts on verdicts written in the present, so it is LIVE ONLY and
    # must produce nothing on history — see test_a_live_only_strategy_is_silent below.
    LIVE_ONLY = {'news_catalyst'}

    def test_every_strategy_runs_and_keeps_the_equity_identity(self):
        for key in STRATEGIES:
            if key in self.LIVE_ONLY:
                continue
            r = self._run(key, {'min_relvol': 0.0} if key != 'vwap_reversion' else {})
            self.assertGreater(r.metrics['trades'], 0, key)
            self.assertAlmostEqual(r.equity[-1][3], 10000 + sum(t.pnl for t in r.trades), places=2, msg=key)
            for t in r.trades:
                self.assertGreaterEqual(t.exit_ts, t.entry_ts)
                if self.ac[t.symbol] != 'crypto':
                    self.assertEqual(t.entry_ts.astimezone(ET).date(), t.exit_ts.astimezone(ET).date(), key)

    def test_a_live_only_strategy_is_silent_on_history(self):
        """A backtest that could read today's news verdicts would be reading answers
        written after the bar. news_catalyst must therefore trade nothing at all
        here, and its equity must be untouched."""
        for key in self.LIVE_ONLY:
            r = self._run(key)
            self.assertEqual(r.metrics['trades'], 0, key)
            self.assertAlmostEqual(r.equity[-1][3], 10000.0, places=2, msg=key)

    def test_strategies_short_stocks_but_never_crypto(self):
        for key, params in (('orb', {'min_relvol': 0.0}), ('ema_momentum', {'min_relvol': 0.0}), ('vwap_reversion', {})):
            r = self._run(key, params)
            sides = {t.side for t in r.trades}
            self.assertIn('short', sides, key)
            self.assertFalse(any(t.side == 'short' and self.ac[t.symbol] == 'crypto' for t in r.trades), key)

    def test_orb_trades_at_most_once_per_symbol_per_day(self):
        r = self._run('orb', {'min_relvol': 0.0, 'range_minutes': 15})
        seen = set()
        for t in r.trades:
            key = (t.symbol, t.entry_ts.astimezone(ET).date())
            self.assertNotIn(key, seen)
            seen.add(key)

    def test_no_entries_inside_the_cutoff_and_flat_before_close(self):
        r = self._run('ema_momentum', {'min_relvol': 0.0}, RiskConfig(no_entries_before_close_min=30, flat_before_close_min=5))
        for t in r.trades:
            if self.ac[t.symbol] == 'crypto':
                continue
            entry_et = t.entry_ts.astimezone(ET)
            self.assertLessEqual(entry_et.hour * 60 + entry_et.minute, 15 * 60 + 30)
            exit_et = t.exit_ts.astimezone(ET)
            self.assertLessEqual(exit_et.hour * 60 + exit_et.minute, 15 * 60 + 55)

    def test_daily_loss_limit_halts_and_records_kill_exit(self):
        r = self._run('ema_momentum', {'min_relvol': 0.0}, RiskConfig(max_daily_loss_pct=0.2, risk_per_trade_pct=2))
        kinds = {e[0] for e in r.risk_events}
        self.assertIn('daily_loss', kinds)
        self.assertIn('halted for the day: daily loss limit', r.metrics['blocked_reasons'])

    def test_warmup_cutoff_suppresses_early_trades(self):
        first_ts = self.frames['QQQ'].index[0].to_pydatetime()
        cutoff = datetime(2026, 8, 27, 13, 30, tzinfo=UTC)
        spec = BacktestSpec('orb', {'min_relvol': 0.0}, ['QQQ', 'NVDA'], '5Min', 10000.0, RiskConfig(),
                            asset_classes=self.ac, act_from=cutoff)
        r = run_backtest(spec, {k: v for k, v in self.frames.items() if k != 'BTC/USD'})
        self.assertTrue(all(t.entry_ts >= cutoff for t in r.trades))
        self.assertGreater(r.metrics['trades'], 0)
        self.assertGreater(cutoff, first_ts)


class MetricsMatchAKnownTradeList(SimpleTestCase):
    def test_basic_metrics(self):
        from main_app.services.broker.base import TradeRecord
        t0 = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)
        trades = [TradeRecord('X', 's', 'long', 10, t0, t0, 100, 110, 100.0, 10.0, 0, 3, 'target'),
                  TradeRecord('X', 's', 'long', 10, t0, t0, 100, 105, 50.0, 5.0, 0, 3, 'target'),
                  TradeRecord('Y', 's', 'long', 10, t0, t0, 100, 95, -50.0, -5.0, 0, 3, 'stop')]
        equity = [(t0, 10000, 0, 10000, 0), (datetime(2026, 8, 25, 20, 0, tzinfo=UTC), 10100, 0, 10100, 100)]
        m = compute_metrics(trades, equity, 10000, bars_seen=100, bars_with_position=25, benchmark=(100, 102), benchmark_symbol='SPY')
        self.assertEqual(m['trades'], 3)
        self.assertAlmostEqual(m['win_rate'], 66.667, places=2)
        self.assertAlmostEqual(m['profit_factor'], 3.0)
        self.assertAlmostEqual(m['expectancy'], 100 / 3)
        self.assertAlmostEqual(m['net_pnl'], 100.0)
        self.assertAlmostEqual(m['exposure_pct'], 25.0)
        self.assertAlmostEqual(m['benchmark_return_pct'], 2.0)
        self.assertAlmostEqual(m['alpha_pct'], -1.0)
        self.assertEqual(m['per_symbol']['Y']['trades'], 1)

    def test_drawdown_and_downsampling(self):
        t = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)
        eq = [(t.replace(day=24 + i // 2, hour=14 + (i % 2) * 5), 0, 0, v, 0) for i, v in enumerate([100, 110, 99, 105, 120, 96])]
        m = compute_metrics([], eq, 100)
        # Drawdown is measured on daily closes (110 → 105 → 96), not on intraday marks.
        self.assertAlmostEqual(m['max_drawdown_pct'], (96 / 110 - 1) * 100)
        self.assertEqual(len(downsample_equity(eq, 3)), 4)
