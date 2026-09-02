"""Run the trading loop (or replay a past session)."""
import logging
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from main_app.services.agent import Agent


class Command(BaseCommand):
    help = 'Run a MoneyTree agent: --market stocks|crypto --mode sim|paper|live, or --replay YYYY-MM-DD [--speed N]'

    def add_arguments(self, parser):
        parser.add_argument('--mode', default='sim', choices=['sim', 'paper', 'live'])
        parser.add_argument('--market', default='stocks', choices=['stocks', 'crypto', 'degen'])
        parser.add_argument('--replay', default='', help='replay this session date into the replay account')
        parser.add_argument('--speed', type=float, default=30.0, help='replay speed multiplier')
        parser.add_argument('--once', action='store_true', help='one tick, then exit')
        parser.add_argument('--provider', default='', help='alpaca | yahoo (default: alpaca if keyed)')
        parser.add_argument('--quiet', action='store_true')

    def handle(self, *args, **o):
        logging.getLogger('moneytree').setLevel(logging.INFO)
        replay = None
        if o['replay']:
            try:
                replay = date.fromisoformat(o['replay'])
            except ValueError:
                raise CommandError('--replay expects YYYY-MM-DD')
        agent = Agent(mode=o['mode'], replay_date=replay, speed=o['speed'], once=o['once'],
                      provider_name=o['provider'] or None, quiet=o['quiet'], market=o['market'])
        try:
            agent.run()
        except RuntimeError as exc:
            raise CommandError(str(exc))
