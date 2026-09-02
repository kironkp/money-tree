"""Run one backtest from the command line and persist it."""
import json
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError

from main_app.models import AgentConfig, BacktestRun, Instrument
from main_app.services.backtest import run_backtest_for_model
from main_app.services.strategies import STRATEGIES


class Command(BaseCommand):
    help = 'Backtest a strategy: --strategy orb --symbols QQQ,SPY --start 2026-07-01 --end 2026-08-29'

    def add_arguments(self, parser):
        parser.add_argument('--strategy', required=True, choices=list(STRATEGIES))
        parser.add_argument('--symbols', default='', help='default: watchlist symbols the strategy supports')
        parser.add_argument('--timeframe', default='')
        parser.add_argument('--start', default='')
        parser.add_argument('--end', default='')
        parser.add_argument('--params', default='{}', help='JSON overrides')
        parser.add_argument('--cash', type=float, default=None)
        parser.add_argument('--tag', default='cli')

    def handle(self, *args, **o):
        cfg = AgentConfig.get()
        cls = STRATEGIES[o['strategy']]
        if o['symbols']:
            symbols = [s.strip().upper() for s in o['symbols'].split(',')]
        else:
            symbols = [i.symbol for i in Instrument.objects.filter(in_watchlist=True, active=True) if cls.supports(i.asset_class)]
        end = date.fromisoformat(o['end']) if o['end'] else date.today()
        start = date.fromisoformat(o['start']) if o['start'] else end - timedelta(days=60)
        try:
            params = json.loads(o['params'])
        except json.JSONDecodeError as exc:
            raise CommandError(f'--params must be JSON: {exc}')
        run = BacktestRun.objects.create(strategy_key=cls.key, params={**cls.defaults(), **params}, symbols=symbols,
                                         timeframe=o['timeframe'] or cfg.timeframe, start=start, end=end,
                                         starting_cash=o['cash'] if o['cash'] else cfg.starting_cash, tag=o['tag'])
        run_backtest_for_model(run)
        run.refresh_from_db()
        m = run.metrics
        self.stdout.write(self.style.SUCCESS(
            f"run #{run.pk}: {m['trades']} trades, net {m['net_pnl']:+.2f} ({m['return_pct']:+.2f}%), "
            f"win {m['win_rate']:.0f}%, PF {m['profit_factor']:.2f}, Sharpe {m['sharpe']:.2f}, "
            f"maxDD {m['max_drawdown_pct']:.2f}%, bench {m.get('benchmark_return_pct', 0):+.2f}% — {run.duration_s:.1f}s"))
