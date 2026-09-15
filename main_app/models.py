"""MoneyTree data model.

Ledger rows (Account, Position, Order, Fill, Trade) use Decimal: they are the
books. Bars are analytics, not books, so they are floats — a million Decimal
rows load an order of magnitude slower and buy nothing.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

D0 = Decimal('0')


class AssetClass(models.TextChoices):
    STOCK = 'stock', 'Stock'
    ETF = 'etf', 'ETF'
    CRYPTO = 'crypto', 'Crypto'
    FOREX = 'forex', 'Forex'


class Mode(models.TextChoices):
    SIM = 'sim', 'Sim'          # internal fills, fake currency
    PAPER = 'paper', 'Paper'    # Alpaca paper account
    LIVE = 'live', 'Live'       # real money (v2)
    REPLAY = 'replay', 'Replay'  # a past session replayed into its own account


class Market(models.TextChoices):
    STOCKS = 'stocks', 'Stocks'   # NYSE/Nasdaq session, 09:30–16:00 ET
    CRYPTO = 'crypto', 'Crypto'   # BTC/ETH, hourly, 24/7
    DEGEN = 'degen', 'Degen'      # altcoins, 1-minute bars, aggressive sizing — the high-risk sandbox
    FOREX = 'forex', 'Forex'      # USD-quoted majors, 5-minute bars, 24/5 (Sun 17:00 – Fri 17:00 ET), margin sizing


class Stage(models.TextChoices):
    SEED = 'seed', 'Seed'          # backtest only
    SPROUT = 'sprout', 'Sprout'    # trades fake currency on the simulator
    SAPLING = 'sapling', 'Sapling'  # trades on the Alpaca paper account
    TREE = 'tree', 'Tree'          # real money


class Qualification(models.TextChoices):
    UNPROVEN = 'unproven', 'Unproven'
    QUALIFIED = 'qualified', 'Qualified'
    QUARANTINED = 'quarantine', 'Quarantined'


class Instrument(models.Model):
    symbol = models.CharField(max_length=16, unique=True)  # AAPL, BTC/USD
    name = models.CharField(max_length=80, blank=True)
    asset_class = models.CharField(max_length=8, choices=AssetClass.choices, default=AssetClass.STOCK)
    # Which agent trades it: stocks, crypto (BTC/ETH), degen (altcoins) or forex (majors).
    market = models.CharField(max_length=8, choices=Market.choices, default=Market.STOCKS)
    tick_size = models.DecimalField(max_digits=12, decimal_places=8, default=Decimal('0.01'))
    # Smallest order quantity step. 1 for stocks (whole shares); crypto is
    # fractional and Alpaca publishes the per-asset increment.
    qty_increment = models.DecimalField(max_digits=16, decimal_places=8, default=Decimal('1'))
    in_watchlist = models.BooleanField(default=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['symbol']

    def __str__(self):
        return self.symbol

    @property
    def is_crypto(self):
        return self.asset_class == AssetClass.CRYPTO

    @property
    def is_forex(self):
        return self.asset_class == AssetClass.FOREX

    @property
    def slug(self):
        return self.symbol.replace('/', '-')

    @staticmethod
    def symbol_from_slug(slug):
        return slug.replace('-', '/')


class Bar(models.Model):
    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE, related_name='bars')
    timeframe = models.CharField(max_length=8)  # 1Min, 5Min, 15Min, 1Day
    ts = models.DateTimeField()  # bar START, UTC
    open = models.FloatField()
    high = models.FloatField()
    low = models.FloatField()
    close = models.FloatField()
    volume = models.FloatField(default=0)
    vwap = models.FloatField(null=True, blank=True)
    trade_count = models.IntegerField(null=True, blank=True)
    # alpaca:sip:split, alpaca:iex, yahoo, synthetic — the feed matters for
    # parity: IEX prints only ~2% of volume, SIP is the consolidated tape.
    source = models.CharField(max_length=32, default='')

    class Meta:
        # One history per feed: the live loop reads IEX bars (what it trades on),
        # backtests read the consolidated tape. Same timestamps, different rows.
        constraints = [
            models.UniqueConstraint(fields=['instrument', 'timeframe', 'source', 'ts'], name='uniq_bar'),
        ]
        indexes = [models.Index(fields=['instrument', 'timeframe', 'source', 'ts'])]
        ordering = ['ts']

    def __str__(self):
        return f'{self.instrument.symbol} {self.timeframe} {self.ts:%Y-%m-%d %H:%M}'


class MarketSession(models.Model):
    """One NYSE trading day. Cached from Alpaca's calendar; the hard-coded list
    in services/data/calendar.py is the fallback."""
    date = models.DateField(unique=True)
    open_utc = models.DateTimeField()
    close_utc = models.DateTimeField()
    early_close = models.BooleanField(default=False)

    class Meta:
        ordering = ['date']

    def __str__(self):
        return f'{self.date} {"(early close)" if self.early_close else ""}'.strip()


class AgentConfig(models.Model):
    """Singleton. Risk limits and fill model, editable at /settings/."""
    mode = models.CharField(max_length=8, choices=Mode.choices, default=Mode.SIM)
    trading_enabled = models.BooleanField(default=True)
    kill_switch = models.BooleanField(default=False)
    timeframe = models.CharField(max_length=8, default='5Min')
    # Crypto pays ~50 bps a round trip. Measured 2026-09-09 over 200 days: the
    # typical 2-ATR target is 2.1x the round trip on 1Hour bars but 4.3-5.5x on
    # 4Hour, so hourly bars left almost nothing after costs and the cost gate
    # blocked 8 of 8 signals. Four-hour bars give a trade room to pay for itself.
    crypto_timeframe = models.CharField(max_length=8, default='4Hour')
    # The degen lane. It ran on 1-minute bars and lost $2,210 of which $1,698 was
    # fees, because at 1Min the typical 2-ATR target is 0.3-0.7x the 0.56% round
    # trip: the fee is larger than the move the trade is trying to capture, so no
    # parameter set can win. At 15Min the same measurement is 1.4-2.3x.
    degen_timeframe = models.CharField(max_length=8, default='15Min')
    # These were once deliberately loose — a sandbox to watch. That produced the
    # loosest settings in the app, on the most expensive venue it trades, applied
    # to the lane that went on to lose a third of its capital at a 14% win rate.
    # Now at parity with every other lane; the looseness was never the edge.
    degen_risk_per_trade_pct = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal('0.5'))
    degen_max_position_pct = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('20'))
    degen_max_open_positions = models.PositiveIntegerField(default=4)
    degen_max_daily_loss_pct = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('2'))
    degen_max_trades_per_day = models.PositiveIntegerField(default=12)
    degen_max_hold_minutes = models.PositiveIntegerField(default=180)
    # Altcoins move together: 121 of the first 165 degen trades were stacked 3+
    # deep in the SAME direction and carried $1,625 of the lane's $2,210 loss.
    degen_max_directional_exposure_pct = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal('50'))
    degen_min_reward_to_cost = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('3'))
    # The forex lane: USD-quoted majors on 5-minute bars, 24/5. Forex is traded
    # on margin, so a position may exceed the account (leverage) and the cost
    # model is a spread, not a commission: fees in bps of notional, slippage in
    # bps (0.3 bps ≈ a third of a pip on EUR/USD).
    # 15-minute bars: on 5-minute bars the spread eats every target (backtest
    # 2026-09-06: PF 0.42 on 5Min vs 0.83 on 15Min, same strategy, same 59 days).
    forex_timeframe = models.CharField(max_length=8, default='15Min')
    forex_leverage = models.DecimalField(max_digits=5, decimal_places=1, default=Decimal('10'))
    forex_risk_per_trade_pct = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal('0.5'))
    forex_max_position_pct = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal('500'))
    # All supported FX pairs are USD-quoted. Longs share one USD factor and
    # shorts share the opposite one, so cap each side separately even when
    # gross buying power remains.
    forex_max_directional_exposure_pct = models.DecimalField(
        max_digits=7, decimal_places=2, default=Decimal('500')
    )
    forex_max_open_positions = models.PositiveIntegerField(default=4)
    forex_max_daily_loss_pct = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('2'))
    forex_max_trades_per_day = models.PositiveIntegerField(default=40)
    forex_max_hold_minutes = models.PositiveIntegerField(default=240)
    forex_min_reward_to_cost = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('2'))
    forex_slippage_bps = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.3'))
    fee_bps_forex = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.5'))
    # Read the news hourly, and stand aside during a confirmed repricing.
    news_enabled = models.BooleanField(default=True)
    # Live pulse: seconds between price checks while waiting for the next bar (0 = off).
    pulse_seconds = models.PositiveIntegerField(default=10)
    starting_cash = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('10000'))

    # Risk — percentages are of account equity.
    risk_per_trade_pct = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal('0.5'))
    max_position_pct = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('20'))
    max_open_positions = models.PositiveIntegerField(default=4)
    max_daily_loss_pct = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('2'))
    max_trades_per_day = models.PositiveIntegerField(default=12)
    # Offsets from the session close, so early-close days and DST just work.
    no_entries_before_close_min = models.PositiveIntegerField(default=30)
    flat_before_close_min = models.PositiveIntegerField(default=5)
    allow_short = models.BooleanField(default=False)
    # Crypto has no session close; positions age out instead.
    max_hold_minutes = models.PositiveIntegerField(default=240)

    # Fill model for the simulator.
    slippage_bps = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('3'))
    fee_bps_stock = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.5'))
    fee_bps_crypto = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('25'))
    # A fill may not exceed this share of the bar's volume; the rest is left unfilled.
    liquidity_cap_pct = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('1'))
    # An entry's target must be at least this many times the round-trip cost
    # (fees + slippage, both sides); otherwise the trade cannot pay for itself.
    min_reward_to_cost = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('3'))

    # Live-mode ritual (v2).
    live_armed_at = models.DateTimeField(null=True, blank=True)
    live_confirm_orders = models.BooleanField(default=True)
    live_confirm_minutes = models.PositiveIntegerField(default=3)
    live_sessions_completed = models.PositiveIntegerField(default=0)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'agent config'

    def __str__(self):
        return f'AgentConfig({self.mode})'

    @classmethod
    def get(cls) -> 'AgentConfig':
        obj = cls.objects.order_by('pk').first()
        if obj is None:
            obj = cls.objects.create()
        return obj

    def fee_bps_for(self, instrument: Instrument) -> Decimal:
        if instrument.is_crypto:
            return self.fee_bps_crypto
        return self.fee_bps_forex if instrument.is_forex else self.fee_bps_stock

    def fee_bps(self) -> dict:
        return {'stock': float(self.fee_bps_stock), 'etf': float(self.fee_bps_stock),
                'crypto': float(self.fee_bps_crypto), 'forex': float(self.fee_bps_forex)}

    def timeframe_for(self, market: str) -> str:
        return {Market.CRYPTO: self.crypto_timeframe, Market.DEGEN: self.degen_timeframe,
                Market.FOREX: self.forex_timeframe}.get(market, self.timeframe)


class Account(models.Model):
    """One ledger per (mode, market): the stocks agent and the crypto agent are
    separate piggy banks with separate processes."""
    mode = models.CharField(max_length=8, choices=Mode.choices)
    market = models.CharField(max_length=8, choices=Market.choices, default=Market.STOCKS)
    name = models.CharField(max_length=40)
    starting_cash = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('10000'))
    cash = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('10000'))
    # For broker-backed accounts these mirror the broker; for sim they equal
    # the computed values at the last mark-to-market.
    equity = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('10000'))
    buying_power = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('10000'))
    day_start_equity = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('10000'))
    day_start_date = models.DateField(null=True, blank=True)
    # Risk state that must survive a restart.
    day_entries = models.PositiveIntegerField(default=0)
    day_halted = models.BooleanField(default=False)
    day_halted_reason = models.CharField(max_length=120, blank=True)
    # Broker reconciliation (paper/live): when we last compared our books with
    # the venue, and whether they agreed. The simulator is its own venue.
    last_reconcile_at = models.DateTimeField(null=True, blank=True)
    reconcile_ok = models.BooleanField(default=True)
    reconcile_note = models.CharField(max_length=300, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reset_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['mode', 'market'], name='uniq_account_mode_market')]
        ordering = ['market', 'mode']

    def __str__(self):
        return f'{self.name}'

    @classmethod
    def for_mode(cls, mode: str, market: str = Market.STOCKS) -> 'Account':
        cfg_cash = AgentConfig.get().starting_cash
        names = {Mode.SIM: 'Sprout (sim)', Mode.PAPER: 'Sapling (paper)',
                 Mode.LIVE: 'Tree (live)', Mode.REPLAY: 'Replay'}
        obj, _ = cls.objects.get_or_create(
            mode=mode, market=market,
            defaults={'name': f'{names.get(mode, mode)} · {market}', 'starting_cash': cfg_cash, 'cash': cfg_cash,
                      'equity': cfg_cash, 'buying_power': cfg_cash, 'day_start_equity': cfg_cash},
        )
        return obj

    @property
    def label(self) -> str:
        return f'{self.get_market_display()} · {self.mode}'

    @property
    def asset_classes(self) -> tuple:
        if self.market in (Market.CRYPTO, Market.DEGEN):
            return (AssetClass.CRYPTO,)
        if self.market == Market.FOREX:
            return (AssetClass.FOREX,)
        return (AssetClass.STOCK, AssetClass.ETF)

    @property
    def lane_asset_class(self) -> str:
        """The asset class that sets this lane's hours: stock, crypto or forex."""
        return self.asset_classes[0]

    @property
    def is_24x7(self) -> bool:
        return self.market in (Market.CRYPTO, Market.DEGEN)

    @property
    def hours_label(self) -> str:
        return {Market.STOCKS: 'US stock session, 09:30–16:00 ET Mon–Fri', Market.CRYPTO: 'around the clock',
                Market.DEGEN: 'around the clock', Market.FOREX: 'Sun 17:00 – Fri 17:00 ET'}[self.market]

    def is_open_at(self, ts) -> bool:
        from main_app.services.data import calendar as cal
        return cal.is_open(ts, self.lane_asset_class)

    @property
    def query(self) -> str:
        """Query string that selects this account on any page."""
        return f'account={self.mode}&market={self.market}'

    @property
    def log_name(self) -> str:
        return f'agent-{self.mode}-{self.market}'


    @property
    def day_pnl(self) -> Decimal:
        return self.equity - self.day_start_equity

    @property
    def total_pnl(self) -> Decimal:
        return self.equity - self.starting_cash

    def reset(self, cash: Decimal | None = None):
        """Wipe the books (positions, orders, trades, snapshots) and restart the bankroll."""
        cash = cash if cash is not None else AgentConfig.get().starting_cash
        self.positions.all().delete()
        self.orders.all().delete()
        self.trades.all().delete()
        self.snapshots.all().delete()
        self.signals.all().delete()
        self.risk_events.all().delete()
        self.starting_cash = cash
        self.cash = cash
        self.equity = cash
        self.buying_power = cash
        self.day_start_equity = cash
        self.day_start_date = None
        self.day_entries = 0
        self.day_halted = False
        self.day_halted_reason = ''
        self.reset_at = timezone.now()
        self.save()
        self.cards.all().delete()
        self.symbol_states.all().delete()
        self.feed.all().delete()


def market_for_symbols(symbols) -> str:
    """The market whose instruments these are (falls back on the symbol shape)."""
    symbols = list(symbols or [])
    if not symbols:
        return Market.STOCKS
    # (a set, not .distinct(): Meta.ordering would add `symbol` to the DISTINCT)
    found = sorted(set(Instrument.objects.filter(symbol__in=symbols).values_list('market', flat=True)))
    if len(found) == 1:
        return found[0]
    return Market.CRYPTO if all('/' in s for s in symbols) else Market.STOCKS


class Position(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='positions')
    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, related_name='positions')
    strategy_key = models.CharField(max_length=40, blank=True)
    qty = models.DecimalField(max_digits=18, decimal_places=8)  # signed: negative = short
    avg_price = models.DecimalField(max_digits=20, decimal_places=8)
    stop_price = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    target_price = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    opened_at = models.DateTimeField()
    entry_bar_ts = models.DateTimeField(null=True, blank=True)
    bars_held = models.PositiveIntegerField(default=0)
    max_hold_until = models.DateTimeField(null=True, blank=True)
    last_price = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    entry_fees = models.DecimalField(max_digits=12, decimal_places=4, default=D0)
    # Found at the broker but not opened by us: counted in exposure, never
    # touched by strategies.
    external = models.BooleanField(default=False)
    # How the exit is protected: engine (simulator / engine-managed levels),
    # bracket (venue-side legs), stop_order (venue-side stop), none.
    protection = models.CharField(max_length=12, default='engine')
    protection_order_id = models.CharField(max_length=80, blank=True)
    closing = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['account', 'instrument'], name='uniq_position'),
        ]

    @property
    def risk_dollars(self) -> Decimal:
        """Planned loss if the stop is hit (0 when unprotected)."""
        if self.stop_price is None:
            return D0
        return (abs(self.qty) * abs(self.avg_price - self.stop_price)).quantize(Decimal('0.01'))

    def distance_pct(self, level) -> Decimal | None:
        price = self.last_price if self.last_price is not None else self.avg_price
        if level is None or not price:
            return None
        return ((level - price) / price * 100).quantize(Decimal('0.01'))

    def __str__(self):
        return f'{self.instrument.symbol} {self.qty} @ {self.avg_price}'

    @property
    def side(self):
        return 'short' if self.qty < 0 else 'long'

    @property
    def market_value(self) -> Decimal:
        price = self.last_price if self.last_price is not None else self.avg_price
        return (self.qty * price).quantize(Decimal('0.01'))

    @property
    def unrealized_pnl(self) -> Decimal:
        price = self.last_price if self.last_price is not None else self.avg_price
        return ((price - self.avg_price) * self.qty).quantize(Decimal('0.01'))

    @property
    def unrealized_pct(self) -> Decimal:
        if not self.avg_price:
            return D0
        price = self.last_price if self.last_price is not None else self.avg_price
        sign = -1 if self.qty < 0 else 1
        return ((price - self.avg_price) / self.avg_price * 100 * sign).quantize(Decimal('0.01'))


class OrderSide(models.TextChoices):
    BUY = 'buy', 'Buy'
    SELL = 'sell', 'Sell'


class OrderType(models.TextChoices):
    MARKET = 'market', 'Market'
    LIMIT = 'limit', 'Limit'
    STOP = 'stop', 'Stop'


class OrderStatus(models.TextChoices):
    NEW = 'new', 'New'
    ACCEPTED = 'accepted', 'Accepted'
    PARTIAL = 'partially_filled', 'Partially filled'
    FILLED = 'filled', 'Filled'
    CANCELED = 'canceled', 'Canceled'
    REJECTED = 'rejected', 'Rejected'
    EXPIRED = 'expired', 'Expired'


class Order(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='orders')
    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, related_name='orders')
    strategy_key = models.CharField(max_length=40, blank=True)
    side = models.CharField(max_length=4, choices=OrderSide.choices)
    qty = models.DecimalField(max_digits=18, decimal_places=8)
    order_type = models.CharField(max_length=8, choices=OrderType.choices, default=OrderType.MARKET)
    limit_price = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    stop_price = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    time_in_force = models.CharField(max_length=4, default='day')
    status = models.CharField(max_length=20, choices=OrderStatus.choices, default=OrderStatus.NEW)
    # Deterministic: mt-{mode}-{strategy}-{symbol}-{bar_ts}-{leg}. A crash
    # between submit and commit cannot double-trade — the broker rejects the
    # duplicate and reconciliation adopts the original.
    client_order_id = models.CharField(max_length=120, unique=True)
    broker_order_id = models.CharField(max_length=80, blank=True)
    leg = models.CharField(max_length=8, default='entry')  # entry, stop, target, exit
    decision_price = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    bar_ts = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=200, blank=True)
    submitted_at = models.DateTimeField(default=timezone.now)
    filled_at = models.DateTimeField(null=True, blank=True)
    filled_qty = models.DecimalField(max_digits=18, decimal_places=8, default=D0)
    filled_avg_price = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    fees = models.DecimalField(max_digits=12, decimal_places=4, default=D0)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ['-submitted_at']
        indexes = [models.Index(fields=['account', 'status'])]

    def __str__(self):
        return f'{self.side} {self.qty} {self.instrument.symbol} ({self.status})'

    @property
    def is_open(self):
        return self.status in (OrderStatus.NEW, OrderStatus.ACCEPTED, OrderStatus.PARTIAL)


class Fill(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='fills')
    ts = models.DateTimeField()
    qty = models.DecimalField(max_digits=18, decimal_places=8)
    price = models.DecimalField(max_digits=20, decimal_places=8)
    fee = models.DecimalField(max_digits=12, decimal_places=4, default=D0)
    # (fill − decision) / decision, signed against us. Feeds slippage calibration.
    realized_slippage_bps = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ['ts']


class Trade(models.Model):
    """A closed round trip — the unit of performance analytics."""
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='trades')
    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, related_name='trades')
    strategy_key = models.CharField(max_length=40, blank=True)
    side = models.CharField(max_length=5, default='long')  # long / short
    qty = models.DecimalField(max_digits=18, decimal_places=8)
    entry_ts = models.DateTimeField()
    exit_ts = models.DateTimeField()
    entry_price = models.DecimalField(max_digits=20, decimal_places=8)
    exit_price = models.DecimalField(max_digits=20, decimal_places=8)
    pnl = models.DecimalField(max_digits=14, decimal_places=2)
    pnl_pct = models.DecimalField(max_digits=8, decimal_places=3)
    fees = models.DecimalField(max_digits=12, decimal_places=4, default=D0)
    bars_held = models.PositiveIntegerField(default=0)
    exit_reason = models.CharField(max_length=16, default='signal')  # stop target time signal eod kill catchup manual
    notes = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['-exit_ts']
        indexes = [models.Index(fields=['account', 'exit_ts'])]

    def __str__(self):
        return f'{self.instrument.symbol} {self.side} {self.pnl:+}'


class EquitySnapshot(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='snapshots')
    ts = models.DateTimeField()
    cash = models.DecimalField(max_digits=14, decimal_places=2)
    positions_value = models.DecimalField(max_digits=14, decimal_places=2)
    equity = models.DecimalField(max_digits=14, decimal_places=2)
    day_pnl = models.DecimalField(max_digits=14, decimal_places=2, default=D0)

    class Meta:
        ordering = ['ts']
        indexes = [models.Index(fields=['account', 'ts'])]


class Signal(models.Model):
    """Every strategy decision, acted on or not. Blocked ones carry the reason —
    the risk manager's 'no' is as informative as its 'yes'."""
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='signals')
    strategy_key = models.CharField(max_length=40)
    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, related_name='signals')
    ts = models.DateTimeField()  # bar ts the decision was made on
    action = models.CharField(max_length=6)  # buy sell close
    strength = models.FloatField(default=1.0)
    price = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    stop_price = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    target_price = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    reason = models.CharField(max_length=200, blank=True)
    acted = models.BooleanField(default=False)
    blocked_reason = models.CharField(max_length=200, blank=True)
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, related_name='signals')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-ts', '-pk']
        indexes = [models.Index(fields=['account', 'ts'])]


class RiskEvent(models.Model):
    ALERT_KINDS = ('daily_loss', 'kill_switch', 'missed_ticks', 'external_position', 'error', 'drift', 'qualification', 'reconcile',
                   'disconnected', 'config_changed', 'watchdog', 'unprotected')
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='risk_events')
    ts = models.DateTimeField(default=timezone.now)
    kind = models.CharField(max_length=32)
    message = models.CharField(max_length=300)
    data = models.JSONField(default=dict, blank=True)
    # Alerts stay on the dashboard until someone acknowledges them.
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-ts']

    @property
    def is_alert(self):
        return self.kind in self.ALERT_KINDS and self.acknowledged_at is None


class AgentRun(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='runs')
    mode = models.CharField(max_length=8, choices=Mode.choices)
    market = models.CharField(max_length=8, choices=Market.choices, default=Market.STOCKS)
    started_at = models.DateTimeField(default=timezone.now)
    last_tick_at = models.DateTimeField(null=True, blank=True)
    stopped_at = models.DateTimeField(null=True, blank=True)
    pid = models.IntegerField(default=0)
    status = models.CharField(max_length=10, default='running')  # running stopped error
    message = models.CharField(max_length=300, blank=True)
    ticks = models.PositiveIntegerField(default=0)
    replay_date = models.DateField(null=True, blank=True)
    speed = models.FloatField(default=1.0)
    # Health inputs: how often a heartbeat is expected, what the loop is doing,
    # and when it plans to act next. The dashboard derives healthy/waiting/
    # stale/disconnected from these instead of a fixed number.
    expected_interval_s = models.PositiveIntegerField(default=60)
    state = models.CharField(max_length=12, default='starting')  # starting waiting ticking sleeping stopping
    next_action_at = models.DateTimeField(null=True, blank=True)
    data_source = models.CharField(max_length=24, blank=True)
    last_bar_ts = models.DateTimeField(null=True, blank=True)
    # Control channel: the web sets these, the loop polls them every 2 s.
    stop_requested = models.BooleanField(default=False)

    class Meta:
        ordering = ['-started_at']

    @property
    def heartbeat_age_s(self):
        ref = self.last_tick_at or self.started_at
        return (timezone.now() - ref).total_seconds()

    @property
    def health(self) -> str:
        """healthy · waiting · stale · disconnected — relative to the expected cadence."""
        if self.status != 'running' or not self.is_alive:
            return 'disconnected'
        age = self.heartbeat_age_s
        expected = max(30, self.expected_interval_s)
        if age > 3 * expected:
            return 'disconnected'
        if age > 1.5 * expected:
            return 'stale'
        return 'waiting' if self.state in ('waiting', 'sleeping') else 'healthy'

    @property
    def is_alive(self):
        if self.status != 'running':
            return False
        if self.pid:
            import os
            try:
                os.kill(self.pid, 0)
            except OSError:
                return False
        return True


class Strategy(models.Model):
    """Persisted configuration for a strategy class in the registry — one row
    per (strategy, market), so the crypto agent tunes its own copy."""
    key = models.CharField(max_length=40)
    market = models.CharField(max_length=8, choices=Market.choices, default=Market.STOCKS)
    name = models.CharField(max_length=80)
    params = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=False)
    symbols = models.JSONField(default=list, blank=True)
    timeframe = models.CharField(max_length=8, default='5Min')
    allocation_pct = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('100'))
    stage = models.CharField(max_length=8, choices=Stage.choices, default=Stage.SEED)
    # Enabled controls whether a strategy may be observed at its stage.
    # Qualification is independent evidence: paper/live agents require it,
    # while sim/replay are allowed to collect evidence on unproven ideas.
    qualification = models.CharField(max_length=12, choices=Qualification.choices,
                                     default=Qualification.UNPROVEN)
    qualification_reason = models.CharField(max_length=300, blank=True)
    qualification_updated_at = models.DateTimeField(null=True, blank=True)
    # Live evidence only counts from here. A promotion installs new parameters and
    # a lane change installs a new timeframe; trades made under the OLD
    # configuration cannot judge the new one, and without this boundary a
    # quarantine earned by a since-replaced configuration was permanent.
    evidence_since = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    history = models.JSONField(default=list, blank=True)  # promotions: {at, version, params, source}
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['market', 'name']
        verbose_name_plural = 'strategies'
        constraints = [models.UniqueConstraint(fields=['key', 'market'], name='uniq_strategy_key_market')]

    def __str__(self):
        return f'{self.name} ({self.market}) v{self.version}'

    @property
    def execution_qualified(self) -> bool:
        return self.qualification == Qualification.QUALIFIED


class Experiment(models.Model):
    strategy_key = models.CharField(max_length=40)
    method = models.CharField(max_length=16, default='grid')  # grid random walk_forward
    param_grid = models.JSONField(default=dict, blank=True)  # {param: [values]}
    symbols = models.JSONField(default=list, blank=True)
    timeframe = models.CharField(max_length=8, default='5Min')
    start = models.DateField()
    end = models.DateField()
    objective = models.CharField(max_length=20, default='sharpe')
    windows = models.JSONField(default=dict, blank=True)  # {train_days, test_days, step_days}
    min_trades = models.PositiveIntegerField(default=10)
    status = models.CharField(max_length=10, default='queued')  # queued running done failed
    progress = models.FloatField(default=0)
    total_runs = models.PositiveIntegerField(default=0)
    done_runs = models.PositiveIntegerField(default=0)
    best_params = models.JSONField(default=dict, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    pid = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.method} {self.strategy_key} #{self.pk}'


class BacktestRun(models.Model):
    strategy_key = models.CharField(max_length=40)
    params = models.JSONField(default=dict, blank=True)
    symbols = models.JSONField(default=list, blank=True)
    timeframe = models.CharField(max_length=8, default='5Min')
    start = models.DateField()
    end = models.DateField()
    starting_cash = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('10000'))
    status = models.CharField(max_length=10, default='queued')  # queued running done failed
    metrics = models.JSONField(default=dict, blank=True)
    equity_curve = models.JSONField(default=list, blank=True)  # [[epoch_s, equity], ...] downsampled
    config_snapshot = models.JSONField(default=dict, blank=True)  # slippage, fees, risk
    experiment = models.ForeignKey(Experiment, on_delete=models.CASCADE, null=True, blank=True, related_name='runs')
    window_label = models.CharField(max_length=40, blank=True)  # walk-forward: "W1 train" / "W1 test"
    tag = models.CharField(max_length=60, blank=True)
    notes = models.TextField(blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_s = models.FloatField(default=0)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.strategy_key} {self.start}→{self.end} #{self.pk}'

    @property
    def trades_count(self):
        return self.metrics.get('trades', 0)


class BacktestTrade(models.Model):
    run = models.ForeignKey(BacktestRun, on_delete=models.CASCADE, related_name='trades')
    symbol = models.CharField(max_length=16)
    side = models.CharField(max_length=5, default='long')
    qty = models.FloatField()
    entry_ts = models.DateTimeField()
    exit_ts = models.DateTimeField()
    entry_price = models.FloatField()
    exit_price = models.FloatField()
    pnl = models.FloatField()
    pnl_pct = models.FloatField()
    fees = models.FloatField(default=0)
    bars_held = models.PositiveIntegerField(default=0)
    exit_reason = models.CharField(max_length=16, default='signal')

    class Meta:
        ordering = ['entry_ts']


class JournalEntry(models.Model):
    date = models.DateField()
    kind = models.CharField(max_length=10, default='manual')  # auto_eod coach manual
    account = models.ForeignKey(Account, on_delete=models.SET_NULL, null=True, blank=True, related_name='journal')
    title = models.CharField(max_length=120)
    body = models.TextField(blank=True)
    metrics = models.JSONField(default=dict, blank=True)
    # Coach proposals: [{title, strategy_key, param_grid, method, rationale}]
    proposals = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-created_at']
        verbose_name_plural = 'journal entries'

    def __str__(self):
        return f'{self.date} {self.kind}: {self.title}'


class ApiUsage(models.Model):
    """One paid API call. The spend ledger.

    Not only this app: `project` lets other apps on this machine post their
    usage here, so a single table answers "where is the API money going".
    Cost is computed at write time from services/spend.PRICES, because a price
    looked up later would be the wrong price.
    """
    ts = models.DateTimeField(default=timezone.now)
    provider = models.CharField(max_length=20, default='anthropic')  # anthropic, openai, serper, …
    project = models.CharField(max_length=30, default='moneytree')
    model = models.CharField(max_length=60)
    purpose = models.CharField(max_length=40, default='coach')
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    # Cached input is billed at a fraction of the input rate; kept separately so
    # a caching change shows up as a cost change instead of hiding in the total.
    cached_tokens = models.PositiveIntegerField(default=0)
    calls = models.PositiveIntegerField(default=1)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=5, default=D0)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['-ts']
        indexes = [models.Index(fields=['ts', 'project']), models.Index(fields=['provider', 'model'])]

    def __str__(self):
        return f'{self.project}/{self.provider} {self.model} ${self.cost_usd}'


class NewsItem(models.Model):
    """One headline, and what a cheap model made of it.

    The bots read the news hourly. This table is the memory of that reading:
    the raw headline, the structured event extracted from it, and — filled in
    later — what the price actually did next. The last part is the point. An
    event type only earns the right to drive a trade once its logged outcomes
    say it has edge, so every row is evidence, not a signal.
    """
    KINDS = ('listing', 'earnings', 'guidance', 'mna', 'regulatory', 'macro', 'product',
             'partnership', 'legal', 'security', 'personnel', 'analyst', 'rumour', 'other')
    DIRECTIONS = ('bullish', 'bearish', 'neutral')

    # --- the headline as published ---
    external_id = models.CharField(max_length=64, unique=True)   # provider id: dedupes re-polls
    source = models.CharField(max_length=40, default='alpaca')
    published_at = models.DateTimeField()
    headline = models.CharField(max_length=500)
    summary = models.TextField(blank=True)
    url = models.URLField(max_length=500, blank=True)
    symbols = models.JSONField(default=list, blank=True)
    market = models.CharField(max_length=8, choices=Market.choices, blank=True)

    # --- what the classifier made of it ---
    classified_at = models.DateTimeField(null=True, blank=True)
    kind = models.CharField(max_length=16, blank=True)
    direction = models.CharField(max_length=8, blank=True)
    magnitude = models.PositiveSmallIntegerField(default=0)   # 1 routine … 5 market-moving
    confidence = models.PositiveSmallIntegerField(default=0)  # 1 rumour … 5 confirmed by the subject
    horizon = models.CharField(max_length=12, blank=True)     # minutes, hours, days, weeks
    novel = models.BooleanField(default=True)                 # false = same story, different outlet
    duplicate_of = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='duplicates')
    rationale = models.CharField(max_length=400, blank=True)
    tradable = models.BooleanField(default=False)             # is any tagged symbol one we can trade
    model = models.CharField(max_length=60, blank=True)

    # --- what happened next, so the claim can be scored ---
    price_at_news = models.JSONField(default=dict, blank=True)   # symbol -> price when read
    outcome = models.JSONField(default=dict, blank=True)         # symbol -> {'60m': pct, '1d': pct}
    outcome_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-published_at']
        indexes = [
            models.Index(fields=['-published_at']),
            models.Index(fields=['market', '-published_at']),
            models.Index(fields=['kind', 'direction']),
        ]

    def __str__(self):
        return f'{self.published_at:%m-%d %H:%M} {self.headline[:60]}'

    @property
    def is_significant(self) -> bool:
        """Worth a human's attention: a confirmed, sizeable, directional event."""
        return (self.magnitude >= 4 and self.confidence >= 4
                and self.direction in ('bullish', 'bearish') and self.novel)


class Briefing(models.Model):
    """One lane's answer to 'what is going on out there right now'.

    A single web-searching prompt per lane, on a schedule. This is the wide
    view — macro, regulation, the story everyone is talking about — which a
    symbol-tagged headline feed structurally cannot give you, because the
    stories that move a whole lane are often tagged to no ticker at all.
    """
    market = models.CharField(max_length=8, choices=Market.choices)
    ts = models.DateTimeField(default=timezone.now)
    headline = models.CharField(max_length=300, blank=True)   # the one-line read
    body = models.TextField(blank=True)                       # the bullets, as written
    items = models.JSONField(default=list, blank=True)        # [{text, url}]
    quiet = models.BooleanField(default=False)                # nothing significant happened
    model = models.CharField(max_length=60, blank=True)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=5, default=D0)
    error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['-ts']
        indexes = [models.Index(fields=['market', '-ts'])]

    def __str__(self):
        return f'{self.market} {self.ts:%m-%d %H:%M} {self.headline[:60]}'


class NewsSession(models.Model):
    """One sitting of the News Agent: every four hours it reads everything new
    and scores each story for whether it can be traded today.

    A session is the unit a human opens and reads. It holds the agent's overall
    read of the moment plus one verdict per story, including the ones it decided
    to do nothing about — a rejection is as much of a thought as a trade.
    """
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    model = models.CharField(max_length=60, blank=True)
    narrative = models.TextField(blank=True)      # the agent's read of the whole moment
    considered = models.PositiveIntegerField(default=0)
    actionable = models.PositiveIntegerField(default=0)   # scored above the threshold
    traded = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=5, default=D0)
    duration_s = models.FloatField(default=0)
    error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f'session {self.started_at:%m-%d %H:%M} — {self.considered} stories, {self.traded} trades'

    @property
    def label(self) -> str:
        return f'{self.started_at.astimezone(timezone.get_current_timezone()):%a %b %-d, %-I:%M %p}'


class NewsVerdict(models.Model):
    """What the agent thought about one story, and what it did about it.

    `score` is the agent's own answer to "out of 10, what is the chance acting
    on this is profitable today". Everything at or above ACT_THRESHOLD is handed
    to the trading lane; everything below is kept anyway, because the record of
    what it declined is what makes the record of what it took meaningful.
    """
    ACT_THRESHOLD = 5          # "if it's over 4"
    DIRECTIONS = (('buy', 'Buy'), ('short', 'Short'), ('none', 'No action'))

    session = models.ForeignKey(NewsSession, on_delete=models.CASCADE, related_name='verdicts')
    news = models.ForeignKey('NewsItem', on_delete=models.SET_NULL, null=True, blank=True,
                             related_name='verdicts')
    headline = models.CharField(max_length=500)      # copied, so a verdict survives pruning
    url = models.URLField(max_length=500, blank=True)

    symbol = models.CharField(max_length=16, blank=True)
    market = models.CharField(max_length=8, choices=Market.choices, blank=True)
    score = models.PositiveSmallIntegerField(default=0)        # 1-10
    direction = models.CharField(max_length=6, choices=DIRECTIONS, default='none')
    thesis = models.TextField(blank=True)                      # why, in plain English
    horizon = models.CharField(max_length=12, blank=True)
    tradable = models.BooleanField(default=False)              # is the symbol one we can trade

    # What happened to the instruction.
    acted = models.BooleanField(default=False)
    acted_at = models.DateTimeField(null=True, blank=True)
    blocked_reason = models.CharField(max_length=200, blank=True)
    trade = models.ForeignKey('Trade', on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='news_verdicts')

    # Was it right? Filled in later, which is the only way this earns trust.
    price_at_verdict = models.FloatField(null=True, blank=True)
    outcome_pct = models.FloatField(null=True, blank=True)      # signed FOR the call
    outcome_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-score', '-created_at']
        indexes = [models.Index(fields=['session', '-score']), models.Index(fields=['symbol', '-created_at'])]

    def __str__(self):
        return f'{self.score}/10 {self.direction} {self.symbol}'

    @property
    def actionable(self) -> bool:
        return self.score >= self.ACT_THRESHOLD and self.direction in ('buy', 'short')

    @property
    def call_text(self) -> str:
        """The instruction as a human would say it: 'BUY AAPL', 'SHORT NZD/USD'."""
        if not self.actionable:
            return 'NO ACTION'
        return f'{self.direction.upper()} {self.symbol}'

    @property
    def was_right(self) -> bool | None:
        if self.outcome_pct is None:
            return None
        return self.outcome_pct > 0


class FeedEvent(models.Model):
    """The running commentary: what the agent sees, decides and does. One row
    per line; the dashboard streams them. Pruned after two weeks."""
    LEVELS = ('system', 'bar', 'signal', 'order', 'fill', 'trade', 'risk', 'journal', 'error')
    PHASES = ('observe', 'evaluate', 'decide', 'size', 'submit', 'fill', 'manage', 'close', 'system', 'alert')
    account = models.ForeignKey(Account, on_delete=models.CASCADE, null=True, blank=True, related_name='feed')
    ts = models.DateTimeField(default=timezone.now)   # when it happened (wall clock)
    bar_ts = models.DateTimeField(null=True, blank=True)  # the market bar being evaluated, if any
    level = models.CharField(max_length=8, default='system')
    phase = models.CharField(max_length=10, default='system')
    symbol = models.CharField(max_length=16, blank=True)
    strategy_key = models.CharField(max_length=40, blank=True)
    text = models.CharField(max_length=600)
    data = models.JSONField(default=dict, blank=True)
    card = models.ForeignKey('TradeCard', on_delete=models.SET_NULL, null=True, blank=True, related_name='events')

    class Meta:
        ordering = ['-id']
        indexes = [models.Index(fields=['account', 'id'])]

    def __str__(self):
        return f'[{self.level}] {self.text[:60]}'

    @classmethod
    def prune(cls, days: int = 14) -> int:
        n, _ = cls.objects.filter(ts__lt=timezone.now() - timezone.timedelta(days=days)).delete()
        m, _ = cls.objects.filter(level='pulse', ts__lt=timezone.now() - timezone.timedelta(hours=24)).delete()
        return n + m


class SignupInvite(models.Model):
    """Sign-up is invite-only: an email on this list may create an account."""
    email = models.EmailField(unique=True)
    note = models.CharField(max_length=120, blank=True)
    make_operator = models.BooleanField(default=False, help_text='Can start/stop the agent and change settings')
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.email


class TradeCard(models.Model):
    """One trade, from decision to reconciliation. The feed's lines hang off
    it; the dashboard's Active/Pending panels render it directly."""
    STATUSES = ('awaiting_approval', 'approved', 'submitted', 'accepted', 'partially_filled', 'filled', 'protected',
                'closing', 'closed', 'rejected', 'canceled', 'expired')
    OPEN = ('filled', 'protected', 'closing')
    PENDING = ('awaiting_approval', 'approved', 'submitted', 'accepted', 'partially_filled')
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='cards')
    entry_order_id = models.CharField(max_length=120, unique=True)  # correlation id everywhere
    symbol = models.CharField(max_length=16)
    strategy_key = models.CharField(max_length=40, blank=True)
    side = models.CharField(max_length=5, default='long')
    status = models.CharField(max_length=20, default='approved')
    bar_ts = models.DateTimeField(null=True, blank=True)
    decision_price = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    planned_entry = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    stop_price = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    target_price = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    qty = models.DecimalField(max_digits=18, decimal_places=8, default=D0)
    risk_dollars = models.DecimalField(max_digits=12, decimal_places=2, default=D0)
    reward_dollars = models.DecimalField(max_digits=12, decimal_places=2, default=D0)
    expected_costs = models.DecimalField(max_digits=12, decimal_places=2, default=D0)
    reward_risk = models.DecimalField(max_digits=8, decimal_places=2, default=D0)
    reason = models.CharField(max_length=300, blank=True)
    rules = models.JSONField(default=list, blank=True)
    broker_order_id = models.CharField(max_length=80, blank=True)
    protection = models.CharField(max_length=12, default='none')  # none engine bracket stop_order
    protection_order_id = models.CharField(max_length=80, blank=True)
    filled_qty = models.DecimalField(max_digits=18, decimal_places=8, default=D0)
    avg_fill = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    fees = models.DecimalField(max_digits=12, decimal_places=4, default=D0)
    slippage_bps = models.FloatField(null=True, blank=True)
    exit_reason = models.CharField(max_length=16, blank=True)
    exit_price = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    gross_pnl = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    net_pnl = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    error = models.CharField(max_length=300, blank=True)
    approval_expires_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.CharField(max_length=80, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['account', 'status'])]

    def __str__(self):
        return f'{self.symbol} {self.side} {self.status}'

    @property
    def is_open(self):
        return self.status in self.OPEN

    @property
    def is_pending(self):
        return self.status in self.PENDING


class SymbolState(models.Model):
    """The latest evaluation of one symbol: every rule with value, threshold
    and pass/fail, plus the plain-English summary. Feeds "closest opportunities"."""
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='symbol_states')
    symbol = models.CharField(max_length=16)
    ts = models.DateTimeField(default=timezone.now)
    bar_ts = models.DateTimeField(null=True, blank=True)
    price = models.FloatField(default=0)
    source = models.CharField(max_length=24, blank=True)
    decision = models.CharField(max_length=12, default='wait')  # wait entry blocked holding
    summary = models.CharField(max_length=600, blank=True)
    rules = models.JSONField(default=list, blank=True)  # [{strategy, rule, value, threshold, ok, text}]
    proximity = models.FloatField(default=0)  # 0..1 — share of rules passing (best strategy)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['account', 'symbol'], name='uniq_symbol_state')]
        ordering = ['-proximity', 'symbol']
