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

from main_app.models import AgentConfig, Bar, Instrument, Strategy
from main_app.services.data import calendar as cal
from main_app.services.data.store import best_source
from main_app.services.strategies.fx_trend import H10_PAIRS, H10_TIMEFRAME

# Series that exist for research rather than for a running agent. Nothing else
# refreshes these, which is exactly why they need naming somewhere.
RESEARCH_SERIES = ((list(H10_PAIRS), H10_TIMEFRAME, 'forex', 'H10 / fx_trend'),)

# Where each research market's history comes from. Stocks are named outright:
# research backtests on SIP, and the default provider falls back to Yahoo
# without keys, which would refresh a feed research never reads.
SYNC_PROVIDER = {'forex': 'yahoo', 'stocks': 'alpaca'}

# How many bars late a series may be before it is called stale, per timeframe.
STALE_AFTER_BARS = 3


def research_series() -> list:
    """RESEARCH_SERIES plus the stock history the nightly walk-forwards read.

    Stock research loads the best source, which is SIP, and the live loop only
    writes IEX. Nothing refreshed SIP, so it stopped on 2026-09-17 and experiments
    #444-447 ran with no bars for the last 25 days of their window.

    Every stocks row, enabled or not: auto_research walks them all, and a parked
    strategy can earn its way back on the result.
    """
    series = list(RESEARCH_SERIES)
    symbols = sorted({sym for row in Strategy.objects.filter(market='stocks') for sym in row.symbols})
    if symbols:
        series.append((symbols, AgentConfig.get().timeframe_for('stocks'), 'stocks',
                       'nightly stock research'))
    return series


class Command(BaseCommand):
    help = 'Refresh research bar series and report staleness for every lane'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=14,
                            help='how far back to re-request (overlap is cheap; gaps are not)')
        parser.add_argument('--no-sync', action='store_true', help='report only')

    def handle(self, *args, **o):
        now = timezone.now()
        series = research_series()
        if not o['no_sync']:
            for symbols, tf, market, why in series:
                self.stdout.write(f'refreshing {tf} for {", ".join(symbols)}  ({why})')
                try:
                    call_command('sync_bars', timeframe=tf, days=o['days'],
                                 symbols=','.join(symbols),
                                 provider=SYNC_PROVIDER.get(market, ''))
                except Exception as exc:                      # never fatal: the caller still has a backup to take
                    self.stderr.write(f'  sync failed: {exc!r}')

        # Who actually reads each series. Without this the report says "32 stale"
        # and every one of them is a 1Min series abandoned two versions ago, so the
        # number gets ignored — which is the failure mode that let the 1Hour forex
        # feed sit dead for 17 days behind a report nobody trusted.
        #
        # An enabled strategy reads its lane's BASE bars whatever its own
        # timeframe says: '' follows the lane, and a coarse one is resampled
        # from the base, never loaded natively. Keyed on Strategy.timeframe this
        # set held ('forex', '') and not one lane base, so a dead live feed would
        # have printed as "stale, unused". Every config row counts: reading a
        # series nobody trades is harmless, missing one that somebody does is not.
        AgentConfig.get()
        markets = set(Strategy.objects.filter(enabled=True).values_list('market', flat=True))
        traded = {(m, cfg.timeframe_for(m)) for cfg in AgentConfig.objects.all() for m in markets}
        traded |= {(m, tf) for syms, tf, m, _ in series}
        # Research loads ONE feed per series, the one best_source picks, and only
        # ever up to the last completed session. So that copy is the one judged,
        # against that close, open market or shut: summed across feeds, a fresh
        # IEX copy hid a SIP copy that had been dead for 25 days, and the 02:00
        # run never saw the stock market open to judge either.
        researched = {(sym, tf) for syms, tf, _, _ in series for sym in syms}

        self.stdout.write(self.style.MIGRATE_HEADING('\nBAR FRESHNESS'))
        self.stdout.write(f"  {'symbol':10} {'tf':7} {'bars':>8} {'latest (UTC)':17} {'age':>12}  state")
        stale = unused = 0
        have = set()
        rows = (Bar.objects.values('instrument__symbol', 'instrument__market', 'timeframe')
                .annotate(n=Count('id'), hi=Max('ts')).order_by('instrument__market', 'timeframe',
                                                                'instrument__symbol'))
        from main_app.services.timeframes import tf_minutes
        for r in rows:
            sym, market, tf = r['instrument__symbol'], r['instrument__market'], r['timeframe']
            n, hi = r['n'], r['hi']
            have.add((sym, tf))
            allowed = tf_minutes(tf) * STALE_AFTER_BARS
            ac = 'forex' if market == 'forex' else ('crypto' if market in ('crypto', 'degen') else 'stock')
            closed_at = cal.last_close(now, ac) if (sym, tf) in researched else None
            if closed_at is not None:
                src = best_source(Instrument.objects.get(symbol=sym), tf)
                copy = Bar.objects.filter(instrument__symbol=sym, timeframe=tf, source=src)
                n, hi = copy.count(), copy.aggregate(hi=Max('ts'))['hi']
            age_min = (now - hi).total_seconds() / 60
            # A closed market is not a stale feed. Only judge freshness when the
            # venue is actually open, or the weekend would flag everything.
            open_now = cal.is_open(now, ac)
            used = (market, tf) in traded
            if closed_at is not None:
                short_min = (closed_at - hi).total_seconds() / 60
                if short_min <= allowed:
                    state = 'ok'
                else:
                    state = self.style.ERROR(f'STALE (stops {short_min / 1440:.1f} d before the last close)')
                    stale += 1
                state += f'  [research reads {src}]'
            elif not open_now:
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
            self.stdout.write(f"  {sym:10} {tf:7} {n:8} {hi:%Y-%m-%d %H:%M}  "
                              f"{age_min / 60:9.1f} h  {state}")
        # A research series with no bars at all has no row above, and silence
        # reads as current: a symbol added to a strategy whose first sync failed
        # is exactly that.
        for sym, tf in sorted(researched - have):
            self.stdout.write(f"  {sym:10} {tf:7} {0:8} {'-':16}  {'-':>9}    "
                              + self.style.ERROR('STALE (no bars at all)'))
            stale += 1
        if stale:
            self.stdout.write(self.style.ERROR(
                f'\n  {stale} series STALE that something actually trades'))
        else:
            self.stdout.write(self.style.SUCCESS(
                '\n  every series an enabled strategy or research job reads is current'))
        if unused:
            self.stdout.write(f'  {unused} more are stale but unread — no enabled strategy '
                              f'uses that timeframe (kept visible, not counted)')
