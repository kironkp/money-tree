from datetime import date

from django.core.management.base import BaseCommand
from django.utils import timezone

from main_app.models import Account
from main_app.services.journal import write_eod_journal


class Command(BaseCommand):
    help = 'Write the end-of-day journal entry for an account'

    def add_arguments(self, parser):
        parser.add_argument('--mode', default='sim')
        parser.add_argument('--market', default='stocks')
        parser.add_argument('--date', default='')
        parser.add_argument('--coach', action='store_true', help='also run the coach review')

    def handle(self, *args, **o):
        account = Account.for_mode(o['mode'], o['market'])
        d = date.fromisoformat(o['date']) if o['date'] else timezone.localdate()
        entry = write_eod_journal(account, d)
        self.stdout.write(self.style.SUCCESS(f'{entry.title}\n{entry.body}'))
        if o['coach']:
            from main_app.services.coach import coach_review
            c = coach_review(account, d)
            self.stdout.write(self.style.SUCCESS(f'\n{c.title}\n{c.body}\nproposals: {len(c.proposals)}'))
