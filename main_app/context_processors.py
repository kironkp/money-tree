from django.conf import settings

from .models import AgentConfig, AgentRun, Mode


def site_context(request):
    if not request.user.is_authenticated:
        return {'VERSION': settings.VERSION}
    cfg = AgentConfig.get()
    run = AgentRun.objects.filter(status='running').select_related('account').first()
    return {
        'VERSION': settings.VERSION,
        'cfg': cfg,
        'agent_run': run if (run and run.is_alive) else None,
        'alpaca_enabled': settings.ALPACA_ENABLED,
        'coach_enabled': settings.COACH_ENABLED,
        'mode_is_live': cfg.mode == Mode.LIVE,
    }
