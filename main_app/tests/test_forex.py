"""The forex lane: 24/5 hours, the New York-close day, margin buying power,
Yahoo symbols, weekend flattening and per-lane cost models."""
from datetime import UTC, datetime
from decimal import Decimal

import numpy as np
import pandas as pd
from django.test import SimpleTestCase, TestCase, override_settings

from main_app.services import indicators as ind
from main_app.services.backtest import BacktestSpec, run_backtest
from main_app.services.broker.base import OrderReq
from main_app.services.broker.sim import SimBroker
from main_app.services.data import calendar as cal
from main_app.services.data.yahoo import yahoo_symbol
from main_app.services.risk import RiskConfig
from main_app.tests.helpers import frames_for


def et(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=cal.ET).astimezone(UTC)


class ForexHours(SimpleTestCase):
    def test_week_runs_sunday_five_pm_to_friday_five_pm_eastern(self):
        self.assertFalse(cal.is_open(et(2026, 9, 5, 12), 'forex'))        # Saturday
        self.assertFalse(cal.is_open(et(2026, 9, 6, 16, 59), 'forex'))    # Sunday before the open
        self.assertTrue(cal.is_open(et(2026, 9, 6, 17), 'forex'))         # Sunday 17:00 ET
        self.assertTrue(cal.is_open(et(2026, 9, 9, 3), 'forex'))          # Wednesday 03:00 ET (London morning)
        self.assertTrue(cal.is_open(et(2026, 9, 11, 16, 59), 'forex'))    # Friday before the close
        self.assertFalse(cal.is_open(et(2026, 9, 11, 17), 'forex'))       # Friday 17:00 ET
        self.assertTrue(cal.is_open(et(2026, 9, 7, 12), 'forex'))         # Labor Day: stocks closed, forex open
        self.assertFalse(cal.is_open(et(2026, 9, 7, 12), 'stock'))

    def test_next_open_is_the_coming_sunday(self):
        nxt = cal.next_open(et(2026, 9, 5, 12), 'forex')
        self.assertEqual(nxt, et(2026, 9, 6, 17))
        self.assertEqual(cal.next_open(et(2026, 9, 9, 3), 'forex'), et(2026, 9, 9, 3))

    def test_forex_day_rolls_at_the_new_york_close(self):
        self.assertEqual(cal.trading_day(et(2026, 9, 6, 17, 30), 'forex').isoformat(), '2026-09-07')
        self.assertEqual(cal.trading_day(et(2026, 9, 7, 16, 59), 'forex').isoformat(), '2026-09-07')
        self.assertEqual(cal.trading_day(et(2026, 9, 7, 17), 'forex').isoformat(), '2026-09-08')
        self.assertEqual(cal.trading_day(et(2026, 9, 7, 17), 'stock').isoformat(), '2026-09-07')

    def test_minutes_to_close_count_down_to_friday(self):
        idx = pd.DatetimeIndex([et(2026, 9, 9, 12), et(2026, 9, 11, 16, 55), et(2026, 9, 11, 17, 5), et(2026, 9, 12, 12)])
        mtc = ind.minutes_to_close(idx, 'forex')
        self.assertAlmostEqual(mtc.iloc[0], (2 * 24 + 5) * 60)
        self.assertAlmostEqual(mtc.iloc[1], 5)
        self.assertTrue(np.isnan(mtc.iloc[2]) and np.isnan(mtc.iloc[3]))
        self.assertTrue(ind.minutes_to_close(idx, 'crypto').isna().all())

    def test_session_key_uses_the_forex_day(self):
        idx = pd.DatetimeIndex([et(2026, 9, 6, 17, 30), et(2026, 9, 7, 9), et(2026, 9, 7, 17, 30)])
        keys = ind.session_key(idx, 'forex').tolist()
        self.assertEqual([k.isoformat() for k in keys], ['2026-09-07', '2026-09-07', '2026-09-08'])

    def test_yahoo_symbols(self):
        self.assertEqual(yahoo_symbol('EUR/USD', 'forex'), 'EURUSD=X')
        self.assertEqual(yahoo_symbol('BTC/USD', 'crypto'), 'BTC-USD')
        self.assertEqual(yahoo_symbol('AAPL'), 'AAPL')


class MarginBuyingPower(SimpleTestCase):
    def test_leverage_lends_and_exposure_consumes_it(self):
        b = SimBroker(10_000, immediate_fills=True, slippage_bps=0, leverage=10, asset_classes={'EUR/USD': 'forex'})
        self.assertEqual(b.account().buying_power, 100_000)
        b.last_price['EUR/USD'] = 1.2
        b.submit(OrderReq(id='e1', symbol='EUR/USD', side='buy', qty=25_000, leg='entry', submitted_ts=datetime.now(UTC)))
        a = b.account()
        self.assertAlmostEqual(a.equity, 10_000 - 25_000 * 1.2 * 0.5 / 1e4, places=2)  # only the fee is gone
        self.assertAlmostEqual(a.buying_power, a.equity * 10 - 30_000, places=2)
        self.assertLess(a.cash, 0)  # the broker lent the difference

    def test_cash_accounts_are_unchanged(self):
        b = SimBroker(10_000, immediate_fills=True, slippage_bps=0)
        b.last_price['AAPL'] = 100.0
        b.submit(OrderReq(id='e1', symbol='AAPL', side='buy', qty=50, leg='entry', submitted_ts=datetime.now(UTC)))
        a = b.account()
        self.assertAlmostEqual(a.buying_power, a.cash, places=2)


class ForexRiskConfig(TestCase):
    def test_forex_lane_uses_margin_and_spread_costs(self):
        from main_app.models import AgentConfig
        cfg = AgentConfig.get()
        fx = RiskConfig.from_model(cfg, 'forex')
        base = RiskConfig.from_model(cfg, 'stocks')
        self.assertEqual(fx.leverage, 10.0)
        self.assertEqual(base.leverage, 1.0)
        self.assertEqual(fx.max_position_pct, 500.0)
        self.assertLess(fx.slippage_bps, base.slippage_bps)
        self.assertAlmostEqual(fx.round_trip_cost_pct('forex'), 0.016)  # 1.6 bps a round trip: spread, not commission

    @override_settings(ALPACA_ENABLED=False)
    def test_seed_creates_the_lane(self):
        from django.core.management import call_command
        from main_app.models import Account, Instrument, Strategy
        call_command('seed_watchlist', verbosity=0)
        self.assertEqual(Instrument.objects.filter(market='forex').count(), 4)
        self.assertTrue(Account.objects.filter(mode='sim', market='forex').exists())
        self.assertTrue(Strategy.objects.get(key='ema_momentum', market='forex').enabled)
        acct = Account.for_mode('sim', 'forex')
        self.assertEqual(acct.lane_asset_class, 'forex')
        self.assertFalse(acct.is_24x7)


class ForexBacktestFlattensForTheWeekend(SimpleTestCase):
    def test_no_position_survives_friday_five_pm(self):
        frames = frames_for(['EUR/USD'], seed=5, vol=0.3)
        df = frames['EUR/USD']
        # Synthetic bars are stock-session shaped; stretch them onto a 24/5 forex week at forex prices.
        n = len(df)
        idx = pd.date_range(et(2026, 8, 23, 17), periods=n, freq='5min', tz='UTC')
        df = df.set_index(idx)
        scale = 1.1 / float(df['close'].iloc[0])
        for c in ('open', 'high', 'low', 'close'):
            df[c] = df[c] * scale
        df['volume'] = 0.0
        spec = BacktestSpec('ema_momentum', {'min_relvol': 0.0}, ['EUR/USD'], '5Min', 10000.0,
                            RiskConfig(leverage=10, max_position_pct=500, slippage_bps=0.3, min_reward_to_cost=2,
                                       fee_bps={'forex': 0.5}),
                            asset_classes={'EUR/USD': 'forex'}, qty_increments={'EUR/USD': 1})
        r = run_backtest(spec, {'EUR/USD': df})
        friday_close = et(2026, 8, 28, 17)
        for t in r.trades:
            self.assertLessEqual(t.exit_ts, friday_close)
        self.assertTrue(all(float(t.qty) == int(t.qty) for t in r.trades))  # whole units
