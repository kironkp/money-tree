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
    from main_app.models import Trade
    sessions = NewsSession.objects.all()
    since = timezone.now() - timedelta(days=7)
    recent = NewsVerdict.objects.filter(created_at__gte=since)
    calls = recent.filter(score__gte=NewsVerdict.ACT_THRESHOLD).exclude(direction='none')
    # Contemporaneous only, the same rule the scoreboard uses. Pooling the rebuilt
    # history here while excluding it there gave two different hit rates for the
    # same agent on two pages, and the bigger, friendlier number was the wrong one.
    # Directional calls only. A verdict of "no action" that was later priced is
    # not a forecast that can be right or wrong, and counting the ones where price
    # happened to drift up turned a record of two losing calls into a hit rate
    # over fifty per cent.
    graded = recent.filter(outcome_at__isnull=False, provenance='contemporaneous',
                           direction__in=('buy', 'short'), outcome_atr_net__isnull=False)
    right = graded.filter(outcome_kind='target').count()
    rebuilt = recent.filter(outcome_at__isnull=False, provenance='reconstructed').count()
    # A verdict becomes an ORDER when its lease is consumed. Whether that order
    # becomes a closed trade happens hours later and is counted separately —
    # the old tile read NewsVerdict.trade, which no code has ever written, so it
    # showed zero on days the agent traded.
    ordered = recent.filter(lease_state='consumed').count()
    traded = Trade.objects.filter(strategy_key='news_catalyst', exit_ts__gte=since).count()
    return render(request, 'news_agent/index.html', {
        'sessions': sessions[:40],
        'pulse': _pulse(sessions),
        'model': MODEL,
        'threshold': NewsVerdict.ACT_THRESHOLD,
        'stats': {
            'sessions_7d': sessions.filter(started_at__gte=since).count(),
            'read_7d': recent.count(),
            'calls_7d': calls.count(),
            'traded_7d': traded,
            'graded': graded.count(),
            'rebuilt': rebuilt,
            'right': right,
            'hit_rate': (right / graded.count() * 100) if graded.count() else None,
            'ordered': ordered,
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


@login_required
def news_agent_scoreboard(request):
    """Is the research arm actually better? The page that decides whether it trades.

    Deliberately shows the sample size and the hurdle next to every number. A
    difference without an n and a threshold is a number people read as a result.
    """
    from main_app.models import Evaluation, SymbolDossier
    from main_app.services.evaluation import assess, decide
    from main_app.services.preregistration import describe

    ev = Evaluation.objects.filter(status='collecting', kind='promotion').first()
    a = assess(ev) if ev else None
    return render(request, 'news_agent/scoreboard.html', {
        'evaluation': ev,
        'prereg': describe(ev) if ev else '',
        'a': a,
        'verdict': decide(ev) if ev else None,
        'history': Evaluation.objects.exclude(status='collecting')[:10],
        'recent': SymbolDossier.objects.filter(error='')[:12],
        'reconstructed': scoreboard(provenance='reconstructed'),
    })
