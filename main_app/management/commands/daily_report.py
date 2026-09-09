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
