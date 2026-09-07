from __future__ import annotations

from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from main_app.forms import strategy_param_form
from main_app.models import (Account, AgentConfig, BacktestRun, Instrument, Qualification, Stage, Strategy,
                             market_for_symbols)
from main_app.services.backtest import run_backtest_for_model
from main_app.services.promotion import (STAGE_ACCOUNT_MODE, STAGE_LABEL, STAGE_ORDER, graduation_checklist,
                                         live_stats, next_stage, previous_stage, qualification_assessment)
from main_app.services.strategies import STRATEGIES, get_strategy_class

from .common import deny_observer, operator_required


def _account_for_stage(stage: str, market: str) -> Account | None:
    mode = STAGE_ACCOUNT_MODE.get(stage)
    return Account.for_mode(mode, market) if mode else None


@login_required
def strategy_list(request):
    cfg = AgentConfig.get()
    groups = {'stocks': [], 'crypto': [], 'degen': [], 'forex': []}
    for row in Strategy.objects.all():
        cls = STRATEGIES.get(row.key)
        if cls is None:
            continue
        account = _account_for_stage(row.stage, row.market) or Account.for_mode('sim', row.market)
        groups.setdefault(row.market, []).append({'row': row, 'cls': cls, 'stats': live_stats(row, account, 30),
                                                  'stage_label': STAGE_LABEL[row.stage], 'account': account,
                                                  'crypto': row.market == 'crypto'})
    missing = [cls for key, cls in STRATEGIES.items() if not Strategy.objects.filter(key=key).exists()]
    return render(request, 'strategies/list.html', {'groups': groups, 'missing': missing, 'cfg': cfg})


@login_required
def strategy_detail(request, market, key):
    row = get_object_or_404(Strategy, key=key, market=market)
    cls = get_strategy_class(key)
    cfg = AgentConfig.get()
    Form = strategy_param_form(cls, row.params)
    instruments = [i for i in Instrument.objects.filter(in_watchlist=True, active=True, market=market)
                   if cls.supports(i.asset_class)]
    if request.method == 'POST':
        denied = deny_observer(request)
        if denied is not None:
            return denied
        action = request.POST.get('action', 'save')
        if action == 'toggle':
            row.enabled = not row.enabled
            row.save(update_fields=['enabled'])
            messages.success(request, f'{row.name} {"enabled" if row.enabled else "disabled"}')
            return redirect('strategy-detail', market=market, key=key)
        if action in ('stage_up', 'stage_down'):
            target = next_stage(row.stage) if action == 'stage_up' else previous_stage(row.stage)
            if target and action == 'stage_up':
                account = _account_for_stage(row.stage, market) or Account.for_mode('sim', market)
                check = graduation_checklist(row, account, cfg)
                broker_target = target in (Stage.SAPLING, Stage.TREE)
                if broker_target and row.qualification != Qualification.QUALIFIED:
                    messages.error(request, f'not ready for {STAGE_LABEL[target]}: statistical qualification is required and cannot be overridden')
                    return redirect('strategy-detail', market=market, key=key)
                override = request.POST.get('override') == 'yes'
                if not check['ready'] and (not override or broker_target):
                    failing = '; '.join(i['name'] for i in check['items'] if not i['ok'])
                    suffix = (' This broker-backed step cannot be overridden.' if broker_target
                              else ' Tick "override" and give a reason to force it.')
                    messages.error(request, f'not ready for {STAGE_LABEL[target]}: {failing}.{suffix}')
                    return redirect('strategy-detail', market=market, key=key)
                source = 'graduated (checklist green)' if check['ready'] else f'OVERRIDE: {request.POST.get("override_reason", "")[:120] or "no reason given"}'
            else:
                source = 'manual stage change'
            if target:
                row.stage = target
                row.history = (row.history or []) + [{'at': str(date.today()), 'stage': target, 'source': source}]
                row.save(update_fields=['stage', 'history'])
                messages.success(request, f'{row.name} is now {STAGE_LABEL[target]} ({source})')
            return redirect('strategy-detail', market=market, key=key)
        if action == 'reset_qualification' and row.qualification == Qualification.QUARANTINED:
            row.qualification = Qualification.UNPROVEN
            row.qualification_reason = 'operator reset quarantine; this version must collect fresh evidence'
            row.qualification_updated_at = timezone.now()
            row.history = (row.history or []) + [{'at': str(date.today()), 'version': row.version,
                                                  'source': 'qualification reset'}]
            row.save(update_fields=['qualification', 'qualification_reason', 'qualification_updated_at', 'history'])
            messages.success(request, 'quarantine reset to unproven; re-enable only for simulator observation')
            return redirect('strategy-detail', market=market, key=key)
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
            row.timeframe = cfg.timeframe_for(market)  # the market's timeframe is the only one that runs
            row.notes = request.POST.get('notes', row.notes)
            if changed:
                row.version += 1
                row.qualification = Qualification.UNPROVEN
                row.qualification_reason = 'manual parameter edit created a new unproven version'
                row.qualification_updated_at = timezone.now()
                row.history = (row.history or []) + [{'at': str(date.today()), 'version': row.version, 'params': params,
                                                      'source': 'manual edit'}]
            row.save()
            messages.success(request, 'saved' + (f' as v{row.version}' if changed else ''))
            return redirect('strategy-detail', market=market, key=key)
    else:
        form = Form()
    account = _account_for_stage(row.stage, market) or Account.for_mode('sim', market)
    check = graduation_checklist(row, account, cfg)
    qualification = qualification_assessment(row, account)
    runs = [r for r in BacktestRun.objects.filter(strategy_key=key, status='done').order_by('-created_at')[:30]
            if market_for_symbols(r.symbols) == market][:8]
    recent_trades = account.trades.filter(strategy_key=key).select_related('instrument').order_by('-exit_ts')[:15]
    return render(request, 'strategies/detail.html', {
        'row': row, 'cls': cls, 'form': form, 'instruments': instruments, 'selected': set(row.symbols or []),
        'check': check, 'qualification': qualification, 'runs': runs, 'recent_trades': recent_trades, 'account': account,
        'stage_order': [(s, STAGE_LABEL[s]) for s in STAGE_ORDER], 'crypto': 'crypto' in cls.asset_classes,
        'cfg': cfg, 'history': list(reversed(row.history or []))[:10],
    })


@operator_required
@require_POST
def strategy_backtest(request, market, key):
    row = get_object_or_404(Strategy, key=key, market=market)
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
        return redirect('strategy-detail', market=market, key=key)
    messages.success(request, f'backtest #{run.pk} finished')
    return redirect('backtest-detail', pk=run.pk)


@operator_required
@require_POST
def strategy_create_missing(request):
    from main_app.services.strategies import all_strategies
    n = 0
    stocks = list(Instrument.objects.filter(in_watchlist=True).exclude(asset_class='crypto').values_list('symbol', flat=True))
    cryptos = list(Instrument.objects.filter(in_watchlist=True, asset_class='crypto').values_list('symbol', flat=True))
    for cls in all_strategies():
        markets = [('stocks', stocks)] + ([('crypto', cryptos)] if 'crypto' in cls.asset_classes else [])
        for market, symbols in markets:
            _, created = Strategy.objects.get_or_create(key=cls.key, market=market, defaults={
                'name': cls.name, 'params': cls.defaults(), 'timeframe': cls.default_timeframe, 'symbols': symbols,
                'notes': cls.description})
            n += created
    messages.success(request, f'{n} strategy rows created')
    return redirect('strategy-list')


@operator_required
@require_POST
def portfolio_backtest(request, market):
    """Every enabled strategy of a market together — conflicts and capital contention included."""
    cfg = AgentConfig.get()
    rows = list(Strategy.objects.filter(market=market, enabled=True))
    if not rows:
        messages.error(request, f'no enabled {market} strategies')
        return redirect('strategy-list')
    symbols = sorted({s for r in rows for s in (r.symbols or [])})
    days = int(request.POST.get('days', 60) or 60)
    end = date.today()
    run = BacktestRun.objects.create(
        strategy_key='portfolio', symbols=symbols, timeframe=cfg.timeframe_for(market), start=end - timedelta(days=days), end=end,
        starting_cash=cfg.starting_cash, tag=f'{market} portfolio ({len(rows)} strategies)',
        params={'strategies': [{'key': r.key, 'params': r.params, 'symbols': r.symbols, 'allocation_pct': float(r.allocation_pct)}
                               for r in rows]})
    try:
        run_backtest_for_model(run)
    except Exception as exc:
        messages.error(request, f'portfolio backtest failed: {exc}')
        return redirect('strategy-list')
    messages.success(request, f'portfolio backtest #{run.pk}: {run.metrics.get("trades", 0)} trades, net {run.metrics.get("net_pnl", 0):+.2f}')
    return redirect('backtest-detail', pk=run.pk)
