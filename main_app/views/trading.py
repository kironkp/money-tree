from __future__ import annotations

import csv

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import render

from main_app.models import AgentConfig, Fill, Order, Signal, Strategy, Trade

from .common import current_account, parse_days, since_days


def _filters(request, qs, date_field: str):
    strategy = request.GET.get('strategy', '')
    symbol = request.GET.get('symbol', '').upper()
    days = parse_days(request, 30, 730)
    if strategy:
        qs = qs.filter(strategy_key=strategy)
    if symbol:
        qs = qs.filter(instrument__symbol=symbol)
    qs = qs.filter(**{f'{date_field}__gte': since_days(days)})
    return qs, {'strategy': strategy, 'symbol': symbol, 'days': days,
                'strategies': list(Strategy.objects.values_list('key', flat=True))}


@login_required
def positions(request):
    account = current_account(request)
    rows = account.positions.select_related('instrument').order_by('-opened_at')
    return render(request, 'trading/positions.html', {'account': account, 'positions': rows,
                                                      'unrealized': sum(float(p.unrealized_pnl) for p in rows)})


@login_required
def orders(request):
    account = current_account(request)
    qs, f = _filters(request, account.orders.select_related('instrument'), 'submitted_at')
    status = request.GET.get('status', '')
    if status:
        qs = qs.filter(status=status)
    page = Paginator(qs.order_by('-submitted_at'), 100).get_page(request.GET.get('page'))
    return render(request, 'trading/orders.html', {'account': account, 'page': page, 'f': f, 'status': status})


@login_required
def trades(request):
    account = current_account(request)
    qs, f = _filters(request, account.trades.select_related('instrument'), 'exit_ts')
    qs = qs.order_by('-exit_ts')
    total = sum(float(t.pnl) for t in qs)
    wins = qs.filter(pnl__gt=0).count()
    n = qs.count()
    page = Paginator(qs, 100).get_page(request.GET.get('page'))
    return render(request, 'trading/trades.html', {
        'account': account, 'page': page, 'f': f, 'total': total, 'count': n,
        'win_rate': (wins / n * 100) if n else 0})


@login_required
def trades_csv(request):
    account = current_account(request)
    qs, _ = _filters(request, account.trades.select_related('instrument'), 'exit_ts')
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="moneytree-{account.mode}-trades.csv"'
    w = csv.writer(resp)
    w.writerow(['symbol', 'strategy', 'side', 'qty', 'entry_ts', 'exit_ts', 'entry_price', 'exit_price', 'pnl',
                'pnl_pct', 'fees', 'bars_held', 'exit_reason'])
    for t in qs.order_by('exit_ts'):
        w.writerow([t.instrument.symbol, t.strategy_key, t.side, t.qty, t.entry_ts.isoformat(), t.exit_ts.isoformat(),
                    t.entry_price, t.exit_price, t.pnl, t.pnl_pct, t.fees, t.bars_held, t.exit_reason])
    return resp


@login_required
def signals(request):
    account = current_account(request)
    qs, f = _filters(request, account.signals.select_related('instrument', 'order'), 'ts')
    which = request.GET.get('which', '')
    if which == 'acted':
        qs = qs.filter(acted=True)
    elif which == 'blocked':
        qs = qs.filter(acted=False).exclude(blocked_reason='')
    blocked_counts = {}
    for r in qs.filter(acted=False).exclude(blocked_reason='').values_list('blocked_reason', flat=True):
        k = r.split(' (')[0]
        blocked_counts[k] = blocked_counts.get(k, 0) + 1
    page = Paginator(qs.order_by('-ts', '-pk'), 100).get_page(request.GET.get('page'))
    return render(request, 'trading/signals.html', {'account': account, 'page': page, 'f': f, 'which': which,
                                                    'blocked_counts': sorted(blocked_counts.items(), key=lambda kv: -kv[1])})


@login_required
def risk_events(request):
    account = current_account(request)
    days = parse_days(request, 30, 730)
    qs = account.risk_events.filter(ts__gte=since_days(days)).order_by('-ts')
    page = Paginator(qs, 100).get_page(request.GET.get('page'))
    return render(request, 'trading/risk_events.html', {'account': account, 'page': page, 'days': days})
