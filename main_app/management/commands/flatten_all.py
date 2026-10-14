"""Emergency: close every position on an account, outside the agent loop."""
from django.core.management.base import BaseCommand, CommandError

from main_app.models import Account
from main_app.services.control import flatten_account, running_agent


class Command(BaseCommand):
    help = 'Close all positions for --mode sim|paper|live at the last known prices'

    def add_arguments(self, parser):
        parser.add_argument('--mode', default='sim')
        parser.add_argument('--market', default='stocks')

    def handle(self, *args, **o):
        # A running agent holds the positions in its own broker and writes them back
        # on its next persist: flattening underneath it books the close here while
        # the position reappears there, and the agent later closes it again. The
        # dashboard refuses the same way. running_agent checks the process, so an
        # agent that died with its row still saying "running" does not block this.
        if running_agent(Account.for_mode(o['mode'], o['market'])):
            raise CommandError('an agent is running on this account — use the kill switch (immediate) '
                               'or stop the agent')
        n = flatten_account(o['mode'], 'manual', o['market'])
        self.stdout.write(self.style.SUCCESS(f'closed {n} positions on the {o["mode"]} account'))
