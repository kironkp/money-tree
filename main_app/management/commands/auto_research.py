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
from main_app.services.optimize import evaluate_fixed_params, grid_from_schema, run_experiment
from main_app.services.promotion import promote

MIN_OOS_PF = 1.1
MIN_VALIDATION_TRADES = 10


def evidence_passes(metrics: dict, min_trades: int = MIN_VALIDATION_TRADES,
                    min_pf: float = MIN_OOS_PF) -> bool:
    """Minimum truth gate for either a confirmation or a promotion."""
    return (
        int(metrics.get('trades', 0) or 0) >= min_trades
        and float(metrics.get('profit_factor', 0) or 0) >= min_pf
        and float(metrics.get('net_pnl', 0) or 0) > 0
        and float(metrics.get('expectancy', 0) or 0) > 0
    )


def comparable_verdict(best_params: dict, current_params: dict, candidate: dict,
                       champion: dict) -> tuple[str, bool]:
    """Decide from candidate/champion results measured on identical bars.

    Returns (human verdict, should_promote). Re-finding current parameters is
    only a confirmation when the held-out evidence gate also passes.
    """
    if not best_params or not candidate:
        return 'kept current params — no valid held-out candidate', False
    same = dict(best_params) == {k: current_params.get(k) for k in best_params}
    candidate_pf = float(candidate.get('profit_factor', 0) or 0)
    champion_pf = float(champion.get('profit_factor', 0) or 0)
    candidate_net = float(candidate.get('net_pnl', 0) or 0)
    champion_net = float(champion.get('net_pnl', 0) or 0)
    if same:
        if evidence_passes(candidate):
            return f'confirmed current params (held-out PF {candidate_pf:.2f})', False
        return (f'current params NOT confirmed — held-out evidence failed '
                f'({candidate.get("trades", 0)} trades, PF {candidate_pf:.2f}, net {candidate_net:+,.2f})'), False
    if evidence_passes(candidate) and candidate_pf > champion_pf * 1.05 and candidate_net > champion_net:
        return (f'PROMOTE: held-out PF {candidate_pf:.2f} vs current {champion_pf:.2f}, '
                f'net {candidate_net:+,.2f} vs {champion_net:+,.2f}'), True
    return (f'kept current params — candidate failed the held-out gate or did not beat the champion '
            f'(candidate PF {candidate_pf:.2f}, current {champion_pf:.2f})'), False


class Command(BaseCommand):
    help = 'Walk-forward every enabled strategy on trailing data; promote only proven improvements'

    def add_arguments(self, parser):
        parser.add_argument('--market', default='', help='stocks|crypto|degen|forex (default: all)')
        parser.add_argument('--days', type=int, default=0, help='trailing window (default per market)')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **o):
        cfg = AgentConfig.get()
        rows = Strategy.objects.filter(enabled=True)
        if o['market']:
            rows = rows.filter(market=o['market'])
        end = date.today()
        for row in rows:
            # Forex history comes from Yahoo, which keeps 59 days of intraday bars.
            days = o['days'] or {'stocks': 240, 'crypto': 540, 'degen': 6, 'forex': 58}[row.market]
            train, test = {'stocks': (120, 40), 'crypto': (180, 60), 'degen': (3, 1), 'forex': (21, 7)}[row.market]
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
            summary = exp.summary or {}
            adaptive_oos = summary.get('oos') or {}
            validation = summary.get('validation') or {}
            candidate = validation.get('candidate') or {}
            validation_window = validation.get('window')
            # Champion and candidate are evaluated on the exact same final
            # held-out window. The stitched adaptive result remains useful as
            # a diagnostic, but cannot justify installing one static config.
            spec = spec_from_models(row.key, row.params, row.symbols, tf, cfg)
            frames = load_frames(row.symbols, tf, start, end)
            bench = load_frames([spec.benchmark_symbol], tf, start, end).get(spec.benchmark_symbol)
            if bench is not None and len(bench) == 0:
                bench = None
            champion = (evaluate_fixed_params(spec, frames, [validation_window], row.params, bench)['metrics']
                        if validation_window else {})
            verdict, should_promote = comparable_verdict(exp.best_params or {}, row.params, candidate, champion)
            if should_promote:
                verdict = f'PROMOTED v{row.version + 1}: ' + verdict.removeprefix('PROMOTE: ')
                if not o['dry_run']:
                    promote(row, exp.best_params, source=f'auto-research experiment #{exp.pk}', metrics=candidate)
            account = Account.for_mode(cfg.mode, row.market)
            JournalEntry.objects.create(
                date=end, kind='auto_eod', account=account, title=f'Auto-research {row.key} ({row.market}): {verdict}',
                body=f'Walk-forward #{exp.pk} over {start}→{end}: adaptive-policy diagnostic '
                     f'{adaptive_oos.get("trades", 0)} trades, net {adaptive_oos.get("net_pnl", 0):+,.2f}, '
                     f'PF {adaptive_oos.get("profit_factor", 0):.2f}, decay {summary.get("decay")}. '
                     f'On the identical final held-out window, candidate: {candidate.get("trades", 0)} trades, '
                     f'PF {candidate.get("profit_factor", 0):.2f}, net {candidate.get("net_pnl", 0):+,.2f}; '
                     f'current champion: {champion.get("trades", 0)} trades, '
                     f'PF {champion.get("profit_factor", 0):.2f}, net {champion.get("net_pnl", 0):+,.2f}. '
                     f'Recommended: {exp.best_params}.',
                metrics={'experiment': exp.pk, 'adaptive_oos': adaptive_oos,
                         'validation_window': validation_window, 'candidate': candidate, 'champion': champion})
            self.stdout.write(self.style.SUCCESS(f'  {verdict}'))
