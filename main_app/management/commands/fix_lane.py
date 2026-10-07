"""Fix a lane's failing sim strategies: lane regime × each strategy's own timeframe.

A regime is (min_reward_to_cost, max_hold_minutes): lane-wide settings, chosen
for the lane. A strategy's TIMEFRAME is its own (v1.73): the lane polls one feed at
its base timeframe, and a strategy may run on a whole multiple of it, resampled
causally from the base bars. For each regime, each target strategy is
walk-forwarded at every candidate timeframe and keeps its best; the lane moves off
its current regime only if another regime's combined adaptive out-of-sample net
(each strategy at its best timeframe and params) beats the current one's. Then,
per strategy, under the chosen regime: promote normally if the candidate passes
the gate; else install it in sim if the selection procedure beats the current
version (at its current timeframe) on the windows it traded; else keep it.
Nothing is ever disabled.

Two phases, so the expensive part runs once and its numbers can be read first:

    manage.py fix_lane --market forex [--strategies a,b] [--timeframes 15Min,1Hour]
                       [--current-regime] [--random N]     measure; writes run/fix-lane-forex.json
    manage.py fix_lane --market forex --apply [--dry-run]  select and install from that plan

The forex window overlaps 2026-09-08..09-24, which MT-A003 had kept untouched;
it is used for selection here (see docs/LOGBOOK.md).
"""
from __future__ import annotations

import fcntl
import json
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from main_app.models import AgentConfig, Experiment, JournalEntry, Account, Market, Strategy
from main_app.services.backtest import load_frames, spec_from_models
from main_app.services.fix_loop import choose_regime, install_if_better, own_timeframe, regime_key
from main_app.services.optimize import evaluate_fixed_params, grid_from_schema, run_experiment, with_risk_overrides
from main_app.services.promotion import promote
from main_app.services.strategies.base import ENTRY_SESSIONS
from main_app.services.timeframes import tf_minutes

from .auto_research import comparable_verdict

WINDOWS = {'forex': (58, 21, 7), 'degen': (120, 30, 10)}   # days, train, test — as auto_research
# The AgentConfig field behind each lane value. Only lanes that own all of them:
# crypto and stocks share min_reward_to_cost and max_hold_minutes, so moving one
# would silently move the other.
LANE_FIELDS = {
    'forex': {'timeframe': 'forex_timeframe', 'min_reward_to_cost': 'forex_min_reward_to_cost',
              'max_hold_minutes': 'forex_max_hold_minutes'},
    'degen': {'timeframe': 'degen_timeframe', 'min_reward_to_cost': 'degen_min_reward_to_cost',
              'max_hold_minutes': 'degen_max_hold_minutes'},
}
SHARES_WITH = {'stocks': 'crypto', 'crypto': 'stocks'}
LONG_HOLD = 1440


def lane_fields(market: str) -> dict:
    """The lane's own AgentConfig fields, or a loud refusal. Every name is checked
    against the model, so a typo can never become a silent new attribute."""
    if market not in LANE_FIELDS:
        raise CommandError(f'lane {market} shares fields with {SHARES_WITH.get(market, "another lane")}; '
                           f'not supported')
    for name in LANE_FIELDS[market].values():
        AgentConfig._meta.get_field(name)      # FieldDoesNotExist if the map is wrong
    return LANE_FIELDS[market]


def candidate_timeframes(base: str, wanted: list[str]) -> list[str]:
    """The base plus every wanted timeframe that is a whole multiple of it."""
    out = [base]
    for tf in wanted:
        if tf != base and tf_minutes(tf) > tf_minutes(base) and tf_minutes(tf) % tf_minutes(base) == 0:
            out.append(tf)
    return out


class Command(BaseCommand):
    help = 'Fix a lane: walk-forward its sim strategies across lane regimes and timeframes, then install'

    def add_arguments(self, parser):
        parser.add_argument('--market', required=True, choices=sorted(Market.values))
        parser.add_argument('--strategies', default='', help='comma-separated keys (default: enabled, non-news)')
        parser.add_argument('--timeframes', default='1Hour',
                            help='strategy timeframes to try besides the lane base (whole multiples only)')
        parser.add_argument('--current-regime', action='store_true', help='measure only the lane\'s current regime')
        parser.add_argument('--random', type=int, default=0,
                            help='random-search this many param combos per window instead of the spread grid')
        parser.add_argument('--apply', action='store_true', help='select and install from the saved plan')
        parser.add_argument('--dry-run', action='store_true', help='with --apply: report, change nothing')
        parser.add_argument('--note', default='', help='with --apply: appended to each install\'s history note')

    def handle(self, *args, **o):
        lane_fields(o['market'])               # refuse before measuring or changing anything
        settings.RUN_DIR.mkdir(exist_ok=True)
        plan_path = settings.RUN_DIR / f'fix-lane-{o["market"]}.json'
        if o['apply']:
            # A handful of fixed backtests: no need to queue behind another
            # lane's hours-long measurement.
            self._apply(o['market'], json.loads(plan_path.read_text()), o['dry_run'], o['note'])
            return
        # The nightly research lock: never two walk-forward searches at once.
        lock = open(settings.RUN_DIR / 'auto_research.lock', 'w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise CommandError('auto_research (or another fix_lane) is running — not starting')
        plan = self._measure(o['market'], o['random'], [k for k in o['strategies'].split(',') if k],
                             [t for t in o['timeframes'].split(',') if t], o['current_regime'])
        plan_path.write_text(json.dumps(plan, indent=2, default=str))
        self.stdout.write(self.style.SUCCESS(f'plan written to {plan_path}'))

    # --- phase 1 ---------------------------------------------------------------
    def _measure(self, market: str, random_n: int, keys: list[str], wanted_tfs: list[str],
                 current_only: bool) -> dict:
        cfg = AgentConfig.get()
        fields = lane_fields(market)
        days, train, test = WINDOWS[market]
        end = date.today()
        start = end - timedelta(days=days)
        base = getattr(cfg, fields['timeframe'])
        cur_mrc = float(getattr(cfg, fields['min_reward_to_cost']))
        cur_hold = int(getattr(cfg, fields['max_hold_minutes']))
        rows = Strategy.objects.filter(market=market, enabled=True, stage='sprout').exclude(key='news_catalyst')
        if keys:
            rows = rows.filter(key__in=keys)
        rows = list(rows.order_by('key'))
        timeframes = candidate_timeframes(base, wanted_tfs)
        overrides = {'entry_session': list(ENTRY_SESSIONS)} if market == 'forex' else None
        windows = {'train_days': train, 'test_days': test}
        if random_n:
            windows.update(search='random', max_combos=random_n)
        regimes = ([(cur_mrc, cur_hold)] if current_only else
                   [(mrc, hold) for mrc in (cur_mrc, 2 * cur_mrc) for hold in (cur_hold, LONG_HOLD)])
        plan = {'market': market, 'start': str(start), 'end': str(end), 'measured_at': timezone.now(),
                'base_timeframe': base, 'timeframes': timeframes,
                'current': regime_key(base, cur_mrc, cur_hold), 'strategies': [r.key for r in rows],
                'regimes': {}}
        for mrc, hold in regimes:
            key = regime_key(base, mrc, hold)
            entry = {'risk_overrides': {'min_reward_to_cost': mrc, 'max_hold_minutes': hold},
                     'experiments': {}, 'best': {}, 'combined_oos_net': 0.0}
            for row in rows:
                entry['experiments'][row.key] = {}
                for tf in timeframes:
                    stf = '' if tf == base else tf
                    exp = Experiment.objects.create(
                        strategy_key=row.key, method='walk_forward',
                        param_grid=grid_from_schema(row.key, overrides), symbols=row.symbols, timeframe=base,
                        strategy_timeframe=stf, start=start, end=end, objective='profit_factor', min_trades=10,
                        windows=windows, risk_overrides=entry['risk_overrides'])
                    self.stdout.write(f'{key} {row.key} @{tf}: experiment #{exp.pk}')
                    run_experiment(exp)
                    exp.refresh_from_db()
                    oos = (exp.summary or {}).get('oos') or {}
                    m = {'experiment': exp.pk, 'status': exp.status, 'best_params': exp.best_params,
                         'oos_net': float(oos.get('net_pnl', 0) or 0), 'oos_pf': float(oos.get('profit_factor', 0) or 0),
                         'oos_trades': int(oos.get('trades', 0) or 0)}
                    entry['experiments'][row.key][tf] = m
                    self.stdout.write(f'    OOS {m["oos_trades"]} trades, net {m["oos_net"]:+,.2f}, PF {m["oos_pf"]:.2f}')
                best_tf = max(timeframes, key=lambda t: entry['experiments'][row.key][t]['oos_net'])
                entry['best'][row.key] = best_tf
                entry['combined_oos_net'] += entry['experiments'][row.key][best_tf]['oos_net']
            plan['regimes'][key] = entry
            self.stdout.write(f'== {key}: combined OOS net {entry["combined_oos_net"]:+,.2f} '
                              f'(best timeframes {entry["best"]})')
        return plan

    # --- phase 2 ---------------------------------------------------------------
    def _apply(self, market: str, plan: dict, dry_run: bool, note: str = '') -> None:
        cfg = AgentConfig.get()
        combined = {k: v['combined_oos_net'] for k, v in plan['regimes'].items()}
        chosen, why = choose_regime(combined, plan['current'])
        regime = plan['regimes'][chosen]
        self.stdout.write(f'regime: {why}')
        lines = [f'Regime: {why}.']
        for key in plan['strategies']:
            row = Strategy.objects.get(market=market, key=key)
            tf = regime['best'][key]
            exp = Experiment.objects.get(pk=regime['experiments'][key][tf]['experiment'])
            verdict = f'@{tf}: ' + self._select(row, exp, cfg, dry_run, note)
            self.stdout.write(f'  {key} {verdict}')
            lines.append(f'{key} {verdict}')
        if chosen != plan['current']:
            fields = lane_fields(market)
            ro = regime['risk_overrides']
            change = (f'lane {market}: min_reward_to_cost → {ro["min_reward_to_cost"]:g}, '
                      f'max_hold_minutes → {ro["max_hold_minutes"]}')
            if not dry_run:
                setattr(cfg, fields['min_reward_to_cost'], Decimal(str(ro['min_reward_to_cost'])))
                setattr(cfg, fields['max_hold_minutes'], int(ro['max_hold_minutes']))
                cfg.save(update_fields=[fields['min_reward_to_cost'], fields['max_hold_minutes']])
                # Evidence gathered under the old regime says nothing about the new one.
                fresh = timezone.now() - timedelta(minutes=5)
                for row in Strategy.objects.filter(market=market, key__in=plan['strategies']):
                    if row.evidence_since is None or row.evidence_since < fresh:
                        row.evidence_since = timezone.now()
                        row.save(update_fields=['evidence_since'])
            self.stdout.write(('WOULD CHANGE ' if dry_run else 'CHANGED ') + change)
            lines.append(('Would change ' if dry_run else 'Changed ') + change + '.')
        if not dry_run:
            JournalEntry.objects.create(
                date=date.today(), kind='research', account=Account.for_mode('sim', market),
                title=f'Fix lane {market}: {chosen}', body='\n'.join(lines),
                metrics={'plan': {k: {'combined_oos_net': v['combined_oos_net'], 'best': v.get('best'),
                                      'experiments': v['experiments']} for k, v in plan['regimes'].items()},
                         'chosen': chosen, 'current': plan['current']})

    def _select(self, row: Strategy, exp: Experiment, cfg, dry_run: bool, note: str = '') -> str:
        """Gate first; otherwise the sim fix loop. Never a disable."""
        summary = exp.summary or {}
        validation = summary.get('validation') or {}
        window = validation.get('window')
        champion = {}
        if window:
            # The champion is what is trading: the current params at the current timeframe.
            spec = with_risk_overrides(
                spec_from_models(row.key, row.params, row.symbols, exp.timeframe, cfg,
                                 strategy_timeframe=own_timeframe(row, exp.timeframe)), exp.risk_overrides)
            frames = load_frames(row.symbols, exp.timeframe, exp.start, exp.end)
            champion = evaluate_fixed_params(spec, frames, [window], row.params)['metrics']
        verdict, should_promote = comparable_verdict(exp.best_params or {}, row.params,
                                                     validation.get('candidate') or {}, champion,
                                                     summary.get('oos') or {})
        if should_promote:
            if dry_run:
                return f'WOULD PROMOTE v{row.version + 1}: {verdict}'
            promote(row, exp.best_params, source=f'fix_lane experiment #{exp.pk}', note=note,
                    metrics=validation.get('candidate'),
                    evidence={'kind': 'held_out_validation', 'experiment': exp.pk, 'window': window,
                              'same_bars': True, 'risk_overrides': exp.risk_overrides,
                              'timeframe': exp.strategy_timeframe})
            if (exp.strategy_timeframe or '') != own_timeframe(row, exp.timeframe):
                row.timeframe = exp.strategy_timeframe or ''    # '' = follow the lane's base
                row.save(update_fields=['timeframe'])
            return f'PROMOTED v{row.version}: {verdict}'
        fix, _ = install_if_better(row, exp, cfg, dry_run=dry_run, note=note)
        return f'{fix} (gate: {verdict})'
