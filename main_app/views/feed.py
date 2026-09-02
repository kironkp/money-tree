"""The live feed: JSON for the poller and a full-page view."""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from main_app.models import FeedEvent
from main_app.services.data import calendar as cal

from .common import account_tabs, current_account


def _serialize(e: FeedEvent, today):
    local = e.ts.astimezone(cal.ET)
    return {'id': e.id, 'ts': int(e.ts.timestamp()),
            't': local.strftime('%H:%M:%S') if local.date() == today else local.strftime('%b %d %H:%M'),
            'level': e.level, 'symbol': e.symbol, 'strategy': e.strategy_key, 'text': e.text}


@login_required
def api_feed(request):
    account = current_account(request)
    try:
        after = int(request.GET.get('after', 0))
    except ValueError:
        after = 0
    try:
        limit = max(1, min(500, int(request.GET.get('limit', 150))))
    except ValueError:
        limit = 150
    levels = [x for x in request.GET.get('levels', '').split(',') if x]
    qs = FeedEvent.objects.filter(account=account)
    if levels:
        qs = qs.filter(level__in=levels)
    if after:
        rows = list(qs.filter(id__gt=after).order_by('id')[:limit])
    else:
        rows = list(qs.order_by('-id')[:limit])[::-1]
    today = timezone.now().astimezone(cal.ET).date()
    return JsonResponse({'events': [_serialize(e, today) for e in rows],
                         'last_id': rows[-1].id if rows else after, 'account': account.label})


@login_required
def feed_page(request):
    account = current_account(request)
    return render(request, 'feed.html', {'account': account, 'tabs': account_tabs()})
