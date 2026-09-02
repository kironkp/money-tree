from django.conf import settings

from .models import AgentConfig, Mode
from .services import control


def site_context(request):
    if not request.user.is_authenticated:
        return {'VERSION': settings.VERSION}
    cfg = AgentConfig.get()
    runs = control.running_agents()
    return {
        'VERSION': settings.VERSION,
        'cfg': cfg,
        'agent_runs': runs,
        'alpaca_enabled': settings.ALPACA_ENABLED,
        'coach_enabled': settings.COACH_ENABLED,
        'mode_is_live': cfg.mode == Mode.LIVE,
        'is_operator': bool(request.user.is_staff),
    }
