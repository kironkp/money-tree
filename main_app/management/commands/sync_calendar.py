"""Cache the exchange calendar from Alpaca into MarketSession (falls back to
the built-in holiday list when no keys are present)."""
from datetime import UTC, date, datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand

from main_app.models import MarketSession
from main_app.services.data import calendar as cal


class Command(BaseCommand):
    help = 'Refresh MarketSession rows from Alpaca (needs keys)'

    def add_arguments(self, parser):
        parser.add_argument('--days-back', type=int, default=400)
        parser.add_argument('--days-ahead', type=int, default=200)

    def handle(self, *args, **o):
        if not settings.ALPACA_ENABLED:
            self.stdout.write('no Alpaca keys — the built-in NYSE holiday table is in use (2024–2027)')
            return
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetCalendarRequest
        client = TradingClient(settings.ALPACA_API_KEY, settings.ALPACA_SECRET_KEY, paper=True)
        start = date.today() - timedelta(days=o['days_back'])
        end = date.today() + timedelta(days=o['days_ahead'])
        rows = client.get_calendar(GetCalendarRequest(start=start, end=end))
        n = 0
        for r in rows:
            open_utc = datetime.combine(r.date, r.open, tzinfo=cal.ET).astimezone(UTC)
            close_utc = datetime.combine(r.date, r.close, tzinfo=cal.ET).astimezone(UTC)
            MarketSession.objects.update_or_create(date=r.date, defaults={
                'open_utc': open_utc, 'close_utc': close_utc, 'early_close': r.close.hour < 16})
            n += 1
        cal.load_overrides_from_db()
        self.stdout.write(self.style.SUCCESS(f'{n} sessions cached'))
