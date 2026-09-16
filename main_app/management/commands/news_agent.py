"""The News Agent: score every new story out of 10 and act above the threshold."""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from main_app.services.dossier import grade as grade_dossiers, refresh
from main_app.services.news_agent import (MODEL, expire_leases, run_session, score_verdicts,
                                          scoreboard)


class Command(BaseCommand):
    help = 'Run one News Agent session: read the new stories, score them, issue instructions'

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=int, default=0, help='look back this far (default: 5)')
        parser.add_argument('--model', default=MODEL)
        parser.add_argument('--scoreboard', action='store_true', help='is a high score actually better?')
        parser.add_argument('--no-research', action='store_true',
                            help='skip the per-company dossiers this sitting')
        parser.add_argument('--research-limit', type=int, default=4,
                            help='how many companies to research this sitting')
        parser.add_argument('--reconstructed', action='store_true',
                            help='score the rebuilt history instead — never evidence, useful for debugging')

    def handle(self, *args, **o):
        if o['scoreboard'] or o['reconstructed']:
            rows = scoreboard(provenance='reconstructed' if o['reconstructed'] else 'contemporaneous')
            if not rows:
                self.stdout.write('No scored calls yet — outcomes are filled in 24h after a verdict.')
                return
            self.stdout.write(f"{'score':>6s}{'n':>6s}{'target %':>10s}{'net ATR':>10s}"
                              f"{'tgt/stop/out':>14s}")
            for r in rows:
                self.stdout.write(f"{r['score']:>6d}{r['n']:>6d}{r['hit_rate']:>9.0f}%"
                                  f"{r['avg_atr_net']:>+10.2f}"
                                  f"{f"{r['targets']}/{r['stops']}/{r['timeouts']}":>14s}")
            self.stdout.write('\nContemporaneous forecasts only. Outcomes rebuilt from bars are '
                              'listed separately with --reconstructed.')
            return

        # Grade and tidy FIRST. These cost nothing, they need no API, and putting
        # them after the session meant an OpenAI outage cost a day of evidence as
        # well as a sitting — the one thing an outage should never take.
        freed = expire_leases()
        if freed:
            self.stdout.write(f'released {freed} abandoned lease(s)')
        scored = score_verdicts()
        if scored['scored']:
            self.stdout.write(f"graded {scored['graded']} call(s) against price"
                              f" ({scored['priced_only']} priced but not directional)")

        # Research runs inside the sitting rather than as a fifth launchd job: the
        # label com.kiron.moneytree.research already belongs to the walk-forward
        # optimizer, StartInterval plists drift out of phase after any reboot so a
        # separate job cannot be relied on to run before the sitting that reads it,
        # and CLAUDE.md warns against a fourth chatty SQLite writer.
        graded = grade_dossiers()
        if graded['graded']:
            self.stdout.write(f"graded {graded['graded']} shadow trade(s)")
        if not o['no_research']:
            for d in refresh(limit=o['research_limit']):
                if d.error:
                    self.stdout.write(self.style.WARNING(f'  {d.symbol}: {d.error[:80]}'))
                else:
                    self.stdout.write(
                        f'  {d.symbol} researched: catalyst {d.score_catalyst}/10, '
                        f'{d.direction}, ${float(d.cost_usd):.4f}')

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

