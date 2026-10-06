"""One-off (2026-10-06): fix a lane's failing sim strategies across lane regimes.

A regime is (timeframe, min_reward_to_cost, max_hold_minutes): lane-wide
settings, so they are chosen for the lane, not per strategy. For each regime and
each target strategy, walk-forward the strategy's own parameters under it. The
lane moves off its current regime only if another regime's combined adaptive
out-of-sample net (each strategy at its best params for that regime) beats the
current one's. Then, per strategy, under the chosen regime: promote normally if
the candidate passes the gate; else install it in sim if it beats the current
version on the same OOS windows; else keep it. Nothing is ever disabled.

Two phases, so the expensive part runs once and its numbers can be read before
anything changes:

    manage.py fix_lane --market forex                  measure; writes run/fix-lane-forex.json
    manage.py fix_lane --market forex --apply          select and install from that plan

The forex 15Min window overlaps 2026-09-08..09-24, which MT-A003 had kept
untouched; that window is used for selection here (see docs/LOGBOOK.md).
"""
from __future__ import annotations

import fcntl
import json
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from main_app.models import AgentConfig, Experiment, JournalEntry, Account, Market, Strategy
from main_app.services.backtest import load_frames, spec_from_models
from main_app.services.fix_loop import choose_regime, install_if_better, regime_key
from main_app.services.optimize import evaluate_fixed_params, grid_from_schema, run_experiment, with_risk_overrides
from main_app.services.promotion import promote
from main_app.services.strategies.base import ENTRY_SESSIONS

from .auto_research import comparable_verdict

WINDOWS = {'forex': (58, 21, 7), 'degen': (120, 30, 10)}   # days, train, test — as auto_research
# The AgentConfig field behind each regime value, per lane. Only lanes that own
# all three: crypto and stocks share min_reward_to_cost and max_hold_minutes, so
# moving one would silently move the other.
LANE_FIELDS = {
    'forex': {'timeframe': 'forex_timeframe', 'min_reward_to_cost': 'forex_min_reward_to_cost',
              'max_hold_minutes': 'forex_max_hold_minutes'},
    'degen': {'timeframe': 'degen_timeframe', 'min_reward_to_cost': 'degen_min_reward_to_cost',
              'max_hold_minutes': 'degen_max_hold_minutes'},
}
SHARES_WITH = {'stocks': 'crypto', 'crypto': 'stocks'}


def lane_fields(market: str) -> dict:
    """The lane's own AgentConfig fields, or a loud refusal. Every name is checked
    against the model, so a typo can never become a silent new attribute."""
    if market not in LANE_FIELDS:
        raise CommandError(f'lane {market} shares fields with {SHARES_WITH.get(market, "another lane")}; '
                           f'not supported')
    for name in LANE_FIELDS[market].values():
        AgentConfig._meta.get_field(name)      # FieldDoesNotExist if the map is wrong
    return LANE_FIELDS[market]
TIMEFRAMES = ('15Min', '1Hour')
LONG_HOLD = 1440


class Command(BaseCommand):
    help = 'Fix a lane: walk-forward its sim strategies across lane regimes, then install by the sim policy'

    def add_arguments(self, parser):
        parser.add_argument('--market', required=True, choices=sorted(Market.values))
        parser.add_argument('--random', type=int, default=0,
                            help='random-search this many param combos per window instead of the spread grid')
        parser.add_argument('--apply', action='store_true', help='select and install from the saved plan')
        parser.add_argument('--dry-run', action='store_true', help='with --apply: report, change nothing')

    def handle(self, *args, **o):
        lane_fields(o['market'])               # refuse before measuring or changing anything
        settings.RUN_DIR.mkdir(exist_ok=True)
        plan_path = settings.RUN_DIR / f'fix-lane-{o["market"]}.json'
        if o['apply']:
            # A handful of fixed backtests: no need to queue behind another
            # lane's hours-long measurement.
            self._apply(o['market'], json.loads(plan_path.read_text()), o['dry_run'])
        else:
            # The nightly research lock: never two walk-forward searches at once.
            lock = open(settings.RUN_DIR / 'auto_research.lock', 'w')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise CommandError('auto_research (or another fix_lane) is running — not starting')
            plan = self._measure(o['market'], o['random'])
            plan_path.write_text(json.dumps(plan, indent=2, default=str))
            self.stdout.write(self.style.SUCCESS(f'plan written to {plan_path}'))

    # --- phase 1 ---------------------------------------------------------------
    def _measure(self, market: str, random_n: int) -> dict:
        cfg = AgentConfig.get()
        days, train, test = WINDOWS[market]
        end = date.today()
        start = end - timedelta(days=days)
        fields = lane_fields(market)
        cur_tf = getattr(cfg, fields['timeframe'])
        cur_mrc = float(getattr(cfg, fields['min_reward_to_cost']))
        cur_hold = int(getattr(cfg, fields['max_hold_minutes']))
        rows = list(Strategy.objects.filter(market=market, enabled=True, stage='sprout')
                    .exclude(key='news_catalyst').order_by('key'))
        overrides = {'entry_session': list(ENTRY_SESSIONS)} if market == 'forex' else None
        windows = {'train_days': train, 'test_days': test}
        if random_n:
            windows.update(search='random', max_combos=random_n)
        regimes = [(tf, mrc, hold) for tf in TIMEFRAMES for mrc in (cur_mrc, 2 * cur_mrc)
                   for hold in (cur_hold, LONG_HOLD)]
        plan = {'market': market, 'start': str(start), 'end': str(end), 'measured_at': timezone.now(),
                'current': regime_key(cur_tf, cur_mrc, cur_hold), 'strategies': [r.key for r in rows],
                'regimes': {}}
        for tf, mrc, hold in regimes:
            key = regime_key(tf, mrc, hold)
            entry = {'timeframe': tf, 'risk_overrides': {'min_reward_to_cost': mrc, 'max_hold_minutes': hold},
                     'experiments': {}, 'combined_oos_net': 0.0}
            for row in rows:
                exp = Experiment.objects.create(
                    strategy_key=row.key, method='walk_forward', param_grid=grid_from_schema(row.key, overrides),
                    symbols=row.symbols, timeframe=tf, start=start, end=end, objective='profit_factor',
                    min_trades=10, windows=windows, risk_overrides=entry['risk_overrides'])
                self.stdout.write(f'{key} {row.key}: experiment #{exp.pk}')
                run_experiment(exp)
                exp.refresh_from_db()
                oos = (exp.summary or {}).get('oos') or {}
                entry['experiments'][row.key] = {
                    'experiment': exp.pk, 'status': exp.status, 'best_params': exp.best_params,
                    'oos_net': float(oos.get('net_pnl', 0) or 0), 'oos_pf': float(oos.get('profit_factor', 0) or 0),
                    'oos_trades': int(oos.get('trades', 0) or 0)}
                entry['combined_oos_net'] += entry['experiments'][row.key]['oos_net']
                self.stdout.write(f'    OOS {entry["experiments"][row.key]["oos_trades"]} trades, '
                                  f'net {entry["experiments"][row.key]["oos_net"]:+,.2f}, '
                                  f'PF {entry["experiments"][row.key]["oos_pf"]:.2f}')
            plan['regimes'][key] = entry
            self.stdout.write(f'== {key}: combined OOS net {entry["combined_oos_net"]:+,.2f}')
        return plan

    # --- phase 2 ---------------------------------------------------------------
    def _apply(self, market: str, plan: dict, dry_run: bool) -> None:
        cfg = AgentConfig.get()
        combined = {k: v['combined_oos_net'] for k, v in plan['regimes'].items()}
        chosen, why = choose_regime(combined, plan['current'])
        regime = plan['regimes'][chosen]
        self.stdout.write(f'regime: {why}')
        lines = [f'Regime: {why}.']
        for key in plan['strategies']:
            row = Strategy.objects.get(market=market, key=key)
            exp = Experiment.objects.get(pk=regime['experiments'][key]['experiment'])
            verdict = self._select(row, exp, cfg, dry_run)
            self.stdout.write(f'  {key}: {verdict}')
            lines.append(f'{key}: {verdict}')
        if chosen != plan['current']:
            tf = regime['timeframe']
            ro = regime['risk_overrides']
            fields = lane_fields(market)
            change = (f'lane {market}: timeframe {getattr(cfg, fields["timeframe"])} → {tf}, min_reward_to_cost → '
                      f'{ro["min_reward_to_cost"]:g}, max_hold_minutes → {ro["max_hold_minutes"]}')
            if not dry_run:
                setattr(cfg, fields['timeframe'], tf)
                setattr(cfg, fields['min_reward_to_cost'], Decimal(str(ro['min_reward_to_cost'])))
                setattr(cfg, fields['max_hold_minutes'], int(ro['max_hold_minutes']))
                cfg.save(update_fields=list(fields.values()))
                # Evidence gathered under the old regime says nothing about the new one.
                # Rows promoted a moment ago already restarted theirs.
                fresh = timezone.now() - timedelta(minutes=5)
                Strategy.objects.filter(Q(evidence_since__isnull=True) | Q(evidence_since__lt=fresh),
                                        market=market, key__in=plan['strategies']
                                        ).update(evidence_since=timezone.now())
            self.stdout.write(('WOULD CHANGE ' if dry_run else 'CHANGED ') + change)
            lines.append(('Would change ' if dry_run else 'Changed ') + change + '.')
        if not dry_run:
            JournalEntry.objects.create(
                date=date.today(), kind='research', account=Account.for_mode('sim', market),
                title=f'Fix lane {market}: {chosen}', body='\n'.join(lines),
                metrics={'plan': {k: {'combined_oos_net': v['combined_oos_net'],
                                      'experiments': v['experiments']} for k, v in plan['regimes'].items()},
                         'chosen': chosen, 'current': plan['current']})

    def _select(self, row: Strategy, exp: Experiment, cfg, dry_run: bool) -> str:
        """Gate first; otherwise the sim fix loop. Never a disable."""
        summary = exp.summary or {}
        validation = summary.get('validation') or {}
        window = validation.get('window')
        champion = {}
        if window:
            spec = with_risk_overrides(spec_from_models(row.key, row.params, row.symbols, exp.timeframe, cfg),
                                       exp.risk_overrides)
            frames = load_frames(row.symbols, exp.timeframe, exp.start, exp.end)
            champion = evaluate_fixed_params(spec, frames, [window], row.params)['metrics']
        verdict, should_promote = comparable_verdict(exp.best_params or {}, row.params,
                                                     validation.get('candidate') or {}, champion,
                                                     summary.get('oos') or {})
        if should_promote:
            if dry_run:
                return f'WOULD PROMOTE v{row.version + 1}: {verdict}'
            promote(row, exp.best_params, source=f'fix_lane experiment #{exp.pk}',
                    metrics=validation.get('candidate'),
                    evidence={'kind': 'held_out_validation', 'experiment': exp.pk, 'window': window,
                              'same_bars': True, 'risk_overrides': exp.risk_overrides})
            return f'PROMOTED v{row.version}: {verdict}'
        fix, _ = install_if_better(row, exp, cfg, dry_run=dry_run)
        return f'{fix} (gate: {verdict})'
