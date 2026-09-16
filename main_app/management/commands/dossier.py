"""Per-company research, in shadow. Writes dossiers; places no orders."""
from django.core.management.base import BaseCommand

from main_app.models import SymbolDossier
from main_app.services.dossier import MODEL, RESEARCHED, budget_left, build, candidates, refresh


class Command(BaseCommand):
    help = 'Research the companies that are in the news today. Shadow only: no trade is placed.'

    def add_arguments(self, parser):
        parser.add_argument('--symbol', help='research one name regardless of the news')
        parser.add_argument('--limit', type=int, default=4, help='how many names this sweep')
        parser.add_argument('--model', default=MODEL)
        parser.add_argument('--candidates', action='store_true', help='who would be researched, free')
        parser.add_argument('--show', help='print the latest dossier for a symbol')

    def handle(self, *a, **o):
        if o['candidates']:
            self.stdout.write(f'budget left today: ${budget_left():.4f}')
            for sym in candidates(limit=o['limit']):
                self.stdout.write(f'  {sym}')
            return
        if o['show']:
            d = SymbolDossier.objects.filter(symbol=o['show'].upper()).first()
            if d is None:
                self.stderr.write('no dossier for that symbol')
                return
            self._show(d)
            return

        if o['symbol']:
            sym = o['symbol'].upper()
            if sym not in RESEARCHED:
                self.stderr.write(f'{sym} is not researched; one of {", ".join(RESEARCHED)}')
                return
            rows = [build(sym)]
        else:
            rows = refresh(limit=o['limit'])
        if not rows:
            self.stdout.write('nothing in the news worth researching')
            return
        total = sum(float(d.cost_usd) for d in rows)
        for d in rows:
            self._line(d)
        self.stdout.write(self.style.SUCCESS(
            f'\n{len(rows)} dossier(s), ${total:.4f}, '
            f'${budget_left():.4f} of today\'s budget left'))

    def _line(self, d):
        if d.error:
            self.stdout.write(self.style.WARNING(f'  {d.symbol:6s} FAILED: {d.error[:90]}'))
            return
        call = f'{d.direction.upper()} {d.symbol}' if d.direction != 'none' else 'no trade'
        self.stdout.write(
            f'  {d.symbol:6s} catalyst {d.score_catalyst}/10  context {d.score_context}/10  '
            f'thesis {d.score_thesis}/10  -> {call:14s} '
            f'p(target) {d.p_target_first or 0:.0%} vs base {d.base_rate or 0:.0%}  '
            f'${float(d.cost_usd):.4f} {d.duration_s:.0f}s')

    def _show(self, d):
        self.stdout.write(f'{d.symbol} — {d.as_of:%a %b %-d, %-I:%M %p}  ({d.model}, {d.service_tier})')
        if d.error:
            self.stdout.write(self.style.ERROR(f'failed: {d.error}'))
            return
        self.stdout.write(f'\ncatalyst {d.score_catalyst}/10 · context {d.score_context}/10 · '
                          f'thesis {d.score_thesis}/10 · {d.direction} · size x{d.size_multiplier}')
        if d.has_catalyst:
            self.stdout.write(f'catalyst: {d.catalyst_headline}\n          {d.catalyst_at:%b %-d %H:%M}Z')
        self.stdout.write(f'forecast: target-first {d.p_target_first:.1%} '
                          f'(base {d.base_rate:.1%}), stop-first {d.p_stop_first:.1%}, '
                          f'neither {d.p_timeout:.1%}; positive after costs {d.p_positive_net:.1%}')
        if d.veto_reason:
            self.stdout.write(f'veto: {d.veto_reason}')
        self.stdout.write(f'\n{d.narrative}\n')
        for title, rows in (('THE CASE FOR', d.bull), ('THE CASE AGAINST', d.bear)):
            self.stdout.write(title)
            for r in rows:
                self.stdout.write(f'  - {r.get("claim", "")} [{", ".join(r.get("facts") or [])}]')
        if d.triggers:
            self.stdout.write('\nWHAT WOULD CHANGE THIS')
            for t in d.triggers:
                self.stdout.write(f'  {t.get("direction", ""):8s} {t.get("condition", "")}')
        self.stdout.write(f'\n{len(d.facts)} facts, {len(d.stories)} stories, '
                          f'{d.searches} search(es), ${float(d.cost_usd):.4f}')
        if d.refused_reason:
            self.stdout.write(self.style.WARNING(f'rejected: {d.refused_reason}'))
