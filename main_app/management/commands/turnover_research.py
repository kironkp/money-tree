"""MT-A003 — does trading LESS on the same forex rules beat trading more?

The live 15Min forex strategies are gross-positive and net-negative: they capture
0.2-0.8 bps a trade against a ~1.6 bps round trip, so the signal is real and the
whole of it goes to the toll. That is an arithmetic problem, not a signal problem,
and the lever is turnover — fewer, larger trades on the SAME rules. No candidate
here introduces a new signal or retunes a strategy parameter.

Two steps, deliberately separate commands run at different times:

    manage.py turnover_research --preregister    writes down what will be tested
    manage.py turnover_research --run            measures it

`--run` refuses to do anything without a prior registration, and the registration
refuses to be overwritten once results exist. That ordering is the entire point:
choosing the selection rule after seeing twelve results is how a grid search gets
reported as a discovery. The registration's `created_at` and the first result's
timestamp are both recorded so the order can be checked rather than trusted.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone as djtz

PREREG = 'docs/mt-a003-preregistration.json'
RESULTS = 'docs/mt-a003-turnover.json'
TITLE = 'MT-A003: turnover controls on the live forex strategies'

# Selection uses bars strictly before this. 2026-09-08..09-24 was spent by MT-A001
# and must never be used to choose anything.
TRAIN_END = datetime(2026, 9, 8, tzinfo=timezone.utc)
# 15Min forex history begins here; the window is half-open [TRAIN_START, TRAIN_END).
TRAIN_START = datetime(2026, 7, 10, tzinfo=timezone.utc)
# Confirmation, when it happens, starts here. Nothing is claimed about it yet.
FORWARD_START = datetime(2026, 9, 25, tzinfo=timezone.utc)

PAIRS = ('EUR/USD', 'GBP/USD', 'AUD/USD', 'NZD/USD')
# news_catalyst is excluded: it produces zero backtest trades by construction, and
# test_backtest.py pins that deliberately. Including it would add an empty row.
STRATEGIES = ('ema_momentum', 'vwap_reversion')

# Six candidates. Each changes ONLY the named control; every other risk setting
# stays at the lane's live value, so a difference is attributable.
#
# `min_reward_to_cost` IS the "minimum expected move as a multiple of round-trip
# cost" — the engine already refuses a trade whose target does not clear that
# multiple of the toll. Live forex runs 2.0.
#
# There is no `min_hold_minutes` knob and adding one is an engine change outside
# this assignment, so minimum hold is expressed as the hourly cadence: on 1Hour
# bars a position cannot be reconsidered for an hour. Candidates 5 and 6 also
# lengthen `max_hold_minutes`, because keeping it at 240 on hourly bars forces an
# exit after four bars and would confound "slower cadence" with "still forced out
# quickly" — candidate 4 is kept unchanged precisely to show that confound.
CANDIDATES = (
    ('baseline', '15Min', {}, 'the unchanged live config — what the desk runs today'),
    ('gate_4x', '15Min', {'min_reward_to_cost': 4.0},
     'target must clear 4x the round trip instead of 2x'),
    ('gate_8x', '15Min', {'min_reward_to_cost': 8.0},
     'target must clear 8x the round trip'),
    ('hourly', '1Hour', {},
     'same rules and same risk, one decision an hour instead of four'),
    ('hourly_hold_24h', '1Hour', {'max_hold_minutes': 1440},
     'hourly cadence, and a position may run a day instead of four hours'),
    ('hourly_gate_4x_hold_24h', '1Hour', {'min_reward_to_cost': 4.0, 'max_hold_minutes': 1440},
     'both controls together'),
)

# --- the selection rule, fixed before any result exists ----------------------
MIN_TRAIN_TRADES = 30       # below this the row is reported but cannot be chosen
MIN_NET_PF_AT_2X = 1.0      # must still make money if the toll doubles
SELECT_ON = 'net_1x'        # the single ranking statistic


def train_frames(frames: dict) -> dict:
    """Cut frames strictly before TRAIN_END.

    Superseded by `research_window.research_frames`, which does the cut before
    quality_gate rather than after and is now the only way this command obtains
    bars. Kept because MT-A003's correction is recorded against this name and
    because it is the cheapest way to express the rule in a test.

    Original note follows.

    Every bar the engine may see, cut strictly before TRAIN_END.

    `load_frames` takes an inclusive end DATE and expands it to cover the whole ET
    session day, so asking for 2026-09-08 returns bars to 2026-09-09 03:45 UTC —
    112 fifteen-minute bars and 28 hourly ones inside the window MT-A001 spent.
    Filtering entries at the boundary is not enough: a position still open at
    midnight exits on those bars, so spent data prices the result.

    This is the third time on this desk a window has been given a start and no end.
    Warm-up was once consumed inside a test window, `act_from` was once set with no
    `act_until`, and run_window's own docstring says both are required. Hence a
    named function and a test that watches what actually reaches the engine.
    """
    return {sym: df[df.index < TRAIN_END] for sym, df in frames.items()}


def registration(now) -> dict:
    return {
        'assignment': 'MT-A003',
        'registered_at': now.isoformat(),
        'question': 'On the live forex rules, does a turnover control beat the unchanged '
                    'live config on train net after costs?',
        'why': 'ema_momentum and vwap_reversion are gross-positive (gross PF 1.07 and 1.21) '
               'and net-negative, capturing 0.2-0.8 bps a trade against a ~1.6 bps round '
               'trip. Cost is the binding constraint, so turnover is the lever.',
        'strategies': list(STRATEGIES),
        'pairs': list(PAIRS),
        'baseline': 'baseline — each strategy on 15Min with the forex lane live RiskConfig',
        'candidates': [{'name': n, 'timeframe': tf, 'risk_override': ro, 'what': why}
                       for n, tf, ro, why in CANDIDATES],
        'train_window': ['2026-07-10', TRAIN_END.date().isoformat()],
        'train_window_note': '15Min forex history begins 2026-07-10, so warm-up is consumed '
                             'INSIDE the train window and effective train bars are reported '
                             'per candidate. There is no earlier data to warm up from.',
        'spent_window': ['2026-09-08', '2026-09-24'],
        'spent_window_note': 'Used by MT-A001. Never used to select or tune anything, '
                             'including "just to look".',
        'forward_confirmation_start': FORWARD_START.isoformat(),
        'selection_rule': {
            'rank_by': SELECT_ON,
            'statement': 'Per strategy, the winner is the candidate with the highest train net '
                         'at 1x cost that ALSO satisfies every gate below. If no candidate '
                         'satisfies them, the recorded outcome is "none beat baseline" and '
                         'nothing is frozen.',
            'gates': [
                f'train trades >= {MIN_TRAIN_TRADES}',
                f'net profit factor at 2x cost >= {MIN_NET_PF_AT_2X}',
                'train net at 1x cost strictly greater than the baseline for that strategy',
            ],
            'blocked_if': f'If the highest-net candidate fails only the trade-count gate, the '
                          f'outcome is "BLOCKED: insufficient train data", reported with the '
                          f'number rather than as a hedge.',
        },
        'what_is_not_claimed': 'No significance is claimed and none can be. Twelve comparisons '
                               '(6 candidates x 2 strategies) with an argmax taken means the '
                               'winner is flattered by selection by construction. This step '
                               'only chooses what to confirm forward from 2026-09-25; the '
                               'forward window is the only thing that can confirm it.',
        'reporting': 'Every candidate is reported including failures: n, gross, fees, '
                     'slippage, net at 1x and 2x, and net profit factor.',
        'constraints': 'Research only. No Strategy row, parameter, risk limit or qualification '
                       'is changed, and nothing is applied to the running agents.',
    }


class Command(BaseCommand):
    help = 'MT-A003 turnover research: pre-register, then measure. Places no orders.'

    def add_arguments(self, parser):
        parser.add_argument('--preregister', action='store_true')
        parser.add_argument('--run', action='store_true')

    def handle(self, *args, **o):
        if o['preregister'] == o['run']:
            raise CommandError('choose exactly one of --preregister and --run')
        if o['preregister']:
            return self._preregister()
        return self._run()

    def _preregister(self):
        from main_app.models import Hypothesis

        if os.path.exists(RESULTS):
            raise CommandError(f'{RESULTS} already exists — refusing to re-register after '
                               f'results are in. That is what pre-registration prevents.')
        if os.path.exists(PREREG):
            raise CommandError(f'{PREREG} already exists — registered at '
                               f'{json.load(open(PREREG))["registered_at"]}')

        now = djtz.now()
        h = Hypothesis.objects.create(
            market='forex', title=TITLE, status=Hypothesis.PROPOSED,
            claim='On the live forex rules, at least one turnover control (a higher '
                  'reward-to-cost gate, an hourly decision cadence, or a longer maximum hold) '
                  'produces a higher train net after costs than the unchanged live config.',
            source='In-app measurement: docs/h10-forward.json baselines, run on each '
                   "strategy's own 15Min timeframe under the forex lane's live risk settings "
                   '(ema_momentum gross PF 1.07 net -$589; vwap_reversion gross PF 1.21 net '
                   '-$415 over 2026-09-08..24). Assignment MT-A003 from Money Tree Reviewer.',
            rationale=json.dumps(registration(now), indent=2, sort_keys=True))

        reg = registration(now)
        reg['hypothesis_id'] = h.id
        reg['hypothesis_created_at'] = h.created_at.isoformat()
        with open(PREREG, 'w') as fh:
            json.dump(reg, fh, indent=2, sort_keys=True)

        self.stdout.write(self.style.MIGRATE_HEADING('\nMT-A003 PRE-REGISTERED — no result computed'))
        self.stdout.write(f'  hypothesis    #{h.id}  created_at {h.created_at.isoformat()}')
        self.stdout.write(f'  candidates    {len(CANDIDATES)} x {len(STRATEGIES)} strategies')
        for n, tf, ro, why in CANDIDATES:
            self.stdout.write(f'    {n:26} {tf:6} {ro or "live risk unchanged"}')
        self.stdout.write(f'  rank by       {SELECT_ON}, gates: n>={MIN_TRAIN_TRADES}, '
                          f'net PF at 2x >= {MIN_NET_PF_AT_2X}, net > baseline')
        self.stdout.write(f'  train         2026-07-10 .. {TRAIN_END:%Y-%m-%d} (exclusive)')
        self.stdout.write(f'  spent         2026-09-08 .. 2026-09-24 — never used to select')
        self.stdout.write(f'  confirm from  {FORWARD_START:%Y-%m-%d}, nothing claimed yet')
        self.stdout.write(self.style.SUCCESS(f'  wrote {PREREG}'))

    # --- measurement -------------------------------------------------------
    def _run(self):
        from datetime import date
        from main_app.models import Hypothesis, Strategy
        from main_app.services.research_window import Window, research_frames, run_window
        from main_app.services.strategies import make_strategy

        if not os.path.exists(PREREG):
            raise CommandError(f'no {PREREG} — register what will be tested before measuring it. '
                               f'Run --preregister first.')
        reg = json.load(open(PREREG))
        h = Hypothesis.objects.filter(id=reg.get('hypothesis_id')).first()
        if h is None:
            raise CommandError(f'registration names hypothesis #{reg.get("hypothesis_id")}, '
                               f'which does not exist — refusing to report against nothing')

        live = {k: Strategy.objects.get(market='forex', key=k).params for k in STRATEGIES}

        # 1Hour forex reaches back years; 15Min starts 2026-07-10. Loading each from
        # its own earliest point gives the hourly candidates a proper warm-up, which
        # is correct — but it would also hand them a longer ENTRY window, which is
        # not a turnover effect. So every candidate is judged on one common entry
        # calendar: the latest warm-up boundary across all of them, which is the
        # 15Min one. Reported, not assumed.
        frames = {tf: research_frames(PAIRS, tf, Window(warmup_start=warm, start=TRAIN_START,
                                                        end=TRAIN_END))
                  for tf, warm in (('15Min', date(2026, 7, 1)), ('1Hour', date(2026, 1, 1)))}
        warm = max(make_strategy(k, live[k]).warmup_bars for k in STRATEGIES)
        entry_from = max(sorted(df.index[df.index < TRAIN_END])[warm].to_pydatetime()
                         for df in frames['15Min'].values())

        bars = {tf: {sym: int(((df.index >= entry_from) & (df.index < TRAIN_END)).sum())
                     for sym, df in fr.items()} for tf, fr in frames.items()}

        rows = []
        # Stamped BEFORE the first run, so it bounds the earliest moment any result
        # could exist rather than the moment the first one finished.
        first_result_at = datetime.now(timezone.utc).isoformat()
        for key in STRATEGIES:
            for name, tf, over, why in CANDIDATES:
                trades, slip = run_window(key, dict(live[key]), dict(over), frames[tf],
                                          entry_from, TRAIN_END, timeframe=tf, pairs=PAIRS)
                t2, _ = run_window(key, dict(live[key]), dict(over), frames[tf],
                                   entry_from, TRAIN_END, timeframe=tf, pairs=PAIRS, cost_mult=2.0)
                # Belt and braces. The frames are already cut, so this can only fire
                # if that truncation is ever removed — which is exactly when it matters.
                late = [t for t in trades + t2 if t.entry_ts >= TRAIN_END]
                if late:
                    raise CommandError(
                        f'{key}/{name}: {len(late)} entry(ies) at or after {TRAIN_END:%Y-%m-%d}, '
                        f'the window MT-A001 spent. Refusing to record a contaminated row.')
                rows.append({'strategy': key, 'candidate': name, 'timeframe': tf,
                             'risk_override': over, 'what': why,
                             **_stats(trades, slip), **_at2x(t2)})

        winners = {k: _select(k, rows) for k in STRATEGIES}
        # A re-run must say what it replaced. The first MT-A003 run was contaminated
        # by bars inside the spent window, and a silent overwrite would have left no
        # trace of a figure that had already been reported.
        superseded = []
        if os.path.exists(RESULTS):
            prev = json.load(open(RESULTS))
            superseded = prev.get('superseded') or []
            # Only an actual supersession is recorded. A re-run that reproduces the
            # same rows — verifying a refactor, say — has superseded nothing, and
            # appending it would pad the record with events that never happened.
            def _key(rows):
                return {(r['strategy'], r['candidate']): (r['trades'], r['net'], r['net_2x'])
                        for r in (rows or [])}
            if _key(prev.get('candidates')) != _key(rows):
                superseded = superseded + [
                    {'measured_at': prev.get('measured_at'), 'candidates': prev.get('candidates'),
                     'outcome': prev.get('outcome'),
                     'train_entry_window': prev.get('train_entry_window'),
                     'why_replaced': prev.get('replaced_because',
                                              'superseded by a later run')}]

        out = {'assignment': 'MT-A003', 'registered_at': reg['registered_at'],
               'superseded': superseded,
               'first_result_at': first_result_at,
               'measured_at': datetime.now(timezone.utc).isoformat(),
               'train_entry_window': [entry_from.isoformat(), TRAIN_END.isoformat()],
               'warmup_bars_consumed': warm, 'train_bars_in_entry_window': bars,
               'forward_confirmation_start': FORWARD_START.isoformat(),
               'forward_result': 'not started — nothing is claimed about it',
               'live_params': live, 'candidates': rows, 'outcome': winners}
        with open(RESULTS, 'w') as fh:
            json.dump(out, fh, indent=2, sort_keys=True)

        h.train_result = {'entry_window': out['train_entry_window'], 'rows': rows,
                          'outcome': winners}
        h.status = (Hypothesis.FORWARD if any(w['frozen'] for w in winners.values())
                    else Hypothesis.REJECTED)
        h.decided_at = djtz.now()
        note = '; '.join(f'{k}: {v["outcome"]}' for k, v in winners.items())
        if superseded:
            prev_rows = {(r['strategy'], r['candidate']): r['net']
                         for r in (superseded[-1].get('candidates') or [])}
            changed = [f'{r["strategy"]}/{r["candidate"]} {prev_rows[(r["strategy"], r["candidate"])]:+.2f}'
                       f'->{r["net"]:+.2f}'
                       for r in rows
                       if (r['strategy'], r['candidate']) in prev_rows
                       and abs(prev_rows[(r['strategy'], r['candidate'])] - r['net']) > 0.005]
            note += (f'. SUPERSEDES a contaminated run measured {superseded[-1]["measured_at"]}: '
                     f'{superseded[-1]["why_replaced"]} '
                     f'{len(changed)} of {len(rows)} rows changed: {"; ".join(changed)}')
        h.decision_note = note
        h.save()
        self._report(reg, out, winners)

    def _report(self, reg, out, winners):
        w = self.stdout.write
        w(self.style.MIGRATE_HEADING('\nMT-A003 TURNOVER RESEARCH — train only, places no orders'))
        w(f'  registered    {reg["registered_at"]}')
        w(f'  first result  {out["first_result_at"]}   (registration must precede it)')
        w(f'  entry window  {out["train_entry_window"][0][:16]} .. {TRAIN_END:%Y-%m-%d} '
          f'after {out["warmup_bars_consumed"]} warm-up bars')
        for tf, b in out['train_bars_in_entry_window'].items():
            w(f'    {tf:6} bars/pair  {", ".join(f"{s} {n}" for s, n in sorted(b.items()))}')
        hdr = (f'\n  {"strategy":15} {"candidate":24} {"n":>4} {"gross":>9} {"fees":>8} '
               f'{"slip":>8} {"net 1x":>9} {"net 2x":>9} {"netPF":>6} {"PF2x":>6}')
        w(hdr)
        w('  ' + '-' * (len(hdr) - 3))
        for r in out['candidates']:
            flag = '' if r['trades'] >= MIN_TRAIN_TRADES else '  <- too few to choose'
            w(f'  {r["strategy"]:15} {r["candidate"]:24} {r["trades"]:4} {r["gross"]:9.2f} '
              f'{r["fees"]:8.2f} {r["slippage"]:8.2f} {r["net"]:9.2f} {r["net_2x"]:9.2f} '
              f'{_fmt(r["net_pf"]):>6} {_fmt(r["net_pf_2x"]):>6}{flag}')
        w('')
        for k, v in winners.items():
            style = self.style.SUCCESS if v['frozen'] else self.style.WARNING
            w(style(f'  {k:15} {v["outcome"]}'))
            for line in v['why']:
                w(f'      {line}')
        w(f'\n  forward confirmation starts {FORWARD_START:%Y-%m-%d}; nothing is claimed about '
          f'it here.\n  No significance is claimed: {len(CANDIDATES) * len(STRATEGIES)} '
          f'comparisons with an argmax taken flatters the winner by construction.')
        w(self.style.SUCCESS(f'  wrote {RESULTS}'))


def _fmt(v):
    return '-' if v is None else f'{v:.2f}'


def _pf(trades) -> float | None:
    won = sum(float(t.pnl) for t in trades if float(t.pnl) > 0)
    lost = -sum(float(t.pnl) for t in trades if float(t.pnl) <= 0)
    return round(won / lost, 3) if lost else None


def _stats(trades, slip) -> dict:
    """Net profit factor, not gross: the question is what the desk keeps."""
    if not trades:
        return {'trades': 0, 'gross': 0.0, 'fees': 0.0, 'slippage': 0.0, 'net': 0.0,
                'net_pf': None, 'bps_captured': None, 'notional': 0.0}
    net = sum(float(t.pnl) for t in trades)
    fees = sum(float(t.fees) for t in trades)
    notional = sum(float(t.entry_price) * float(t.qty) for t in trades)
    legs = sum(1 + (0 if t.exit_reason == 'target' else 1) for t in trades)
    slipc = notional / len(trades) * legs * slip / 1e4
    gross = net + fees + slipc
    return {'trades': len(trades), 'gross': round(gross, 2), 'fees': round(fees, 2),
            'slippage': round(slipc, 2), 'net': round(net, 2), 'net_pf': _pf(trades),
            'notional': round(notional, 2),
            'bps_captured': round(gross / notional * 1e4, 2) if notional else None}


def _at2x(trades) -> dict:
    return {'net_2x': round(sum(float(t.pnl) for t in trades), 2),
            'net_pf_2x': _pf(trades), 'trades_2x': len(trades)}


def _select(strategy: str, rows: list) -> dict:
    """Apply the pre-registered rule exactly. No judgement is exercised here."""
    mine = [r for r in rows if r['strategy'] == strategy]
    base = next(r for r in mine if r['candidate'] == 'baseline')
    others = [r for r in mine if r['candidate'] != 'baseline']
    ranked = sorted(others, key=lambda r: r[SELECT_ON.replace('net_1x', 'net')], reverse=True)
    top = ranked[0] if ranked else None

    def ok(r):
        return (r['trades'] >= MIN_TRAIN_TRADES
                and r['net_pf_2x'] is not None and r['net_pf_2x'] >= MIN_NET_PF_AT_2X
                and r['net'] > base['net'])

    passing = [r for r in ranked if ok(r)]
    why = [f'baseline net at 1x: {base["net"]:+.2f} over {base["trades"]} trades']
    if passing:
        w = passing[0]
        why += [f'winner {w["candidate"]}: net {w["net"]:+.2f} over {w["trades"]} trades, '
                f'net PF at 2x {_fmt(w["net_pf_2x"])}',
                'frozen for forward confirmation only — not a result']
        return {'outcome': f'FROZEN: {w["candidate"]}', 'frozen': True, 'winner': w['candidate'],
                'why': why, 'forward_confirmation_start': FORWARD_START.isoformat()}
    if (top is not None and top['net'] > base['net'] and top['trades'] < MIN_TRAIN_TRADES
            and top['net_pf_2x'] is not None and top['net_pf_2x'] >= MIN_NET_PF_AT_2X):
        why += [f'best net was {top["candidate"]} at {top["net"]:+.2f}, but on only '
                f'{top["trades"]} trades against a pre-registered floor of {MIN_TRAIN_TRADES}',
                'the number, not a hedge: this cannot be separated from noise']
        return {'outcome': 'BLOCKED: insufficient train data', 'frozen': False,
                'winner': None, 'why': why}
    why += ['no candidate cleared every pre-registered gate']
    # "none beat baseline" is the registered label for "nothing cleared the gates",
    # and it is kept exactly as registered. But on its own it would hide a candidate
    # that beat the baseline on net and died on robustness, which is a different and
    # more interesting failure — and the one worth looking at again after more data.
    if top is not None and top['net'] > base['net']:
        failed = []
        if top['trades'] < MIN_TRAIN_TRADES:
            failed.append(f'n {top["trades"]} < {MIN_TRAIN_TRADES}')
        if top['net_pf_2x'] is None or top['net_pf_2x'] < MIN_NET_PF_AT_2X:
            failed.append(f'net PF at 2x {_fmt(top["net_pf_2x"])} < {MIN_NET_PF_AT_2X}')
        why += [f'NOTE: {top["candidate"]} did beat the baseline on net '
                f'({top["net"]:+.2f} vs {base["net"]:+.2f} over {top["trades"]} trades) '
                f'and failed on {" and ".join(failed)}',
                'not frozen, because the gate it failed is the one that asks whether the '
                'edge survives a worse toll — which is the entire question here']
    why += ['reported as a failure, which is the honest and most likely outcome']
    return {'outcome': 'none beat baseline', 'frozen': False, 'winner': None, 'why': why}
