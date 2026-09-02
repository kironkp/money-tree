"""Seed the watchlist, the singleton config, the accounts and one Strategy row
per registry strategy (disabled, stage Seed, default params)."""
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand

from main_app.models import Account, AgentConfig, AssetClass, Instrument, Market, Mode, Strategy
from main_app.services.strategies import all_strategies

DEFAULTS = [
    ('SPY', 'SPDR S&P 500', AssetClass.ETF), ('QQQ', 'Invesco QQQ', AssetClass.ETF),
    ('AAPL', 'Apple', AssetClass.STOCK), ('NVDA', 'NVIDIA', AssetClass.STOCK), ('TSLA', 'Tesla', AssetClass.STOCK),
    ('AMD', 'AMD', AssetClass.STOCK), ('MSFT', 'Microsoft', AssetClass.STOCK), ('AMZN', 'Amazon', AssetClass.STOCK),
    ('META', 'Meta', AssetClass.STOCK),
    ('BTC/USD', 'Bitcoin', AssetClass.CRYPTO), ('ETH/USD', 'Ether', AssetClass.CRYPTO),
]
CRYPTO_INCREMENTS = {'BTC/USD': Decimal('0.0001'), 'ETH/USD': Decimal('0.001')}


class Command(BaseCommand):
    help = 'Seed watchlist instruments, config, accounts and strategy rows'

    def handle(self, *args, **options):
        cfg = AgentConfig.get()
        for symbol, name, ac in DEFAULTS:
            inst, created = Instrument.objects.get_or_create(symbol=symbol, defaults={
                'name': name, 'asset_class': ac,
                'qty_increment': CRYPTO_INCREMENTS.get(symbol, Decimal('1')),
                'tick_size': Decimal('0.01') if ac != AssetClass.CRYPTO else Decimal('0.01'),
            })
            if created:
                self.stdout.write(f'  + {symbol}')
        if settings.ALPACA_ENABLED:
            self.refresh_increments()
        for mode in (Mode.SIM, Mode.PAPER, Mode.REPLAY):
            for market in (Market.STOCKS, Market.CRYPTO):
                Account.for_mode(mode, market)
        stocks = [s for s, _, ac in DEFAULTS if ac != AssetClass.CRYPTO]
        cryptos = [s for s, _, ac in DEFAULTS if ac == AssetClass.CRYPTO]
        for cls in all_strategies():
            markets = [(Market.STOCKS, stocks)]
            if 'crypto' in cls.asset_classes:
                markets.append((Market.CRYPTO, cryptos))
            for market, symbols in markets:
                row, created = Strategy.objects.get_or_create(key=cls.key, market=market, defaults={
                    'name': cls.name, 'params': cls.defaults(), 'timeframe': cls.default_timeframe,
                    'symbols': symbols, 'notes': cls.description,
                })
                if created:
                    self.stdout.write(f'  + strategy {cls.key} ({market})')
        self.stdout.write(self.style.SUCCESS(
            f'watchlist: {Instrument.objects.filter(in_watchlist=True).count()} instruments, '
            f'{Strategy.objects.count()} strategies, config timeframe {cfg.timeframe}, starting cash {cfg.starting_cash}'))

    def refresh_increments(self):
        try:
            from alpaca.trading.client import TradingClient
            client = TradingClient(settings.ALPACA_API_KEY, settings.ALPACA_SECRET_KEY, paper=True)
            for inst in Instrument.objects.filter(asset_class=AssetClass.CRYPTO):
                asset = client.get_asset(inst.symbol)
                inc = getattr(asset, 'min_trade_increment', None)
                if inc:
                    inst.qty_increment = Decimal(str(inc))
                    inst.save(update_fields=['qty_increment'])
                    self.stdout.write(f'  {inst.symbol}: increment {inc}')
        except Exception as exc:
            self.stderr.write(f'could not refresh crypto increments from Alpaca: {exc}')
