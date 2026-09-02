from __future__ import annotations

from datetime import date

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from main_app.forms import JournalForm
from main_app.models import AgentConfig, ApiUsage, Experiment, JournalEntry
from main_app.services import procs
from main_app.services.journal import write_eod_journal
from main_app.services.optimize import grid_from_schema

from .common import current_account, operator_required


@login_required
def journal_list(request):
    account = current_account(request)
    kind = request.GET.get('kind', '')
    qs = JournalEntry.objects.select_related('account').order_by('-date', '-created_at')
    if kind:
        qs = qs.filter(kind=kind)
    page = Paginator(qs, 30).get_page(request.GET.get('page'))
    usage = ApiUsage.objects.all()[:1]
    total_cost = sum(float(u.cost_usd) for u in ApiUsage.objects.all())
    return render(request, 'journal/list.html', {'page': page, 'kind': kind, 'account': account,
                                                 'form': JournalForm(initial={'date': timezone.localdate()}),
                                                 'coach_enabled': settings.COACH_ENABLED, 'coach_model': settings.COACH_MODEL,
                                                 'coach_cost': total_cost, 'coach_calls': ApiUsage.objects.count()})


@operator_required
@require_POST
def journal_add(request):
    form = JournalForm(request.POST)
    if form.is_valid():
        JournalEntry.objects.create(date=form.cleaned_data['date'], kind='manual', account=current_account(request),
                                    title=form.cleaned_data['title'], body=form.cleaned_data['body'])
        messages.success(request, 'note saved')
    else:
        messages.error(request, 'title and date are required')
    return redirect('journal-list')


@operator_required
@require_POST
def journal_eod_now(request):
    account = current_account(request)
    try:
        d = date.fromisoformat(request.POST.get('date', '')) if request.POST.get('date') else timezone.localdate()
    except ValueError:
        d = timezone.localdate()
    entry = write_eod_journal(account, d, auto_disable=False)
    messages.success(request, f'journal written: {entry.title}')
    return redirect('journal-list')


@operator_required
@require_POST
def journal_coach_now(request):
    if not settings.COACH_ENABLED:
        messages.error(request, 'set ANTHROPIC_API_KEY in .env to wake the coach')
        return redirect('journal-list')
    account = current_account(request)
    from main_app.services.coach import coach_review
    try:
        entry = coach_review(account, timezone.localdate())
    except Exception as exc:
        messages.error(request, f'coach failed: {exc}')
        return redirect('journal-list')
    messages.success(request, f'coach review saved with {len(entry.proposals)} proposals')
    return redirect('journal-list')


@operator_required
@require_POST
def journal_run_proposal(request, pk, n):
    entry = get_object_or_404(JournalEntry, pk=pk)
    try:
        prop = entry.proposals[n]
    except (IndexError, TypeError):
        messages.error(request, 'no such proposal')
        return redirect('journal-list')
    cfg = AgentConfig.get()
    from main_app.models import Instrument
    from main_app.services.strategies import get_strategy_class
    cls = get_strategy_class(prop['strategy_key'])
    account = current_account(request)
    symbols = [i.symbol for i in Instrument.objects.filter(in_watchlist=True, active=True, market=account.market)
               if cls.supports(i.asset_class)]
    end = timezone.localdate()
    exp = Experiment.objects.create(
        strategy_key=cls.key, method=prop.get('method', 'grid'), param_grid=grid_from_schema(cls.key, prop.get('param_grid')),
        symbols=symbols, timeframe=cfg.timeframe, start=end - timezone.timedelta(days=90), end=end,
        objective='sharpe', windows={'train_days': 40, 'test_days': 15})
    procs.spawn_manage(['optimize', '--experiment', str(exp.pk)], f'experiment-{exp.pk}', nice=10)
    messages.success(request, f'experiment #{exp.pk} started from the coach proposal “{prop["title"]}”')
    return redirect('experiment-detail', pk=exp.pk)


@operator_required
@require_POST
def journal_delete(request, pk):
    entry = get_object_or_404(JournalEntry, pk=pk)
    entry.delete()
    messages.success(request, 'entry deleted')
    return redirect('journal-list')
