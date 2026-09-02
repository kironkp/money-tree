"""Promotion (params → live strategy row) and the graduation checklist that
gates each stage of Seed → Sprout → Sapling → Tree."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from statistics import median

from django.utils import timezone

from main_app.models import Account, AgentConfig, BacktestRun, Fill, Stage, Strategy, Trade

STAGE_ORDER = [Stage.SEED, Stage.SPROUT, Stage.SAPLING, Stage.TREE]
STAGE_LABEL = {Stage.SEED: 'Seed (backtest only)', Stage.SPROUT: 'Sprout (sim, fake currency)',
               Stage.SAPLING: 'Sapling (Alpaca paper)', Stage.TREE: 'Tree (real money)'}
STAGE_ACCOUNT_MODE = {Stage.SPROUT: 'sim', Stage.SAPLING: 'paper', Stage.TREE: 'live'}

REQUIREMENTS = {
    'sessions': 20, 'trades': 30, 'profit_factor': 1.3, 'max_drawdown_pct': 8.0, 'expectancy_ratio': 0.7,
}


def promote(row: Strategy, params: dict, source: str, note: str = '', metrics: dict | None = None,
            run_id: int | None = None) -> Strategy:
    """Install params as the strategy's live configuration (new version, history kept)."""
    row.version += 1
    row.params = dict(params)
    row.history = (row.history or []) + [{
        'at': timezone.now().isoformat(), 'version': row.version, 'params': dict(params), 'source': source,
        'note': note, 'run_id': run_id, 'metrics': _subset(metrics or {}),
    }]
    row.save()
    return row


def _subset(m: dict) -> dict:
    keys = ('trades', 'net_pnl', 'return_pct', 'win_rate', 'profit_factor', 'expectancy', 'sharpe', 'max_drawdown_pct',
            'benchmark_return_pct', 'alpha_pct')
    return {k: m[k] for k in keys if k in m}


def baseline_metrics(row: Strategy) -> dict:
    """Backtest metrics behind the current params: last promotion, else the
    latest finished backtest for this strategy."""
    for h in reversed(row.history or []):
        if h.get('metrics'):
            return h['metrics']
    run = BacktestRun.objects.filter(strategy_key=row.key, status='done').order_by('-created_at').first()
    return _subset(run.metrics) if run else {}


def next_stage(stage: str) -> str | None:
    i = STAGE_ORDER.index(stage)
    return STAGE_ORDER[i + 1] if i + 1 < len(STAGE_ORDER) else None


def previous_stage(stage: str) -> str | None:
    i = STAGE_ORDER.index(stage)
    return STAGE_ORDER[i - 1] if i > 0 else None


def live_stats(row: Strategy, account: Account | None, days: int = 60) -> dict:
    if account is None:
        return {'trades': 0, 'sessions': 0}
    since = timezone.now() - timedelta(days=days)
    trades = list(Trade.objects.filter(account=account, strategy_key=row.key, exit_ts__gte=since).order_by('exit_ts'))
    n = len(trades)
    pnls = [float(t.pnl) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    equity, peak, max_dd = 0.0, 0.0, 0.0
    start = float(account.starting_cash) or 1.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = min(max_dd, (equity - peak) / (start + peak) * 100)
    sessions = len({t.exit_ts.astimezone(timezone.get_current_timezone()).date() for t in trades})
    fills = Fill.objects.filter(order__account=account, order__strategy_key=row.key, ts__gte=since,
                                realized_slippage_bps__isnull=False).values_list('realized_slippage_bps', flat=True)
    slips = [float(x) for x in fills]
    return {
        'trades': n, 'sessions': sessions, 'net_pnl': sum(pnls), 'win_rate': (len(wins) / n * 100) if n else 0.0,
        'profit_factor': (gross_win / gross_loss) if gross_loss else (gross_win if gross_win else 0.0),
        'expectancy': (sum(pnls) / n) if n else 0.0, 'max_drawdown_pct': max_dd,
        'slippage_median_bps': median(slips) if slips else None, 'last_20_expectancy': (sum(pnls[-20:]) / min(n, 20)) if n else 0.0,
    }


def graduation_checklist(row: Strategy, account: Account | None, cfg: AgentConfig | None = None) -> dict:
    """What it takes to move to the next stage, with current values."""
    cfg = cfg or AgentConfig.get()
    stats = live_stats(row, account)
    base = baseline_metrics(row)
    nxt = next_stage(row.stage)
    items = []
    if row.stage == Stage.SEED:
        ok_bt = bool(base) and base.get('trades', 0) >= REQUIREMENTS['trades'] and base.get('profit_factor', 0) >= 1.0
        items.append({'name': 'A finished backtest with ≥ 30 trades and profit factor ≥ 1.0', 'ok': ok_bt,
                      'actual': f"{base.get('trades', 0)} trades, PF {base.get('profit_factor', 0):.2f}" if base else 'no backtest yet',
                      'required': '30 trades, PF ≥ 1.0'})
        items.append({'name': 'Params promoted from a backtest or experiment', 'ok': bool(row.history),
                      'actual': f'v{row.version}, {len(row.history or [])} promotions', 'required': '≥ 1 promotion'})
    else:
        items.append({'name': f'Sessions traded at this stage', 'ok': stats['sessions'] >= REQUIREMENTS['sessions'],
                      'actual': stats['sessions'], 'required': f"≥ {REQUIREMENTS['sessions']}"})
        items.append({'name': 'Closed trades', 'ok': stats['trades'] >= REQUIREMENTS['trades'],
                      'actual': stats['trades'], 'required': f"≥ {REQUIREMENTS['trades']}"})
        items.append({'name': 'Profit factor', 'ok': stats.get('profit_factor', 0) >= REQUIREMENTS['profit_factor'],
                      'actual': f"{stats.get('profit_factor', 0):.2f}", 'required': f"≥ {REQUIREMENTS['profit_factor']}"})
        items.append({'name': 'Max drawdown', 'ok': stats.get('max_drawdown_pct', 0) >= -REQUIREMENTS['max_drawdown_pct'],
                      'actual': f"{stats.get('max_drawdown_pct', 0):.2f}%", 'required': f"≥ -{REQUIREMENTS['max_drawdown_pct']}%"})
        bt_exp = base.get('expectancy')
        if bt_exp:
            ratio = stats.get('expectancy', 0) / bt_exp if bt_exp else 0
            items.append({'name': 'Live expectancy vs backtest', 'ok': ratio >= REQUIREMENTS['expectancy_ratio'],
                          'actual': f"{stats.get('expectancy', 0):+.2f} vs {bt_exp:+.2f} ({ratio:.0%})",
                          'required': f"≥ {REQUIREMENTS['expectancy_ratio']:.0%} of backtest"})
        slip = stats.get('slippage_median_bps')
        items.append({'name': 'Realized slippage within the configured allowance',
                      'ok': slip is not None and slip <= float(cfg.slippage_bps),
                      'actual': '—' if slip is None else f'{slip:.1f} bps', 'required': f'≤ {cfg.slippage_bps} bps'})
        if row.stage == Stage.SAPLING:
            items.append({'name': 'Note: paper fills are generous (no queue). Start Tree small and recalibrate slippage.',
                          'ok': True, 'actual': '', 'required': ''})
    return {'stage': row.stage, 'stage_label': STAGE_LABEL[row.stage], 'next': nxt,
            'next_label': STAGE_LABEL[nxt] if nxt else None, 'items': items,
            'ready': all(i['ok'] for i in items) and nxt is not None, 'stats': stats, 'baseline': base}
