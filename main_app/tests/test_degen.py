"""The degen lane: burst strategy rules, per-market risk overrides, tiny prices."""
from datetime import UTC, datetime
from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from main_app.services.backtest import BacktestSpec, run_backtest
from main_app.services.risk import RiskConfig
from main_app.services.strategies import make_strategy
from main_app.tests.helpers import frames_for


class BurstStrategyFiresOnSharpMoves(SimpleTestCase):
    def test_backtest_runs_and_rules_explain_themselves(self):
        frames = frames_for(['SOL/USD'], seed=11, vol=1.2, timeframe='1Min')
        spec = BacktestSpec('burst', {'min_relvol': 0.0, 'min_move_pct': 0.3}, ['SOL/USD'], '1Min', 10000.0,
                            RiskConfig(min_reward_to_cost=1.2, max_hold_minutes=45), asset_classes={'SOL/USD': 'crypto'},
                            qty_increments={'SOL/USD': 1e-8})
        r = run_backtest(spec, frames)
        self.assertGreater(r.metrics['trades'], 0)
        self.assertTrue(all(t.bars_held <= 46 for t in r.trades))
        strat = make_strategy('burst', {})
        df = strat.prepare(frames['SOL/USD'], 'crypto', '1Min')
        row = list(df.itertuples())[-1]
        from main_app.services.strategies.base import Context
        rules = strat.rules(Context(symbol='SOL/USD', asset_class='crypto', timeframe='1Min', ts=datetime.now(UTC)), row)
        self.assertEqual({r.name for r in rules} - {'cooldown'}, {'move', 'volume'})


class DegenRiskOverrides(TestCase):
    def test_degen_lane_is_no_looser_than_the_others(self):
        """Degen was once the deliberately loose sandbox. It then lost a third of
        its capital at a 14% win rate, so the loosest settings in the app sat on
        the worst lane and on the most expensive venue. Risk is now at parity,
        and the cost gate is at least as strict, because altcoin fees are the
        highest we pay."""
        from main_app.models import AgentConfig
        cfg = AgentConfig.get()
        base = RiskConfig.from_model(cfg, 'stocks')
        degen = RiskConfig.from_model(cfg, 'degen')
        self.assertEqual(degen.risk_per_trade_pct, base.risk_per_trade_pct)
        self.assertEqual(degen.max_daily_loss_pct, base.max_daily_loss_pct)
        self.assertLessEqual(degen.max_trades_per_day, base.max_trades_per_day)
        self.assertGreaterEqual(degen.min_reward_to_cost, base.min_reward_to_cost)
        # It still has its own knobs — parity is a choice, not a merge.
        self.assertEqual(degen.max_hold_minutes, int(cfg.degen_max_hold_minutes))

    def test_sub_cent_prices_survive_the_ledger(self):
        from main_app.models import Account, Instrument, Position
        inst = Instrument.objects.create(symbol='PEPE/USD', asset_class='crypto', market='degen', qty_increment=Decimal('0.00000001'))
        acct = Account.for_mode('sim', 'degen')
        p = Position.objects.create(account=acct, instrument=inst, qty=Decimal('150000000'), avg_price=Decimal('0.00001080'),
                                    opened_at=datetime.now(UTC), stop_price=Decimal('0.00001069'))
        p.refresh_from_db()
        self.assertEqual(p.avg_price, Decimal('0.00001080'))
        self.assertGreater(p.risk_dollars, 0)
