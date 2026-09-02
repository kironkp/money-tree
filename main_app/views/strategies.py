from __future__ import annotations

from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from main_app.forms import strategy_param_form
from main_app.models import Account, AgentConfig, BacktestRun, Instrument, Stage, Strategy
from main_app.services.backtest import run_backtest_for_model
from main_app.services.promotion import (STAGE_ACCOUNT_MODE, STAGE_LABEL, STAGE_ORDER, graduation_checklist,
                                         live_stats, next_stage, previous_stage)
from main_app.services.strategies import STRATEGIES, get_strategy_class


def _account_for_stage(stage: str) -> Account | None:
    mode = STAGE_ACCOUNT_MODE.get(stage)
    return Account.for_mode(mode) if mode else None


@login_required
def strategy_list(request):
    cfg = AgentConfig.get()
    rows = []
    for row in Strategy.objects.all():
        cls = STRATEGIES.get(row.key)
        if cls is None:
            continue
        account = _account_for_stage(row.stage) or Account.for_mode('sim')
        rows.append({'row': row, 'cls': cls, 'stats': live_stats(row, account, 30),
                     'stage_label': STAGE_LABEL[row.stage], 'account': account,
                     'crypto': 'crypto' in cls.asset_classes})
    missing = [cls for key, cls in STRATEGIES.items() if not Strategy.objects.filter(key=key).exists()]
    return render(request, 'strategies/list.html', {'rows': rows, 'missing': missing, 'cfg': cfg})


@login_required
def strategy_detail(request, key):
    row = get_object_or_404(Strategy, key=key)
    cls = get_strategy_class(key)
    cfg = AgentConfig.get()
    Form = strategy_param_form(cls, row.params)
    instruments = [i for i in Instrument.objects.filter(in_watchlist=True, active=True) if cls.supports(i.asset_class)]
    if request.method == 'POST':
        action = request.POST.get('action', 'save')
        if action == 'toggle':
            row.enabled = not row.enabled
            row.save(update_fields=['enabled'])
            messages.success(request, f'{row.name} {"enabled" if row.enabled else "disabled"}')
            return redirect('strategy-detail', key=key)
        if action in ('stage_up', 'stage_down'):
            target = next_stage(row.stage) if action == 'stage_up' else previous_stage(row.stage)
            if target:
                row.stage = target
                row.history = (row.history or []) + [{'at': str(date.today()), 'stage': target, 'source': 'manual stage change'}]
                row.save(update_fields=['stage', 'history'])
                messages.success(request, f'{row.name} is now {STAGE_LABEL[target]}')
            return redirect('strategy-detail', key=key)
        form = Form(request.POST)
        if form.is_valid():
            params = {k: v for k, v in form.cleaned_data.items() if v is not None}
            for p in cls.params:
                if p.type == 'bool':
                    params[p.name] = bool(form.cleaned_data.get(p.name))
                elif p.type == 'choice':
                    params[p.name] = p.coerce(type(p.default)(params[p.name])) if p.name in params else p.default
            changed = params != row.params
            row.params = params
            row.symbols = [s for s in request.POST.getlist('symbols') if Instrument.objects.filter(symbol=s).exists()]
            row.allocation_pct = request.POST.get('allocation_pct') or row.allocation_pct
            row.notes = request.POST.get('notes', row.notes)
            if changed:
                row.version += 1
                row.history = (row.history or []) + [{'at': str(date.today()), 'version': row.version, 'params': params,
                                                      'source': 'manual edit'}]
            row.save()
            messages.success(request, 'saved' + (f' as v{row.version}' if changed else ''))
            return redirect('strategy-detail', key=key)
    else:
        form = Form()
    account = _account_for_stage(row.stage) or Account.for_mode('sim')
    check = graduation_checklist(row, account, cfg)
    runs = BacktestRun.objects.filter(strategy_key=key, status='done').order_by('-created_at')[:8]
    recent_trades = account.trades.filter(strategy_key=key).select_related('instrument').order_by('-exit_ts')[:15]
    return render(request, 'strategies/detail.html', {
        'row': row, 'cls': cls, 'form': form, 'instruments': instruments, 'selected': set(row.symbols or []),
        'check': check, 'runs': runs, 'recent_trades': recent_trades, 'account': account,
        'stage_order': [(s, STAGE_LABEL[s]) for s in STAGE_ORDER], 'crypto': 'crypto' in cls.asset_classes,
        'cfg': cfg, 'history': list(reversed(row.history or []))[:10],
    })


@login_required
@require_POST
def strategy_backtest(request, key):
    row = get_object_or_404(Strategy, key=key)
    cfg = AgentConfig.get()
    days = int(request.POST.get('days', 60) or 60)
    end = date.today()
    run = BacktestRun.objects.create(strategy_key=key, params=row.params, symbols=row.symbols, timeframe=row.timeframe,
                                     start=end - timedelta(days=days), end=end, starting_cash=cfg.starting_cash,
                                     tag=f'{row.key} v{row.version}')
    try:
        run_backtest_for_model(run)
    except Exception as exc:
        messages.error(request, f'backtest failed: {exc}')
        return redirect('strategy-detail', key=key)
    messages.success(request, f'backtest #{run.pk} finished')
    return redirect('backtest-detail', pk=run.pk)


@login_required
@require_POST
def strategy_create_missing(request):
    from main_app.services.strategies import all_strategies
    n = 0
    stocks = list(Instrument.objects.filter(in_watchlist=True).exclude(asset_class='crypto').values_list('symbol', flat=True))
    for cls in all_strategies():
        _, created = Strategy.objects.get_or_create(key=cls.key, defaults={
            'name': cls.name, 'params': cls.defaults(), 'timeframe': cls.default_timeframe, 'symbols': stocks,
            'notes': cls.description})
        n += created
    messages.success(request, f'{n} strategy rows created')
    return redirect('strategy-list')
