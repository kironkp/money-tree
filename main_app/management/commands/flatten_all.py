"""Emergency: close every position on an account, outside the agent loop."""
from django.core.management.base import BaseCommand

from main_app.services.control import flatten_account


class Command(BaseCommand):
    help = 'Close all positions for --mode sim|paper|live at the last known prices'

    def add_arguments(self, parser):
        parser.add_argument('--mode', default='sim')

    def handle(self, *args, **o):
        n = flatten_account(o['mode'], 'manual')
        self.stdout.write(self.style.SUCCESS(f'closed {n} positions on the {o["mode"]} account'))
