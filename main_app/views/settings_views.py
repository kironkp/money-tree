from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from main_app.forms import AgentConfigForm, InviteForm
from main_app.models import Account, AgentConfig, Market, Mode, SignupInvite
from main_app.services import control

from .common import deny_observer, operator_required


@login_required
def settings_view(request):
    cfg = AgentConfig.get()
    if request.method == 'POST':
        denied = deny_observer(request)
        if denied is not None:
            return denied
        form = AgentConfigForm(request.POST, instance=cfg)
        if form.is_valid():
            form.save()
            messages.success(request, 'settings saved — running agents pick up risk limits on their next tick')
            return redirect('settings')
        messages.error(request, 'fix the highlighted fields')
    else:
        form = AgentConfigForm(instance=cfg)
    accounts = []
    for mode in (Mode.SIM, Mode.PAPER, Mode.REPLAY) + ((Mode.LIVE,) if cfg.mode == Mode.LIVE else ()):
        for market in Market.values:
            accounts.append(Account.for_mode(mode, market))
    User = get_user_model()
    return render(request, 'settings.html', {
        'form': form, 'cfg': cfg, 'accounts': accounts,
        'keys': {'alpaca_paper': settings.ALPACA_ENABLED, 'alpaca_live': bool(settings.ALPACA_LIVE_API_KEY),
                 'live_armed_env': settings.LIVE_TRADING_ARMED, 'coach': settings.COACH_ENABLED,
                 'coach_model': settings.COACH_MODEL, 'email': bool(settings.EMAIL_HOST)},
        'runs': control.running_agents(),
        'users': User.objects.order_by('-is_staff', 'username'), 'invites': SignupInvite.objects.all(),
        'invite_form': InviteForm(), 'allowed_emails': settings.SIGNUP_ALLOWED_EMAILS,
    })


@operator_required
@require_POST
def people(request):
    """Invites and operator toggles (Settings → People)."""
    action = request.POST.get('action')
    User = get_user_model()
    if action == 'invite':
        form = InviteForm(request.POST)
        if form.is_valid():
            SignupInvite.objects.update_or_create(
                email=form.cleaned_data['email'].lower(),
                defaults={'note': form.cleaned_data['note'], 'make_operator': form.cleaned_data['make_operator'],
                          'invited_by': request.user, 'used_at': None})
            messages.success(request, f"{form.cleaned_data['email']} can now sign up at /accounts/signup/")
        else:
            messages.error(request, 'enter a valid email')
    elif action == 'revoke':
        SignupInvite.objects.filter(pk=request.POST.get('id')).delete()
        messages.success(request, 'invite revoked')
    elif action == 'toggle_operator':
        if not request.user.is_superuser:
            messages.error(request, 'only the owner can change operator rights')
            return redirect('settings')
        user = User.objects.filter(pk=request.POST.get('id')).first()
        if user and not user.is_superuser:
            user.is_staff = not user.is_staff
            user.save(update_fields=['is_staff'])
            messages.success(request, f'{user.email or user.username} is now {"an operator" if user.is_staff else "view-only"}')
    return redirect('settings')


@operator_required
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
        for market in (Market.STOCKS, Market.CRYPTO):
            Account.for_mode(Mode.LIVE, market)
        messages.warning(request, 'LIVE mode armed. Start an agent with --mode live. Real money from here on.')
        return redirect('settings')
    if mode in (Mode.SIM, Mode.PAPER):
        if mode == Mode.PAPER and not settings.ALPACA_ENABLED:
            messages.error(request, 'paper mode needs Alpaca paper keys in .env')
            return redirect('settings')
        cfg.mode = mode
        cfg.save(update_fields=['mode'])
        messages.success(request, f'mode set to {mode}')
    return redirect('settings')


@operator_required
@require_POST
def reset_account(request):
    mode = request.POST.get('mode', 'sim')
    market = request.POST.get('market', Market.STOCKS)
    if mode == Mode.LIVE:
        messages.error(request, 'the live account cannot be reset from here')
        return redirect('settings')
    account = Account.for_mode(mode, market)
    if control.running_agent(account):
        messages.error(request, 'stop that agent before resetting its account')
        return redirect('settings')
    account.reset()
    messages.success(request, f'{account.name} reset to {account.starting_cash}')
    return redirect('settings')
