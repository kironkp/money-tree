"""Nightly self-improvement: for every enabled strategy, walk-forward the
trailing window on real bars; promote the search's recommendation only when
its out-of-sample edge beats the current params' out-of-sample edge and clears
costs. Everything is written to the journal so the operator can see what the
machine tried, what it kept, and why."""
from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from main_app.models import Account, AgentConfig, Experiment, JournalEntry, Strategy
from main_app.services.backtest import load_frames, run_backtest, spec_from_models
from main_app.services.metrics import objective_value
from main_app.services.optimize import grid_from_schema, run_experiment
from main_app.services.promotion import promote

MIN_OOS_PF = 1.1


class Command(BaseCommand):
    help = 'Walk-forward every enabled strategy on trailing data; promote only proven improvements'

    def add_arguments(self, parser):
        parser.add_argument('--market', default='', help='stocks|crypto|degen (default: all)')
        parser.add_argument('--days', type=int, default=0, help='trailing window (default per market)')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **o):
        cfg = AgentConfig.get()
        rows = Strategy.objects.filter(enabled=True)
        if o['market']:
            rows = rows.filter(market=o['market'])
        end = date.today()
        for row in rows:
            days = o['days'] or {'stocks': 240, 'crypto': 540, 'degen': 6}[row.market]
            train, test = {'stocks': (120, 40), 'crypto': (180, 60), 'degen': (3, 1)}[row.market]
            start = end - timedelta(days=days)
            tf = cfg.timeframe_for(row.market)
            self.stdout.write(f'{row.key} ({row.market}) — walk-forward {start}→{end} on {tf}, train {train}d / test {test}d')
            exp = Experiment.objects.create(strategy_key=row.key, method='walk_forward', param_grid=grid_from_schema(row.key),
                                            symbols=row.symbols, timeframe=tf, start=start, end=end, objective='profit_factor',
                                            min_trades=10, windows={'train_days': train, 'test_days': test})
            try:
                run_experiment(exp)
            except Exception as exc:
                self.stderr.write(f'  failed: {exc!r}')
                continue
            exp.refresh_from_db()
            oos = (exp.summary or {}).get('oos') or {}
            # Baseline: the CURRENT params over the same stitched test windows.
            spec = spec_from_models(row.key, row.params, row.symbols, tf, cfg)
            frames = load_frames(row.symbols, tf, start, end)
            base = run_backtest(spec, frames).metrics
            new_pf, base_pf = float(oos.get('profit_factor', 0)), float(base.get('profit_factor', 0))
            verdict = 'kept current params'
            same = {k: v for k, v in (exp.best_params or {}).items()} == {k: row.params.get(k) for k in (exp.best_params or {})}
            if same and exp.best_params:
                verdict = f'confirmed current params (OOS PF {new_pf:.2f})'
            elif exp.best_params and oos.get('trades', 0) >= 10 and new_pf >= MIN_OOS_PF and new_pf > base_pf * 1.05:
                verdict = f'PROMOTED v{row.version + 1}: OOS PF {new_pf:.2f} vs current {base_pf:.2f}'
                if not o['dry_run']:
                    promote(row, exp.best_params, source=f'auto-research experiment #{exp.pk}', metrics=oos)
            elif new_pf < 1.0 and base_pf < 1.0:
                verdict = f'no edge either way (search OOS PF {new_pf:.2f}, current {base_pf:.2f})'
            account = Account.for_mode(cfg.mode, row.market)
            JournalEntry.objects.create(
                date=end, kind='auto_eod', account=account, title=f'Auto-research {row.key} ({row.market}): {verdict}',
                body=f'Walk-forward #{exp.pk} over {start}→{end}: out-of-sample {oos.get("trades", 0)} trades, '
                     f'net {oos.get("net_pnl", 0):+,.2f}, PF {new_pf:.2f}, decay {(exp.summary or {}).get("decay")}. '
                     f'Current params over the same span: {base.get("trades", 0)} trades, PF {base_pf:.2f}. '
                     f'Recommended: {exp.best_params}.',
                metrics={'experiment': exp.pk, 'oos': oos, 'baseline': {k: base.get(k) for k in ('trades', 'net_pnl', 'profit_factor')}})
            self.stdout.write(self.style.SUCCESS(f'  {verdict}'))
