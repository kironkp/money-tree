from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from main_app.forms import InstrumentForm
from main_app.models import AgentConfig, Bar, Instrument, Trade
from main_app.services import procs
from main_app.services.data import calendar as cal
from main_app.services.data import provider_status
from main_app.services.data.store import coverage, load_frame, quality_gate


@login_required
def data_index(request):
    cfg = AgentConfig.get()
    rows = []
    for inst in Instrument.objects.all():
        cov = coverage(inst, cfg.timeframe)
        rows.append({'inst': inst, 'cov': cov})
    now = timezone.now()
    s = cal.session_at(now)
    return render(request, 'data/index.html', {
        'rows': rows, 'cfg': cfg, 'form': InstrumentForm(), 'providers': provider_status(),
        'market_open': bool(s), 'next_open': cal.next_open(now), 'now': now,
        'log': procs.tail('sync', 25), 'timeframes': ['1Min', '5Min', '15Min'],
        'upcoming': [x for x in cal.sessions_between(now.date(), (now + timedelta(days=21)).date()) if x.early_close][:3],
        'total_bars': Bar.objects.count(),
    })


@login_required
@require_POST
def instrument_add(request):
    form = InstrumentForm(request.POST)
    if form.is_valid():
        inst = form.save()
        messages.success(request, f'{inst.symbol} added to the watchlist')
    else:
        messages.error(request, '; '.join(f'{k}: {", ".join(v)}' for k, v in form.errors.items()))
    return redirect('data-index')


@login_required
@require_POST
def instrument_toggle(request, slug):
    inst = get_object_or_404(Instrument, symbol=Instrument.symbol_from_slug(slug))
    inst.in_watchlist = not inst.in_watchlist
    inst.save(update_fields=['in_watchlist'])
    messages.success(request, f'{inst.symbol} {"added to" if inst.in_watchlist else "removed from"} the watchlist')
    return redirect('data-index')


@login_required
@require_POST
def instrument_delete(request, slug):
    inst = get_object_or_404(Instrument, symbol=Instrument.symbol_from_slug(slug))
    if inst.positions.exists() or inst.trades.exists() or inst.orders.exists():
        messages.error(request, f'{inst.symbol} has ledger history — remove it from the watchlist instead')
    else:
        inst.delete()
        messages.success(request, f'{inst.symbol} deleted')
    return redirect('data-index')


@login_required
@require_POST
def data_sync(request):
    cfg = AgentConfig.get()
    days = request.POST.get('days', '60')
    provider = request.POST.get('provider', '')
    tf = request.POST.get('timeframe', cfg.timeframe)
    args = ['sync_bars', '--days', str(int(days)), '--timeframe', tf]
    if provider:
        args += ['--provider', provider]
    symbols = request.POST.get('symbols', '').strip()
    if symbols:
        args += ['--symbols', symbols]
    if request.POST.get('resync'):
        args.append('--resync')
    pid = procs.spawn_manage(args, 'sync')
    messages.success(request, f'history sync started (pid {pid}) — the log below updates as it runs')
    return redirect('data-index')


@login_required
def sync_log(request):
    return render(request, 'partials/log_tail.html', {'log': procs.tail('sync', 25), 'name': 'sync'})


@login_required
def instrument_chart(request, slug):
    inst = get_object_or_404(Instrument, symbol=Instrument.symbol_from_slug(slug))
    cfg = AgentConfig.get()
    tf = request.GET.get('timeframe', cfg.timeframe)
    df = load_frame(inst, tf, limit=int(request.GET.get('bars', 600)))
    df, rep = quality_gate(df, tf, inst.asset_class)
    trades = inst.trades.select_related('account').order_by('-exit_ts')[:200]
    return render(request, 'data/chart.html', {'inst': inst, 'tf': tf, 'bars': len(df), 'issues': rep.issues,
                                               'trades': trades, 'cov': coverage(inst, tf),
                                               'first': df.index[0] if len(df) else None, 'last': df.index[-1] if len(df) else None})


@login_required
def api_bars(request, slug):
    inst = get_object_or_404(Instrument, symbol=Instrument.symbol_from_slug(slug))
    cfg = AgentConfig.get()
    tf = request.GET.get('timeframe', cfg.timeframe)
    df = load_frame(inst, tf, limit=int(request.GET.get('bars', 600)))
    bars = [{'time': int(ts.timestamp()), 'open': float(o), 'high': float(h), 'low': float(l), 'close': float(c),
             'volume': float(v)} for ts, o, h, l, c, v in zip(df.index, df['open'], df['high'], df['low'], df['close'], df['volume'])]
    since = df.index[0].to_pydatetime() if len(df) else datetime.now(UTC)
    markers = []
    for t in inst.trades.filter(exit_ts__gte=since).order_by('entry_ts'):
        markers.append({'time': int(t.entry_ts.timestamp()), 'position': 'belowBar' if t.side == 'long' else 'aboveBar',
                        'color': '#3fb950' if t.side == 'long' else '#f0883e', 'shape': 'arrowUp' if t.side == 'long' else 'arrowDown',
                        'text': f'{t.side} {t.qty:g} @ {t.entry_price}'})
        markers.append({'time': int(t.exit_ts.timestamp()), 'position': 'aboveBar' if t.side == 'long' else 'belowBar',
                        'color': '#3fb950' if t.pnl > 0 else '#f85149', 'shape': 'circle', 'text': f'{t.exit_reason} {float(t.pnl):+.2f}'})
    return JsonResponse({'bars': bars, 'markers': markers})
