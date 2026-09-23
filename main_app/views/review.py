"""The two review cycles, made visible.

The owner's requirement is that both cycles show last run, next run, findings,
actions taken, failed checks and current blockers. A reviewer whose output lives
only in a log is a reviewer nobody reads.
"""
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from main_app.models import Account, Hypothesis, ReviewFinding, ReviewRun
from main_app.services.review.runner import cycle_status


@login_required
def review_index(request):
    cycles = [cycle_status(ReviewRun.OPERATIONAL), cycle_status(ReviewRun.IMPROVEMENT)]
    for c in cycles:
        c['overdue'] = bool(c['next_due_at'] and c['next_due_at'] < timezone.now())
    return render(request, 'review/index.html', {
        'cycles': cycles,
        'runs': ReviewRun.objects.all()[:20],
        'hypotheses': Hypothesis.objects.all()[:10],
        'halts': Account.objects.filter(review_halt=True),
        'resolved_recently': ReviewFinding.objects.filter(status=ReviewFinding.RESOLVED)
                             .order_by('-resolved_at')[:10],
    })


@login_required
def finding_list(request):
    qs = ReviewFinding.objects.select_related('account', 'first_run').order_by('-last_seen_at')
    status = request.GET.get('status', ReviewFinding.OPEN)
    if status != 'all':
        qs = qs.filter(status=status)
    for f in ('cycle', 'severity', 'check_key'):
        if request.GET.get(f):
            qs = qs.filter(**{f: request.GET[f]})
    return render(request, 'review/findings.html', {
        'page': Paginator(qs, 50).get_page(request.GET.get('page')),
        'status': status,
        'statuses': [ReviewFinding.OPEN, ReviewFinding.ACKED, ReviewFinding.RESOLVED, 'all'],
        'severities': ReviewFinding.SEVERITIES,
    })


@login_required
@require_POST
def finding_ack(request, pk):
    """Acknowledge. Deliberately NOT 'resolve' — a finding closes when the check
    that raised it runs clean, not when somebody clicks a button."""
    f = get_object_or_404(ReviewFinding, pk=pk)
    f.status = ReviewFinding.ACKED
    f.resolution = f'acknowledged by {request.user.username} at {timezone.now():%Y-%m-%d %H:%M}'
    f.save(update_fields=['status', 'resolution'])
    return redirect(request.META.get('HTTP_REFERER') or 'review-findings')


@login_required
@require_POST
def clear_halt(request, market):
    """Lift a review halt by hand.

    The reviewer lifts its own halt when the fault clears, so using this means
    overriding it. Recorded against the operator for exactly that reason.
    """
    account = get_object_or_404(Account, market=market, mode='sim')
    account.review_halt = False
    account.review_halt_reason = ''
    account.review_halt_at = None
    account.save(update_fields=['review_halt', 'review_halt_reason', 'review_halt_at'])
    account.risk_events.create(ts=timezone.now(), kind='review_halt',
                               message=f'halt lifted manually by {request.user.username}')
    return redirect('review-index')


@login_required
def hypothesis_detail(request, pk):
    return render(request, 'review/hypothesis.html', {'h': get_object_or_404(Hypothesis, pk=pk)})


@login_required
def audit_export(request, market):
    """The full audit trail as a zip. Not a tax export; the README says so."""
    from main_app.services.audit_export import write_bundle
    import io as _io
    account = get_object_or_404(Account, market=market, mode=request.GET.get('mode', 'sim'))
    now = timezone.now()
    buf = _io.BytesIO()
    write_bundle(account, buf, now)
    resp = HttpResponse(buf.getvalue(), content_type='application/zip')
    resp['Content-Disposition'] = (
        f'attachment; filename="moneytree-audit-{account.market}-{account.mode}-{now:%Y%m%d}.zip"')
    return resp
