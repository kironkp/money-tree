"""Run an Experiment (parameter search) — used by the web UI as a subprocess
and directly from the CLI."""
import json
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError

from main_app.models import AgentConfig, Experiment, Instrument
from main_app.services.optimize import grid_from_schema, run_experiment
from main_app.services.strategies import STRATEGIES


class Command(BaseCommand):
    help = 'Run an experiment: --experiment ID, or create one with --strategy/--method/--start/--end'

    def add_arguments(self, parser):
        parser.add_argument('--experiment', type=int, default=0)
        parser.add_argument('--strategy', default='', choices=[''] + list(STRATEGIES))
        parser.add_argument('--method', default='walk_forward', choices=['grid', 'random', 'walk_forward'])
        parser.add_argument('--symbols', default='')
        parser.add_argument('--start', default='')
        parser.add_argument('--end', default='')
        parser.add_argument('--objective', default='sharpe')
        parser.add_argument('--train-days', type=int, default=40)
        parser.add_argument('--test-days', type=int, default=15)
        parser.add_argument('--grid', default='', help='JSON {param: [values]} overrides')

    def handle(self, *args, **o):
        if o['experiment']:
            exp = Experiment.objects.filter(pk=o['experiment']).first()
            if exp is None:
                raise CommandError('no such experiment')
        else:
            if not o['strategy']:
                raise CommandError('--strategy is required when creating an experiment')
            cfg = AgentConfig.get()
            cls = STRATEGIES[o['strategy']]
            symbols = ([s.strip().upper() for s in o['symbols'].split(',')] if o['symbols'] else
                       [i.symbol for i in Instrument.objects.filter(in_watchlist=True, active=True) if cls.supports(i.asset_class)])
            end = date.fromisoformat(o['end']) if o['end'] else date.today()
            start = date.fromisoformat(o['start']) if o['start'] else end - timedelta(days=90)
            grid = grid_from_schema(cls.key, json.loads(o['grid']) if o['grid'] else None)
            exp = Experiment.objects.create(strategy_key=cls.key, method=o['method'], param_grid=grid, symbols=symbols,
                                            timeframe=cfg.timeframe, start=start, end=end, objective=o['objective'],
                                            windows={'train_days': o['train_days'], 'test_days': o['test_days']})
        run_experiment(exp)
        exp.refresh_from_db()
        self.stdout.write(self.style.SUCCESS(f'experiment #{exp.pk} {exp.status}: best {exp.best_params}'))
        s = exp.summary
        if 'oos' in s:
            m = s['oos']
            self.stdout.write(f"  OOS: {m['trades']} trades, net {m['net_pnl']:+.2f}, PF {m['profit_factor']:.2f}, "
                              f"Sharpe {m['sharpe']:.2f}, decay {s.get('decay')}")
        elif s.get('ranked'):
            r = s['ranked'][0]
            self.stdout.write(f"  best objective {r['objective']:.3f} ({r['trades']} trades, net {r['net_pnl']:+.2f}), "
                              f"stability {s.get('stability')}")
