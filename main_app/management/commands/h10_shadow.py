"""Accumulate forward evidence for H10, one night at a time.

H10 has a positive held-out backtest and no forward record. The existing
promotion gate wants roughly 30 trades after costs and H10 makes about eight a
fortnight, so nothing but elapsed time produces that evidence — more code cannot.
This starts the clock and keeps it honest.

It is SHADOW: it places no orders, enables no Strategy row, and touches nothing
the running agents read. It replays the frozen H10_SPEC over bars that closed
after the forward start and appends completed round trips to a committed JSON
artifact.

Four rules, because this file is what a promotion or a rejection will rest on and
each of these is a way such a record quietly flatters itself:

  * Only COMPLETED round trips are recorded. `run_frames` flattens whatever is
    open at the final bar and stamps it `exit_reason='end'` — that is the
    backtest boundary, not an exit the strategy chose, and booking it would mean
    marking an open position to market and calling it a result. Those are
    reported separately as in flight and enter the record only once they close
    for a real reason.
  * Appends are idempotent on a deterministic key, so a re-run, a crashed run,
    or two runs in one night cannot double-count. A record that grows when you
    look at it is not a record.
  * The record accepts ONE window and ONE spec. A different `--start`, or a
    retuned H10_SPEC, is a different hypothesis and is refused rather than
    silently appended to the same evidence.
  * Writes are atomic. The nightly job runs this under a kill -9 watchdog and
    then commits and pushes whatever is on disk.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand, CommandError

from main_app.services.strategies import make_strategy
from main_app.services.strategies.fx_trend import (H10_PAIRS, H10_RISK, H10_SPEC, H10_TIMEFRAME)

# Chosen so the shadow can never overlap the window MT-A001 spent. That window is
# burnt: it has been looked at, and looking again would make it a selection set.
FORWARD_START = datetime(2026, 9, 25, tzinfo=timezone.utc)
ARTIFACT = 'docs/h10-shadow.json'

# The backtest driver's own end-of-run flatten. Not an exit the strategy chose.
BOUNDARY_EXIT = 'end'


def trade_key(t) -> str:
    """Identity of a round trip, independent of when it was observed."""
    return f'{t.symbol}|{t.entry_ts.isoformat()}|{t.exit_ts.isoformat()}|{t.strategy_key}'


def load_record(path: str) -> dict:
    if not os.path.exists(path):
        return {'forward_start': FORWARD_START.isoformat(), 'spec': dict(H10_SPEC),
                'risk': dict(H10_RISK), 'pairs': list(H10_PAIRS), 'timeframe': H10_TIMEFRAME,
                'trades': {}, 'runs': []}
    with open(path) as fh:
        return json.load(fh)


def write_record(path: str, rec: dict) -> None:
    """Write through a temp file in the same directory, then `os.replace`.

    `open(path, 'w')` truncates before it writes anything. This runs from the
    02:00 job under a `kill -9` watchdog, and that job then commits and pushes
    whatever is on disk — so a mid-write kill would publish a truncated record and
    the next run would fail to parse its own evidence. `os.replace` is atomic
    within a filesystem: a reader sees either the old record or the new one.
    """
    from main_app.services.research_window import merge_artifact
    # The accepted measurement is the trades and their totals. `runs` is an
    # append-only log and is deliberately not a stamp: appending an entry is
    # provenance, rewriting or dropping one is not.
    rec = merge_artifact(path, rec, measured=lambda d: (d.get('trades'), d.get('totals')),
                         stamps=('last_run', 'window', 'slippage_bps'))
    folder = os.path.dirname(os.path.abspath(path)) or '.'
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.h10-shadow-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as fh:
            json.dump(rec, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _per_trade(t: dict, slippage_bps: float) -> tuple[float, float]:
    """`(gross pnl, round-trip cost at 1x)` for one recorded round trip.

    Slippage sits inside the fill price, so `pnl` is already net of it and has to
    be added back to see the raw edge. It is charged on entry always and on exit
    only when that leg is not a limit fill, since a target rests at its level.
    """
    legs = 1 + (0 if t['exit_reason'] == 'target' else 1)
    cost = t['fees'] + t['notional'] * legs * slippage_bps / 1e4
    return t['pnl'] + cost, cost


def _pf(values: list[float]) -> float | None:
    won = sum(v for v in values if v > 0)
    lost = -sum(v for v in values if v <= 0)
    return round(won / lost, 3) if lost else None


def summarise(trades: list[dict], slippage_bps: float, cost_mult: float = 1.0) -> dict:
    """Totals over recorded round trips. Costs scale; the price move does not.

    A cost multiple may only ever make the record worse. The first version of this
    built each trade as `pnl + fees*m + slip`, which ADDS cost back as the multiple
    rises: profit factor climbed 0.42 → 0.486 → 0.561 with costs, exactly inverted,
    on the one figure a promotion decision reads. It survived because there was a
    test that net falls with cost and none that PF does. There is now.

    Two profit factors are reported because they answer different questions.
    `gross_pf` is the signal with no toll charged and is identical at every
    multiple — if it ever moves with `cost_mult`, the multiplier is leaking into
    the price move. `net_pf` is what the desk would have kept and must fall.

    Note `net` at 2x/3x is NOT the same quantity as h10_forward's rows of that
    name. That command re-runs the backtest with higher costs, so a wider spread
    can move a fill or trip a different exit. A forward record cannot do that —
    these trades already happened — so the higher cost is charged against the fills
    that actually occurred. Compare 1x figures, not these.
    """
    if not trades:
        return {'trades': 0, 'gross': 0.0, 'net': 0.0, 'gross_pf': None, 'net_pf': None}
    rows = [_per_trade(t, slippage_bps) for t in trades]
    gross = sum(g for g, _ in rows)
    cost = sum(c for _, c in rows) * cost_mult
    fees = sum(t['fees'] for t in trades) * cost_mult
    nets = [g - c * cost_mult for g, c in rows]
    notional = sum(t['notional'] for t in trades)
    return {
        'trades': len(trades), 'gross': round(gross, 2), 'fees': round(fees, 2),
        'slippage': round(cost - fees, 2), 'net': round(sum(nets), 2),
        'gross_pf': _pf([g for g, _ in rows]), 'net_pf': _pf(nets),
        'bps_captured': round(gross / notional * 1e4, 2) if notional else None,
        'bps_cost': round(cost / notional * 1e4, 2) if notional else None,
    }


class Command(BaseCommand):
    help = "Record H10's forward trades on newly closed bars. Places no orders."

    def add_arguments(self, parser):
        parser.add_argument('--start', default=FORWARD_START.date().isoformat())
        parser.add_argument('--out', default=ARTIFACT)
        parser.add_argument('--quiet', action='store_true')

    def handle(self, *args, **o):
        from main_app.management.commands.h10_forward import _window_bounds
        from main_app.services.research_window import (Window, inclusive_through,
                                                       research_frames, run_window)

        start = datetime.fromisoformat(o['start']).replace(tzinfo=timezone.utc)
        is_record = os.path.abspath(o['out']) == os.path.abspath(ARTIFACT)
        existed = os.path.exists(o['out'])
        rec = load_record(o['out'])
        if not existed:
            # A fresh file pins whatever window it was opened on, so a scratch run
            # can measure any period. The committed record is pinned by the
            # constant below and cannot be reopened on a different one.
            rec['forward_start'] = start.isoformat()

        # --- the record accepts one window and one spec ----------------------
        # Both refusals guard the same thing: evidence that silently changes what
        # it is evidence OF. Appending the spent 09-08..24 window, or extending the
        # file after H10_SPEC was retuned, would leave a record whose n and PF look
        # like one hypothesis while describing two.
        if is_record and start != FORWARD_START:
            raise CommandError(
                f'refusing to write the committed record on {start:%Y-%m-%d}: it is pinned to '
                f'{FORWARD_START:%Y-%m-%d}. Measure another window with --out to a scratch path.')
        if rec.get('forward_start') != start.isoformat():
            raise CommandError(
                f'refusing to extend a record opened on {rec.get("forward_start")} with a run '
                f'starting {start.isoformat()} — that would be two windows in one n.')
        if rec.get('spec') != dict(H10_SPEC) or rec.get('risk') != dict(H10_RISK):
            raise CommandError(
                'refusing to extend this record: the live H10_SPEC/H10_RISK no longer matches the '
                'spec pinned in it. A retuned spec is a different hypothesis and needs its own '
                f'record.\n  pinned: {rec.get("spec")} / {rec.get("risk")}\n'
                f'  live  : {dict(H10_SPEC)} / {dict(H10_RISK)}')

        if not existed:
            # Persist the skeleton only once the guards pass, so a refused run
            # never leaves a record behind. From here on the file exists carrying
            # the spec it will be judged under, and nobody can claim later that it
            # was opened after a good first week.
            write_record(o['out'], rec)
            self.stdout.write(f'opened an empty forward record at {o["out"]}')

        try:
            start, end = _window_bounds(start)
        except SystemExit as exc:
            self.stderr.write(str(exc))
            return
        if end <= start:
            self.stdout.write(f'no bars yet after {start:%Y-%m-%d} — nothing to record')
            return

        need = make_strategy('fx_trend', dict(H10_SPEC)).warmup_bars
        warm_from = (start - timedelta(days=max(120, need // 4))).date()
        # Half-open: `end` is the latest closed bar, so the window runs through it
        # and stops. research_frames cuts before quality_gate ever sees the tail.
        end = inclusive_through(end)
        frames = research_frames(H10_PAIRS, H10_TIMEFRAME,
                                 Window(warmup_start=warm_from, start=start, end=end))
        available = min(int((df.index < start).sum()) for df in frames.values())
        if available < need:
            self.stderr.write(f'warm-up too short ({available} bars, need {need}) — not recording, '
                              f'because silence would look like "no trades"')
            return

        all_trades, slip = run_window('fx_trend', dict(H10_SPEC), dict(H10_RISK), frames, start, end,
                                      timeframe=H10_TIMEFRAME, pairs=H10_PAIRS)
        done = [t for t in all_trades if t.exit_reason != BOUNDARY_EXIT]
        in_flight = [t for t in all_trades if t.exit_reason == BOUNDARY_EXIT]

        before = len(rec['trades'])
        for t in done:
            rec['trades'][trade_key(t)] = {
                'symbol': t.symbol, 'side': t.side, 'qty': float(t.qty),
                'entry_ts': t.entry_ts.isoformat(), 'exit_ts': t.exit_ts.isoformat(),
                'entry_price': float(t.entry_price), 'exit_price': float(t.exit_price),
                'pnl': float(t.pnl), 'fees': float(t.fees),
                'notional': float(t.entry_price) * float(t.qty),
                'exit_reason': t.exit_reason, 'bars_held': int(t.bars_held),
            }
        added = len(rec['trades']) - before
        rows = list(rec['trades'].values())

        rec['last_run'] = datetime.now(timezone.utc).isoformat()
        rec['window'] = [start.isoformat(), end.isoformat()]
        rec['slippage_bps'] = slip
        rec['in_flight'] = [{'symbol': t.symbol, 'side': t.side,
                             'entry_ts': t.entry_ts.isoformat()} for t in in_flight]
        rec['totals'] = {'1x': summarise(rows, slip), '2x': summarise(rows, slip, 2.0),
                         '3x': summarise(rows, slip, 3.0)}
        # Append-only, one entry per invocation including a re-run that adds
        # nothing — that is how a missed nightly run is told apart from a night
        # where nothing happened. It used to keep only the last 19, which dropped
        # existing entries; a log that forgets is not a log.
        rec['runs'] = (rec.get('runs') or []) + [
            {'at': rec['last_run'], 'window_end': end.isoformat(), 'added': added,
             'total': len(rows), 'in_flight': len(in_flight)}]
        write_record(o['out'], rec)

        if o.get('verbosity', 1) == 0 or (o['quiet'] and not added and not in_flight):
            return
        t1 = rec['totals']['1x']
        self.stdout.write(self.style.MIGRATE_HEADING('\nH10 FORWARD SHADOW — places no orders'))
        self.stdout.write(f'  window        {start:%Y-%m-%d} .. {end:%Y-%m-%d %H:%M} UTC')
        self.stdout.write(f'  added         {added} completed round trip(s) this run')
        self.stdout.write(f'  in flight     {len(in_flight)} (open at the last bar — NOT recorded; '
                          f'marking one to market would be inventing a result)')
        self.stdout.write(f'  cumulative    n={t1["trades"]}  gross {t1["gross"]:+.2f}  '
                          f'gross PF {t1["gross_pf"]}  (no toll charged)')
        self.stdout.write(f'    at 1x cost  net {t1["net"]:+.2f}  net PF {t1["net_pf"]}')
        for m in ('2x', '3x'):
            tm = rec['totals'][m]
            self.stdout.write(f'    at {m} cost  net {tm["net"]:+.2f}  net PF {tm["net_pf"]}')
        gate = 30
        self.stdout.write(f'  promotion gate wants ~{gate} trades after costs; '
                          f'{max(0, gate - t1["trades"])} to go at roughly 8 a fortnight')
        self.stdout.write(self.style.SUCCESS(f'  wrote {o["out"]}'))
