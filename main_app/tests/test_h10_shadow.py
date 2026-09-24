"""The forward shadow record must accumulate honestly.

Two ways a forward record quietly lies, both guarded here. It double-counts,
because a job that runs twice appends twice and the number grows when you look at
it. And it books open positions, because the backtest driver flattens whatever is
still open at the final bar — a mark to market wearing an exit's clothes. At n=8
either one is the difference between a positive and a negative record.
"""
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from main_app.management.commands.h10_shadow import BOUNDARY_EXIT, summarise, trade_key
from main_app.services.strategies.fx_trend import H10_RISK, H10_SPEC


class _Trade:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class ATradeKeyIdentifiesTheRoundTrip(SimpleTestCase):
    """The key is what makes a re-run idempotent, so it has to be a function of
    the trade alone — never of when it was observed."""

    def _t(self, **over):
        base = dict(symbol='EUR/USD', strategy_key='fx_trend',
                    entry_ts=datetime(2026, 9, 25, 8, tzinfo=timezone.utc),
                    exit_ts=datetime(2026, 9, 26, 8, tzinfo=timezone.utc))
        return _Trade(**{**base, **over})

    def test_the_same_trade_yields_the_same_key(self):
        self.assertEqual(trade_key(self._t()), trade_key(self._t()))

    def test_each_field_changes_the_key(self):
        for field, value in (('symbol', 'GBP/USD'), ('strategy_key', 'ema_momentum'),
                             ('entry_ts', datetime(2026, 9, 25, 9, tzinfo=timezone.utc)),
                             ('exit_ts', datetime(2026, 9, 27, 8, tzinfo=timezone.utc))):
            self.assertNotEqual(trade_key(self._t()), trade_key(self._t(**{field: value})),
                                f'two different trades collide on {field}')

    def test_two_entries_on_one_pair_in_one_hour_are_still_distinct(self):
        """Same symbol, same entry bar, different exits — the case a key built
        from symbol and entry alone would silently merge into one."""
        a = self._t()
        b = self._t(exit_ts=datetime(2026, 9, 30, 8, tzinfo=timezone.utc))
        self.assertNotEqual(trade_key(a), trade_key(b))


class TotalsScaleCostsAndNotTheMove(SimpleTestCase):
    def _rows(self):
        return [{'pnl': 40.0, 'fees': -2.0, 'notional': 25000.0, 'exit_reason': 'stop'},
                {'pnl': -25.0, 'fees': -2.0, 'notional': 25000.0, 'exit_reason': 'target'}]

    def test_gross_is_the_same_at_every_cost_multiple(self):
        """Costs are what a multiplier is allowed to move. If gross drifts, the
        multiplier is being applied to the price move, which would make a bad
        strategy look robust to costs it never paid."""
        g = {m: summarise(self._rows(), 0.8, m)['gross'] for m in (1.0, 2.0, 3.0)}
        self.assertEqual(len(set(g.values())), 1, f'gross moved with cost: {g}')

    def test_higher_costs_never_improve_the_net(self):
        nets = [summarise(self._rows(), 0.8, m)['net'] for m in (1.0, 2.0, 3.0)]
        self.assertEqual(nets, sorted(nets, reverse=True), nets)

    def test_an_empty_record_is_zero_and_not_a_crash(self):
        self.assertEqual(summarise([], 0.8)['trades'], 0)


class TheShadowRecordAccumulatesWithoutDoubleCounting(TestCase):
    WARMUP = 900

    def setUp(self):
        from main_app.models import Bar, Instrument
        self.inst = (Instrument.objects.filter(symbol='EUR/USD').first()
                     or Instrument.objects.create(symbol='EUR/USD', asset_class='forex',
                                                  market='forex'))
        self.base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        px = 1.10
        for k in range(self.WARMUP + 900):
            leg = (k // 150) % 2
            px += 0.0012 if leg == 0 else -0.0012
            Bar.objects.create(instrument=self.inst, timeframe='1Hour',
                               ts=self.base + timedelta(hours=k), open=px,
                               high=px + 0.0006, low=px - 0.0006, close=px, volume=1000)
        self.start = self.base + timedelta(hours=self.WARMUP)
        self.tmp = Path(tempfile.mkdtemp()) / 'shadow.json'

    def _run(self):
        call_command('h10_shadow', start=self.start.date().isoformat(), out=str(self.tmp),
                     quiet=True, verbosity=0)
        return json.loads(self.tmp.read_text())

    def test_the_fixture_actually_records_trades(self):
        """Guards the guard: an empty record would make every assertion below
        vacuous, which is exactly how a window test on this desk once passed
        while proving nothing."""
        self.assertGreater(len(self._run()['trades']), 0,
                           'no trades recorded — the idempotency test proves nothing')

    def test_running_twice_does_not_change_the_record(self):
        first = self._run()
        second = self._run()
        self.assertGreater(len(first['trades']), 0)
        self.assertEqual(sorted(first['trades']), sorted(second['trades']),
                         'a second run changed the set of recorded trades')
        self.assertEqual(second['runs'][-1]['added'], 0,
                         'the second run claimed to add trades it had already recorded')

    def test_the_totals_do_not_move_on_a_re_run(self):
        first = self._run()['totals']['1x']
        second = self._run()['totals']['1x']
        self.assertEqual(first, second, 'the headline numbers changed without new data')

    def test_an_open_position_is_reported_in_flight_and_never_recorded(self):
        """The load-bearing one. `run_frames` flattens what is open at the last
        bar and stamps it `end`; booking that marks an unrealised position to
        market and calls it a result.

        Proven by truncating the data mid-trade rather than by asserting the
        filter against itself: take the longest-held recorded trade, delete every
        bar after two hours past its entry, and require that the same trade now
        shows as in flight and is absent from the record.

        The longest hold, not the latest entry — the first version of this test
        truncated six hours after whichever trade entered last, and that trade had
        already stopped out inside the six hours, so nothing was open and the test
        failed for a reason that had nothing to do with the rule it guards.
        """
        from main_app.models import Bar
        full = self._run()
        victim = max(full['trades'].values(), key=lambda r: r['bars_held'])
        self.assertGreater(victim['bars_held'], 4,
                           'no trade is held long enough to be truncated mid-flight')
        entry = datetime.fromisoformat(victim['entry_ts'])
        Bar.objects.filter(instrument=self.inst, timeframe='1Hour',
                           ts__gt=entry + timedelta(hours=2)).delete()

        self.tmp.unlink()
        rec = self._run()
        self.assertEqual(len(rec['in_flight']), 1, 'no position was open at the truncated end')
        self.assertEqual(rec['in_flight'][0]['entry_ts'], victim['entry_ts'])
        for row in rec['trades'].values():
            self.assertNotEqual(row['exit_reason'], BOUNDARY_EXIT,
                                'a boundary flatten was recorded as a completed trade')
            self.assertNotEqual(row['entry_ts'], victim['entry_ts'],
                                'an open position was booked as a round trip')

    def test_the_record_pins_the_spec_it_was_produced_under(self):
        """A record whose spec is not written down cannot be audited later, and
        a silently retuned spec would extend the same record under a different
        hypothesis."""
        rec = self._run()
        self.assertEqual(rec['spec'], dict(H10_SPEC))
        self.assertEqual(rec['risk'], dict(H10_RISK))
