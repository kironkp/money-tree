"""Pull history into the bar store. Alpaca when keyed (years of SIP bars),
else Yahoo (60 days of 5-min), or synthetic for a keyless demo."""
from datetime import UTC, datetime, timedelta

from django.core.management.base import BaseCommand, CommandError

from main_app.models import AgentConfig, Instrument
from main_app.services.data import get_provider
from main_app.services.data.store import resync, sync_bars


class Command(BaseCommand):
    help = 'Fetch and store bars for the watchlist (or --symbols)'

    def add_arguments(self, parser):
        parser.add_argument('--symbols', default='', help='comma-separated; default = watchlist')
        parser.add_argument('--timeframe', default='', help='1Min/5Min/15Min/1Day; default = config')
        parser.add_argument('--days', type=int, default=60)
        parser.add_argument('--provider', default='', help='alpaca | alpaca-iex | yahoo | synthetic (default: alpaca if keyed else yahoo)')
        parser.add_argument('--resync', action='store_true', help='wipe and refetch')

    def handle(self, *args, **o):
        cfg = AgentConfig.get()
        timeframe = o['timeframe'] or cfg.timeframe
        provider = get_provider(o['provider'] or None)
        qs = Instrument.objects.filter(active=True)
        if o['symbols']:
            symbols = [s.strip().upper() for s in o['symbols'].split(',') if s.strip()]
            qs = qs.filter(symbol__in=symbols)
            if qs.count() != len(symbols):
                missing = set(symbols) - set(qs.values_list('symbol', flat=True))
                raise CommandError(f'unknown symbols: {", ".join(sorted(missing))} — add them at /data/ first')
        else:
            qs = qs.filter(in_watchlist=True)
        end = datetime.now(UTC)
        start = end - timedelta(days=o['days'])
        total = 0
        yahoo = None
        for inst in qs:
            fn = resync if o['resync'] else sync_bars
            prov = provider
            if inst.asset_class == 'forex' and provider.name not in ('yahoo', 'synthetic'):
                # Alpaca has no forex; Yahoo is the lane's feed either way.
                yahoo = yahoo or get_provider('yahoo')
                prov = yahoo
            try:
                res = fn(inst, timeframe, start, end, prov)
            except Exception as exc:
                self.stderr.write(f'  {inst.symbol}: FAILED {exc!r}')
                continue
            total += res['added']
            issues = f" — {'; '.join(res['issues'])}" if res['issues'] else ''
            self.stdout.write(f"  {inst.symbol:8s} {timeframe}: fetched {res['fetched']:6d}, added {res['added']:6d}{issues}")
        self.stdout.write(self.style.SUCCESS(f'{provider.name}: {total} bars added'))
