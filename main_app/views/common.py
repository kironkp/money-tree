"""Shared bits for the views."""
from __future__ import annotations

from datetime import timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.utils import timezone

from main_app.models import Account, AgentConfig, Market, Mode


def current_account(request, cfg: AgentConfig | None = None) -> Account:
    """The account a page is about: ?account=<mode>&market=<market>."""
    cfg = cfg or AgentConfig.get()
    mode = request.GET.get('account') or request.POST.get('account') or cfg.mode
    if mode not in Mode.values:
        mode = cfg.mode
    market = request.GET.get('market') or request.POST.get('market') or Market.STOCKS
    if market not in Market.values:
        market = Market.STOCKS
    return Account.for_mode(mode, market)


def account_tabs(cfg: AgentConfig | None = None) -> list[Account]:
    cfg = cfg or AgentConfig.get()
    markets = (Market.STOCKS, Market.CRYPTO, Market.DEGEN)
    tabs = [Account.for_mode(cfg.mode, m) for m in markets]
    if cfg.mode != Mode.SIM:
        tabs += [Account.for_mode(Mode.SIM, m) for m in markets]
    tabs += [Account.for_mode(Mode.REPLAY, m) for m in (Market.STOCKS, Market.CRYPTO, Market.DEGEN)]
    return tabs


def is_operator(user) -> bool:
    return bool(user.is_authenticated and user.is_active and user.is_staff)


def deny_observer(request):
    """Redirect response for a view-only user trying to change something, else None."""
    if is_operator(request.user):
        return None
    messages.error(request, 'Your account is view-only. Ask the owner to make you an operator (Settings → People).')
    return redirect(request.META.get('HTTP_REFERER') or 'dashboard')


def operator_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        denied = deny_observer(request)
        if denied is not None:
            return denied
        return view(request, *args, **kwargs)
    return wrapped


def parse_days(request, default: int = 7, cap: int = 365) -> int:
    try:
        return max(1, min(cap, int(request.GET.get('days', default))))
    except (TypeError, ValueError):
        return default


def since_days(days: int):
    return timezone.now() - timedelta(days=days)
