"""The News Agent: score every new story out of 10 and act above the threshold."""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from main_app.services.news_agent import MODEL, run_session, score_verdicts, scoreboard


class Command(BaseCommand):
    help = 'Run one News Agent session: read the new stories, score them, issue instructions'

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=int, default=0, help='look back this far (default: 5)')
        parser.add_argument('--model', default=MODEL)
        parser.add_argument('--scoreboard', action='store_true', help='is a high score actually better?')

    def handle(self, *args, **o):
        if o['scoreboard']:
            rows = scoreboard()
            if not rows:
                self.stdout.write('No scored calls yet — outcomes are filled in 24h after a verdict.')
                return
            self.stdout.write(f"{'score':>6s}{'n':>6s}{'right %':>10s}{'avg move':>11s}")
            for r in rows:
                self.stdout.write(f"{r['score']:>6d}{r['n']:>6d}{r['hit_rate']:>9.0f}%{r['avg_move_pct']:>+11.2f}")
            return

        since = timezone.now() - timedelta(hours=o['hours']) if o['hours'] else None
        s = run_session(since=since, model=o['model'])
        if s.error:
            self.stderr.write(f'session failed: {s.error}')
            return
        self.stdout.write(self.style.SUCCESS(
            f'session #{s.pk}: {s.considered} stories, {s.actionable} actionable, '
            f'${float(s.cost_usd):.4f}, {s.duration_s:.0f}s'))
        if s.narrative:
            self.stdout.write(f'\n{s.narrative}\n')
        for v in s.verdicts.all()[:40]:
            mark = '→ ' + v.call_text if v.actionable else '  no action'
            self.stdout.write(f'  {v.score:2d}/10 {mark:22s} {v.headline[:70]}')
            if v.thesis:
                self.stdout.write(f'        {v.thesis[:150]}')
        scored = score_verdicts()
        if scored['scored']:
            self.stdout.write(f"\nscored {scored['scored']} older calls against price")
