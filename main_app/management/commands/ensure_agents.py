"""Start any lane agent that is not running, and leave the others alone.

`restart_agents` stops and respawns, which is right after a promotion and wrong
on a timer — running it every five minutes would kill a working agent every five
minutes. This one is idempotent: it starts what is missing and touches nothing
else, so launchd can call it repeatedly and at boot.

That gap was real. The four agents were only ever started by the 02:10 nightly
research job, so a reboot at ten in the morning meant no trading until two the
following night, with nothing anywhere reporting it.
"""
from django.core.management.base import BaseCommand

from main_app.models import Market, Strategy
from main_app.services import control, procs


class Command(BaseCommand):
    help = 'Start any sim lane agent that is not currently running (safe to repeat)'

    def add_arguments(self, parser):
        parser.add_argument('--mode', default='sim', choices=['sim', 'paper', 'live'])
        parser.add_argument('--all', action='store_true',
                            help='include lanes with no enabled strategy')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **o):
        markets = [m for m in Market.values
                   if o['all'] or Strategy.objects.filter(market=m, enabled=True).exists()]
        running = {r.market for r in control.running_agents() if r.mode == o['mode']}
        started = 0
        for market in markets:
            if market in running:
                self.stdout.write(f'{market}: already running')
                continue
            if o['dry_run']:
                self.stdout.write(self.style.WARNING(f'{market}: would start'))
                continue
            pid = procs.spawn_manage(['run_agent', '--mode', o['mode'], '--market', market],
                                     f'agent-{o["mode"]}-{market}')
            self.stdout.write(self.style.SUCCESS(f'{market}: started pid {pid}'))
            started += 1
        if not started and not o['dry_run']:
            self.stdout.write('nothing to do')
