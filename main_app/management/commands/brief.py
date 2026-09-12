"""One searching prompt per lane: what is going on out there right now."""
from django.core.management.base import BaseCommand

from main_app.models import Market
from main_app.services.briefing import brief_all, brief_lane


class Command(BaseCommand):
    help = 'Ask a web-searching model what is happening in each lane'

    def add_arguments(self, parser):
        parser.add_argument('--market', default='', help='one lane (default: all with a watchlist)')

    def handle(self, *args, **o):
        rows = [brief_lane(o['market'])] if o['market'] in Market.values else brief_all()
        total = 0.0
        for r in rows:
            total += float(r.cost_usd)
            if r.error:
                self.stderr.write(f'{r.market}: FAILED {r.error[:160]}')
                continue
            flag = ' (quiet)' if r.quiet else ''
            self.stdout.write(self.style.SUCCESS(f'{r.market}{flag}: {r.headline}'))
            for item in r.items:
                self.stdout.write(f'   · {item["text"][:150]}')
        self.stdout.write(f'\n{len(rows)} briefing(s), about ${total:.4f} including the search fee')
