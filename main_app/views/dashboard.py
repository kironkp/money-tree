from __future__ import annotations

import json
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from main_app.models import AgentConfig, EquitySnapshot, Mode, RiskEvent, Signal, Strategy, Trade
from main_app.services import control, procs
from main_app.services.data import calendar as cal
from main_app.services.journal import day_summary
from main_app.services.risk import RiskConfig, RiskManager

from .common import current_account, parse_days


def _market_state(now):
    s = cal.session_at(now)
    if s:
        return {'open': True, 'label': f'Open · closes {s.close_utc.astimezone(cal.ET):%H:%M} ET',
                'closes_in': int((s.close_utc - now).total_seconds())}
    nxt = cal.next_open(now)
    return {'open': False, 'label': f'Closed · opens {nxt.astimezone(cal.ET):%a %b %-d %H:%M} ET',
            'opens_in': int((nxt - now).total_seconds())}


def _panel_context(request, cfg, account):
    now = timezone.now()
    today = cal.session_date(now)
    positions = list(account.positions.select_related('instrument').order_by('-opened_at'))
    for p in positions:
        p.strategy_name = p.strategy_key or '—'
    day_start = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()), cal.ET)
    trades_today = list(account.trades.select_related('instrument').filter(exit_ts__gte=day_start).order_by('-exit_ts')[:50])
    signals = list(account.signals.select_related('instrument').order_by('-ts', '-pk')[:15])
    risk_events = list(account.risk_events.order_by('-ts')[:8])
    run = control.running_agent()
    rm = RiskManager(RiskConfig.from_model(cfg))
    rm.new_day(today, float(account.day_start_equity))
    equity = float(account.equity)
    loss_used = rm.daily_loss_used_pct(equity)
    day_pnl = float(account.day_pnl)
    pnl_today = sum(float(t.pnl) for t in trades_today)
    return {
        'account': account, 'positions': positions, 'trades_today': trades_today, 'signals': signals,
        'risk_events': risk_events, 'run': run, 'market': _market_state(now), 'now': now,
        'loss_used_pct': loss_used, 'day_pnl': day_pnl, 'pnl_today_closed': pnl_today,
        'positions_used': f'{len([p for p in positions if not p.external])}/{cfg.max_open_positions}',
        'unrealized': sum(float(p.unrealized_pnl) for p in positions),
        'enabled_strategies': Strategy.objects.filter(enabled=True).count(),
        'wins_today': sum(1 for t in trades_today if t.pnl > 0),
    }


@login_required
def dashboard(request):
    cfg = AgentConfig.get()
    account = current_account(request, cfg)
    ctx = _panel_context(request, cfg, account)
    ctx.update({'accounts': ['sim', 'paper', 'replay'] + (['live'] if cfg.mode == Mode.LIVE else []),
                'days': parse_days(request, 7), 'day_options': [1, 7, 30, 90]})
    return render(request, 'dashboard.html', ctx)


@login_required
def dashboard_panels(request):
    cfg = AgentConfig.get()
    account = current_account(request, cfg)
    return render(request, 'partials/dashboard_panels.html', _panel_context(request, cfg, account))


@login_required
def api_equity(request, mode):
    account = current_account(request) if mode == 'current' else None
    if account is None:
        from main_app.models import Account
        account = Account.for_mode(mode)
    days = parse_days(request, 7, 365)
    since = timezone.now() - timedelta(days=days)
    rows = list(account.snapshots.filter(ts__gte=since).order_by('ts').values_list('ts', 'equity'))
    if len(rows) > 2000:
        step = len(rows) // 2000 + 1
        rows = rows[::step]
    data = [[int(ts.timestamp()), float(eq)] for ts, eq in rows]
    return JsonResponse({'equity': data, 'starting_cash': float(account.starting_cash)})


@login_required
@require_POST
def agent_start(request):
    mode = request.POST.get('mode', 'sim')
    if mode not in ('sim', 'paper', 'live'):
        messages.error(request, 'unknown mode')
        return redirect('dashboard')
    if control.running_agent():
        messages.warning(request, 'an agent is already running')
        return redirect('dashboard')
    cfg = AgentConfig.get()
    if mode == 'live' and cfg.mode != Mode.LIVE:
        messages.error(request, 'arm live mode in Settings first')
        return redirect('dashboard')
    pid = procs.spawn_manage(['run_agent', '--mode', mode], f'agent-{mode}')
    messages.success(request, f'agent started in {mode} mode (pid {pid}) — it waits for the next bar')
    return redirect('dashboard')


@login_required
@require_POST
def agent_stop(request):
    run = control.running_agent()
    if run and procs.stop(run.pid):
        messages.success(request, f'stop requested (pid {run.pid}); positions are flattened on the way out')
    else:
        messages.info(request, 'no running agent')
    return redirect('dashboard')


@login_required
@require_POST
def kill_switch(request):
    cfg = AgentConfig.get()
    turn_on = request.POST.get('state') != 'off'
    cfg.kill_switch = turn_on
    cfg.save(update_fields=['kill_switch'])
    if turn_on:
        account = current_account(request, cfg)
        if not control.running_agent(account):
            try:
                n = control.flatten_account(account.mode, 'kill')
                messages.warning(request, f'kill switch ON — {n} positions closed')
            except Exception as exc:
                messages.error(request, f'kill switch ON but flatten failed: {exc}')
        else:
            messages.warning(request, 'kill switch ON — the agent flattens on its next tick')
    else:
        messages.success(request, 'kill switch off — entries allowed again')
    return redirect('dashboard')


@login_required
@require_POST
def flatten_now(request):
    account = current_account(request)
    try:
        n = control.flatten_account(account.mode, 'manual')
        messages.warning(request, f'{n} positions closed on {account.name}')
    except Exception as exc:
        messages.error(request, f'flatten failed: {exc}')
    return redirect('dashboard')


@login_required
def agent_log(request):
    run = control.running_agent()
    name = f'agent-{run.mode}' if run else request.GET.get('name', 'agent-sim')
    return render(request, 'partials/log_tail.html', {'log': procs.tail(name, 60), 'name': name})
