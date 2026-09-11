"""Read the news: ingest headlines, classify the novel ones, score old ones.

Run hourly. Every step is capped, so a busy news day costs a known amount.
"""
from django.core.management.base import BaseCommand

from main_app.services.news import MAX_CLASSIFY_PER_RUN, classify, event_scoreboard, ingest, score_outcomes


class Command(BaseCommand):
    help = 'Ingest market news, classify novel headlines, and score older ones against price'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=MAX_CLASSIFY_PER_RUN,
                            help='max model calls this run')
        parser.add_argument('--no-classify', action='store_true', help='ingest only, no model calls')
        parser.add_argument('--scoreboard', action='store_true', help='print whether any event type has edge')

    def handle(self, *args, **o):
        if o['scoreboard']:
            rows = event_scoreboard()
            if not rows:
                self.stdout.write('No scored events yet — outcomes are filled in 24h after a story.')
                return
            self.stdout.write(f"{'kind':14s}{'dir':9s}{'n':>5s}{'hit %':>8s}{'avg move':>10s}")
            for r in rows:
                self.stdout.write(f"{r['kind']:14s}{r['direction']:9s}{r['n']:5d}"
                                  f"{r['hit_rate']:8.0f}{r['avg_move_pct']:+10.2f}")
            return

        got = ingest()
        self.stdout.write(f"ingest: fetched {got['fetched']}, stored {got['stored']}, "
                          f"{got['not_ours']} about things we do not trade")
        if o['no_classify']:
            return
        done = classify(limit=o['limit'])
        self.stdout.write(f"classify: {done['classified']} headlines"
                          + (f", {done['failed']} failed" if done.get('failed') else '')
                          + (f" ({done['reason']})" if done.get('reason') else ''))
        scored = score_outcomes()
        self.stdout.write(self.style.SUCCESS(f"scored {scored['scored']} older stories against price"))
