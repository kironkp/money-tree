"""The map: every bot on the desk, wired up and live."""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render

from main_app.services.graph_model import graph
from main_app.services.graph_state import state


@login_required
def machine(request):
    g = graph()
    return render(request, 'graph/index.html', {
        'graph': g,
        'state': state(),
        'counts': {'nodes': len(g['nodes']), 'edges': len(g['edges'])},
        'lanes': g['lanes'],
    })


@login_required
def machine_state(request):
    """Live values only. The graph is never rebuilt from this — text and classes change."""
    return JsonResponse({'state': state()})
