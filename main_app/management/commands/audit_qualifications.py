"""Explain or persist every strategy version's evidence state."""
from django.core.management.base import BaseCommand

from main_app.models import Account, Market, Mode, Stage, Strategy
from main_app.services.promotion import STAGE_ACCOUNT_MODE, qualification_assessment, refresh_qualification


class Command(BaseCommand):
    help = 'Audit strategy evidence; --apply persists qualification and quarantines measured no-edge'

    def add_arguments(self, parser):
        parser.add_argument('--market', choices=Market.values, default='')
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        rows = Strategy.objects.order_by('market', 'name')
        if options['market']:
            rows = rows.filter(market=options['market'])
        changed = 0
        for row in rows:
            mode = STAGE_ACCOUNT_MODE.get(row.stage, Mode.SIM)
            # Seed has no execution account; use the simulator only as a view
            # of any forward observations already collected for this market.
            if row.stage == Stage.SEED:
                mode = Mode.SIM
            account = Account.for_mode(mode, row.market)
            if options['apply']:
                assessment, state_changed = refresh_qualification(row, account)
                changed += int(state_changed)
            else:
                assessment, state_changed = qualification_assessment(row, account), False
            stats = assessment['stats']
            self.stdout.write(
                f'{row.market:7} {row.key:18} v{row.version:<3} {assessment["state"]:10} '
                f'{stats.get("trades", 0):>4} trades  PF {stats.get("profit_factor", 0):>5.2f}  '
                f'net {stats.get("net_pnl", 0):>+10.2f} — {assessment["reason"]}'
            )
        suffix = 'persisted' if options['apply'] else 'dry run; pass --apply to persist'
        self.stdout.write(self.style.SUCCESS(f'{rows.count()} strategies audited; {changed} state changes {suffix}'))
