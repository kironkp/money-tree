"""Restart the running sim agents so promoted parameters take effect.

A promotion is written to the Strategy row, but a live agent reads its
strategies once at startup — so research could improve the parameters at 02:10
and the agent would keep trading the old ones all day. This runs right after
research and closes that loop.

Positions are PRESERVED: the agent is sent a plain SIGTERM without setting
AgentRun.stop_requested, which the shutdown path treats as an infrastructure
restart rather than an operator stop, so nothing is flattened into an
off-strategy exit.
"""
import time

from django.core.management.base import BaseCommand

from main_app.models import Market, Mode, Strategy
from main_app.services import control, procs


class Command(BaseCommand):
    help = 'Stop and restart the sim agents (preserving positions) so new parameters take effect'

    def add_arguments(self, parser):
        parser.add_argument('--mode', default='sim', choices=['sim', 'paper', 'live'])
        parser.add_argument('--markets', default='', help='comma-separated (default: every market with an enabled strategy)')
        parser.add_argument('--all', action='store_true', help='restart even lanes with no enabled strategy')

    def handle(self, *args, **o):
        if o['markets']:
            markets = [m.strip() for m in o['markets'].split(',') if m.strip() in Market.values]
        else:
            markets = [m for m in Market.values
                       if o['all'] or Strategy.objects.filter(market=m, enabled=True).exists()]
        running = {r.market: r for r in control.running_agents() if r.mode == o['mode']}
        for market in markets:
            run = running.get(market)
            if run is not None:
                procs.stop(run.pid)          # SIGTERM only — positions survive
                for _ in range(60):
                    if not procs.alive(run.pid):
                        break
                    time.sleep(1)
                if procs.alive(run.pid):
                    self.stderr.write(f'{market}: pid {run.pid} did not exit; leaving it alone')
                    continue
                self.stdout.write(f'{market}: stopped pid {run.pid}')
            pid = procs.spawn_manage(['run_agent', '--mode', o['mode'], '--market', market],
                                     f'agent-{o["mode"]}-{market}')
            self.stdout.write(self.style.SUCCESS(f'{market}: started pid {pid}'))
        for market in Market.values:
            if market not in markets and market in running:
                self.stdout.write(f'{market}: left running (no enabled strategy to reload)')
