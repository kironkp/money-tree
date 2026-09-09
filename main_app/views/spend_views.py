"""Spend ingest and the spend page.

Other apps on this machine post their model usage here so one ledger answers
"where is the API money going". Authenticated by a shared secret rather than a
session, because the callers are servers, not browsers.
"""
from __future__ import annotations

import hmac
import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from main_app.services.spend import day_spend, projected_monthly, range_spend, record

from .common import parse_days


@csrf_exempt
@require_POST
def api_spend_ingest(request):
    """POST one API call into the ledger.

    curl -X POST http://localhost:8003/api/spend/ \\
      -H 'X-Spend-Token: <SPEND_INGEST_TOKEN>' -H 'Content-Type: application/json' \\
      -d '{"project":"findit","provider":"openai","model":"gpt-4o",
           "purpose":"assistant","input_tokens":4200,"output_tokens":300}'
    """
    token = getattr(settings, 'SPEND_INGEST_TOKEN', '')
    if not token:
        return JsonResponse({'error': 'ingest disabled: set SPEND_INGEST_TOKEN in .env'}, status=503)
    sent = request.headers.get('X-Spend-Token', '')
    # Constant-time compare: this endpoint is reachable through the tunnel.
    if not sent or not hmac.compare_digest(sent, token):
        return JsonResponse({'error': 'bad token'}, status=403)
    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'error': 'bad json'}, status=400)
    if not payload.get('model'):
        return JsonResponse({'error': 'model is required'}, status=400)
    row = record(
        str(payload['model'])[:60],
        provider=str(payload.get('provider', 'openai'))[:20],
        project=str(payload.get('project', 'unknown'))[:30],
        purpose=str(payload.get('purpose', ''))[:40],
        input_tokens=int(payload.get('input_tokens') or 0),
        output_tokens=int(payload.get('output_tokens') or 0),
        cached_tokens=int(payload.get('cached_tokens') or 0),
        calls=int(payload.get('calls') or 1),
        cost_usd=payload.get('cost_usd'),
        note=str(payload.get('note', ''))[:200],
    )
    if row is None:
        return JsonResponse({'error': 'could not record'}, status=500)
    return JsonResponse({'ok': True, 'id': row.pk, 'cost_usd': float(row.cost_usd)})


@login_required
def spend_page(request):
    days = parse_days(request, 30, 365)
    s = range_spend(days)
    return render(request, 'spend.html', {
        'today': day_spend(), 'range': s, 'days': days, 'day_options': [1, 7, 30, 90],
        'projected': projected_monthly(days),
        'ingest_enabled': bool(getattr(settings, 'SPEND_INGEST_TOKEN', '')),
    })
