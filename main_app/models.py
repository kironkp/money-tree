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

    # --- the news arm's own limits ----------------------------------------
    # Preregistered on 2026-09-17 and frozen: changing any of these starts a new
    # evaluation identifier rather than extending the running one, because a
    # limit tuned mid-experiment is a result chosen after seeing the data.
    # Seven megacap names are one bet wearing seven hats, so the cap that matters
    # is the aggregate, not the per-position one.
    news_max_correlated_exposure_pct = models.DecimalField(max_digits=7, decimal_places=2,
                                                           default=Decimal('40'))
    news_max_same_direction_positions = models.PositiveIntegerField(default=3)
    news_daily_loss_pct = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('1'))
    news_weekly_loss_pct = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('2.5'))
    news_max_consecutive_losses = models.PositiveIntegerField(default=10)
    # Slippage is an assumption whose only evidence is circular — the simulator
    # measures its own fills. If realised slippage runs at twice the configured
    # number, the cost model is wrong and every gate built on it is wrong too.
    news_slippage_trip_multiple = models.DecimalField(max_digits=5, decimal_places=2,
                                                      default=Decimal('2'))
    # What the research step may spend in a day. The owner's knob; a module
    # constant in dossier.py is the ceiling a web edit cannot raise.
    research_budget_usd_per_day = models.DecimalField(max_digits=8, decimal_places=4,
                                                      default=Decimal('0.20'))

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
    # A scoring epoch. The money resets; the memory does not.
    #
    # An account can be given a clean slate — after a strategy that dominated the
    # record is switched off, say — without deleting a single trade. Reports show
    # the epoch by default so the current setup can be judged on its own results,
    # while `promotion.lifetime_verdict` keeps counting EVERY trade ever taken.
    #
    # That split is deliberate and it is the lesson from `evidence_since`: resetting
    # the clock on a losing strategy is how burst's earned quarantine was erased,
    # after which the lane lost another $1,077. A reset must never be able to buy
    # a failed idea a second life.
    epoch_started_at = models.DateTimeField(null=True, blank=True)
    epoch_note = models.CharField(max_length=200, blank=True)
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
    # Set by the operational reviewer when a critical check fails. Blocks NEW
    # entries only — open positions stay managed, because abandoning a live stop
    # is more dangerous than the problem that tripped the check. Durable and
    # per-lane, unlike risk.blocks, which lives in one process's memory, and
    # gentler than AgentConfig.kill_switch, which flattens the whole desk.
    review_halt = models.BooleanField(default=False)
    review_halt_reason = models.CharField(max_length=300, blank=True)
    review_halt_at = models.DateTimeField(null=True, blank=True)
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
    # A second brake, and the important thing about it is that a version bump
    # cannot release it. `evidence_since` is reset on every promotion, which is
    # correct for judging PARAMETERS — trades taken under old settings say
    # nothing about new ones. But it also let a losing IDEA run forever thirty
    # trades at a time: burst earned a quarantine at 165 trades and -$2,210, an
    # evidence reset erased it, and the lane lost another $1,077 before anyone
    # noticed. This one counts every trade the strategy has ever taken and only
    # an operator can clear it.
    lifetime_halt = models.BooleanField(default=False)
    lifetime_halt_reason = models.CharField(max_length=300, blank=True)
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

    # --- reservation accounting ------------------------------------------
    # A call is RESERVED at an estimate before it is made and SETTLED against
    # the usage the API actually returned. Retries, timeouts and `incomplete`
    # responses all cost money while producing nothing, so counting the things
    # a call was meant to create is not budgeting — counting the calls is. A
    # reservation that fails keeps its estimate rather than vanishing, because
    # a budget that forgets failed calls reads them as headroom.
    STATES = (('settled', 'Settled'), ('reserved', 'Reserved'), ('failed', 'Failed'))
    state = models.CharField(max_length=8, choices=STATES, default='settled')
    # Idempotency: settling one key twice must not charge twice.
    reservation_key = models.CharField(max_length=64, blank=True)
    estimated_usd = models.DecimalField(max_digits=10, decimal_places=5, default=D0)
    attempts = models.PositiveIntegerField(default=1)
    # Billing follows the tier the API ACTUALLY served, not the one requested:
    # Flex and Standard are different rates and a silent fallback is a silent
    # overspend.
    service_tier = models.CharField(max_length=12, blank=True)
    reasoning_tokens = models.PositiveIntegerField(default=0)
    search_calls = models.PositiveIntegerField(default=0)        # billed per search action
    search_content_tokens = models.PositiveIntegerField(default=0)
    # True while the cost is modelled rather than read back from the API.
    provisional = models.BooleanField(default=False)

    class Meta:
        ordering = ['-ts']
        indexes = [models.Index(fields=['ts', 'project']), models.Index(fields=['provider', 'model']),
                   models.Index(fields=['state', '-ts'])]
        constraints = [
            models.UniqueConstraint(fields=['reservation_key'], name='uniq_api_reservation_key',
                                    condition=~models.Q(reservation_key='')),
        ]

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
    article_id = models.CharField(max_length=64, blank=True)     # the provider's bare id
    source = models.CharField(max_length=40, default='alpaca')
    published_at = models.DateTimeField()
    headline = models.CharField(max_length=500)
    summary = models.TextField(blank=True)
    # The article body, tags stripped. The same free Alpaca call returns it; the
    # first version asked for include_content=False and then scored 144-character
    # summaries truncated to 220.
    content = models.TextField(blank=True)
    url = models.URLField(max_length=500, blank=True)
    symbols = models.JSONField(default=list, blank=True)
    market = models.CharField(max_length=8, choices=Market.choices, blank=True)

    # --- the clocks ------------------------------------------------------
    # Freshness is measured from `first_public_at` and from nothing else.
    # Ingestion time flatters a slow poller, and a wire story revised three times
    # has three `updated_at`s but only one moment it first reached the market —
    # which is the moment the price reacted to, and the only one a latency claim
    # can honestly be made against.
    first_public_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    ingested_at = models.DateTimeField(default=timezone.now)
    # Revisions of one article are one event. Twelve outlets rewriting a story is
    # `duplicate_of`; the wire updating its own copy three times is this.
    content_hash = models.CharField(max_length=40, blank=True)
    revision = models.PositiveSmallIntegerField(default=1)

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
            models.Index(fields=['article_id']),
        ]

    def __str__(self):
        return f'{self.published_at:%m-%d %H:%M} {self.headline[:60]}'

    @property
    def is_significant(self) -> bool:
        """Worth a human's attention: a confirmed, sizeable, directional event."""
        return (self.magnitude >= 4 and self.confidence >= 4
                and self.direction in ('bullish', 'bearish') and self.novel)

    @property
    def first_public(self):
        """The moment the market could first have known. Never the moment we did."""
        return self.first_public_at or self.published_at

    @property
    def age_minutes(self) -> float:
        return (timezone.now() - self.first_public).total_seconds() / 60

    @property
    def ingest_lag_seconds(self) -> float | None:
        """How long after publication we saw it. Part of the strategy's latency."""
        if not self.ingested_at:
            return None
        return (self.ingested_at - self.first_public).total_seconds()


class UnmatchedSymbol(models.Model):
    """A ticker the news keeps mentioning that this desk cannot trade.

    `news.ingest` used to throw these away — `if not symbols: skipped += 1` — so
    the only trace was an aggregate in a log line: "fetched 50, stored 8, 34 about
    things we do not trade". A 68% discard rate, and no record of WHAT was
    discarded.

    That is how UNI was missed. Not because anyone decided against it, but
    because the question "what are we blind to?" had no answer anywhere in the
    system. This table is that answer, and it costs one row per ticker per day.

    It is a LEDGER, not a trigger. Nothing here adds an instrument; the whole
    point is to make the gap measurable before anyone argues about filling it.
    """
    symbol = models.CharField(max_length=24)
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)
    mentions = models.PositiveIntegerField(default=1)
    headline = models.CharField(max_length=500, blank=True)   # the most recent one
    url = models.URLField(max_length=500, blank=True)

    class Meta:
        ordering = ['-mentions', '-last_seen']
        constraints = [models.UniqueConstraint(fields=['symbol'], name='uniq_unmatched_symbol')]
        indexes = [models.Index(fields=['-last_seen'])]

    def __str__(self):
        return f'{self.symbol} × {self.mentions}'


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
    # Where this score came from, when it came from research rather than a
    # headline. Null is the headline arm, which is most of the table.
    dossier = models.ForeignKey('SymbolDossier', on_delete=models.SET_NULL, null=True, blank=True,
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

    # --- how this row may be used as evidence ----------------------------
    # A forecast recorded before the fact and an outcome rebuilt afterwards are
    # not the same kind of thing. The 266 rows written before 2026-09-17 were
    # scored without a price, graded against a window the desk never traded,
    # and judged under thresholds that have since changed: they describe the
    # score distribution honestly and they are useful for debugging the grader,
    # but they are not experimental evidence and never enter a promotion
    # statistic.
    PROVENANCE = (('contemporaneous', 'Recorded before the fact'),
                  ('reconstructed', 'Rebuilt afterwards'))
    provenance = models.CharField(max_length=16, choices=PROVENANCE, default='contemporaneous')
    arm = models.CharField(max_length=16, default='headline')   # headline | catalyst | catalyst_ctx
    horizon_bucket = models.CharField(max_length=2, default='B0')

    # --- the geometry the call was actually betting on -------------------
    atr_at_verdict = models.FloatField(null=True, blank=True)
    outcome_kind = models.CharField(max_length=8, blank=True)   # target | stop | timeout
    outcome_atr_net = models.FloatField(null=True, blank=True)  # signed, in ATRs, net of round trip
    mfe_atr = models.FloatField(null=True, blank=True)          # best it ever looked
    mae_atr = models.FloatField(null=True, blank=True)          # worst it ever looked

    # --- the decision clock ------------------------------------------------
    # Research latency is part of strategy performance, so it is recorded rather
    # than assumed away: a dossier that takes 32 seconds enters 32 seconds late,
    # and a shadow trade may not be priced before `decision_eligible_at`.
    research_started_at = models.DateTimeField(null=True, blank=True)
    research_completed_at = models.DateTimeField(null=True, blank=True)
    decision_eligible_at = models.DateTimeField(null=True, blank=True)
    quote_at = models.DateTimeField(null=True, blank=True)   # the tape the price came from

    # --- the lease ---------------------------------------------------------
    # One underlying EVENT may become one order, once. `acted` alone could not
    # express that: it was set before the risk manager had spoken, so the 68%
    # of signals that get blocked destroyed their instruction, and four sittings
    # that saw the same QQQ story shorted it four times into a rising market.
    LEASE_STATES = (('available', 'Available'), ('leased', 'Leased'), ('consumed', 'Consumed'))
    event_key = models.CharField(max_length=64, blank=True)     # identity of the underlying event
    lease_state = models.CharField(max_length=10, choices=LEASE_STATES, default='available')
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    lease_owner = models.CharField(max_length=64, blank=True)   # which worker holds it

    class Meta:
        ordering = ['-score', '-created_at']
        indexes = [models.Index(fields=['session', '-score']), models.Index(fields=['symbol', '-created_at']),
                   models.Index(fields=['event_key', 'lease_state'])]
        constraints = [
            # Two workers cannot hold the same event at once. This is the
            # constraint, not a convention: the duplicate-QQQ failure was a race,
            # and a race is only closed in the database.
            models.UniqueConstraint(fields=['event_key'], name='uniq_leased_event',
                                    condition=models.Q(lease_state='leased') & ~models.Q(event_key='')),
        ]

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


class SymbolDossier(models.Model):
    """What we know about one company right now, and what it implies for today.

    The News Agent scores headlines. This scores a COMPANY: the stories clustered
    together, the filed and vendor numbers underneath them, where the price
    already is, and a two-sided case — which is the unit of analysis the owner
    was actually asking for when he compared a 4/10 on a listicle against a
    sourced research note.

    Three scores, because there are three questions and only one of them can move
    a position on a desk that force-closes after 240 minutes:

      CATALYST  a dated, firm-specific event inside the next day. The only tier
                that may ever open a trade.
      CONTEXT   guidance, estimate revisions, positioning. May shrink a position
                or veto it. Never enlarges one.
      THESIS    valuation, growth, product cycle. Display only.

    Built in SHADOW: written, scored, displayed and graded, emitting no
    instruction and touching no order, until a preregistered gate says the
    research arm beats the headline arm on a paired daily comparison.
    """
    ARMS = (('shadow', 'Shadow'), ('armed', 'Armed'))
    DIRECTIONS = (('buy', 'Buy'), ('short', 'Short'), ('none', 'No action'))

    symbol = models.CharField(max_length=16)
    market = models.CharField(max_length=8, choices=Market.choices, blank=True)
    as_of = models.DateTimeField(default=timezone.now)
    arm = models.CharField(max_length=8, choices=ARMS, default='shadow')

    # --- the clocks. Research latency is part of performance, so it is recorded.
    research_started_at = models.DateTimeField(null=True, blank=True)
    research_completed_at = models.DateTimeField(null=True, blank=True)
    decision_eligible_at = models.DateTimeField(null=True, blank=True)

    # --- the evidence. Documented shapes, following Strategy.history convention.
    # facts:    {label, value, unit, period, tier, source, url, as_of, quote,
    #            accession, form, xbrl_tag}
    # stories:  {news_id, headline, url, first_public_at, age_minutes, weight}
    # bull/bear:{claim, facts: [label, ...]}
    # triggers: {condition, direction, metric, comparator, threshold, window}
    facts = models.JSONField(default=list, blank=True)
    stories = models.JSONField(default=list, blank=True)
    bull = models.JSONField(default=list, blank=True)
    bear = models.JSONField(default=list, blank=True)
    triggers = models.JSONField(default=list, blank=True)
    narrative = models.TextField(blank=True)

    # --- the catalyst, if there is one. Dated or it does not count.
    catalyst_headline = models.CharField(max_length=500, blank=True)
    catalyst_url = models.URLField(max_length=500, blank=True)
    catalyst_at = models.DateTimeField(null=True, blank=True)   # first_public_at of the event

    # --- the three scores. Human-facing summaries, never given a Brier score:
    # a 1-10 rating is not a probability and cannot be scored as one.
    score_catalyst = models.PositiveSmallIntegerField(default=0)
    score_context = models.PositiveSmallIntegerField(default=0)
    score_thesis = models.PositiveSmallIntegerField(default=0)
    direction = models.CharField(max_length=6, choices=DIRECTIONS, default='none')

    # --- the forecasts. These ARE probabilities of defined events, and they are
    # what the scoreboard grades. The first three describe one barrier race and
    # must sum to 1; p_positive_net is a separate binary question.
    p_target_first = models.FloatField(null=True, blank=True)
    p_stop_first = models.FloatField(null=True, blank=True)
    p_timeout = models.FloatField(null=True, blank=True)
    p_positive_net = models.FloatField(null=True, blank=True)
    # The model's own spread. Descriptive until an empirical or conformal
    # procedure establishes coverage — NOT a validated interval.
    p_low = models.FloatField(null=True, blank=True)
    p_high = models.FloatField(null=True, blank=True)
    base_rate = models.FloatField(null=True, blank=True)   # measured, supplied to the model

    # --- what it would have done, had it been armed.
    size_multiplier = models.FloatField(default=1.0)       # clamped to <= 1.0
    veto_reason = models.CharField(max_length=300, blank=True)
    refused_reason = models.CharField(max_length=300, blank=True)
    thin_evidence = models.BooleanField(default=False)

    # --- what it cost, measured rather than modelled.
    model = models.CharField(max_length=60, blank=True)
    service_tier = models.CharField(max_length=12, blank=True)
    searches = models.PositiveSmallIntegerField(default=0)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    reasoning_tokens = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=5, default=D0)
    duration_s = models.FloatField(default=0)
    error = models.CharField(max_length=300, blank=True)

    # --- what actually happened, filled in once the race has run --------------
    # Two numbers, because the ablation is a separate confirmatory question: what
    # the catalyst alone would have earned, and what it earned after CONTEXT and
    # THESIS were allowed to shrink or veto it. Until that comparison passes its
    # own gate, the multiplier may only ever reduce.
    entry_at = models.DateTimeField(null=True, blank=True)
    entry_price = models.FloatField(null=True, blank=True)
    entry_atr = models.FloatField(null=True, blank=True)
    outcome_kind = models.CharField(max_length=8, blank=True)      # target | stop | timeout
    net_atr_catalyst_only = models.FloatField(null=True, blank=True)
    net_atr_combined = models.FloatField(null=True, blank=True)
    outcome_at = models.DateTimeField(null=True, blank=True)
    evaluation = models.ForeignKey('Evaluation', null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='dossiers')

    class Meta:
        ordering = ['-as_of']
        indexes = [models.Index(fields=['symbol', '-as_of']), models.Index(fields=['-as_of']),
                   models.Index(fields=['evaluation', '-as_of'])]

    def __str__(self):
        return f'{self.symbol} {self.as_of:%m-%d %H:%M} {self.score_catalyst}/10'

    @property
    def has_catalyst(self) -> bool:
        return bool(self.catalyst_at and self.catalyst_headline)

    @property
    def catalyst_age_minutes(self) -> float | None:
        if not self.catalyst_at:
            return None
        return (timezone.now() - self.catalyst_at).total_seconds() / 60

    @property
    def probabilities_consistent(self) -> bool:
        parts = [self.p_target_first, self.p_stop_first, self.p_timeout]
        if any(p is None for p in parts):
            return False
        return abs(sum(parts) - 1.0) <= 0.02

    @property
    def edge_vs_base_rate(self) -> float | None:
        """How far the forecast departs from what the tape does unprompted."""
        if self.p_target_first is None or self.base_rate is None:
            return None
        return self.p_target_first - self.base_rate

    @property
    def citable_facts(self) -> list:
        return [f for f in (self.facts or []) if f.get('value') is not None and f.get('source')]


class Evaluation(models.Model):
    """A frozen experiment. Nothing here may change while it is collecting.

    The point of writing all of this down before the first observation is that a
    threshold tuned after seeing the data is not a threshold, it is a result
    chosen to be favourable. So the model, the exact prompt, the schema, every
    limit and the kill rule are hashed into `fingerprint`; if any of them moves,
    this evaluation is superseded and a new one starts at n=0 rather than
    quietly inheriting evidence collected under different rules.

    ONE primary hypothesis: the paired difference in total net ATR per day
    between the complete research trading policy and the current headline-only
    policy, including the days each chose not to trade. Policy-level and
    per-day, because an arm that earns more per trade while trading three times
    as often looks better on every per-trade metric while losing more money.

    Promotion is decided ONLY at preregistered checkpoints, with alpha spent
    across them. A scheduled job that re-checks an ordinary confidence interval
    and promotes the first time one passes will promote noise with probability
    approaching one.
    """
    KINDS = (('promotion', 'Promotion'), ('decay', 'Post-promotion decay'))
    STATUSES = (('collecting', 'Collecting'), ('promoted', 'Promoted'),
                ('demoted', 'Demoted'), ('superseded', 'Superseded'))

    identifier = models.CharField(max_length=40, unique=True)
    kind = models.CharField(max_length=10, choices=KINDS, default='promotion')
    status = models.CharField(max_length=12, choices=STATUSES, default='collecting')
    opened_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)
    # A decay epoch starts at n=0 when its predecessor is promoted: an
    # all-history sequence lets old results outvote a strategy that is failing
    # right now.
    predecessor = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='successors')

    fingerprint = models.CharField(max_length=40)
    frozen = models.JSONField(default=dict, blank=True)   # model, prompt/schema hashes, limits

    # The hypothesis, in numbers, fixed before collection.
    delta_min = models.FloatField(default=0.0)      # ATR/day, the economic hurdle
    alpha = models.FloatField(default=0.05)
    beta = models.FloatField(default=0.20)          # 1 - power
    method = models.CharField(max_length=60, default='fixed-checkpoints/obf/block-bootstrap')
    checkpoints = models.JSONField(default=list, blank=True)       # [trading days]
    checkpoints_done = models.JSONField(default=list, blank=True)  # [{n, spent, lower, decision}]
    kill_rule = models.CharField(max_length=300, blank=True)
    note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['-opened_at']

    def __str__(self):
        return f'{self.identifier} ({self.status})'

    @property
    def is_open(self) -> bool:
        return self.status == 'collecting'

    @property
    def next_checkpoint(self) -> int | None:
        done = {c.get('n') for c in (self.checkpoints_done or [])}
        return next((n for n in (self.checkpoints or []) if n not in done), None)


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


class ReviewRun(models.Model):
    """One execution of one review cycle.

    Two cycles, deliberately separate. The operational one asks "is the machine
    doing what it was told"; the improvement one asks "is what it was told any
    good". Mixing them produces a reviewer that quietly retunes a strategy in
    response to a plumbing fault.
    """
    OPERATIONAL, IMPROVEMENT = 'operational', 'improvement'
    CYCLES = [(OPERATIONAL, 'Operational'), (IMPROVEMENT, 'Improvement')]

    cycle = models.CharField(max_length=12, choices=CYCLES)
    trigger = models.CharField(max_length=10, default='schedule')   # schedule event manual
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, default='running')     # running ok failed
    checks_run = models.PositiveIntegerField(default=0)
    checks_failed = models.PositiveIntegerField(default=0)
    findings_opened = models.PositiveIntegerField(default=0)
    findings_repeated = models.PositiveIntegerField(default=0)
    actions = models.JSONField(default=list, blank=True)            # what it DID, not what it saw
    error = models.TextField(blank=True)
    summary = models.JSONField(default=dict, blank=True)
    # So the app can show "next run" without knowing the schedule itself.
    next_due_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at']
        indexes = [models.Index(fields=['cycle', '-started_at'])]

    def __str__(self):
        return f'{self.cycle} {self.started_at:%Y-%m-%d %H:%M} {self.status}'

    @property
    def duration_s(self) -> float:
        return (self.finished_at - self.started_at).total_seconds() if self.finished_at else 0.0


class ReviewFinding(models.Model):
    """One problem, recorded once.

    `fingerprint` is what makes a restart safe: the same fault seen again bumps
    seen_count and last_seen_at instead of writing a second row, so a reviewer
    that runs every fifteen minutes does not bury the operator in duplicates of
    one unfixed thing — and does not forget it either.
    """
    CRITICAL, WARN, INFO = 'critical', 'warn', 'info'
    SEVERITIES = [(CRITICAL, 'Critical'), (WARN, 'Warning'), (INFO, 'Info')]
    OPEN, ACKED, RESOLVED = 'open', 'acknowledged', 'resolved'
    STATUSES = [(OPEN, 'Open'), (ACKED, 'Acknowledged'), (RESOLVED, 'Resolved')]

    cycle = models.CharField(max_length=12, default=ReviewRun.OPERATIONAL)
    check_key = models.CharField(max_length=40)
    severity = models.CharField(max_length=8, choices=SEVERITIES, default=WARN)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, null=True, blank=True,
                                related_name='review_findings')
    fingerprint = models.CharField(max_length=120)
    title = models.CharField(max_length=200)
    detail = models.TextField(blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    action_taken = models.CharField(max_length=60, blank=True)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    seen_count = models.PositiveIntegerField(default=1)
    first_run = models.ForeignKey(ReviewRun, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='opened_findings')
    last_run = models.ForeignKey(ReviewRun, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='seen_findings')
    status = models.CharField(max_length=14, choices=STATUSES, default=OPEN)
    resolved_at = models.DateTimeField(null=True, blank=True)
    # Whether the thing that was wrong is now right, checked by the same code
    # that found it — "did the fix work" rather than "was a fix attempted".
    resolution = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['-last_seen_at']
        constraints = [models.UniqueConstraint(fields=['cycle', 'check_key', 'fingerprint'],
                                               name='uniq_finding_fingerprint')]
        indexes = [models.Index(fields=['status', '-last_seen_at'])]

    def __str__(self):
        return f'[{self.severity}] {self.title}'


class Hypothesis(models.Model):
    """A sourced idea about how to trade better, and everything that happened to it.

    A hypothesis with no source is not recorded. The point of the improvement
    cycle is to replace "the model suggested" with "this paper claims X, here is
    the out-of-sample test, here is what an adversarial reviewer said about it".
    """
    PROPOSED, BACKTESTED, REJECTED, FORWARD, ACCEPTED = (
        'proposed', 'backtested', 'rejected', 'forward_testing', 'accepted')
    STATUSES = [(PROPOSED, 'Proposed'), (BACKTESTED, 'Backtested'), (REJECTED, 'Rejected'),
                (FORWARD, 'Forward testing'), (ACCEPTED, 'Accepted')]

    created_at = models.DateTimeField(default=timezone.now)
    run = models.ForeignKey(ReviewRun, on_delete=models.SET_NULL, null=True, blank=True,
                            related_name='hypotheses')
    market = models.CharField(max_length=8, choices=Market.choices, default=Market.FOREX)
    title = models.CharField(max_length=200)
    claim = models.TextField()
    # Required. A citation, a URL, or the exact in-app measurement it came from.
    source = models.TextField()
    rationale = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=STATUSES, default=PROPOSED)
    train_result = models.JSONField(default=dict, blank=True)
    test_result = models.JSONField(default=dict, blank=True)     # out of sample
    paper_result = models.JSONField(default=dict, blank=True)    # forward, paper only
    challenge = models.JSONField(default=dict, blank=True)       # the adversarial verdict
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    # Nothing here is ever applied automatically. This records that a human did.
    applied_at = models.DateTimeField(null=True, blank=True)
    applied_by = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'hypotheses'

    def __str__(self):
        return f'{self.title} ({self.status})'


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
