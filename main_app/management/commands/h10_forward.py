"""Measure H10 (fx_trend) on bars it has never seen.

H10 is the one candidate on this desk with a positive held-out backtest: over
2026-01-01..2026-09-07 it returned n=100, +$1,081.84, gross profit factor 1.583,
capturing 10.93 bps against a 1.60 bps toll, and +$916.77 under an hour-shaped
spread model. It has status forward_testing and no forward evidence at all — no
Strategy row runs fx_trend and its paper_result is empty.

Bars after the held-out boundary were never used to fit or select it, so replaying
the FROZEN spec over them is the cheapest honest out-of-sample look available.

Two things this command will not do, by construction:

  * It does not search or tune. The parameters come from `H10_SPEC` in
    strategies/fx_trend.py and are asserted against it, so a run that quietly
    used different numbers cannot be reported as a forward test of H10.
  * It does not let a trade enter outside the window. `act_from` stops the engine
    acting early and the trade filter stops it late. Setting only the first was
    the bug that put 38% of a "train" window's trades inside the test window.

It writes a JSON artifact and touches nothing else: not Hypothesis #10, not
strategy rows, not risk limits, not qualification, not the running agents.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand

from main_app.models import AgentConfig, Bar, Instrument, Strategy
from main_app.services.backtest import load_frames, run_backtest, spec_from_models
from main_app.services.strategies.fx_trend import (H10_HELD_OUT, H10_PAIRS, H10_RISK, H10_SPEC,
                                                   H10_TIMEFRAME)

# The hour-shaped spread model, as used when H10 was measured. A flat toll is the
# wrong SHAPE for FX: the rollover and Asia open are several times the London/NY
# rate, and a strategy's viability can rest entirely on which hours it trades.
BASE_BPS_PER_SIDE = 0.8


def spread_multiplier(hour: int) -> float:
    if 7 <= hour <= 20:
        return 1.0          # London + New York
    if 2 <= hour <= 6:
        return 1.5          # Asia mid-session
    return 3.0              # 21:00-01:00 rollover / Asia open


def _window_bounds(start: datetime) -> tuple[datetime, datetime]:
    latest = (Bar.objects.filter(instrument__symbol__in=H10_PAIRS, timeframe=H10_TIMEFRAME)
              .order_by('-ts').values_list('ts', flat=True).first())
    if latest is None:
        raise SystemExit(f'no {H10_TIMEFRAME} bars for {", ".join(H10_PAIRS)} — nothing to measure')
    return start, latest


def _measure(trades, slippage_bps: float) -> dict:
    if not trades:
        return {'trades': 0}
    net = sum(float(t.pnl) for t in trades)
    fees = sum(float(t.fees) for t in trades)
    notional = sum(float(t.entry_price) * float(t.qty) for t in trades)
    # Slippage sits inside the fill price, so pnl is already net of it and it has
    # to be added back to see the raw edge. Charged on entry always and on exit
    # only when that leg is not a limit fill, since a target rests at its level.
    legs = sum(1 + (0 if t.exit_reason == 'target' else 1) for t in trades)
    slip = notional / len(trades) * legs * slippage_bps / 1e4
    gross = net + fees + slip
    per = slip / len(trades)
    gw = sum(float(t.pnl) + float(t.fees) + per for t in trades
             if float(t.pnl) + float(t.fees) + per > 0)
    gl = -sum(float(t.pnl) + float(t.fees) + per for t in trades
              if float(t.pnl) + float(t.fees) + per <= 0)
    hour_shaped = sum(
        float(t.entry_price) * float(t.qty) * BASE_BPS_PER_SIDE / 1e4
        * (spread_multiplier(t.entry_ts.hour) + spread_multiplier(t.exit_ts.hour))
        for t in trades)
    return {
        'trades': len(trades), 'gross': round(gross, 2), 'fees': round(fees, 2),
        'slippage': round(slip, 2), 'net': round(net, 2), 'notional': round(notional, 2),
        'gross_pf': round(gw / gl, 3) if gl else None,
        'bps_captured': round(gross / notional * 1e4, 2) if notional else None,
        'bps_cost': round((fees + slip) / notional * 1e4, 2) if notional else None,
        'net_hour_shaped_cost': round(gross - fees - hour_shaped, 2),
        'entries_in_rollover': sum(1 for t in trades if t.entry_ts.hour >= 21 or t.entry_ts.hour <= 1),
        'first_entry': min(t.entry_ts for t in trades).isoformat(),
        'last_entry': max(t.entry_ts for t in trades).isoformat(),
    }


def coverage(frames, start, end) -> dict:
    """First and last bar each series actually holds inside [start, end].

    A baseline replayed on a timeframe whose history does not reach the window
    start is measuring a SHORTER window, and comparing it like for like flatters
    or damns it for a reason that has nothing to do with the strategy. 15Min forex
    only begins 2026-07-10, so this is a live hazard on this desk rather than a
    hypothetical one.
    """
    out = {}
    for sym, df in frames.items():
        idx = df.index[(df.index >= start) & (df.index <= end)]
        out[sym] = {'bars': int(len(idx)),
                    'first': idx[0].to_pydatetime().isoformat() if len(idx) else None,
                    'last': idx[-1].to_pydatetime().isoformat() if len(idx) else None}
    return out


def coverage_gaps(cov: dict, start: datetime, timeframe: str) -> list[str]:
    """Series that do not reach the window start. Two bars of slack, because a
    feed that begins one bar late is a boundary, not a gap."""
    from main_app.services.timeframes import tf_minutes
    slack = tf_minutes(timeframe) * 2
    out = []
    for sym, c in sorted(cov.items()):
        if not c['bars']:
            out.append(f'{sym}: NO bars inside the window at all')
            continue
        gap = (datetime.fromisoformat(c['first']) - start).total_seconds() / 60
        if gap > slack:
            out.append(f'{sym}: first in-window bar {c["first"][:16]}, '
                       f'{gap / 60:.1f}h after the window opened')
    return out


def run_window(key: str, params: dict, risk_over: dict, frames, start, end,
               cost_mult: float = 1.0, timeframe: str = H10_TIMEFRAME,
               pairs=H10_PAIRS) -> tuple[list, float]:
    """Replay one strategy over [start, end]. Returns (trades in window, slippage_bps).

    `timeframe` and `risk_over` are arguments rather than constants because a
    BASELINE has to be run as it actually lives. Running the live rows on H10's
    1Hour frames under H10's risk settings and calling the result "live params"
    described a configuration that has never existed.
    """
    spec = spec_from_models(key, params, list(pairs), timeframe, AgentConfig.get())
    if risk_over:
        spec.risk = replace(spec.risk, **risk_over)
    if cost_mult != 1.0:
        spec.fee_bps = {k: v * cost_mult for k, v in spec.fee_bps.items()}
        spec.risk = replace(spec.risk, slippage_bps=spec.risk.slippage_bps * cost_mult)
    spec.act_from = start
    res = run_backtest(spec, frames)
    # act_from stops the engine acting early; this stops it acting late. Both are
    # required — setting only the first is the leak that invalidated a whole
    # train window.
    return [t for t in res.trades if start <= t.entry_ts <= end], spec.risk.slippage_bps


class Command(BaseCommand):
    help = "Replay H10's frozen spec over bars it has never seen, after costs"

    def add_arguments(self, parser):
        parser.add_argument('--start', default='2026-09-08',
                            help='first day the strategy may act (default: the day after H10\'s held-out end)')
        parser.add_argument('--end', default='',
                            help='last day it may act (default: the newest bar available). '
                                 'Set both --start and --end to reproduce a past window.')
        parser.add_argument('--out', default='docs/h10-forward.json')
        parser.add_argument('--no-write', action='store_true')

    def handle(self, *args, **o):
        # The spec is checked, not assumed, and checked with a raise rather than an
        # assert: `python -O` strips asserts, which is exactly the run where you
        # least want a spec guard to quietly vanish. All eight keys, not four.
        expected = {'lookback_h': 480, 'min_move_atr': 1.0, 'stop_atr_mult': 4.0, 'atr_len': 24,
                    'cooldown_h': 168, 'allow_short': True, 'hour_from': 7, 'hour_to': 21}
        if dict(H10_SPEC) != expected:
            raise SystemExit(f'H10_SPEC has drifted from the recorded hypothesis.\n'
                             f'  recorded: {expected}\n  found:    {dict(H10_SPEC)}\n'
                             f'A run on different numbers is a different hypothesis.')

        start = datetime.fromisoformat(o['start']).replace(tzinfo=timezone.utc)
        start, end = _window_bounds(start)
        if o['end']:
            end = datetime.fromisoformat(o['end']).replace(tzinfo=timezone.utc, hour=23, minute=59)
        # Warm-up comes from bars BEFORE the window; the engine may not act on them.
        #
        # This must be counted in BARS, not hours. FX is shut at weekends, so 552
        # calendar hours is about 460 bars — and the strategy needs 760 before it
        # may emit anything. The first version of this command loaded 689 bars
        # total, the whole window was swallowed by warm-up, and it reported "no
        # trades" as though that were a measurement. It was silence.
        from main_app.services.strategies import make_strategy
        need = make_strategy('fx_trend', dict(H10_SPEC)).warmup_bars
        warm_from = (start - timedelta(days=max(120, need // 4))).date()
        frames = load_frames(list(H10_PAIRS), H10_TIMEFRAME, warm_from, end.date())
        in_window = {s_: int((df.index >= start).sum()) for s_, df in frames.items()}
        shortest = min((len(df) - n) for (s_, df), n in zip(frames.items(), in_window.values()))
        if shortest < need:
            raise SystemExit(
                f'warm-up would eat the window: the strategy needs {need} bars before it may act '
                f'and only {shortest} exist before {start:%Y-%m-%d}. Load more history and re-run; '
                f'do not report the resulting silence as a result.')

        w = self.style.WARNING
        self.stdout.write(self.style.MIGRATE_HEADING(
            '\nH10 FORWARD TEST — bars never used to fit or select it'))
        self.stdout.write(f'  held-out window was   {H10_HELD_OUT[0]} .. {H10_HELD_OUT[1]}')
        self.stdout.write(f'  this window is        {start:%Y-%m-%d %H:%M} .. {end:%Y-%m-%d %H:%M} UTC')
        self.stdout.write(f'  warm-up loaded from   {warm_from} (no trade may enter before the window)')
        self.stdout.write(f'  spec                  {H10_SPEC}')
        self.stdout.write(f'  risk                  {H10_RISK}')
        bars = {s_: len(df) for s_, df in frames.items()}
        self.stdout.write(f'  bars loaded           {bars}')
        self.stdout.write(f'  of those, in window   {in_window}')
        self.stdout.write(f'  warm-up needed        {need} bars before the window (have '
                          f'{shortest}) — checked, not assumed')

        result = {'window': [start.isoformat(), end.isoformat()], 'spec': dict(H10_SPEC),
                  'risk': dict(H10_RISK), 'pairs': list(H10_PAIRS), 'timeframe': H10_TIMEFRAME,
                  'held_out_window': list(H10_HELD_OUT), 'bars_loaded': bars,
                  'bars_in_window': in_window, 'warmup_bars_required': need,
                  'warmup_bars_available': shortest, 'costs': {}}

        trades, slip = run_window('fx_trend', dict(H10_SPEC), dict(H10_RISK), frames, start, end)
        base = _measure(trades, slip)
        result['h10'] = base
        self.stdout.write(self.style.MIGRATE_HEADING('\n  H10 at the modelled toll'))
        self._row('h10 (1x cost)', base)

        self.stdout.write('    (2x and 3x below are full RE-RUNS, not a cost overlay: a higher toll'
                          '\n     changes which trades survive the gates, so gross moves too)')
        for mult in (2.0, 3.0):
            tr, sl = run_window('fx_trend', dict(H10_SPEC), dict(H10_RISK), frames, start, end,
                                cost_mult=mult)
            m = _measure(tr, sl)
            result['costs'][f'{mult:g}x'] = m
            self._row(f'h10 ({mult:g}x cost)', m)

        self.stdout.write(self.style.MIGRATE_HEADING('\n  Baselines on the SAME window'))
        self.stdout.write('    flat (no trades)              net       0.00   <- the honest benchmark')
        result['baselines'] = {'flat': {'net': 0.0}}
        # Each live row is replayed on ITS OWN timeframe with the forex lane's own
        # RiskConfig and no H10 overrides — that is what "baseline" has to mean.
        for row in Strategy.objects.filter(market='forex', enabled=True).order_by('key'):
            tf_frames = (frames if row.timeframe == H10_TIMEFRAME
                         else load_frames(list(H10_PAIRS), row.timeframe, warm_from, end.date()))
            tr, sl = run_window(row.key, dict(row.params), {}, tf_frames, start, end,
                                timeframe=row.timeframe)
            m = _measure(tr, sl)
            m['timeframe'] = row.timeframe
            m['risk'] = 'forex lane live RiskConfig'
            # A baseline that cannot reach the window start is not a baseline for
            # this window, and saying so is the whole point of printing it.
            cov = coverage(tf_frames, start, end)
            m['coverage'] = cov
            gaps = coverage_gaps(cov, start, row.timeframe)
            m['coverage_gaps'] = gaps
            result['baselines'][row.key] = m
            self._row(f'{row.key} ({row.timeframe}, live risk)', m)
            for sym, c in sorted(cov.items()):
                self.stdout.write(f'        {sym:9} {c["bars"]:5} bars  '
                                  f'{(c["first"] or "-")[:16]} .. {(c["last"] or "-")[:16]}')
            for g in gaps:
                self.stdout.write(w(f'        WARNING {row.timeframe} does not cover the window '
                                    f'start — {g}'))
            if gaps:
                self.stdout.write(w('        this baseline measured a SHORTER window than H10 '
                                    'and is not comparable like for like'))

        # Kept only so the earlier, mislabelled figures remain comparable. This
        # is NOT how these strategies run.
        self.stdout.write('    --- same rows forced onto H10\'s 1Hour frames and risk settings,')
        self.stdout.write('        which is not how either of them runs: ---')
        for key in ('ema_momentum', 'vwap_reversion'):
            row = Strategy.objects.filter(key=key, market='forex').first()
            if row is None:
                continue
            tr, sl = run_window(key, dict(row.params), dict(H10_RISK), frames, start, end)
            m = _measure(tr, sl)
            result['baselines'][f'{key}_1hour_h10_risk'] = m
            self._row(f'{key} (1Hour, H10 risk)', m)

        n = base.get('trades', 0)
        self.stdout.write(self.style.MIGRATE_HEADING('\n  What this can and cannot tell you'))
        self.stdout.write(
            f'    n={n} over ~{(end - start).days} calendar days. H10 traded 100 times in eight\n'
            f'    months, so a window this short cannot move a t-statistic and is not evidence\n'
            f'    for or against the hypothesis. It is an observation. Hypothesis #10 is\n'
            f'    unchanged by this command.')
        if n and n < 10:
            self.stdout.write(w(f'    Treat {n} trades as anecdote, not signal.'))

        if not o['no_write']:
            with open(o['out'], 'w') as fh:
                json.dump(result, fh, indent=2, default=str)
            self.stdout.write(self.style.SUCCESS(f'\n  wrote {o["out"]}'))

    def _row(self, label, m):
        if not m.get('trades'):
            self.stdout.write(f'    {label:30} no trades in the window')
            return
        self.stdout.write(
            f"    {label:30} n={m['trades']:3}  gross {m['gross']:+9.2f}  fees {m['fees']:7.2f}  "
            f"slip {m['slippage']:7.2f}  net {m['net']:+9.2f}  PF {m['gross_pf']}\n"
            f"    {'':30} captured {m['bps_captured']} bps vs cost {m['bps_cost']} bps  |  "
            f"net at hour-shaped spreads {m['net_hour_shaped_cost']:+.2f}  |  "
            f"rollover entries {m['entries_in_rollover']}/{m['trades']}")
