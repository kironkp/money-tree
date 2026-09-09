"""What the paid APIs cost, and who spent it."""
from django.core.management.base import BaseCommand

from main_app.services.spend import day_spend, projected_monthly, range_spend


class Command(BaseCommand):
    help = 'Show recorded API spend by project, provider, model and purpose'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30)

    def handle(self, *args, **o):
        today = day_spend()
        s = range_spend(o['days'])
        self.stdout.write(self.style.SUCCESS(
            f"Today ${today['total']:.2f} ({today['calls']} calls) · "
            f"last {o['days']} days ${s['total']:.2f} ({s['calls']} calls) · "
            f"at this rate ${projected_monthly(o['days']):.2f}/month"))
        if not s['calls']:
            self.stdout.write('\nNothing recorded. The ledger only knows about calls that write to it:\n'
                              '  - this app (the coach) writes automatically\n'
                              '  - other projects have to post to it, or their spend stays invisible here\n'
                              'Provider dashboards remain the source of truth for anything not reporting in.')
            return
        for label, key in (('project', 'by_project'), ('provider', 'by_provider'),
                           ('model', 'by_model'), ('purpose', 'by_purpose')):
            rows = s.get(key) or {}
            if not rows:
                continue
            self.stdout.write(f'\nBy {label}:')
            for name, v in rows.items():
                share = v['cost'] / s['total'] * 100 if s['total'] else 0
                self.stdout.write(f"  {name:28s} ${v['cost']:9.2f}  {share:5.1f}%  "
                                  f"{v['calls']:6d} calls  {v['in']:,} in / {v['out']:,} out")
        per_day = s.get('per_day') or {}
        if per_day:
            self.stdout.write('\nBy day:')
            for d, c in list(per_day.items())[-14:]:
                self.stdout.write(f"  {d}  ${c:8.2f}  {'█' * min(40, int(c * 4))}")
