"""Shared bits for the views."""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from main_app.models import Account, AgentConfig, Mode


def current_account(request, cfg: AgentConfig | None = None) -> Account:
    """The account the page is about: ?account=replay overrides the config mode."""
    cfg = cfg or AgentConfig.get()
    mode = request.GET.get('account') or request.POST.get('account') or cfg.mode
    if mode not in Mode.values:
        mode = cfg.mode
    return Account.for_mode(mode)


def parse_days(request, default: int = 7, cap: int = 365) -> int:
    try:
        return max(1, min(cap, int(request.GET.get('days', default))))
    except (TypeError, ValueError):
        return default


def since_days(days: int):
    return timezone.now() - timedelta(days=days)
