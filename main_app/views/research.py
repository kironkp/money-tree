from __future__ import annotations

import json
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from main_app.models import Account, AgentConfig, Bar, BacktestRun, Experiment, Instrument, Market, Mode, Strategy, market_for_symbols
from main_app.services import control, procs
from main_app.services.backtest import run_backtest_for_model
from main_app.services.data import calendar as cal
from main_app.services.optimize import grid_from_schema
from main_app.services.promotion import promote
from main_app.services.strategies import STRATEGIES, get_strategy_class

from .common import deny_observer, operator_required


def _symbols_for(request, cls):
    chosen = request.POST.getlist('symbols')
    if chosen:
        return [s for s in chosen if Instrument.objects.filter(symbol=s).exists()]
    return [i.symbol for i in Instrument.objects.filter(in_watchlist=True, active=True) if cls.supports(i.asset_class)]


def _parse_params(request, cls):
    params = {}
    for p in cls.params:
        raw = request.POST.get(f'param_{cls.key}_{p.name}')
        if p.type == 'bool':
            params[p.name] = raw in ('on', 'true', '1')
        elif raw not in (None, ''):
            try:
                params[p.name] = p.coerce(raw)
            except (TypeError, ValueError):
                params[p.name] = p.default
        else:
            params[p.name] = p.default
    return params


def _launch_context():
    cfg = AgentConfig.get()
    instruments = list(Instrument.objects.filter(in_watchlist=True, active=True))
    coverage = {}
    for i in instruments:
        first = Bar.objects.filter(instrument=i, timeframe=cfg.timeframe).order_by('ts').values_list('ts', flat=True).first()
        last = Bar.objects.filter(instrument=i, timeframe=cfg.timeframe).order_by('-ts').values_list('ts', flat=True).first()
        coverage[i.symbol] = (first, last)
    strategies = []
    for key, cls in STRATEGIES.items():
        row = Strategy.objects.filter(key=key).first()
        strategies.append({'cls': cls, 'row': row, 'schema': cls.schema(), 'params': (row.params if row else cls.defaults()),
                           'grid': grid_from_schema(key)})
    end = date.today()
    return {'cfg': cfg, 'instruments': instruments, 'coverage': coverage, 'strategies': strategies,
            'default_start': end - timedelta(days=60), 'default_end': end,
            'objectives': ['sharpe', 'profit_factor', 'net_pnl', 'expectancy', 'sortino']}


@login_required
def backtest_list(request):
    if request.method == 'POST':
        denied = deny_observer(request)
        if denied is not None:
            return denied
        cls = get_strategy_class(request.POST.get('strategy', 'orb'))
        cfg = AgentConfig.get()
        try:
            start = date.fromisoformat(request.POST.get('start'))
            end = date.fromisoformat(request.POST.get('end'))
        except (TypeError, ValueError):
            messages.error(request, 'pick a start and end date')
            return redirect('backtest-list')
        run = BacktestRun.objects.create(
            strategy_key=cls.key, params=_parse_params(request, cls), symbols=_symbols_for(request, cls),
            timeframe=request.POST.get('timeframe') or cfg.timeframe, start=start, end=end,
            starting_cash=request.POST.get('cash') or cfg.starting_cash, tag=request.POST.get('tag', '')[:60])
        try:
            run_backtest_for_model(run)
        except Exception as exc:
            messages.error(request, f'backtest failed: {exc}')
            return redirect('backtest-detail', pk=run.pk)
        messages.success(request, f'backtest #{run.pk}: {run.metrics.get("trades", 0)} trades, net {run.metrics.get("net_pnl", 0):+.2f}')
        return redirect('backtest-detail', pk=run.pk)
    qs = BacktestRun.objects.filter(experiment__isnull=True).order_by('-created_at')
    page = Paginator(qs, 40).get_page(request.GET.get('page'))
    ctx = _launch_context()
    ctx.update({'page': page})
    return render(request, 'research/backtests.html', ctx)


@login_required
def backtest_detail(request, pk):
    run = get_object_or_404(BacktestRun, pk=pk)
    trades = run.trades.order_by('-exit_ts')[:300]
    strategies = Strategy.objects.filter(key=run.strategy_key, market=market_for_symbols(run.symbols))
    m = run.metrics or {}
    breakdowns = [('By symbol', m.get('per_symbol', {})), ('By hour (ET)', m.get('per_hour', {})),
                  ('By weekday', m.get('per_weekday', {})), ('By exit', m.get('per_exit_reason', {}))]
    return render(request, 'research/backtest_detail.html', {
        'run': run, 'm': m, 'trades': trades, 'strategies': strategies, 'breakdowns': breakdowns,
        'equity_json': json.dumps(run.equity_curve), 'params_json': json.dumps(run.params, indent=1),
        'blocked': sorted((m.get('blocked_reasons') or {}).items(), key=lambda kv: -kv[1]),
    })


@login_required
def api_backtest_equity(request, pk):
    run = get_object_or_404(BacktestRun, pk=pk)
    return JsonResponse({'equity': run.equity_curve, 'starting_cash': float(run.starting_cash)})


@operator_required
@require_POST
def backtest_promote(request, pk):
    run = get_object_or_404(BacktestRun, pk=pk)
    row = Strategy.objects.filter(key=run.strategy_key, market=market_for_symbols(run.symbols)).first()
    if row is None:
        messages.error(request, 'no strategy row to promote into — visit Strategies first')
        return redirect('backtest-detail', pk=pk)
    promote(row, run.params, source=f'backtest #{run.pk}', note=request.POST.get('note', ''), metrics=run.metrics, run_id=run.pk)
    if request.POST.get('symbols') == 'yes':
        row.symbols = run.symbols
        row.save(update_fields=['symbols'])
    messages.success(request, f'{row.name} is now v{row.version} with these params')
    return redirect('strategy-detail', market=row.market, key=row.key)


@operator_required
@require_POST
def backtest_delete(request, pk):
    run = get_object_or_404(BacktestRun, pk=pk)
    run.delete()
    messages.success(request, f'backtest #{pk} deleted')
    return redirect('backtest-list')


@login_required
def backtest_compare(request):
    ids = [int(x) for x in request.GET.get('ids', '').split(',') if x.strip().isdigit()][:4]
    runs = list(BacktestRun.objects.filter(pk__in=ids))
    keys = ['trades', 'net_pnl', 'return_pct', 'win_rate', 'profit_factor', 'expectancy', 'sharpe', 'sortino',
            'max_drawdown_pct', 'exposure_pct', 'avg_hold_minutes', 'fees', 'benchmark_return_pct', 'alpha_pct']
    rows = [(k, [(r.metrics or {}).get(k) for r in runs]) for k in keys]
    return render(request, 'research/compare.html', {'runs': runs, 'rows': rows,
                                                     'curves': json.dumps([{'label': f'#{r.pk} {r.strategy_key}', 'data': r.equity_curve} for r in runs])})


# --- experiments ---------------------------------------------------------

@login_required
def experiment_list(request):
    if request.method == 'POST':
        denied = deny_observer(request)
        if denied is not None:
            return denied
        cls = get_strategy_class(request.POST.get('strategy', 'orb'))
        cfg = AgentConfig.get()
        try:
            start = date.fromisoformat(request.POST.get('start'))
            end = date.fromisoformat(request.POST.get('end'))
        except (TypeError, ValueError):
            messages.error(request, 'pick a start and end date')
            return redirect('experiment-list')
        overrides = {}
        for p in cls.params:
            raw = request.POST.get(f'grid_{cls.key}_{p.name}', '').strip()
            if raw:
                vals = []
                for tok in raw.replace(';', ',').split(','):
                    tok = tok.strip()
                    if not tok:
                        continue
                    try:
                        vals.append(p.coerce(tok if p.type != 'bool' else tok.lower() in ('1', 'true', 'on', 'yes')))
                    except (TypeError, ValueError):
                        pass
                if vals:
                    overrides[p.name] = vals
        grid = grid_from_schema(cls.key, overrides)
        method = request.POST.get('method', 'walk_forward')
        exp = Experiment.objects.create(
            strategy_key=cls.key, method=method, param_grid=grid, symbols=_symbols_for(request, cls),
            timeframe=request.POST.get('timeframe') or cfg.timeframe, start=start, end=end,
            objective=request.POST.get('objective', 'sharpe'), min_trades=int(request.POST.get('min_trades', 10) or 10),
            windows={'train_days': int(request.POST.get('train_days', 40) or 40),
                     'test_days': int(request.POST.get('test_days', 15) or 15)})
        procs.spawn_manage(['optimize', '--experiment', str(exp.pk)], f'experiment-{exp.pk}', nice=10)
        messages.success(request, f'experiment #{exp.pk} started in the background')
        return redirect('experiment-detail', pk=exp.pk)
    page = Paginator(Experiment.objects.order_by('-created_at'), 30).get_page(request.GET.get('page'))
    ctx = _launch_context()
    ctx.update({'page': page, 'agent_running': bool(control.running_agent())})
    return render(request, 'research/experiments.html', ctx)


@login_required
def experiment_detail(request, pk):
    exp = get_object_or_404(Experiment, pk=pk)
    s = exp.summary or {}
    ranked = s.get('ranked', [])
    param_names = list(exp.param_grid.keys())
    strategy = Strategy.objects.filter(key=exp.strategy_key, market=market_for_symbols(exp.symbols)).first()
    runs = exp.runs.order_by('window_label', '-created_at')[:60] if exp.method == 'walk_forward' else []
    return render(request, 'research/experiment_detail.html', {
        'exp': exp, 's': s, 'ranked': ranked[:40], 'param_names': param_names, 'strategy': strategy,
        'windows': s.get('windows', []), 'oos': s.get('oos'), 'runs': runs,
        'oos_json': json.dumps(s.get('oos_equity', [])), 'freq': sorted((s.get('param_frequency') or {}).items(), key=lambda kv: -kv[1]),
        'log': procs.tail(f'experiment-{exp.pk}', 20), 'stability': s.get('stability'),
    })


@login_required
def experiment_progress(request, pk):
    exp = get_object_or_404(Experiment, pk=pk)
    return render(request, 'partials/experiment_progress.html', {'exp': exp, 'log': procs.tail(f'experiment-{exp.pk}', 8)})


@operator_required
@require_POST
def experiment_promote(request, pk):
    exp = get_object_or_404(Experiment, pk=pk)
    params = exp.best_params
    raw = request.POST.get('params_json')
    if raw:
        try:
            params = json.loads(raw)
        except json.JSONDecodeError:
            messages.error(request, 'bad params JSON')
            return redirect('experiment-detail', pk=pk)
    if not params:
        messages.error(request, 'no params to promote')
        return redirect('experiment-detail', pk=pk)
    row = Strategy.objects.filter(key=exp.strategy_key, market=market_for_symbols(exp.symbols)).first()
    if row is None:
        messages.error(request, 'no strategy row to promote into')
        return redirect('experiment-detail', pk=pk)
    metrics = (exp.summary or {}).get('oos') or ((exp.summary or {}).get('ranked') or [{}])[0]
    promote(row, params, source=f'experiment #{exp.pk} ({exp.method})', note=request.POST.get('note', ''),
            metrics=metrics if isinstance(metrics, dict) else {}, run_id=None)
    messages.success(request, f'{row.name} is now v{row.version}')
    return redirect('strategy-detail', market=row.market, key=row.key)


@operator_required
@require_POST
def experiment_stop(request, pk):
    exp = get_object_or_404(Experiment, pk=pk)
    if procs.stop(exp.pid):
        exp.status, exp.error = 'failed', 'stopped by user'
        exp.save(update_fields=['status', 'error'])
        messages.info(request, 'experiment stopped')
    else:
        messages.info(request, 'experiment process not running')
    return redirect('experiment-detail', pk=pk)


@operator_required
@require_POST
def experiment_delete(request, pk):
    exp = get_object_or_404(Experiment, pk=pk)
    procs.stop(exp.pid)
    exp.delete()
    messages.success(request, f'experiment #{pk} deleted')
    return redirect('experiment-list')


# --- replay ----------------------------------------------------------------

@login_required
def replay(request):
    cfg = AgentConfig.get()
    market = request.GET.get('market') or request.POST.get('market') or Market.STOCKS
    if market not in Market.values:
        market = Market.STOCKS
    replay_account = Account.for_mode(Mode.REPLAY, market)
    if request.method == 'POST':
        denied = deny_observer(request)
        if denied is not None:
            return denied
        try:
            d = date.fromisoformat(request.POST.get('date', ''))
        except ValueError:
            messages.error(request, 'pick a date')
            return redirect('replay')
        if control.running_agent(replay_account):
            messages.error(request, f'a {market} replay is already running — stop it from the dashboard first')
            return redirect(f"{request.build_absolute_uri('/replay/')}?market={market}")
        speed = request.POST.get('speed', '30')
        pid = procs.spawn_manage(['run_agent', '--replay', d.isoformat(), '--speed', speed, '--market', market],
                                 replay_account.log_name)
        messages.success(request, f'replaying {d} ({market}) at {speed}× (pid {pid}) — watch the live feed')
        return redirect(f"{request.build_absolute_uri('/')}?{replay_account.query}")
    classes = ('crypto',) if market == Market.CRYPTO else ('stock', 'etf')
    dates = Bar.objects.filter(timeframe=cfg.timeframe, instrument__in_watchlist=True,
                               instrument__asset_class__in=classes).dates('ts', 'day', order='DESC')[:40]
    sessions = [d for d in dates if market == Market.CRYPTO or cal.session_for(d)]
    enabled = Strategy.objects.filter(enabled=True, market=market).count()
    return render(request, 'research/replay.html', {'dates': sessions, 'cfg': cfg, 'enabled': enabled, 'market': market,
                                                    'log': procs.tail(replay_account.log_name, 30),
                                                    'run': control.running_agent(replay_account)})
