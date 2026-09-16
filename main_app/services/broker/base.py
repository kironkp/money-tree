"""Broker contract plus the plain data types the engine trades in.

Everything here is float-based pure Python: the simulator, the backtester and
the optimizer workers all run without Django. The DB recorder turns these
into Decimal ledger rows at the edge.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class OrderReq:
    id: str                      # deterministic client order id
    symbol: str
    side: str                    # buy | sell
    qty: float
    order_type: str = 'market'   # market | limit | stop
    limit_price: float | None = None
    stop_price: float | None = None
    leg: str = 'entry'           # entry | exit
    strategy_key: str = ''
    reason: str = ''
    decision_price: float | None = None
    bar_ts: datetime | None = None
    submitted_ts: datetime | None = None
    # Entry-only: protective levels attached to the position on fill.
    stop: float | None = None
    target: float | None = None
    # Exit-only.
    exit_reason: str = 'signal'
    # Filled in by the broker.
    status: str = 'new'          # new accepted partially_filled filled canceled rejected
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    fees: float = 0.0
    filled_ts: datetime | None = None
    broker_order_id: str = ''
    error: str = ''

    @property
    def is_open(self):
        return self.status in ('new', 'accepted', 'partially_filled')


@dataclass
class Fill:
    order_id: str
    symbol: str
    ts: datetime
    side: str
    qty: float
    price: float
    fee: float = 0.0
    slippage_bps: float | None = None


@dataclass
class Position:
    symbol: str
    qty: float                   # signed; negative = short
    avg_price: float
    entry_ts: datetime
    strategy_key: str = ''
    stop: float | None = None
    target: float | None = None
    bars_held: int = 0
    entry_bar_ts: datetime | None = None
    entry_fees: float = 0.0
    last_price: float | None = None
    max_hold_until: datetime | None = None
    external: bool = False
    entry_order_id: str = ''
    # Exit protection: engine (levels evaluated by us), bracket / stop_order
    # (venue-side), none. `closing` = an exit is in flight; more exits are ignored.
    protection: str = 'engine'
    protection_order_id: str = ''
    closing: bool = False

    @property
    def side(self):
        return 'short' if self.qty < 0 else 'long'

    def market_value(self, price: float | None = None) -> float:
        p = price if price is not None else (self.last_price if self.last_price is not None else self.avg_price)
        return self.qty * p

    def unrealized(self, price: float | None = None) -> float:
        p = price if price is not None else (self.last_price if self.last_price is not None else self.avg_price)
        return (p - self.avg_price) * self.qty


@dataclass
class TradeRecord:
    symbol: str
    strategy_key: str
    side: str
    qty: float
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    pnl: float
    pnl_pct: float
    fees: float
    bars_held: int
    exit_reason: str
    entry_order_id: str = ''
    exit_order_id: str = ''


@dataclass
class AccountState:
    cash: float
    equity: float
    positions_value: float
    buying_power: float


def evaluate_exit(pos: Position, bar) -> tuple[str, float] | None:
    """Stop/target check against one completed bar.

    Gap-through: if the bar opened beyond the level, the fill is the open.
    Stop wins when both levels are touched in the same bar (conservative).
    Targets are limits: they fill at the level, never better in this model.
    """
    o, h, l = float(bar.open), float(bar.high), float(bar.low)
    if pos.qty > 0:
        if pos.stop is not None and l <= pos.stop:
            return 'stop', min(o, pos.stop)
        if pos.target is not None and h >= pos.target:
            return 'target', max(o, pos.target) if o >= pos.target else pos.target
    elif pos.qty < 0:
        if pos.stop is not None and h >= pos.stop:
            return 'stop', max(o, pos.stop)
        if pos.target is not None and l <= pos.target:
            return 'target', min(o, pos.target) if o <= pos.target else pos.target
    return None


class Broker:
    """What the engine needs from any broker."""
    name = 'abstract'
    immediate_fills = True  # False → market orders fill on the next bar's open

    @property
    def cash(self) -> float:
        raise NotImplementedError

    @property
    def positions(self) -> dict[str, Position]:
        raise NotImplementedError

    def account(self) -> AccountState:
        raise NotImplementedError

    def submit(self, order: OrderReq) -> OrderReq:
        raise NotImplementedError

    def on_bar(self, symbol: str, bar, ts: datetime) -> list:
        """Process a completed bar: pending fills, stop/target exits, marks."""
        raise NotImplementedError

    def close_position(self, symbol: str, price: float, ts: datetime, reason: str, order_id: str) -> OrderReq | None:
        raise NotImplementedError

    def cancel_open_orders(self, symbol: str | None = None) -> int:
        raise NotImplementedError

    def can_short(self, symbol: str) -> tuple[bool, str]:
        """May this symbol be sold short right now, and if not, why not?

        Asked immediately before every short, never cached: shortability and
        borrow availability are venue state that changes during the day, and a
        stale yes is an order that gets rejected at best and creates an
        unhedgeable position at worst. The simulator lends freely and says so;
        a real venue has to be asked.
        """
        return True, ''

    def sync(self) -> dict:
        """Reconcile with the venue (no-op for the simulator)."""
        return {}

    def open_orders_for(self, symbol: str | None = None) -> list:
        return []

    def protection_for(self, symbol: str) -> tuple[str, str]:
        """(kind, order id) of the exit protection on an open position."""
        pos = self.positions.get(symbol)
        if pos is None or pos.qty == 0:
            return 'none', ''
        return pos.protection, pos.protection_order_id

    def drain_events(self) -> list:
        """Fills/trades produced since the last drain, for the recorder."""
        return []
