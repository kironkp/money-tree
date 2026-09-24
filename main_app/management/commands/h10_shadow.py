"""Accumulate forward evidence for H10, one night at a time.

H10 has a positive held-out backtest and no forward record. The existing
promotion gate wants roughly 30 trades after costs and H10 makes about eight a
fortnight, so nothing but elapsed time produces that evidence — more code cannot.
This starts the clock and keeps it honest.

It is SHADOW: it places no orders, enables no Strategy row, and touches nothing
the running agents read. It replays the frozen H10_SPEC over bars that closed
after the forward start and appends completed round trips to a committed JSON
artifact.

Two decisions worth stating, because both could quietly inflate the record:

  * Only COMPLETED round trips are recorded. `run_frames` flattens whatever is
    open at the final bar and stamps it `exit_reason='end'` — that is the
    backtest boundary, not an exit the strategy chose, and booking it would mean
    marking an open position to market and calling it a result. Those are
    reported separately as in flight and enter the record only once they close
    for a real reason.
  * Appends are idempotent on a deterministic key, so a re-run, a crashed run,
    or two runs in one night cannot double-count. A record that grows when you
    look at it is not a record.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand

from main_app.services.backtest import load_frames
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


def summarise(trades: list[dict], slippage_bps: float, cost_mult: float = 1.0) -> dict:
    """Totals over recorded round trips. Costs scale; the price move does not.

    Note this is NOT the same quantity as h10_forward's 2x/3x rows. That command
    re-runs the backtest with higher costs, so wider costs can move a fill or trip a
    different exit. A forward record cannot do that — these trades already happened —
    so here the higher cost is charged against the fills that actually occurred. The
    two agreed to within $0.11 on the 2026-09-08..24 window; they will diverge more
    the further a cost multiplier moves an exit. Compare 1x figures, not these.
    """
    if not trades:
        return {'trades': 0, 'net': 0.0, 'gross': 0.0}
    net = sum(t['pnl'] for t in trades)
    fees = sum(t['fees'] for t in trades) * cost_mult
    notional = sum(t['notional'] for t in trades)
    legs = sum(1 + (0 if t['exit_reason'] == 'target' else 1) for t in trades)
    slip = notional / len(trades) * legs * slippage_bps / 1e4 * cost_mult
    base_fees = sum(t['fees'] for t in trades)
    base_slip = notional / len(trades) * legs * slippage_bps / 1e4
    gross = net + base_fees + base_slip           # the raw edge, cost-independent
    per = slip / len(trades)
    adj = [t['pnl'] + t['fees'] * cost_mult + per for t in trades]
    gw = sum(x for x in adj if x > 0)
    gl = -sum(x for x in adj if x <= 0)
    return {
        'trades': len(trades), 'gross': round(gross, 2), 'fees': round(fees, 2),
        'slippage': round(slip, 2), 'net': round(gross - fees - slip, 2),
        'gross_pf': round(gw / gl, 3) if gl else None,
        'bps_captured': round(gross / notional * 1e4, 2) if notional else None,
    }


class Command(BaseCommand):
    help = "Record H10's forward trades on newly closed bars. Places no orders."

    def add_arguments(self, parser):
        parser.add_argument('--start', default=FORWARD_START.date().isoformat())
        parser.add_argument('--out', default=ARTIFACT)
        parser.add_argument('--quiet', action='store_true')

    def handle(self, *args, **o):
        from main_app.management.commands.h10_forward import _measure, run_window, _window_bounds

        start = datetime.fromisoformat(o['start']).replace(tzinfo=timezone.utc)
        rec = load_record(o['out'])
        # Persist the skeleton before anything is measured. The record then exists
        # from the moment the clock starts, carrying the spec it will be judged
        # under, and nobody can later claim it was opened after a good first week.
        if not os.path.exists(o['out']):
            with open(o['out'], 'w') as fh:
                json.dump(rec, fh, indent=2, sort_keys=True)
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
        frames = load_frames(list(H10_PAIRS), H10_TIMEFRAME, warm_from, end.date())
        available = min(int((df.index < start).sum()) for df in frames.values())
        if available < need:
            self.stderr.write(f'warm-up too short ({available} bars, need {need}) — not recording, '
                              f'because silence would look like "no trades"')
            return

        all_trades, slip = run_window('fx_trend', dict(H10_SPEC), dict(H10_RISK), frames, start, end)
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
        rec['runs'] = (rec.get('runs') or [])[-19:] + [
            {'at': rec['last_run'], 'window_end': end.isoformat(), 'added': added,
             'total': len(rows), 'in_flight': len(in_flight)}]
        with open(o['out'], 'w') as fh:
            json.dump(rec, fh, indent=2, sort_keys=True)

        if o.get('verbosity', 1) == 0 or (o['quiet'] and not added and not in_flight):
            return
        t1 = rec['totals']['1x']
        self.stdout.write(self.style.MIGRATE_HEADING('\nH10 FORWARD SHADOW — places no orders'))
        self.stdout.write(f'  window        {start:%Y-%m-%d} .. {end:%Y-%m-%d %H:%M} UTC')
        self.stdout.write(f'  added         {added} completed round trip(s) this run')
        self.stdout.write(f'  in flight     {len(in_flight)} (open at the last bar — NOT recorded; '
                          f'marking one to market would be inventing a result)')
        self.stdout.write(f'  cumulative    n={t1["trades"]}  gross {t1.get("gross", 0):+.2f}  '
                          f'net {t1.get("net", 0):+.2f}  PF {t1.get("gross_pf")}')
        for m in ('2x', '3x'):
            tm = rec['totals'][m]
            self.stdout.write(f'    at {m} cost  net {tm.get("net", 0):+.2f}  PF {tm.get("gross_pf")}')
        gate = 30
        self.stdout.write(f'  promotion gate wants ~{gate} trades after costs; '
                          f'{max(0, gate - t1["trades"])} to go at roughly 8 a fortnight')
        self.stdout.write(self.style.SUCCESS(f'  wrote {o["out"]}'))
