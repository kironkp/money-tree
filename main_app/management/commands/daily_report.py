"""The 17:30 daily report: emailed, journalled, and printed.

Runs whether or not anything traded — a quiet lane still has to account for
itself. Never raises into launchd: a failure is logged and mailed as a stub so
a silent evening is unambiguous evidence that the job itself did not run.
"""
from datetime import date

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand

from main_app.services.report import build_report, render_html, render_text, save_journal


class Command(BaseCommand):
    help = 'Build (and email) the daily per-lane learning + P&L report'

    def add_arguments(self, parser):
        parser.add_argument('--date', default='', help='YYYY-MM-DD (default: today in ET)')
        parser.add_argument('--mode', default='sim', choices=['sim', 'paper', 'live'])
        parser.add_argument('--to', default='', help='override the recipient')
        parser.add_argument('--no-email', action='store_true', help='print only')
        parser.add_argument('--no-journal', action='store_true')

    def handle(self, *args, **o):
        d = date.fromisoformat(o['date']) if o['date'] else None
        rep = build_report(d, mode=o['mode'])
        text = render_text(rep)
        # The operational cycle stopped emailing per-run on 2026-09-25 (it runs every
        # 15 minutes). Its open findings ride here instead, so a standing fault is
        # still put in front of a human once a day rather than not at all.
        text = text + _open_findings_block()
        self.stdout.write(text)

        if not o['no_journal']:
            save_journal(rep)
            self.stdout.write(self.style.SUCCESS('journal rows written'))

        if o['no_email']:
            return
        to = o['to'] or getattr(settings, 'REPORT_EMAIL', '') or getattr(settings, 'DEFAULT_TO_EMAIL', '')
        if not to:
            self.stderr.write('no recipient configured (REPORT_EMAIL / DJANGO_SUPERUSER_EMAIL) — not emailed')
            return
        if not settings.EMAIL_HOST:
            self.stderr.write('no mail server configured — not emailed')
            return
        t = rep['total']
        subject = (f"MoneyTree {rep['date']:%b %-d}: {t['net']:+,.2f} today · "
                   + ' · '.join(f"{lane['title']} {lane['pnl']['net']:+,.0f}" for lane in rep['lanes']))
        try:
            msg = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, [to])
            msg.attach_alternative(render_html(rep), 'text/html')
            msg.send(fail_silently=False)
            self.stdout.write(self.style.SUCCESS(f'emailed to {to}'))
        except Exception as exc:
            self.stderr.write(f'email failed: {exc!r}')


def _open_findings_block() -> str:
    """Open operational findings, appended to the daily report."""
    from main_app.models import ReviewFinding

    # Explicit rank, not `-severity`: that sorts alphabetically, which puts WARN
    # above CRITICAL and buries the only lines that matter.
    rank = {ReviewFinding.CRITICAL: 0, ReviewFinding.WARN: 1}
    rows = sorted(ReviewFinding.objects.filter(status=ReviewFinding.OPEN),
                  key=lambda f: (rank.get(f.severity, 2), -f.seen_count))[:20]
    if not rows:
        return '\n\nOperational review: no open findings.\n'
    out = [f'\n\nOperational review: {len(rows)} open finding(s)',
           '(the 15-minute cycle no longer emails per run; this is the daily summary)', '']
    for f in rows:
        lane = f.account.market if f.account else 'desk'
        out.append(f'  [{f.severity.upper()}] {lane}: {f.title}  (seen {f.seen_count}x, '
                   f'{f.check_key})')
    out.append('')
    out.append('Full detail at /review. Nothing here changed a strategy or a risk limit.')
    return '\n'.join(out) + '\n'
