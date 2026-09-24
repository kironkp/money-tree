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
        raise CommandError('--run is not built yet; the registration is committed first '
                           'so that the order cannot be argued about later')

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
