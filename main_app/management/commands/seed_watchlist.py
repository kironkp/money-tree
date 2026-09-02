"""Seed the watchlist, the singleton config, the accounts and one Strategy row
per registry strategy (disabled, stage Seed, default params)."""
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand

from main_app.models import Account, AgentConfig, AssetClass, Instrument, Market, Mode, Strategy
from main_app.services.strategies import all_strategies

DEFAULTS = [
    ('SPY', 'SPDR S&P 500', AssetClass.ETF, 'stocks'), ('QQQ', 'Invesco QQQ', AssetClass.ETF, 'stocks'),
    ('AAPL', 'Apple', AssetClass.STOCK, 'stocks'), ('NVDA', 'NVIDIA', AssetClass.STOCK, 'stocks'),
    ('TSLA', 'Tesla', AssetClass.STOCK, 'stocks'), ('AMD', 'AMD', AssetClass.STOCK, 'stocks'),
    ('MSFT', 'Microsoft', AssetClass.STOCK, 'stocks'), ('AMZN', 'Amazon', AssetClass.STOCK, 'stocks'),
    ('META', 'Meta', AssetClass.STOCK, 'stocks'),
    ('BTC/USD', 'Bitcoin', AssetClass.CRYPTO, 'crypto'), ('ETH/USD', 'Ether', AssetClass.CRYPTO, 'crypto'),
    # The degen lane: liquid, volatile altcoins Alpaca lets a paper account trade.
    ('SOL/USD', 'Solana', AssetClass.CRYPTO, 'degen'), ('DOGE/USD', 'Dogecoin', AssetClass.CRYPTO, 'degen'),
    ('XRP/USD', 'XRP', AssetClass.CRYPTO, 'degen'), ('AVAX/USD', 'Avalanche', AssetClass.CRYPTO, 'degen'),
    ('LINK/USD', 'Chainlink', AssetClass.CRYPTO, 'degen'), ('ADA/USD', 'Cardano', AssetClass.CRYPTO, 'degen'),
    ('PEPE/USD', 'Pepe', AssetClass.CRYPTO, 'degen'), ('SHIB/USD', 'Shiba Inu', AssetClass.CRYPTO, 'degen'),
    ('WIF/USD', 'dogwifhat', AssetClass.CRYPTO, 'degen'), ('BONK/USD', 'Bonk', AssetClass.CRYPTO, 'degen'),
    ('TRUMP/USD', 'Official Trump', AssetClass.CRYPTO, 'degen'), ('HYPE/USD', 'Hyperliquid', AssetClass.CRYPTO, 'degen'),
]
CRYPTO_INCREMENTS = {'BTC/USD': Decimal('0.0001'), 'ETH/USD': Decimal('0.001')}
ALT_INCREMENT = Decimal('0.00000001')


class Command(BaseCommand):
    help = 'Seed watchlist instruments, config, accounts and strategy rows'

    def handle(self, *args, **options):
        cfg = AgentConfig.get()
        for symbol, name, ac, market in DEFAULTS:
            inst, created = Instrument.objects.get_or_create(symbol=symbol, defaults={
                'name': name, 'asset_class': ac, 'market': market,
                'qty_increment': CRYPTO_INCREMENTS.get(symbol, ALT_INCREMENT if ac == AssetClass.CRYPTO else Decimal('1')),
                'tick_size': Decimal('0.01') if ac != AssetClass.CRYPTO else Decimal('0.00000001'),
            })
            if created:
                self.stdout.write(f'  + {symbol} ({market})')
            elif inst.market != market:
                inst.market = market
                inst.save(update_fields=['market'])
        if settings.ALPACA_ENABLED:
            self.refresh_increments()
        for mode in (Mode.SIM, Mode.PAPER, Mode.REPLAY):
            for market in (Market.STOCKS, Market.CRYPTO, Market.DEGEN):
                Account.for_mode(mode, market)
        by_market = {m: [s for s, _, _, mk in DEFAULTS if mk == m] for m in (Market.STOCKS, Market.CRYPTO, Market.DEGEN)}
        wanted = {'orb': [Market.STOCKS], 'vwap_reversion': [Market.STOCKS, Market.CRYPTO],
                  'ema_momentum': [Market.STOCKS, Market.CRYPTO, Market.DEGEN], 'burst': [Market.DEGEN]}
        for cls in all_strategies():
            for market in wanted.get(cls.key, [Market.STOCKS]):
                row, created = Strategy.objects.get_or_create(key=cls.key, market=market, defaults={
                    'name': cls.name, 'params': cls.defaults(), 'timeframe': cfg.timeframe_for(market),
                    'symbols': by_market[market], 'notes': cls.description,
                    # The degen lane exists to be watched: its burst strategy starts enabled at Sprout.
                    'enabled': cls.key == 'burst', 'stage': 'sprout' if cls.key == 'burst' else 'seed',
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
