"""Run a review cycle from the scheduler or by hand.

    manage.py review operational      # every 15 min, and after fills in-process
    manage.py review improvement      # daily, after the close

Never raises into launchd: a crash is caught, recorded as a failed ReviewRun and
mailed, because a review job that dies silently is indistinguishable from a
review job that found nothing — and those two must never look the same.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from main_app.models import ReviewRun

CADENCE = {ReviewRun.OPERATIONAL: timedelta(minutes=15), ReviewRun.IMPROVEMENT: timedelta(days=1)}


class Command(BaseCommand):
    help = 'Run the operational or improvement review cycle'

    def add_arguments(self, parser):
        parser.add_argument('cycle', choices=[ReviewRun.OPERATIONAL, ReviewRun.IMPROVEMENT])
        parser.add_argument('--market', default='', help='one lane only (default: every lane that can hold a position)')
        parser.add_argument('--mode', default='', help='one mode only (sim/paper/live)')
        parser.add_argument('--no-email', action='store_true')
        parser.add_argument('--quiet', action='store_true')

    def handle(self, *args, **o):
        from main_app.models import Account
        from main_app.services.review.runner import run_improvement, run_operational

        cycle = o['cycle']
        from main_app.services.review.runner import reviewable_accounts
        accounts = None
        if o['market'] or o['mode']:
            q = reviewable_accounts()
            if o['market']:
                q = q.filter(market=o['market'])
            if o['mode']:
                q = q.filter(mode=o['mode'])
            accounts = list(q)
            if not accounts:
                self.stderr.write(f'no account matches market={o["market"]!r} mode={o["mode"]!r}')
                return
        now = timezone.now()
        fn = run_operational if cycle == ReviewRun.OPERATIONAL else run_improvement
        try:
            run = fn(accounts=accounts, trigger='schedule', now=now,
                     notify=not o['no_email'], next_due_at=now + CADENCE[cycle])
        except Exception as exc:
            # The runners already trap their own failures; this is the last net,
            # for an error raised before a run row could even be created.
            run = ReviewRun.objects.create(cycle=cycle, trigger='schedule', started_at=now,
                                           finished_at=timezone.now(), status='failed',
                                           error=repr(exc), next_due_at=now + CADENCE[cycle])
            self.stderr.write(f'{cycle} review failed to start: {exc!r}')
            raise SystemExit(1)

        if o['quiet'] and run.status == 'ok' and not run.findings_opened:
            return
        self.stdout.write(f'{cycle} review {run.status}: {run.checks_run} checks, '
                          f'{run.checks_failed} crashed, {run.findings_opened} new findings, '
                          f'{run.findings_repeated} repeats')
        for a in run.actions or []:
            self.stdout.write(self.style.WARNING(f'  action: {a}'))
        if run.error:
            self.stderr.write(run.error[:800])
            raise SystemExit(1)
