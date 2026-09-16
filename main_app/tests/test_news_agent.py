"""The News Agent: verdicts become orders, guardrails hold, backtests stay blind."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

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
        # Emitting a signal LEASES the instruction; it is not spent until a broker
        # takes the order. Risk blocks two thirds of signals, and the first version
        # marked the verdict acted here — so a thesis was forgotten because the
        # account happened to be at its position limit that minute.
        v = NewsVerdict.objects.get()
        self.assertEqual(v.lease_state, 'leased')
        self.assertFalse(v.acted)

    def test_a_blocked_signal_hands_the_instruction_back(self):
        self._verdict()
        sigs = self.strat.on_bar(self._ctx(), self.bar, self.df, len(self.df) - 1)
        self.strat.on_signal_blocked(sigs[0], 'daily loss limit reached')
        v = NewsVerdict.objects.get()
        self.assertEqual(v.lease_state, 'available')
        self.assertFalse(v.acted)
        self.assertIn('daily loss', v.blocked_reason)
        self.assertIsNotNone(na.pending_for('QQQ'), 'it must be offered again next bar')

    def test_an_accepted_signal_spends_the_instruction(self):
        self._verdict()
        sigs = self.strat.on_bar(self._ctx(), self.bar, self.df, len(self.df) - 1)
        self.strat.on_signal_accepted(sigs[0])
        v = NewsVerdict.objects.get()
        self.assertEqual(v.lease_state, 'consumed')
        self.assertTrue(v.acted)
        self.assertIsNotNone(v.acted_at)

    def test_a_second_worker_cannot_take_a_leased_event(self):
        v = self._verdict()
        other = make_strategy('news_catalyst', {})
        other.live = True
        self.assertTrue(na.claim(v, owner='worker-a'))
        self.assertFalse(na.claim(NewsVerdict.objects.get(), owner='worker-b'),
                         'two workers must not hold one event')

    def test_an_abandoned_lease_comes_back(self):
        v = self._verdict()
        na.claim(v, owner='crashed')
        NewsVerdict.objects.filter(pk=v.pk).update(
            lease_expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(na.expire_leases(), 1)
        self.assertEqual(NewsVerdict.objects.get().lease_state, 'available')

    def test_consuming_one_call_collapses_the_rest_of_the_stack(self):
        keep = self._verdict(headline='rates up')
        also = self._verdict(headline='yields spike again')
        na.claim(keep)
        na.consume(keep)
        self.assertEqual(NewsVerdict.objects.get(pk=also.pk).lease_state, 'consumed',
                         'four sittings seeing one story must not become four shorts')

    def test_a_symbol_cools_off_after_it_trades(self):
        first = self._verdict()
        na.claim(first)
        na.consume(first)
        self._verdict(headline='a different rates story')
        self.assertIsNone(na.pending_for('QQQ'),
                          'one instruction per symbol per hold, however many stories arrive')

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
    def _graded(self, session, **kw):
        base = dict(session=session, headline='x', symbol='QQQ', market='stocks', score=7,
                    direction='buy', tradable=True, outcome_at=timezone.now(),
                    outcome_kind='target', outcome_atr_net=1.8, outcome_pct=2.0)
        base.update(kw)
        return NewsVerdict.objects.create(**base)

    def test_outcomes_are_signed_for_the_direction_called(self):
        s = NewsSession.objects.create(model='test')
        self._graded(s, headline='up', direction='buy', outcome_pct=2.0)
        self._graded(s, headline='down', direction='short', outcome_pct=1.5)
        board = na.scoreboard()
        self.assertEqual(board[0]['score'], 7)
        self.assertEqual(board[0]['n'], 2)
        self.assertEqual(board[0]['hit_rate'], 100.0)      # both reached target, one long one short

    def test_rebuilt_history_is_never_counted_as_evidence(self):
        s = NewsSession.objects.create(model='test')
        self._graded(s, provenance='reconstructed')
        self.assertEqual(na.scoreboard(), [],
                         'an outcome rebuilt from bars is not a forecast recorded before the fact')
        self.assertEqual(len(na.scoreboard(provenance='reconstructed')), 1)

    def test_the_control_group_is_kept(self):
        """A losing 3/10 is what makes a winning 6/10 mean something."""
        s = NewsSession.objects.create(model='test')
        self._graded(s, score=3, outcome_kind='stop', outcome_atr_net=-1.6, outcome_pct=-1.1)
        self._graded(s, score=6)
        board = {row['score']: row for row in na.scoreboard()}
        self.assertEqual(sorted(board), [3, 6])
        self.assertLess(board[3]['avg_atr_net'], board[6]['avg_atr_net'])

    def test_call_text_reads_like_an_instruction(self):
        s = NewsSession.objects.create(model='test')
        v = NewsVerdict.objects.create(session=s, headline='x', symbol='NZD/USD', market='forex',
                                       score=6, direction='short', tradable=True)
        self.assertEqual(v.call_text, 'SHORT NZD/USD')
        quiet = NewsVerdict.objects.create(session=s, headline='y', score=2, direction='none')
        self.assertEqual(quiet.call_text, 'NO ACTION')


class TheNewsArmHasItsOwnLimits(TestCase):
    """Preregistered, machine-enforced, and nobody is asked for permission."""

    def setUp(self):
        from main_app.models import Account, AgentConfig
        Instrument.objects.create(symbol='AAPL', asset_class='stock', market='stocks')
        self.cfg = AgentConfig.get()
        self.account = Account.objects.create(mode='sim', market='stocks',
                                              starting_cash=10000, cash=10000)
        self.account.equity = 10000.0

    def _pos(self, qty, value):
        class P:
            def __init__(self, qty, value):
                self.qty = qty
                self._v = value

            def market_value(self):
                return self._v
        return P(qty, value)

    def test_it_allows_an_ordinary_entry(self):
        from main_app.services.news_risk import check
        self.assertEqual(check(self.account, 'AAPL', 'buy', {}), '')

    def test_three_positions_on_one_side_is_the_limit(self):
        from main_app.services.news_risk import check
        book = {s: self._pos(10, 100.0) for s in ('NVDA', 'MSFT', 'META')}
        self.assertIn('same side', check(self.account, 'AAPL', 'buy', book))

    def test_correlated_exposure_is_capped_across_names_that_move_together(self):
        from main_app.services.news_risk import check
        # Two positions, each individually modest, together over 40% of equity.
        book = {'NVDA': self._pos(10, 2500.0), 'MSFT': self._pos(10, 2000.0)}
        self.assertIn('correlated exposure', check(self.account, 'AAPL', 'buy', book))

    def test_a_weekly_loss_halts_the_arm_and_stays_halted(self):
        from main_app.models import RiskEvent
        from main_app.services.news_risk import check
        self._losing_trade(Decimal('-300'))           # 3% of a 10,000 account
        first = check(self.account, 'AAPL', 'buy', {})
        self.assertIn('weekly loss', first)
        self.assertTrue(RiskEvent.objects.filter(kind='news_halt').exists())
        # Sticky: it does not clear just because the next bar arrived.
        self.assertIn('halted for the week', check(self.account, 'AAPL', 'buy', {}))

    def test_ten_losses_in_a_row_halts_the_arm(self):
        from main_app.services.news_risk import check
        for _ in range(10):
            self._losing_trade(Decimal('-1'))
        self.assertIn('in a row', check(self.account, 'AAPL', 'buy', {}))

    def _losing_trade(self, pnl):
        from main_app.models import Trade
        inst = Instrument.objects.get(symbol='AAPL')
        Trade.objects.create(account=self.account, instrument=inst, strategy_key='news_catalyst',
                             side='long', qty=1, entry_ts=timezone.now(), exit_ts=timezone.now(),
                             entry_price=Decimal('100'), exit_price=Decimal('99'), pnl=pnl,
                             pnl_pct=Decimal('-1'), fees=Decimal('0'), bars_held=1,
                             exit_reason='stop')


class ShortsAreCheckedAtTheVenue(TestCase):
    def test_an_unanswerable_borrow_question_is_a_no(self):
        """Failing open here would mean being short something nobody can cover."""
        from main_app.services.broker.alpaca import AlpacaBroker

        class Exploding:
            def get_asset(self, symbol):
                raise RuntimeError('network down')
        broker = AlpacaBroker.__new__(AlpacaBroker)
        broker.client, broker.asset_classes = Exploding(), {}
        ok, why = broker.can_short('AAPL')
        self.assertFalse(ok)
        self.assertIn('could not confirm', why)

    def test_hard_to_borrow_is_refused(self):
        from main_app.services.broker.alpaca import AlpacaBroker

        class Asset:
            tradable, shortable, easy_to_borrow = True, True, False

        class Client:
            def get_asset(self, symbol):
                return Asset()
        broker = AlpacaBroker.__new__(AlpacaBroker)
        broker.client, broker.asset_classes = Client(), {}
        ok, why = broker.can_short('AAPL')
        self.assertFalse(ok)
        self.assertIn('hard to borrow', why)
