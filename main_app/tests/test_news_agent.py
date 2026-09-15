"""The News Agent: verdicts become orders, guardrails hold, backtests stay blind."""
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
from django.test import TestCase
from django.utils import timezone

from main_app.models import Instrument, NewsSession, NewsVerdict
from main_app.services import news_agent as na
from main_app.services.strategies import make_strategy
from main_app.services.strategies.base import Context


def _bars(n=60, price=100.0):
    idx = pd.date_range('2026-09-15 09:30', periods=n, freq='5min', tz='UTC')
    return pd.DataFrame({'open': price, 'high': price * 1.004, 'low': price * 0.996,
                         'close': price, 'volume': 10_000.0}, index=idx)


class VerdictsBecomeOrders(TestCase):
    def setUp(self):
        Instrument.objects.create(symbol='QQQ', asset_class='etf', market='stocks')
        Instrument.objects.create(symbol='BTC/USD', asset_class='crypto', market='crypto')
        self.session = NewsSession.objects.create(model='test')
        self.strat = make_strategy('news_catalyst', {})
        self.strat.live = True
        self.df = self.strat.prepare(_bars(), 'etf', '5Min')
        self.bar = list(self.df.itertuples())[-1]

    def _verdict(self, **kw):
        base = dict(session=self.session, headline='Yields top 5%', symbol='QQQ', market='stocks',
                    score=6, direction='short', thesis='rates up, long duration down', tradable=True)
        base.update(kw)
        return NewsVerdict.objects.create(**base)

    def _ctx(self, symbol='QQQ', asset_class='etf'):
        return Context(symbol=symbol, asset_class=asset_class, timeframe='5Min',
                       ts=self.df.index[-1].to_pydatetime())

    def test_a_score_of_six_produces_a_short(self):
        self._verdict()
        sigs = self.strat.on_bar(self._ctx(), self.bar, self.df, len(self.df) - 1)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].action, 'sell')
        self.assertGreater(sigs[0].stop, sigs[0].price)     # stop above for a short
        self.assertLess(sigs[0].target, sigs[0].price)

    def test_a_buy_call_produces_a_long(self):
        self._verdict(direction='buy', score=8)
        sigs = self.strat.on_bar(self._ctx(), self.bar, self.df, len(self.df) - 1)
        self.assertEqual(sigs[0].action, 'buy')
        self.assertLess(sigs[0].stop, sigs[0].price)

    def test_below_the_threshold_does_nothing(self):
        self._verdict(score=4)
        self.assertEqual(self.strat.on_bar(self._ctx(), self.bar, self.df, len(self.df) - 1), [])

    def test_one_verdict_can_only_fire_once(self):
        self._verdict()
        first = self.strat.on_bar(self._ctx(), self.bar, self.df, len(self.df) - 1)
        second = self.strat.on_bar(self._ctx(), self.bar, self.df, len(self.df) - 1)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [], 'the same verdict must not be traded twice')
        self.assertTrue(NewsVerdict.objects.get().acted)

    def test_a_backtest_never_sees_a_verdict(self):
        """Verdicts are written in the present; a backtest reading them is reading answers."""
        self._verdict()
        blind = make_strategy('news_catalyst', {})        # .live defaults False
        self.assertEqual(blind.on_bar(self._ctx(), self.bar, self.df, len(self.df) - 1), [])
        self.assertFalse(NewsVerdict.objects.get().acted)

    def test_it_does_not_add_to_an_open_position(self):
        from main_app.services.strategies.base import PositionView
        self._verdict()
        ctx = self._ctx()
        ctx.position = PositionView(qty=10, avg_price=100.0, entry_ts=timezone.now())
        self.assertEqual(self.strat.on_bar(ctx, self.bar, self.df, len(self.df) - 1), [])


class GuardrailsHoldEvenWhenTheModelIgnoresThem(TestCase):
    def setUp(self):
        Instrument.objects.create(symbol='BTC/USD', asset_class='crypto', market='crypto')
        self.session = NewsSession.objects.create(model='test')

    def test_a_short_on_spot_crypto_is_refused_at_write_time(self):
        # run_session applies this before the row is ever stored; assert the rule itself.
        self.assertFalse(na._shortable('crypto'))
        self.assertFalse(na._shortable('degen'))
        self.assertTrue(na._shortable('stocks'))
        self.assertTrue(na._shortable('forex'))

    def test_an_untradable_symbol_yields_no_instruction(self):
        NewsVerdict.objects.create(session=self.session, headline='Nikkei plunges', symbol='NIKKEI',
                                   market='', score=9, direction='short', tradable=False)
        self.assertIsNone(na.pending_for('NIKKEI'))


class InstructionsExpireOnTheirLanesClock(TestCase):
    def setUp(self):
        Instrument.objects.create(symbol='QQQ', asset_class='etf', market='stocks')
        Instrument.objects.create(symbol='SOL/USD', asset_class='crypto', market='degen')
        self.session = NewsSession.objects.create(model='test')

    def _aged(self, symbol, market, hours):
        v = NewsVerdict.objects.create(session=self.session, headline='x', symbol=symbol,
                                       market=market, score=7, direction='buy', tradable=True)
        NewsVerdict.objects.filter(pk=v.pk).update(created_at=timezone.now() - timedelta(hours=hours))
        return v

    def test_an_evening_call_on_stocks_survives_the_overnight_close(self):
        self._aged('QQQ', 'stocks', hours=14)
        self.assertIsNotNone(na.pending_for('QQQ'),
                             'a 4-hour window would discard every evening call on stocks')

    def test_a_stale_call_is_not_an_order(self):
        self._aged('QQQ', 'stocks', hours=30)
        self.assertIsNone(na.pending_for('QQQ'))

    def test_the_fast_lane_expires_faster(self):
        self._aged('SOL/USD', 'degen', hours=6)
        self.assertIsNone(na.pending_for('SOL/USD'))


class TheScoreboardJudgesTheAgent(TestCase):
    def test_outcomes_are_signed_for_the_direction_called(self):
        s = NewsSession.objects.create(model='test')
        NewsVerdict.objects.create(session=s, headline='up', symbol='QQQ', market='stocks', score=7,
                                   direction='buy', tradable=True, outcome_pct=2.0,
                                   outcome_at=timezone.now())
        NewsVerdict.objects.create(session=s, headline='down', symbol='QQQ', market='stocks', score=7,
                                   direction='short', tradable=True, outcome_pct=1.5,
                                   outcome_at=timezone.now())
        board = na.scoreboard()
        self.assertEqual(board[0]['score'], 7)
        self.assertEqual(board[0]['n'], 2)
        self.assertEqual(board[0]['hit_rate'], 100.0)      # both right, one long one short

    def test_call_text_reads_like_an_instruction(self):
        s = NewsSession.objects.create(model='test')
        v = NewsVerdict.objects.create(session=s, headline='x', symbol='NZD/USD', market='forex',
                                       score=6, direction='short', tradable=True)
        self.assertEqual(v.call_text, 'SHORT NZD/USD')
        quiet = NewsVerdict.objects.create(session=s, headline='y', score=2, direction='none')
        self.assertEqual(quiet.call_text, 'NO ACTION')
