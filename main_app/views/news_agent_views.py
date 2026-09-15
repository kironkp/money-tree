"""The News Agent tab: what it read, what it thought, and what it did about it."""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Avg, Count, Q
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from main_app.models import NewsSession, NewsVerdict
from main_app.services.news_agent import MODEL, scoreboard


def _pulse(sessions) -> dict:
    """The orb's state: what the agent is doing, and how lately."""
    latest = sessions.first()
    if latest is None:
        return {'state': 'idle', 'since': None, 'last': None}
    age_h = (timezone.now() - latest.started_at).total_seconds() / 3600
    if latest.error:
        state = 'error'
    elif latest.finished_at is None:
        state = 'thinking'
    elif age_h > 6:
        state = 'stale'          # the four-hourly job has not run
    elif latest.traded or latest.actionable:
        state = 'acting'
    else:
        state = 'watching'
    return {'state': state, 'last': latest, 'age_h': age_h}


@login_required
def news_agent(request):
    sessions = NewsSession.objects.all()
    since = timezone.now() - timedelta(days=7)
    recent = NewsVerdict.objects.filter(created_at__gte=since)
    calls = recent.filter(score__gte=NewsVerdict.ACT_THRESHOLD).exclude(direction='none')
    graded = recent.filter(outcome_at__isnull=False)
    right = graded.filter(outcome_pct__gt=0).count()
    return render(request, 'news_agent/index.html', {
        'sessions': sessions[:40],
        'pulse': _pulse(sessions),
        'model': MODEL,
        'threshold': NewsVerdict.ACT_THRESHOLD,
        'stats': {
            'sessions_7d': sessions.filter(started_at__gte=since).count(),
            'read_7d': recent.count(),
            'calls_7d': calls.count(),
            'traded_7d': recent.filter(acted=True, trade__isnull=False).count(),
            'graded': graded.count(),
            'right': right,
            'hit_rate': (right / graded.count() * 100) if graded.count() else None,
            'avg_score': recent.aggregate(a=Avg('score'))['a'] or 0,
            'cost_7d': sum(float(s.cost_usd) for s in sessions.filter(started_at__gte=since)),
        },
        'top_calls': calls.select_related('session').order_by('-created_at')[:8],
        'board': scoreboard(30),
    })


@login_required
def news_agent_session(request, pk):
    session = get_object_or_404(NewsSession, pk=pk)
    verdicts = list(session.verdicts.all())
    return render(request, 'news_agent/session.html', {
        'session': session,
        'acted': [v for v in verdicts if v.actionable],
        'passed': [v for v in verdicts if not v.actionable],
        'counts': session.verdicts.aggregate(
            n=Count('id'), acted=Count('id', filter=Q(score__gte=NewsVerdict.ACT_THRESHOLD))),
        'threshold': NewsVerdict.ACT_THRESHOLD,
        'prev': NewsSession.objects.filter(started_at__lt=session.started_at).first(),
        'next': NewsSession.objects.filter(started_at__gt=session.started_at).order_by('started_at').first(),
    })
