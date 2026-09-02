"""What is configured, what is reachable, what time the market thinks it is."""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from main_app.models import AgentConfig, AgentRun, Bar, Instrument, Strategy
from main_app.services.data import calendar as cal
from main_app.services.data import provider_status


class Command(BaseCommand):
    help = 'Print configuration and environment status'

    def handle(self, *args, **o):
        now = timezone.now()
        cfg = AgentConfig.get()
        ps = provider_status()
        self.stdout.write(f'MoneyTree v{settings.VERSION}')
        self.stdout.write(f'  mode={cfg.mode} trading_enabled={cfg.trading_enabled} kill_switch={cfg.kill_switch} timeframe={cfg.timeframe}')
        self.stdout.write(f'  alpaca keys: {"yes" if ps["alpaca"] else "no"}   live armed: {settings.LIVE_TRADING_ARMED}   coach: {"yes" if settings.COACH_ENABLED else "no"} ({settings.COACH_MODEL})')
        self.stdout.write(f'  default data provider: {ps["default"]}')
        s = cal.session_at(now)
        state = f'OPEN until {s.close_utc.astimezone(cal.ET):%H:%M} ET' if s else f'closed — next open {cal.next_open(now).astimezone(cal.ET):%a %b %d %H:%M} ET'
        self.stdout.write(f'  market: {state}  (now {now.astimezone(cal.ET):%Y-%m-%d %H:%M:%S} ET)')
        self.stdout.write(f'  instruments: {Instrument.objects.filter(in_watchlist=True).count()} in watchlist, bars stored: {Bar.objects.count()}')
        for row in Strategy.objects.all():
            self.stdout.write(f'  strategy {row.key}: enabled={row.enabled} stage={row.stage} v{row.version} params={row.params}')
        run = AgentRun.objects.filter(status="running").first()
        self.stdout.write(f'  agent: {"running pid " + str(run.pid) + " (" + run.mode + ")" if run and run.is_alive else "not running"}')
        if settings.ALPACA_ENABLED:
            try:
                from alpaca.trading.client import TradingClient
                acct = TradingClient(settings.ALPACA_API_KEY, settings.ALPACA_SECRET_KEY, paper=True).get_account()
                self.stdout.write(f'  alpaca paper account: equity {acct.equity} cash {acct.cash} status {acct.status}')
            except Exception as exc:
                self.stdout.write(self.style.WARNING(f'  alpaca paper account: ERROR {exc}'))
