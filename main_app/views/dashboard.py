from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from main_app.models import Account, AgentConfig, Market, Mode, RiskEvent, Strategy, SymbolState, TradeCard
from main_app.services import control, procs
from main_app.services.data import calendar as cal
from main_app.services.status import build_status, portfolio_risk

from .common import account_tabs, current_account, operator_required, parse_days


def _panel_context(request, cfg, account):
    now = timezone.now()
    today = cal.session_date(now)
    run = control.running_agent(account)
    positions = {p.instrument.symbol: p for p in account.positions.select_related('instrument')}
    day_start = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()), cal.ET)
    trades_today = list(account.trades.select_related('instrument').filter(exit_ts__gte=day_start).order_by('-exit_ts')[:50])
    signals = list(account.signals.select_related('instrument', 'order').order_by('-ts', '-pk')[:12])
    active_cards = list(account.cards.filter(status__in=TradeCard.OPEN).order_by('-opened_at'))
    for c in active_cards:
        p = positions.get(c.symbol)
        c.position = p
        c.dist_stop = p.distance_pct(p.stop_price) if p else None
        c.dist_target = p.distance_pct(p.target_price) if p else None
        c.age_min = int((now - (c.opened_at or c.created_at)).total_seconds() // 60)
    pending_cards = list(account.cards.filter(status__in=TradeCard.PENDING).order_by('-created_at'))
    open_orders = list(account.orders.select_related('instrument').filter(status__in=['new', 'accepted', 'partially_filled'])
                       .order_by('-submitted_at')[:20])
    alerts = list(account.risk_events.filter(kind__in=RiskEvent.ALERT_KINDS, acknowledged_at__isnull=True).order_by('-ts')[:20])
    states = list(account.symbol_states.order_by('-proximity', 'symbol'))
    for st in states:
        st.failing = [r for r in (st.rules or []) if not r.get('ok')]
        st.passing = [r for r in (st.rules or []) if r.get('ok')]
    return {
        'account': account, 'run': run, 'status': build_status(account, cfg, run), 'risk': portfolio_risk(account, cfg),
        'positions': list(positions.values()), 'trades_today': trades_today, 'signals': signals,
        'active_cards': active_cards, 'pending_cards': pending_cards, 'open_orders': open_orders,
        'alerts': alerts, 'states': states[:8], 'now': now,
        'pnl_today_closed': sum(float(t.pnl) for t in trades_today), 'wins_today': sum(1 for t in trades_today if t.pnl > 0),
        'unrealized': sum(float(p.unrealized_pnl) for p in positions.values()),
        'enabled_strategies': Strategy.objects.filter(enabled=True, market=account.market).count(),
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
def status_strip(request):
    cfg = AgentConfig.get()
    account = current_account(request, cfg)
    run = control.running_agent(account)
    return render(request, 'partials/status_strip.html', {'account': account, 'status': build_status(account, cfg, run),
                                                          'risk': portfolio_risk(account, cfg)})


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


def _back(request, account):
    return redirect(f"{request.build_absolute_uri('/')}?{account.query}")


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
        return _back(request, account)
    if mode == 'live' and cfg.mode != Mode.LIVE:
        messages.error(request, 'arm live mode in Settings first')
        return redirect('dashboard')
    pid = procs.spawn_manage(['run_agent', '--mode', mode, '--market', market], account.log_name)
    messages.success(request, f'{market} agent started in {mode} mode (pid {pid}) — watch the decision journal')
    return _back(request, account)


@operator_required
@require_POST
def agent_stop(request):
    account = current_account(request)
    run = control.running_agent(account)
    if run:
        run.stop_requested = True
        run.save(update_fields=['stop_requested'])
        procs.stop(run.pid)
        if account.mode == Mode.LIVE:
            from django.conf import settings
            what = 'positions are flattened on the way out' if settings.LIVE_FLATTEN_ON_EXIT else \
                'LIVE positions stay OPEN with their broker-side stops (LIVE_FLATTEN_ON_EXIT=0)'
        else:
            what = 'open positions are flattened on the way out'
        messages.success(request, f'stop requested for the {account.market} agent (pid {run.pid}) — it stops within seconds; {what}')
    else:
        messages.info(request, f'no running {account.market} agent')
    return _back(request, account)


@operator_required
@require_POST
def kill_switch(request):
    cfg = AgentConfig.get()
    turn_on = request.POST.get('state') != 'off'
    cfg.kill_switch = turn_on
    cfg.save(update_fields=['kill_switch'])
    account = current_account(request, cfg)
    if turn_on:
        closed, running = 0, []
        for acct in (Account.for_mode(cfg.mode, Market.STOCKS), Account.for_mode(cfg.mode, Market.CRYPTO)):
            if control.running_agent(acct):
                running.append(acct.market)
            else:
                try:
                    closed += control.flatten_account(acct.mode, 'kill', acct.market)
                except Exception as exc:
                    messages.error(request, f'kill switch ON but flatten of {acct.name} failed: {exc}')
        note = f'; running agents ({", ".join(running)}) flatten within 2 s' if running else ''
        messages.warning(request, f'KILL SWITCH ON for every agent — {closed} positions closed here{note}. Entries stay blocked until you reset it.')
    else:
        messages.success(request, 'kill switch off — entries allowed again')
    return _back(request, account)


@operator_required
@require_POST
def flatten_now(request):
    account = current_account(request)
    if control.running_agent(account):
        messages.info(request, 'an agent is running on this account — use the kill switch (immediate) or stop the agent')
        return _back(request, account)
    try:
        n = control.flatten_account(account.mode, 'manual', account.market)
        messages.warning(request, f'{n} positions closed on {account.name}')
    except Exception as exc:
        messages.error(request, f'flatten failed: {exc}')
    return _back(request, account)


@operator_required
@require_POST
def alert_ack(request, pk):
    account = current_account(request)
    if pk == 0:
        account.risk_events.filter(acknowledged_at__isnull=True).update(acknowledged_at=timezone.now())
    else:
        RiskEvent.objects.filter(pk=pk).update(acknowledged_at=timezone.now())
    return _back(request, account)


@operator_required
@require_POST
def card_decide(request, pk, verdict):
    card = get_object_or_404(TradeCard, pk=pk)
    account = card.account
    if card.status != 'awaiting_approval':
        messages.info(request, 'that entry is no longer waiting')
        return _back(request, account)
    if verdict == 'approve':
        card.status = 'approved'
        card.approved_by = request.user.email or request.user.username
        card.save(update_fields=['status', 'approved_by'])
        messages.success(request, f'{card.symbol} entry approved — the agent submits it on its next control poll (≤ 2 s)')
    else:
        card.status, card.error = 'rejected', 'rejected by operator'
        card.save(update_fields=['status', 'error'])
        messages.info(request, f'{card.symbol} entry rejected')
    return _back(request, account)


@login_required
def agent_log(request):
    name = request.GET.get('name') or current_account(request).log_name
    return render(request, 'partials/log_tail.html', {'log': procs.tail(name, 60), 'name': name})
