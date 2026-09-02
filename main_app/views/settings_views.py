from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from main_app.forms import AgentConfigForm
from main_app.models import Account, AgentConfig, Mode
from main_app.services import control


@login_required
def settings_view(request):
    cfg = AgentConfig.get()
    if request.method == 'POST':
        form = AgentConfigForm(request.POST, instance=cfg)
        if form.is_valid():
            form.save()
            messages.success(request, 'settings saved — the agent picks up risk limits on its next tick')
            return redirect('settings')
        messages.error(request, 'fix the highlighted fields')
    else:
        form = AgentConfigForm(instance=cfg)
    accounts = [Account.for_mode(m) for m in (Mode.SIM, Mode.PAPER, Mode.REPLAY)]
    if cfg.mode == Mode.LIVE:
        accounts.append(Account.for_mode(Mode.LIVE))
    return render(request, 'settings.html', {
        'form': form, 'cfg': cfg, 'accounts': accounts,
        'keys': {'alpaca_paper': settings.ALPACA_ENABLED, 'alpaca_live': bool(settings.ALPACA_LIVE_API_KEY),
                 'live_armed_env': settings.LIVE_TRADING_ARMED, 'coach': settings.COACH_ENABLED,
                 'coach_model': settings.COACH_MODEL},
        'run': control.running_agent(),
    })


@login_required
@require_POST
def set_mode(request):
    cfg = AgentConfig.get()
    mode = request.POST.get('mode')
    if mode == Mode.LIVE:
        if request.POST.get('confirm', '').strip() != 'ARM LIVE':
            messages.error(request, 'type ARM LIVE exactly to arm real-money trading')
            return redirect('settings')
        if not settings.LIVE_TRADING_ARMED:
            messages.error(request, 'LIVE_TRADING_ARMED=1 must also be set in .env (and the server restarted)')
            return redirect('settings')
        if not settings.ALPACA_LIVE_API_KEY:
            messages.error(request, 'ALPACA_LIVE_API_KEY / SECRET are missing in .env')
            return redirect('settings')
        cfg.mode = Mode.LIVE
        cfg.live_armed_at = timezone.now()
        cfg.live_confirm_orders = True
        cfg.save()
        Account.for_mode(Mode.LIVE)
        messages.warning(request, 'LIVE mode armed. Start the agent with --mode live. Real money from here on.')
        return redirect('settings')
    if mode in (Mode.SIM, Mode.PAPER):
        if mode == Mode.PAPER and not settings.ALPACA_ENABLED:
            messages.error(request, 'paper mode needs Alpaca paper keys in .env')
            return redirect('settings')
        cfg.mode = mode
        cfg.save(update_fields=['mode'])
        messages.success(request, f'mode set to {mode}')
    return redirect('settings')


@login_required
@require_POST
def reset_account(request):
    mode = request.POST.get('mode', 'sim')
    if mode == Mode.LIVE:
        messages.error(request, 'the live account cannot be reset from here')
        return redirect('settings')
    if control.running_agent(Account.for_mode(mode)):
        messages.error(request, 'stop the agent before resetting its account')
        return redirect('settings')
    account = Account.for_mode(mode)
    account.reset()
    messages.success(request, f'{account.name} reset to {account.starting_cash}')
    return redirect('settings')
