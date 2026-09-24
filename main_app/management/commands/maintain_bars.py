"""Keep the bar series a hypothesis depends on from going quietly stale.

H10 is measured on 1Hour forex bars. No agent trades that timeframe — the forex
lane runs 15Min — so nothing refreshed it, and it stopped dead at 2026-09-07 05:00,
which happened to be the exact boundary of H10's held-out window. The gap was
invisible because staleness was only ever noticed for series something was
actively trading.

So this does two things and reports both: refreshes the series research depends on,
and prints how stale every lane's bars are whether or not anything trades them. A
feed that nobody watches is a feed that stops.

Runs from deploy/git-push.sh at 02:00, ahead of the data backup, so the night's
snapshot carries fresh bars.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db.models import Count, Max
from django.utils import timezone

from main_app.models import Bar, Instrument, Strategy
from main_app.services.data import calendar as cal
from main_app.services.strategies.fx_trend import H10_PAIRS, H10_TIMEFRAME

# Series that exist for research rather than for a running agent. Nothing else
# refreshes these, which is exactly why they need naming somewhere.
RESEARCH_SERIES = ((list(H10_PAIRS), H10_TIMEFRAME, 'forex', 'H10 / fx_trend'),)

# How many bars late a series may be before it is called stale, per timeframe.
STALE_AFTER_BARS = 3


class Command(BaseCommand):
    help = 'Refresh research bar series and report staleness for every lane'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=14,
                            help='how far back to re-request (overlap is cheap; gaps are not)')
        parser.add_argument('--no-sync', action='store_true', help='report only')

    def handle(self, *args, **o):
        now = timezone.now()
        if not o['no_sync']:
            for symbols, tf, market, why in RESEARCH_SERIES:
                self.stdout.write(f'refreshing {tf} for {", ".join(symbols)}  ({why})')
                try:
                    call_command('sync_bars', timeframe=tf, days=o['days'],
                                 symbols=','.join(symbols),
                                 provider='yahoo' if market == 'forex' else '')
                except Exception as exc:                      # never fatal: the caller still has a backup to take
                    self.stderr.write(f'  sync failed: {exc!r}')

        # Who actually reads each series. Without this the report says "32 stale"
        # and every one of them is a 1Min series abandoned two versions ago, so the
        # number gets ignored — which is the failure mode that let the 1Hour forex
        # feed sit dead for 17 days behind a report nobody trusted.
        traded = {(s_.market, s_.timeframe) for s_ in Strategy.objects.filter(enabled=True)}
        traded |= {(m, tf) for syms, tf, m, _ in RESEARCH_SERIES}

        self.stdout.write(self.style.MIGRATE_HEADING('\nBAR FRESHNESS'))
        self.stdout.write(f"  {'symbol':10} {'tf':7} {'bars':>8} {'latest (UTC)':17} {'age':>12}  state")
        stale = unused = 0
        rows = (Bar.objects.values('instrument__symbol', 'instrument__market', 'timeframe')
                .annotate(n=Count('id'), hi=Max('ts')).order_by('instrument__market', 'timeframe',
                                                                'instrument__symbol'))
        from main_app.services.timeframes import tf_minutes
        for r in rows:
            sym, market, tf = r['instrument__symbol'], r['instrument__market'], r['timeframe']
            age_min = (now - r['hi']).total_seconds() / 60
            allowed = tf_minutes(tf) * STALE_AFTER_BARS
            # A closed market is not a stale feed. Only judge freshness when the
            # venue is actually open, or the weekend would flag everything.
            ac = 'forex' if market == 'forex' else ('crypto' if market in ('crypto', 'degen') else 'stock')
            open_now = cal.is_open(now, ac)
            used = (market, tf) in traded
            if not open_now:
                state = 'market shut'
            elif age_min <= allowed:
                state = 'ok'
            elif used:
                state = self.style.ERROR(f'STALE ({age_min / tf_minutes(tf):.0f} bars late)')
                stale += 1
            else:
                # Dead, but nothing reads it. Still shown — a series that quietly
                # stopped is worth seeing — just not counted as a live problem.
                state = f'stale, unused ({age_min / tf_minutes(tf):.0f} bars)'
                unused += 1
            self.stdout.write(f"  {sym:10} {tf:7} {r['n']:8} {r['hi']:%Y-%m-%d %H:%M}  "
                              f"{age_min / 60:9.1f} h  {state}")
        if stale:
            self.stdout.write(self.style.ERROR(
                f'\n  {stale} series STALE that something actually trades'))
        else:
            self.stdout.write(self.style.SUCCESS(
                '\n  every series an enabled strategy or research job reads is current'))
        if unused:
            self.stdout.write(f'  {unused} more are stale but unread — no enabled strategy '
                              f'uses that timeframe (kept visible, not counted)')
