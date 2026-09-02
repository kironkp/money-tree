"""Independent emergency close: if a paper/live agent is dead or stale while
its account still holds positions, cancel its orders and close everything at
the venue. Run every 5 minutes from launchd (deploy/launchd/…watchdog.plist)."""
from django.core.management.base import BaseCommand
from django.utils import timezone

from main_app.models import Account, AgentConfig, Instrument, RiskEvent
from main_app.services import control
from main_app.services.ledger import DBRecorder, hydrate_broker, persist_broker
from main_app.services.narrator import Narrator


class Command(BaseCommand):
    help = 'Close positions on paper/live accounts whose agent is dead or stale'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **o):
        cfg = AgentConfig.get()
        acted = 0
        for account in Account.objects.filter(mode__in=['paper', 'live']):
            n_pos = account.positions.count()
            run = control.running_agent(account)
            health = run.health if run else 'disconnected'
            self.stdout.write(f'{account.name}: agent {health}, {n_pos} positions')
            if n_pos == 0 or health in ('healthy', 'waiting'):
                continue
            if o['dry_run']:
                self.stdout.write(self.style.WARNING(f'  would flatten {account.name}'))
                continue
            try:
                from main_app.services.broker.alpaca import AlpacaBroker
                instruments = {i.symbol: i for i in Instrument.objects.filter(asset_class__in=account.asset_classes)}
                broker = AlpacaBroker(paper=(account.mode == 'paper'), asset_classes={s: i.asset_class for s, i in instruments.items()},
                                      mode_is_live=(cfg.mode == 'live'))
                hydrate_broker(account, broker)
                broker.sync()
                broker.cancel_open_orders()
                closed = 0
                now = timezone.now()
                for symbol, pos in list(broker.positions.items()):
                    if pos.qty and broker.close_position(symbol, pos.last_price or pos.avg_price, now, 'watchdog',
                                                         f'mt-watchdog-{symbol.replace("/", "")}-{int(now.timestamp())}'):
                        closed += 1
                rec = DBRecorder(account, instruments)
                for kind, obj, order in broker.drain_events():
                    (rec.on_fill if kind == 'fill' else rec.on_trade)(obj, order) if kind == 'fill' else rec.on_trade(obj)
                persist_broker(account, broker, instruments)
                RiskEvent.objects.create(account=account, kind='watchdog',
                                         message=f'watchdog closed {closed} position(s): agent was {health}')
                Narrator(account).say('risk', f'WATCHDOG: the {account.market} agent was {health} with {n_pos} open position(s) — '
                                      f'closed {closed} at the venue.', phase='alert')
                acted += 1
                self.stdout.write(self.style.WARNING(f'  flattened {closed} on {account.name}'))
            except Exception as exc:
                self.stderr.write(f'  FAILED for {account.name}: {exc!r}')
                RiskEvent.objects.create(account=account, kind='watchdog', message=f'watchdog FAILED: {exc!r}'[:300])
        self.stdout.write(self.style.SUCCESS(f'watchdog done — acted on {acted} account(s)'))
