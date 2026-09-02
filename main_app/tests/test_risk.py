from datetime import UTC, datetime

from django.test import SimpleTestCase

from main_app.services.broker.base import AccountState, Position
from main_app.services.risk import RiskConfig, RiskManager
from main_app.services.strategies.base import Context, Signal

T0 = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)


def sig(price=100.0, stop=98.0, action='buy', symbol='X'):
    return Signal(action, symbol, T0, price, stop, 104.0)


def ctx(mtc=200.0, symbol='X', ac='stock'):
    return Context(symbol=symbol, asset_class=ac, timeframe='5Min', ts=T0, minutes_to_close=mtc)


def acct(equity=10000.0, cash=None):
    cash = equity if cash is None else cash
    return AccountState(cash=cash, equity=equity, positions_value=equity - cash, buying_power=cash)


class RiskManagerSizesByStopDistance(SimpleTestCase):
    def test_qty_is_risk_dollars_over_stop_distance(self):
        rm = RiskManager(RiskConfig(risk_per_trade_pct=0.5, max_position_pct=50))
        rm.new_day(T0.date(), 10000)
        d = rm.evaluate(sig(100, 98), ctx(), acct(), {}, 'stock')
        self.assertTrue(d.allowed)
        self.assertEqual(d.qty, 25)  # $50 risk / $2 stop

    def test_position_cap_and_whole_shares(self):
        rm = RiskManager(RiskConfig(risk_per_trade_pct=5, max_position_pct=20))
        rm.new_day(T0.date(), 10000)
        d = rm.evaluate(sig(100, 99), ctx(), acct(), {}, 'stock')
        self.assertEqual(d.qty, 20)  # $2000 cap / $100
        d2 = rm.evaluate(sig(3000, 2990), ctx(), acct(), {}, 'stock')
        self.assertFalse(d2.allowed)
        self.assertIn('position cap', d2.reason)

    def test_crypto_gets_fractional_qty(self):
        rm = RiskManager(RiskConfig(risk_per_trade_pct=0.5, max_position_pct=20), {'BTC/USD': 0.0001})
        rm.new_day(T0.date(), 10000)
        d = rm.evaluate(sig(100000, 99000, symbol='BTC/USD'), ctx(None, 'BTC/USD', 'crypto'), acct(), {}, 'crypto')
        self.assertTrue(d.allowed)
        self.assertAlmostEqual(d.qty, 0.02)


class RiskManagerRefusesTradesThatCannotPayTheirFees(SimpleTestCase):
    def test_small_target_on_crypto_is_blocked_and_wide_target_passes(self):
        rm = RiskManager(RiskConfig(min_reward_to_cost=3.0, slippage_bps=3.0), {'BTC/USD': 0.0001})
        rm.new_day(T0.date(), 10000)
        # round-trip cost on crypto = 2 × (25 + 3) bps = 0.56% → target must be ≥ 1.68% away
        small = Signal('buy', 'BTC/USD', T0, 100000.0, 99800.0, 100400.0)
        d = rm.evaluate(small, ctx(None, 'BTC/USD', 'crypto'), acct(), {}, 'crypto')
        self.assertFalse(d.allowed)
        self.assertIn('round-trip cost', d.reason)
        wide = Signal('buy', 'BTC/USD', T0, 100000.0, 99000.0, 102000.0)
        self.assertTrue(rm.evaluate(wide, ctx(None, 'BTC/USD', 'crypto'), acct(), {}, 'crypto').allowed)
        # stocks: 2 × (0.5 + 3) bps = 0.07% → a 0.4% target clears 3× easily
        stock = Signal('buy', 'X', T0, 100.0, 99.5, 100.4)
        self.assertTrue(rm.evaluate(stock, ctx(), acct(), {}, 'stock').allowed)


class RiskManagerBlocksWhatItShould(SimpleTestCase):
    def setUp(self):
        self.rm = RiskManager(RiskConfig(max_open_positions=2, max_trades_per_day=3))
        self.rm.new_day(T0.date(), 10000)

    def test_stock_shorts_are_the_strategys_call_but_crypto_spot_cannot_short(self):
        d = self.rm.evaluate(Signal('sell', 'X', T0, 100.0, 102.0, 96.0), ctx(), acct(), {}, 'stock')
        self.assertTrue(d.allowed)
        self.assertIn('crypto', self.rm.evaluate(sig(action='sell'), ctx(None, ac='crypto'), acct(), {}, 'crypto').reason)

    def test_already_in_position_and_max_positions(self):
        pos = {'X': Position('X', 10, 100, T0), 'Y': Position('Y', 10, 100, T0)}
        self.assertIn('already', self.rm.evaluate(sig(), ctx(), acct(), pos, 'stock').reason)
        self.assertIn('max open', self.rm.evaluate(sig(symbol='Z'), ctx(symbol='Z'), acct(), pos, 'stock').reason)

    def test_entry_cutoff_before_close(self):
        d = self.rm.evaluate(sig(), ctx(mtc=20), acct(), {}, 'stock')
        self.assertIn('last 30 min', d.reason)

    def test_trades_per_day(self):
        for _ in range(3):
            self.rm.record_entry()
        self.assertIn('max trades', self.rm.evaluate(sig(), ctx(), acct(), {}, 'stock').reason)

    def test_daily_loss_halts_the_day(self):
        self.assertFalse(self.rm.daily_loss_breached(9900))
        self.assertTrue(self.rm.daily_loss_breached(9790))  # 2% of 10000
        d = self.rm.evaluate(sig(), ctx(), acct(equity=9790), {}, 'stock')
        self.assertFalse(d.allowed)
        self.assertTrue(self.rm.day.halted)
        self.assertEqual(self.rm.daily_loss_used_pct(9900), 50.0)

    def test_kill_switch_and_disabled(self):
        self.rm.kill_switch = True
        self.assertIn('kill', self.rm.evaluate(sig(), ctx(), acct(), {}, 'stock').reason)
        self.rm.kill_switch = False
        self.rm.trading_enabled = False
        self.assertIn('disabled', self.rm.evaluate(sig(), ctx(), acct(), {}, 'stock').reason)
