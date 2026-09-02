from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from main_app.models import Account, AgentConfig, Market, Mode, Strategy
from main_app.services import control, procs
from main_app.services.data import calendar as cal
from main_app.services.risk import RiskConfig, RiskManager

from .common import account_tabs, current_account, operator_required, parse_days


def _market_state(now, market: str):
    if market == Market.CRYPTO:
        return {'open': True, 'label': 'Crypto · open 24/7'}
    s = cal.session_at(now)
    if s:
        return {'open': True, 'label': f'Stocks open · closes {s.close_utc.astimezone(cal.ET):%H:%M} ET'}
    nxt = cal.next_open(now)
    return {'open': False, 'label': f'Stocks closed · opens {nxt.astimezone(cal.ET):%a %b %-d %H:%M} ET'}


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
    run = control.running_agent(account)
    rm = RiskManager(RiskConfig.from_model(cfg))
    rm.new_day(today, float(account.day_start_equity))
    equity = float(account.equity)
    return {
        'account': account, 'positions': positions, 'trades_today': trades_today, 'signals': signals,
        'risk_events': risk_events, 'run': run, 'market': _market_state(now, account.market), 'now': now,
        'loss_used_pct': rm.daily_loss_used_pct(equity), 'day_pnl': float(account.day_pnl),
        'pnl_today_closed': sum(float(t.pnl) for t in trades_today),
        'positions_used': f'{len([p for p in positions if not p.external])}/{cfg.max_open_positions}',
        'unrealized': sum(float(p.unrealized_pnl) for p in positions),
        'enabled_strategies': Strategy.objects.filter(enabled=True, market=account.market).count(),
        'wins_today': sum(1 for t in trades_today if t.pnl > 0),
    }


@login_required
def dashboard(request):
    cfg = AgentConfig.get()
    account = current_account(request, cfg)
    ctx = _panel_context(request, cfg, account)
    ctx.update({'tabs': account_tabs(cfg), 'days': parse_days(request, 7), 'day_options': [1, 7, 30, 90]})
    return render(request, 'dashboard.html', ctx)


@login_required
def dashboard_panels(request):
    cfg = AgentConfig.get()
    account = current_account(request, cfg)
    return render(request, 'partials/dashboard_panels.html', _panel_context(request, cfg, account))


@login_required
def api_equity(request, mode):
    account = current_account(request) if mode == 'current' else Account.for_mode(mode, request.GET.get('market', Market.STOCKS))
    days = parse_days(request, 7, 365)
    since = timezone.now() - timedelta(days=days)
    rows = list(account.snapshots.filter(ts__gte=since).order_by('ts').values_list('ts', 'equity'))
    if len(rows) > 2000:
        step = len(rows) // 2000 + 1
        rows = rows[::step]
    return JsonResponse({'equity': [[int(ts.timestamp()), float(eq)] for ts, eq in rows],
                         'starting_cash': float(account.starting_cash)})


@operator_required
@require_POST
def agent_start(request):
    mode = request.POST.get('mode', 'sim')
    market = request.POST.get('market', Market.STOCKS)
    if mode not in ('sim', 'paper', 'live') or market not in Market.values:
        messages.error(request, 'unknown mode or market')
        return redirect('dashboard')
    cfg = AgentConfig.get()
    account = Account.for_mode(mode, market)
    if control.running_agent(account):
        messages.warning(request, f'the {market} {mode} agent is already running')
        return redirect(f"{request.build_absolute_uri('/')}?{account.query}")
    if mode == 'live' and cfg.mode != Mode.LIVE:
        messages.error(request, 'arm live mode in Settings first')
        return redirect('dashboard')
    pid = procs.spawn_manage(['run_agent', '--mode', mode, '--market', market], account.log_name)
    messages.success(request, f'{market} agent started in {mode} mode (pid {pid}) — watch the live feed')
    return redirect(f"{request.build_absolute_uri('/')}?{account.query}")


@operator_required
@require_POST
def agent_stop(request):
    account = current_account(request)
    run = control.running_agent(account)
    if run and procs.stop(run.pid):
        messages.success(request, f'stop requested for the {account.market} agent (pid {run.pid}); positions are flattened on the way out')
    else:
        messages.info(request, f'no running {account.market} agent')
    return redirect(f"{request.build_absolute_uri('/')}?{account.query}")


@operator_required
@require_POST
def kill_switch(request):
    cfg = AgentConfig.get()
    turn_on = request.POST.get('state') != 'off'
    cfg.kill_switch = turn_on
    cfg.save(update_fields=['kill_switch'])
    account = current_account(request, cfg)
    if turn_on:
        closed = 0
        for acct in (Account.for_mode(cfg.mode, Market.STOCKS), Account.for_mode(cfg.mode, Market.CRYPTO)):
            if not control.running_agent(acct):
                try:
                    closed += control.flatten_account(acct.mode, 'kill', acct.market)
                except Exception as exc:
                    messages.error(request, f'kill switch ON but flatten of {acct.name} failed: {exc}')
        messages.warning(request, f'kill switch ON for every agent — {closed} positions closed here; running agents flatten on their next tick')
    else:
        messages.success(request, 'kill switch off — entries allowed again')
    return redirect(f"{request.build_absolute_uri('/')}?{account.query}")


@operator_required
@require_POST
def flatten_now(request):
    account = current_account(request)
    try:
        n = control.flatten_account(account.mode, 'manual', account.market)
        messages.warning(request, f'{n} positions closed on {account.name}')
    except Exception as exc:
        messages.error(request, f'flatten failed: {exc}')
    return redirect(f"{request.build_absolute_uri('/')}?{account.query}")


@login_required
def agent_log(request):
    name = request.GET.get('name') or current_account(request).log_name
    return render(request, 'partials/log_tail.html', {'log': procs.tail(name, 60), 'name': name})
