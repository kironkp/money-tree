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


class Mode(models.TextChoices):
    SIM = 'sim', 'Sim'          # internal fills, fake currency
    PAPER = 'paper', 'Paper'    # Alpaca paper account
    LIVE = 'live', 'Live'       # real money (v2)
    REPLAY = 'replay', 'Replay'  # a past session replayed into its own account


class Stage(models.TextChoices):
    SEED = 'seed', 'Seed'          # backtest only
    SPROUT = 'sprout', 'Sprout'    # trades fake currency on the simulator
    SAPLING = 'sapling', 'Sapling'  # trades on the Alpaca paper account
    TREE = 'tree', 'Tree'          # real money


class Instrument(models.Model):
    symbol = models.CharField(max_length=16, unique=True)  # AAPL, BTC/USD
    name = models.CharField(max_length=80, blank=True)
    asset_class = models.CharField(max_length=8, choices=AssetClass.choices, default=AssetClass.STOCK)
    tick_size = models.DecimalField(max_digits=10, decimal_places=6, default=Decimal('0.01'))
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
        constraints = [
            models.UniqueConstraint(fields=['instrument', 'timeframe', 'ts'], name='uniq_bar'),
        ]
        indexes = [models.Index(fields=['instrument', 'timeframe', 'ts'])]
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

    # Live-mode ritual (v2).
    live_armed_at = models.DateTimeField(null=True, blank=True)
    live_confirm_orders = models.BooleanField(default=True)
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
        return self.fee_bps_crypto if instrument.is_crypto else self.fee_bps_stock


class Account(models.Model):
    mode = models.CharField(max_length=8, choices=Mode.choices, unique=True)
    name = models.CharField(max_length=40)
    starting_cash = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('10000'))
    cash = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('10000'))
    # For broker-backed accounts these mirror the broker; for sim they equal
    # the computed values at the last mark-to-market.
    equity = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('10000'))
    buying_power = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('10000'))
    day_start_equity = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('10000'))
    day_start_date = models.DateField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reset_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f'{self.name} ({self.mode})'

    @classmethod
    def for_mode(cls, mode: str) -> 'Account':
        cfg_cash = AgentConfig.get().starting_cash
        names = {Mode.SIM: 'Sprout (sim)', Mode.PAPER: 'Sapling (paper)',
                 Mode.LIVE: 'Tree (live)', Mode.REPLAY: 'Replay'}
        obj, _ = cls.objects.get_or_create(
            mode=mode,
            defaults={'name': names.get(mode, mode), 'starting_cash': cfg_cash, 'cash': cfg_cash,
                      'equity': cfg_cash, 'buying_power': cfg_cash, 'day_start_equity': cfg_cash},
        )
        return obj

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
        self.reset_at = timezone.now()
        self.save()


class Position(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='positions')
    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, related_name='positions')
    strategy_key = models.CharField(max_length=40, blank=True)
    qty = models.DecimalField(max_digits=18, decimal_places=8)  # signed: negative = short
    avg_price = models.DecimalField(max_digits=14, decimal_places=4)
    stop_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    target_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    opened_at = models.DateTimeField()
    entry_bar_ts = models.DateTimeField(null=True, blank=True)
    bars_held = models.PositiveIntegerField(default=0)
    max_hold_until = models.DateTimeField(null=True, blank=True)
    last_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    entry_fees = models.DecimalField(max_digits=12, decimal_places=4, default=D0)
    # Found at the broker but not opened by us: counted in exposure, never
    # touched by strategies.
    external = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['account', 'instrument'], name='uniq_position'),
        ]

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
    limit_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    stop_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    time_in_force = models.CharField(max_length=4, default='day')
    status = models.CharField(max_length=20, choices=OrderStatus.choices, default=OrderStatus.NEW)
    # Deterministic: mt-{mode}-{strategy}-{symbol}-{bar_ts}-{leg}. A crash
    # between submit and commit cannot double-trade — the broker rejects the
    # duplicate and reconciliation adopts the original.
    client_order_id = models.CharField(max_length=120, unique=True)
    broker_order_id = models.CharField(max_length=80, blank=True)
    leg = models.CharField(max_length=8, default='entry')  # entry, stop, target, exit
    decision_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    bar_ts = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=200, blank=True)
    submitted_at = models.DateTimeField(default=timezone.now)
    filled_at = models.DateTimeField(null=True, blank=True)
    filled_qty = models.DecimalField(max_digits=18, decimal_places=8, default=D0)
    filled_avg_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
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
    price = models.DecimalField(max_digits=14, decimal_places=4)
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
    entry_price = models.DecimalField(max_digits=14, decimal_places=4)
    exit_price = models.DecimalField(max_digits=14, decimal_places=4)
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
    price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    stop_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    target_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    reason = models.CharField(max_length=200, blank=True)
    acted = models.BooleanField(default=False)
    blocked_reason = models.CharField(max_length=200, blank=True)
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, related_name='signals')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-ts', '-pk']
        indexes = [models.Index(fields=['account', 'ts'])]


class RiskEvent(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='risk_events')
    ts = models.DateTimeField(default=timezone.now)
    kind = models.CharField(max_length=32)  # daily_loss, kill_switch, missed_ticks, external_position, block, flatten, error
    message = models.CharField(max_length=300)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-ts']


class AgentRun(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='runs')
    mode = models.CharField(max_length=8, choices=Mode.choices)
    started_at = models.DateTimeField(default=timezone.now)
    last_tick_at = models.DateTimeField(null=True, blank=True)
    stopped_at = models.DateTimeField(null=True, blank=True)
    pid = models.IntegerField(default=0)
    status = models.CharField(max_length=10, default='running')  # running stopped error
    message = models.CharField(max_length=300, blank=True)
    ticks = models.PositiveIntegerField(default=0)
    replay_date = models.DateField(null=True, blank=True)
    speed = models.FloatField(default=1.0)

    class Meta:
        ordering = ['-started_at']

    @property
    def heartbeat_age_s(self):
        ref = self.last_tick_at or self.started_at
        return (timezone.now() - ref).total_seconds()

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
    """Persisted configuration for a strategy class in the registry."""
    key = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=80)
    params = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=False)
    symbols = models.JSONField(default=list, blank=True)
    timeframe = models.CharField(max_length=8, default='5Min')
    allocation_pct = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('100'))
    stage = models.CharField(max_length=8, choices=Stage.choices, default=Stage.SEED)
    version = models.PositiveIntegerField(default=1)
    history = models.JSONField(default=list, blank=True)  # promotions: {at, version, params, source}
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'strategies'

    def __str__(self):
        return f'{self.name} v{self.version}'


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
    ts = models.DateTimeField(default=timezone.now)
    model = models.CharField(max_length=60)
    purpose = models.CharField(max_length=40, default='coach')
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=5, default=D0)

    class Meta:
        ordering = ['-ts']
