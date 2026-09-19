"""Store a morning diagnosis so it appears in the app and in the evening email.

The workflow that produces these runs outside Django — it is a Claude Code
workflow, not a management command — so this is the seam between the two. It
takes the JSON the workflow returns and writes one JournalEntry, which is the
app's existing home for "something worth reading later".

`kind` is 'diagnosis' and the field is 10 characters wide, which it just fits;
'daily_diagnosis' would be silently truncated, and a truncated kind is a row
nobody can filter for.
"""
from __future__ import annotations

import json
from datetime import date as date_cls

from django.core.management.base import BaseCommand
from django.utils import timezone

from main_app.models import JournalEntry

RANK = {'critical': '🔴', 'warn': '🟠', 'note': '·'}


class Command(BaseCommand):
    help = 'Record a daily diagnosis (JSON on stdin or --file) as a journal entry'

    def add_arguments(self, parser):
        parser.add_argument('--file', help='path to the workflow JSON (default: stdin)')
        parser.add_argument('--date', help='YYYY-MM-DD (default: today)')

    def handle(self, *args, **o):
        raw = open(o['file']).read() if o['file'] else self._stdin()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.stderr.write(f'not valid JSON: {exc}')
            return
        when = date_cls.fromisoformat(o['date']) if o['date'] else timezone.localdate()

        findings = data.get('findings') or []
        counts = data.get('counts') or {}
        crit = counts.get('critical', sum(1 for f in findings if f.get('severity') == 'critical'))
        warn = counts.get('warn', sum(1 for f in findings if f.get('severity') == 'warn'))

        if crit:
            title = f'{crit} critical, {warn} warning'
        elif warn:
            title = f'{warn} warning{"" if warn == 1 else "s"}, nothing critical'
        else:
            title = 'Nothing critical'

        lines = []
        for h in (data.get('headlines') or []):
            lines.append(f"**{h.get('title', h.get('angle', ''))}** — {h.get('headline', '')}")
        if findings:
            lines.append('')
            for f in findings:
                lines.append(f"{RANK.get(f.get('severity'), '·')} **{f.get('what', '')}**")
                if f.get('evidence'):
                    lines.append(f"    {f['evidence']}")
                if f.get('action') and f['action'].lower() != 'none':
                    lines.append(f"    → {f['action']}")
        else:
            lines.append('')
            lines.append('No findings. Everything checked came back unchanged.')

        entry, created = JournalEntry.objects.update_or_create(
            date=when, kind='diagnosis',
            defaults={'title': f'Morning check: {title}', 'body': '\n'.join(lines),
                      'metrics': {'counts': counts, 'findings': findings}},
        )
        verb = 'recorded' if created else 'updated'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} diagnosis for {when}: {len(findings)} finding(s), {crit} critical, {warn} warn'))

    def _stdin(self) -> str:
        import sys
        return sys.stdin.read()
