"""The simulator: fake currency, honest fills.

Fill model
- Market entries/exits: at the NEXT bar's open (backtest/replay) or at the
  last price (live), ± `slippage_bps` against us.
- Stops: gap-through at the open, otherwise at the stop, minus slippage.
- Targets: limit orders — fill at the level, no slippage.
- Fees: `fee_bps` of notional per fill, per asset class.
- Liquidity: a fill may not exceed `liquidity_cap_pct` of the bar's volume;
  the remainder is canceled ('liquidity').
- Whole shares for stocks; crypto rounds down to the asset's increment.
"""
from __future__ import annotations

import math
from datetime import datetime

from .base import AccountState, Broker, Fill, OrderReq, Position, TradeRecord, evaluate_exit


def round_qty(qty: float, increment: float) -> float:
    if increment <= 0:
        return qty
    steps = math.floor(qty / increment + 1e-9)
    return round(steps * increment, 8)


class SimBroker(Broker):
    name = 'sim'

    def __init__(self, cash: float, *, immediate_fills: bool = False, slippage_bps: float = 3.0,
                 fee_bps: dict | None = None, liquidity_cap_pct: float = 1.0,
                 asset_classes: dict | None = None, qty_increments: dict | None = None):
        self._cash = float(cash)
        self.starting_cash = float(cash)
        self.immediate_fills = immediate_fills
        self.slippage_bps = float(slippage_bps)
        self.fee_bps = {'stock': 0.5, 'etf': 0.5, 'crypto': 25.0, **(fee_bps or {})}
        self.liquidity_cap_pct = float(liquidity_cap_pct)
        self.asset_classes = asset_classes or {}
        self.qty_increments = qty_increments or {}
        self._positions: dict[str, Position] = {}
        self.open_orders: dict[str, OrderReq] = {}
        self.orders: dict[str, OrderReq] = {}
        self.last_price: dict[str, float] = {}
        self.last_volume: dict[str, float] = {}
        self.trades: list[TradeRecord] = []
        self.fills: list[Fill] = []
        self._events: list = []

    # --- state ------------------------------------------------------------
    @property
    def cash(self) -> float:
        return self._cash

    @property
    def positions(self) -> dict[str, Position]:
        return self._positions

    def positions_value(self) -> float:
        return sum(p.market_value(self.last_price.get(p.symbol)) for p in self._positions.values())

    @property
    def equity(self) -> float:
        return self._cash + self.positions_value()

    def account(self) -> AccountState:
        pv = self.positions_value()
        eq = self._cash + pv
        # Cash account semantics: no margin. Shorts consume cash like longs.
        pending = sum(o.qty * (o.decision_price or self.last_price.get(o.symbol, 0.0))
                      for o in self.open_orders.values() if o.leg == 'entry')
        return AccountState(cash=self._cash, equity=eq, positions_value=pv,
                            buying_power=max(0.0, self._cash - pending))

    def hydrate(self, cash: float, positions: list[Position]) -> None:
        """Restore state from the DB after a restart."""
        self._cash = float(cash)
        self._positions = {p.symbol: p for p in positions}
        for p in positions:
            if p.last_price is not None:
                self.last_price[p.symbol] = p.last_price

    def asset_class(self, symbol: str) -> str:
        return self.asset_classes.get(symbol, 'stock')

    def _increment(self, symbol: str) -> float:
        return float(self.qty_increments.get(symbol, 1.0 if self.asset_class(symbol) != 'crypto' else 0.0001))

    def _fee(self, symbol: str, notional: float) -> float:
        return abs(notional) * self.fee_bps.get(self.asset_class(symbol), 0.5) / 1e4

    def _slip(self, price: float, side: str) -> float:
        adj = self.slippage_bps / 1e4
        return price * (1 + adj) if side == 'buy' else price * (1 - adj)

    # --- orders -----------------------------------------------------------
    def submit(self, order: OrderReq) -> OrderReq:
        if order.id in self.orders:
            order.status = 'rejected'
            order.error = 'duplicate client order id'
            return order
        order.qty = round_qty(order.qty, self._increment(order.symbol))
        if order.qty <= 0:
            order.status = 'rejected'
            order.error = 'quantity rounds to zero'
            self.orders[order.id] = order
            return order
        if order.leg == 'exit':
            pos = self._positions.get(order.symbol)
            if pos is None or pos.qty == 0:
                order.status, order.error = 'rejected', 'no position to close'
                self.orders[order.id] = order
                return order
            if pos.closing or any(o.symbol == order.symbol and o.leg == 'exit' for o in self.open_orders.values()):
                order.status, order.error = 'rejected', 'an exit is already in flight (duplicate ignored)'
                self.orders[order.id] = order
                return order
            pos.closing = True
        order.status = 'accepted'
        self.orders[order.id] = order
        if self.immediate_fills:
            price = self.last_price.get(order.symbol)
            if price is None:
                order.status = 'rejected'
                order.error = 'no price for symbol'
                return order
            self._fill(order, self._slip(price, order.side), order.submitted_ts or order.bar_ts, self.last_volume.get(order.symbol))
        else:
            self.open_orders[order.id] = order
        return order

    def cancel_open_orders(self, symbol: str | None = None) -> int:
        n = 0
        for oid, o in list(self.open_orders.items()):
            if symbol is None or o.symbol == symbol:
                o.status = 'canceled'
                del self.open_orders[oid]
                if o.leg == 'exit' and o.symbol in self._positions:
                    self._positions[o.symbol].closing = False
                n += 1
        return n

    def open_orders_for(self, symbol: str | None = None) -> list:
        return [o for o in self.open_orders.values() if symbol is None or o.symbol == symbol]

    def _fill(self, order: OrderReq, price: float, ts: datetime, bar_volume: float | None, apply_slippage_bps: bool = True) -> None:
        if order.leg == 'exit':
            pos = self._positions.get(order.symbol)
            if pos is None or pos.qty == 0:
                # The position is already gone (a stop/target got there first): never reverse.
                order.status, order.error = 'canceled', 'position already closed'
                self.open_orders.pop(order.id, None)
                return
            order.qty = min(order.qty, abs(pos.qty))
        qty = order.qty - order.filled_qty
        if bar_volume is not None and bar_volume > 0 and self.liquidity_cap_pct > 0:
            cap = round_qty(bar_volume * self.liquidity_cap_pct / 100.0, self._increment(order.symbol))
            if cap <= 0:
                order.status = 'canceled'
                order.error = 'liquidity: bar volume too small'
                self.open_orders.pop(order.id, None)
                return
            if qty > cap:
                qty = cap
        # Exits always close the whole position, cap or not — a partial exit
        # leaves an unmanaged remainder, which is worse than optimistic liquidity.
        if order.leg == 'exit':
            qty = order.qty - order.filled_qty
        notional = qty * price
        fee = self._fee(order.symbol, notional)
        decision = order.decision_price
        slip = None
        if decision:
            signed = (price - decision) / decision * 1e4
            slip = signed if order.side == 'buy' else -signed
        if order.side == 'buy':
            self._cash -= notional + fee
        else:
            self._cash += notional - fee
        order.filled_qty += qty
        order.filled_avg_price = price if order.filled_avg_price is None else (
            (order.filled_avg_price * (order.filled_qty - qty) + price * qty) / order.filled_qty)
        order.fees += fee
        order.filled_ts = ts
        fill = Fill(order.id, order.symbol, ts, order.side, qty, price, fee, slip)
        self.fills.append(fill)
        self._events.append(('fill', fill, order))
        if order.filled_qty + 1e-12 >= order.qty:
            order.status = 'filled'
            self.open_orders.pop(order.id, None)
        else:
            order.status = 'canceled'  # remainder canceled (liquidity cap)
            order.error = 'liquidity: partial fill, remainder canceled'
            self.open_orders.pop(order.id, None)
        self._apply_to_position(order, qty, price, fee, ts)

    def _apply_to_position(self, order: OrderReq, qty: float, price: float, fee: float, ts: datetime) -> None:
        signed = qty if order.side == 'buy' else -qty
        pos = self._positions.get(order.symbol)
        if pos is None or pos.qty == 0:
            self._positions[order.symbol] = Position(
                symbol=order.symbol, qty=signed, avg_price=price, entry_ts=ts, strategy_key=order.strategy_key,
                stop=order.stop, target=order.target, entry_bar_ts=order.bar_ts, entry_fees=fee,
                last_price=price, entry_order_id=order.id,
            )
            return
        if (pos.qty > 0) == (signed > 0):
            total = pos.qty + signed
            pos.avg_price = (pos.avg_price * pos.qty + price * signed) / total
            pos.qty = total
            pos.entry_fees += fee
            return
        # Closing (fully, in v1).
        closed = min(abs(pos.qty), qty)
        pnl = (price - pos.avg_price) * closed * (1 if pos.qty > 0 else -1)
        fees = pos.entry_fees + fee
        cost = pos.avg_price * closed
        self.trades.append(TradeRecord(
            symbol=pos.symbol, strategy_key=pos.strategy_key, side=pos.side, qty=closed,
            entry_ts=pos.entry_ts, exit_ts=ts, entry_price=pos.avg_price, exit_price=price,
            pnl=pnl - fees, pnl_pct=((pnl - fees) / cost * 100) if cost else 0.0, fees=fees,
            bars_held=pos.bars_held, exit_reason=order.exit_reason,
            entry_order_id=pos.entry_order_id, exit_order_id=order.id,
        ))
        self._events.append(('trade', self.trades[-1], order))
        pos.qty += signed
        if abs(pos.qty) < 1e-9:
            del self._positions[order.symbol]

    # --- bars -------------------------------------------------------------
    def on_bar(self, symbol: str, bar, ts: datetime) -> list:
        """Pending fills at the open, then stop/target exits, then marks."""
        o = float(bar.open)
        vol = float(getattr(bar, 'volume', 0.0) or 0.0)
        for oid, order in list(self.open_orders.items()):
            if order.symbol != symbol:
                continue
            if order.order_type == 'market':
                self._fill(order, self._slip(o, order.side), ts, vol)
            elif order.order_type == 'limit':
                lp = order.limit_price
                if order.side == 'buy' and float(bar.low) <= lp:
                    self._fill(order, min(o, lp), ts, vol)
                elif order.side == 'sell' and float(bar.high) >= lp:
                    self._fill(order, max(o, lp), ts, vol)
            elif order.order_type == 'stop':
                sp = order.stop_price
                if order.side == 'buy' and float(bar.high) >= sp:
                    self._fill(order, self._slip(max(o, sp), 'buy'), ts, vol)
                elif order.side == 'sell' and float(bar.low) <= sp:
                    self._fill(order, self._slip(min(o, sp), 'sell'), ts, vol)
        pos = self._positions.get(symbol)
        if pos is not None and not pos.external and not pos.closing:
            hit = evaluate_exit(pos, bar)
            if hit is not None:
                reason, level = hit
                side = 'sell' if pos.qty > 0 else 'buy'
                price = self._slip(level, side) if reason == 'stop' else level
                order = OrderReq(id=f'{pos.entry_order_id or symbol}-{reason}-{int(ts.timestamp())}', symbol=symbol,
                                 side=side, qty=abs(pos.qty), leg='exit', strategy_key=pos.strategy_key,
                                 reason=f'{reason} {level:.4f}', decision_price=level, bar_ts=ts,
                                 submitted_ts=ts, exit_reason=reason, status='accepted')
                self.orders[order.id] = order
                self._fill(order, price, ts, None)
        pos = self._positions.get(symbol)
        if pos is not None:
            pos.bars_held += 1
            pos.last_price = float(bar.close)
        self.last_price[symbol] = float(bar.close)
        self.last_volume[symbol] = vol
        return []

    def close_position(self, symbol: str, price: float, ts: datetime, reason: str, order_id: str) -> OrderReq | None:
        """Immediate exit at `price` ± slippage (EOD flatten, kill switch, catch-up)."""
        pos = self._positions.get(symbol)
        if pos is None or pos.qty == 0:
            return None
        # One coordinated close: drop any in-flight exit for this symbol first,
        # then fill the whole remaining quantity at once.
        self.cancel_open_orders(symbol)
        side = 'sell' if pos.qty > 0 else 'buy'
        order = OrderReq(id=order_id, symbol=symbol, side=side, qty=abs(pos.qty), leg='exit',
                         strategy_key=pos.strategy_key, reason=reason, decision_price=price, bar_ts=ts,
                         submitted_ts=ts, exit_reason=reason, status='accepted')
        if order.id in self.orders:
            order.id = f'{order_id}-{int(ts.timestamp())}'
        self.orders[order.id] = order
        pos.closing = True
        self._fill(order, self._slip(price, side), ts, None)
        return order

    def mark(self, prices: dict[str, float]) -> None:
        for s, p in prices.items():
            self.last_price[s] = p
            if s in self._positions:
                self._positions[s].last_price = p

    def drain_events(self) -> list:
        ev, self._events = self._events, []
        return ev
