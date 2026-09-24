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

from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from main_app.management.commands.h10_shadow import (BOUNDARY_EXIT, summarise, trade_key,
                                                     write_record)
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


class ACostMultipleMayOnlyMakeTheRecordWorse(SimpleTestCase):
    """The inverted-PF bug, pinned.

    The first version built each trade as `pnl + fees*m + slip`. Because `pnl` is
    already net of 1x cost, raising the multiple ADDED cost back: profit factor on
    the real spent window climbed 0.42 -> 0.486 -> 0.561 as costs rose. That is the
    single number a promotion decision reads, reported backwards.

    It survived because a test asserted net falls with cost and nothing asserted PF
    does. Both are here now, and so is the reason they are different questions:
    gross is the signal before any toll and must not move at all.
    """
    MULTIPLES = (1.0, 2.0, 3.0, 10.0)

    def _rows(self):
        # Positive fees: TradeRecord stores cost as a positive number, so
        # gross = net + fees. A negative fixture here would have hidden the sign.
        return [{'pnl': 40.0, 'fees': 2.0, 'notional': 25000.0, 'exit_reason': 'stop'},
                {'pnl': -25.0, 'fees': 2.0, 'notional': 25000.0, 'exit_reason': 'target'}]

    def test_profit_factor_never_improves_as_costs_rise(self):
        pf = [summarise(self._rows(), 0.8, m)['net_pf'] for m in self.MULTIPLES]
        self.assertEqual(pf, sorted(pf, reverse=True),
                         f'higher costs improved the profit factor: {pf}')
        self.assertLess(pf[-1], pf[0], 'costs made no difference to PF at all')

    def test_net_never_improves_as_costs_rise(self):
        nets = [summarise(self._rows(), 0.8, m)['net'] for m in self.MULTIPLES]
        self.assertEqual(nets, sorted(nets, reverse=True), nets)

    def test_gross_is_identical_at_every_cost_multiple(self):
        """If gross moves with the multiplier, the multiplier is leaking into the
        price move and a bad strategy looks robust to costs it never paid."""
        for field in ('gross', 'gross_pf', 'bps_captured'):
            seen = {summarise(self._rows(), 0.8, m)[field] for m in self.MULTIPLES}
            self.assertEqual(len(seen), 1, f'{field} moved with cost: {seen}')

    def test_at_1x_the_net_is_exactly_what_the_trades_made(self):
        """The anchor. 1x must reproduce the recorded P&L with nothing added or
        removed, or every multiple above it is measured from the wrong place."""
        self.assertAlmostEqual(summarise(self._rows(), 0.8)['net'],
                               sum(r['pnl'] for r in self._rows()), places=2)

    def test_costs_rise_with_the_multiple(self):
        for field in ('fees', 'slippage'):
            vals = [summarise(self._rows(), 0.8, m)[field] for m in self.MULTIPLES]
            self.assertEqual(vals, sorted(vals), f'{field} did not rise with cost: {vals}')

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


class TheRecordAcceptsOneWindowAndOneSpec(SimpleTestCase):
    """Evidence that silently changes what it is evidence OF.

    Two ways in. `--start 2026-09-08` with the default output would append the
    window MT-A001 already spent to the forward record, so an n and a PF would
    describe two windows while reading as one. And a retuned H10_SPEC would extend
    the same file under a different hypothesis — the same class of mistake as the
    `evidence_since` reset that erased burst's earned quarantine.

    SimpleTestCase on purpose: these guards must fire before any bar is loaded, so
    a refusal cannot depend on the database being in any particular state.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.tmp = self.dir / 'scratch.json'

    def _open_at(self, iso_date):
        """Open a scratch record pinned to a given window, without measuring."""
        rec = {'forward_start': f'{iso_date}T00:00:00+00:00', 'spec': dict(H10_SPEC),
               'risk': dict(H10_RISK), 'pairs': [], 'timeframe': '1Hour',
               'trades': {}, 'runs': []}
        self.tmp.write_text(json.dumps(rec))
        return rec

    def test_the_committed_record_refuses_any_window_but_its_own(self):
        """Read-only: the guard fires before the real record is opened for writing."""
        with self.assertRaises(CommandError) as cm:
            call_command('h10_shadow', start='2026-09-08', verbosity=0)
        self.assertIn('pinned to', str(cm.exception))
        self.assertIn('scratch path', str(cm.exception))

    def test_an_existing_record_refuses_a_different_start(self):
        self._open_at('2026-09-25')
        with self.assertRaises(CommandError) as cm:
            call_command('h10_shadow', start='2026-09-08', out=str(self.tmp), verbosity=0)
        self.assertIn('two windows in one n', str(cm.exception))

    def test_a_record_pinned_to_a_different_spec_is_refused(self):
        rec = self._open_at('2026-09-25')
        rec['spec'] = dict(H10_SPEC, stop_atr_mult=2.5)      # as if the spec were retuned
        self.tmp.write_text(json.dumps(rec))
        with self.assertRaises(CommandError) as cm:
            call_command('h10_shadow', start='2026-09-25', out=str(self.tmp), verbosity=0)
        self.assertIn('different hypothesis', str(cm.exception))

    def test_a_record_pinned_to_different_risk_is_refused(self):
        rec = self._open_at('2026-09-25')
        rec['risk'] = dict(H10_RISK, max_hold_minutes=1440)
        self.tmp.write_text(json.dumps(rec))
        with self.assertRaises(CommandError):
            call_command('h10_shadow', start='2026-09-25', out=str(self.tmp), verbosity=0)

    def test_a_refused_run_leaves_the_record_exactly_as_it_was(self):
        """Refusing is only half of it. A guard that raised after touching the file
        would still have damaged the evidence it was protecting."""
        self._open_at('2026-09-25')
        before = self.tmp.read_bytes()
        with self.assertRaises(CommandError):
            call_command('h10_shadow', start='2026-09-08', out=str(self.tmp), verbosity=0)
        self.assertEqual(self.tmp.read_bytes(), before, 'a refused run modified the record')


class AFreshScratchRecordMayBeOpenedOnAnyWindow(TestCase):
    """The positive control for the guards above.

    Without it those tests would pass just as well if the command refused
    everything. A scratch path is how any other window gets measured, so it has to
    stay open — it is only the committed record that is pinned.
    """

    def test_a_fresh_scratch_file_pins_the_window_it_was_opened_on(self):
        folder = Path(tempfile.mkdtemp())
        fresh = folder / 'fresh.json'
        call_command('h10_shadow', start='2026-09-08', out=str(fresh), verbosity=0)
        rec = json.loads(fresh.read_text())
        self.assertEqual(rec['forward_start'], '2026-09-08T00:00:00+00:00')
        self.assertEqual(rec['spec'], dict(H10_SPEC), 'the live spec was not pinned into it')

    def test_reopening_that_scratch_file_on_its_own_window_is_allowed(self):
        folder = Path(tempfile.mkdtemp())
        fresh = folder / 'fresh.json'
        call_command('h10_shadow', start='2026-09-08', out=str(fresh), verbosity=0)
        call_command('h10_shadow', start='2026-09-08', out=str(fresh), verbosity=0)


class TheRecordIsWrittenAtomically(SimpleTestCase):
    """`open(path, 'w')` truncates before it writes.

    This runs from the 02:00 job under a `kill -9` watchdog, and that job then
    commits and pushes whatever is on disk — so a mid-write kill would publish a
    truncated record and the next run would fail to parse its own evidence.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.tmp = self.dir / 'rec.json'
        self.good = {'forward_start': '2026-09-25T00:00:00+00:00', 'trades': {'a': 1}}
        write_record(str(self.tmp), self.good)

    def test_a_crash_mid_write_leaves_the_previous_record_intact(self):
        import main_app.management.commands.h10_shadow as mod

        def die(*a, **k):
            raise KeyboardInterrupt('watchdog kill -9')

        with mock.patch.object(mod.json, 'dump', side_effect=die):
            with self.assertRaises(KeyboardInterrupt):
                write_record(str(self.tmp), {'trades': {'a': 1, 'b': 2}})
        self.assertEqual(json.loads(self.tmp.read_text()), self.good,
                         'a failed write damaged the record that was already there')

    def test_a_crash_leaves_no_temp_file_behind(self):
        import main_app.management.commands.h10_shadow as mod
        with mock.patch.object(mod.json, 'dump', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                write_record(str(self.tmp), {'trades': {}})
        leftover = [p.name for p in self.dir.iterdir() if p.name != 'rec.json']
        self.assertEqual(leftover, [], f'temp files left behind: {leftover}')

    def test_a_successful_write_replaces_the_content(self):
        write_record(str(self.tmp), {'trades': {'c': 3}})
        self.assertEqual(json.loads(self.tmp.read_text()), {'trades': {'c': 3}})
