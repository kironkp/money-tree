"""MT-A008 / H19 — does a slower trend rule on FX clear its own costs?

Every after-cost positive on this desk has come from holding longer. H10 (hourly
trend, week-long holds) made +$1,082 on its held-out window. MT-A003 showed that
cutting turnover flips ema_momentum from -$1,281.70 to +$272.31 on train, then dies
at 2x cost with a net profit factor of 0.921. The failure has never been the signal;
it has been cost eating a real gross edge.

So this grids decision CADENCE and HOLD LENGTH on the existing trend rules, and
gates on surviving a doubled toll. No strategy parameter is retuned: only the
timeframe and the maximum hold vary, except where a parameter is expressed in BARS
and must be rescaled to hold its calendar horizon constant (see TF_PARAMS).

Two commands, run at different times and committed separately:

    manage.py trend_research --preregister    writes down what will be tested
    manage.py trend_research --run            measures it

`--run` refuses without a registration, and registering is refused once a result
exists. Choosing a rule after seeing the table is how a grid search becomes a
discovery.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone as djtz

PREREG = 'docs/mt-a008-preregistration.json'
RESULTS = 'docs/mt-a008-trend.json'
FORWARD = 'docs/mt-a008-forward.json'
TITLE = 'MT-A008 / H19: lower-turnover trend rules on FX majors'

# Half-open [TRAIN_START, TRAIN_END). Chosen to stop exactly where H10's held-out
# window begins, so selection cannot touch it.
TRAIN_START = datetime(2024, 11, 1, tzinfo=timezone.utc)
TRAIN_END = datetime(2026, 1, 1, tzinfo=timezone.utc)

# Neither may be used to select or tune anything, ever.
HELD_OUT = (datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 8, tzinfo=timezone.utc))      # H10's held-out window
SPENT = (datetime(2026, 9, 8, tzinfo=timezone.utc),
         datetime(2026, 9, 25, tzinfo=timezone.utc))        # spent by MT-A001
FORWARD_START = datetime(2026, 9, 25, tzinfo=timezone.utc)

PAIRS = ('EUR/USD', 'GBP/USD', 'AUD/USD', 'NZD/USD')
WARMUP_FROM = {'1Hour': date(2024, 9, 1), '1Day': date(2010, 1, 1)}

# `lookback_h` and `cooldown_h` are read as BARS — `df['close'].shift(lb)` — despite
# their names. On 1Hour bar and hour coincide, which is why it has never mattered.
# On 1Day, H10's 480 would mean 480 DAYS and 168 would mean eight months. These hold
# the CALENDAR horizon constant instead: 20 days of trend, 7 days of cooldown.
# atr_len 14 on daily because a 1-bar ATR is degenerate and 14 is conventional.
# Every value here was fixed before any result was computed.
TF_PARAMS = {
    '1Hour': {'lookback_h': 480, 'cooldown_h': 168, 'atr_len': 24, 'hour_from': 7, 'hour_to': 21},
    '1Day': {'lookback_h': 20, 'cooldown_h': 7, 'atr_len': 14, 'hour_from': 0, 'hour_to': 24},
}
HOLDS = (('72h', 4320), ('week', 7200))
KEYS = ('ema_momentum', 'fx_trend')
TIMEFRAMES = ('1Hour', '1Day')

# --- the selection rule, fixed before any result exists ----------------------
MIN_TRAIN_TRADES = 30
MIN_NET_AT_1X = 0.0          # strictly greater
MIN_NET_PF_AT_2X = 1.0
SELECT_ON = 'net'            # train net at 1x cost
# The comparator a candidate must beat, since the true live baseline is unevaluable
# on this window. Not a candidate and never ranked.
LIVE_PARAMS_ROW = 'live_params_1hour'


def candidates() -> list[dict]:
    """The grid. Eight: two rules x two cadences x two holds."""
    out = []
    for key in KEYS:
        for tf in TIMEFRAMES:
            for label, hold in HOLDS:
                out.append({'name': f'{key}_{tf}_{label}', 'strategy': key, 'timeframe': tf,
                            'max_hold_minutes': hold,
                            'what': f'{key} deciding every {tf}, held at most {label}'})
    return out


def params_for(cand: dict, live: dict) -> dict:
    """The exact strategy parameters a candidate hands the engine.

    One definition, used by the harness and by the fidelity test, so the two cannot
    disagree about what was actually run. ema_momentum keeps its LIVE parameters
    untouched — only cadence and hold vary. fx_trend takes H10's frozen spec with
    only the bar-expressed values rescaled per TF_PARAMS.
    """
    from main_app.services.strategies.fx_trend import H10_SPEC

    if cand['strategy'] == 'fx_trend':
        return dict(H10_SPEC, **TF_PARAMS[cand['timeframe']])
    return dict(live[cand['strategy']])


def risk_for(cand: dict) -> dict:
    """The risk overrides a candidate applies, and nothing else.

    `min_reward_to_cost` 0 for fx_trend because it emits a stop and NO target, so
    the cost gate has no target to measure and would refuse every entry — that is
    why H10_RISK carries it. ema_momentum emits a target at rr=2.0, so it keeps the
    lane's live gate and is judged under the cost rule it actually trades under.
    """
    over = {'max_hold_minutes': cand['max_hold_minutes']}
    if cand['strategy'] == 'fx_trend':
        over['min_reward_to_cost'] = 0.0
    return over


def registration(now) -> dict:
    return {
        'assignment': 'MT-A008', 'hypothesis': 'H19',
        'registered_at': now.isoformat(),
        'question': 'On FX majors, does a slower trend rule produce train net above zero '
                    'at 1x cost AND a net profit factor of at least 1.0 at 2x cost?',
        'why': 'Every after-cost positive on this desk came from holding longer. H10 '
               '(hourly, week holds) +$1,081.84 held out. MT-A003 turned ema_momentum from '
               '-$1,281.70 to +$272.31 by cutting turnover, then failed 2x cost at 0.921. '
               'Cost eating a real gross edge is the recurring failure, so cadence and hold '
               'are the levers.',
        'candidates': candidates(),
        'timeframe_params': TF_PARAMS,
        'timeframe_params_note': 'lookback_h and cooldown_h are read as BARS despite their '
                                 'names. The 1Day values hold the calendar horizon constant '
                                 '(20 days of trend, 7 days of cooldown) rather than '
                                 'inheriting 480 days and 8 months. Fixed before any result.',
        'risk_overrides': {c['name']: risk_for(c) for c in candidates()},
        'risk_overrides_note': 'min_reward_to_cost 0 for fx_trend only, because it emits a stop '
                               'and no target, so the cost gate has nothing to measure and '
                               'would refuse every entry — the reason H10_RISK carries it. '
                               'ema_momentum emits a target at rr=2.0 and keeps the lane live '
                               'gate, so it is judged under the cost rule it actually trades '
                               'under. Everything else is the forex lane live RiskConfig.',
        'warmup_from': {tf: d.isoformat() for tf, d in WARMUP_FROM.items()},
        'no_retuning': 'Only timeframe and max hold vary. ema_momentum uses its live '
                       'parameters unchanged; fx_trend uses H10_SPEC with only the '
                       'bar-expressed values rescaled as above.',
        'train_window': [TRAIN_START.isoformat(), TRAIN_END.isoformat()],
        'train_window_note': 'Half-open. Ends exactly where H10 held-out begins, so selection '
                             'cannot touch it. 1Hour FX history starts 2024-09-08, leaving '
                             '928-929 warm-up bars per pair before the window against '
                             "FxTrend's 760-bar requirement — asserted at run time, not assumed.",
        'forbidden_windows': {'h10_held_out': [HELD_OUT[0].isoformat(), HELD_OUT[1].isoformat()],
                              'spent_by_mt_a001': [SPENT[0].isoformat(), SPENT[1].isoformat()]},
        'comparators': {
            'flat': 'net 0. The honest benchmark, and what the net>0 gate enforces.',
            'h10_frozen': "H10's frozen spec and risk on 1Hour. A fixed comparator, NOT a "
                          'candidate — it cannot win and is not ranked.',
            'live_params_1hour': 'The live ema_momentum parameters run at 1Hour. Labelled '
                                 'live PARAMETERS, not the live CONFIGURATION: the live rows '
                                 'trade 15Min, and 15Min FX history begins 2026-07-10, so the '
                                 'live baseline has zero bars in this window and is '
                                 'unevaluable. Substituting a different timeframe and calling '
                                 'it the live baseline is the MT-A001 C2 error.',
        },
        'unevaluable_gate': 'The TRUE live configuration — ema_momentum and vwap_reversion at '
                            '15Min — is UNEVALUABLE on this window: 15Min FX history begins '
                            '2026-07-10, so it has zero bars here. Recorded rather than quietly '
                            'dropped. The gate is therefore written explicitly as: beats flat '
                            '(net at 1x > 0) AND beats the live_params_1hour row on train net '
                            'at 1x. That row is the live PARAMETERS on a different cadence, '
                            'which is a fair comparator for a cadence experiment and is not '
                            'the live configuration.',
        'param_ranges_are_advisory': "Param.coerce() converts type only — it does NOT clamp to "
                                     'min/max or snap to step, which are used solely by the '
                                     "optimizer's grid(). Checked before registering, because "
                                     'fx_trend declares lookback_h min 48 and the 1Day value is '
                                     '20: had it clamped, every daily candidate would silently '
                                     'have run at 48 days. It does not, and a test asserts the '
                                     'engine receives exactly these values for every candidate.',
        'no_4hour': 'The grid named 1Hour / 4Hour / 1Day. There are ZERO 4Hour forex bars in '
                    'the database and Yahoo offers no 4h interval, so 4Hour would have to be '
                    'resampled from 1Hour — a new data source with its own alignment questions. '
                    'Not built to fit a grid shape. 1Hour and 1Day only.',
        'selection_rule': {
            'rank_by': 'train net at 1x cost',
            'gates': [f'train trades >= {MIN_TRAIN_TRADES}',
                      f'train net at 1x cost > {MIN_NET_AT_1X} (beats flat)',
                      f'train net at 1x cost > the {LIVE_PARAMS_ROW} comparator',
                      f'net profit factor at 2x cost >= {MIN_NET_PF_AT_2X}'],
            'statement': 'The winner is the candidate with the highest train net at 1x cost '
                         'that satisfies every gate. If none does, the outcome is "none" and '
                         'nothing is frozen.',
        },
        'forward_confirmation_start': FORWARD_START.isoformat(),
        'what_is_not_claimed': 'No significance is claimed and none is available: 8 candidates '
                               'with an argmax taken flatters the winner by construction. A '
                               'train result is not a positive result. Only the forward record '
                               'from 2026-09-25 can confirm anything, and this step does not '
                               'claim it has.',
        'reporting': 'Every candidate reported including failures: n, gross, fees, slippage, '
                     'net at 1x and 2x, and net profit factor.',
        'constraints': 'Research only. No Strategy row, parameter, risk limit or qualification '
                       'is changed, and nothing is applied to the running agents.',
    }


class Command(BaseCommand):
    help = 'MT-A008 trend research: pre-register, then measure. Places no orders.'

    def add_arguments(self, parser):
        parser.add_argument('--preregister', action='store_true')
        parser.add_argument('--run', action='store_true')

    def handle(self, *args, **o):
        if o['preregister'] == o['run']:
            raise CommandError('choose exactly one of --preregister and --run')
        if o['preregister']:
            return self._preregister()
        raise CommandError('--run is not built yet; the registration is committed first so '
                           'that the order cannot be argued about later')

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
            claim='On FX majors, at least one slower trend configuration (1Hour or 1Day '
                  'decisions, held 72h or to the week close) produces train net above zero '
                  'after 1x costs AND a net profit factor of at least 1.0 after 2x costs.',
            source='In-app measurement: docs/h10-forward-heldout.json (H10 held out n=100, '
                   '+$1,081.84, gross PF 1.583) and docs/mt-a003-turnover.json '
                   '(ema_momentum hourly_hold_24h -$1,281.70 -> +$272.31 on train, net PF at '
                   '2x 0.921). Assignment MT-A008 from Money Tree Reviewer.',
            rationale=json.dumps(registration(now), indent=2, sort_keys=True))

        reg = registration(now)
        reg['hypothesis_id'] = h.id
        reg['hypothesis_created_at'] = h.created_at.isoformat()
        with open(PREREG, 'w') as fh:
            json.dump(reg, fh, indent=2, sort_keys=True)

        w = self.stdout.write
        w(self.style.MIGRATE_HEADING('\nMT-A008 / H19 PRE-REGISTERED — no result computed'))
        w(f'  hypothesis    #{h.id}  created_at {h.created_at.isoformat()}')
        w(f'  candidates    {len(candidates())}')
        for c in candidates():
            w(f'    {c["name"]:34} hold {c["max_hold_minutes"]} min')
        w(f'  gates         n>={MIN_TRAIN_TRADES}, net at 1x > {MIN_NET_AT_1X}, '
          f'net PF at 2x >= {MIN_NET_PF_AT_2X}; rank by {SELECT_ON}')
        w(f'  train         {TRAIN_START:%Y-%m-%d} .. {TRAIN_END:%Y-%m-%d} (exclusive)')
        w(f'  forbidden     H10 held-out {HELD_OUT[0]:%Y-%m-%d}..{HELD_OUT[1]:%Y-%m-%d}; '
          f'spent {SPENT[0]:%Y-%m-%d}..{SPENT[1]:%Y-%m-%d}')
        w(f'  confirm from  {FORWARD_START:%Y-%m-%d}; nothing claimed about it here')
        w(self.style.SUCCESS(f'  wrote {PREREG}'))
