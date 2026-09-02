"""Alpaca broker adapter (paper and live share it; the keys differ).

Stocks: bracket orders carry the stop and target server-side. Crypto: simple
orders only (Alpaca has no crypto brackets), so the engine evaluates exits
on every bar exactly like the simulator and fires a market exit.

Live money is refused unless settings.LIVE_TRADING_ARMED is on AND the
AgentConfig mode is 'live' — two switches in two places, on purpose.
"""
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from django.conf import settings

from .base import AccountState, Broker, Fill, OrderReq, Position, TradeRecord, evaluate_exit

log = logging.getLogger('moneytree.broker.alpaca')


class LiveTradingRefused(RuntimeError):
    pass


class AlpacaBroker(Broker):
    name = 'alpaca'
    immediate_fills = True

    def __init__(self, paper: bool = True, asset_classes: dict | None = None, qty_increments: dict | None = None,
                 mode_is_live: bool = False):
        if not paper:
            if not (settings.LIVE_TRADING_ARMED and mode_is_live):
                raise LiveTradingRefused('live trading is not armed (LIVE_TRADING_ARMED=1 and mode=live are both required)')
            key, secret = settings.ALPACA_LIVE_API_KEY, settings.ALPACA_LIVE_SECRET_KEY
        else:
            key, secret = settings.ALPACA_API_KEY, settings.ALPACA_SECRET_KEY
        if not (key and secret):
            raise RuntimeError('Alpaca keys missing for this mode')
        from alpaca.trading.client import TradingClient
        self.client = TradingClient(key, secret, paper=paper)
        self.paper = paper
        self.asset_classes = asset_classes or {}
        self.qty_increments = qty_increments or {}
        self._positions: dict[str, Position] = {}
        self._cash = 0.0
        self._equity = 0.0
        self._buying_power = 0.0
        self.last_price: dict[str, float] = {}
        self.orders: dict[str, OrderReq] = {}
        self._events: list = []
        self._last_order_sync: datetime | None = None
        self.trades: list[TradeRecord] = []

    # --- state ------------------------------------------------------------
    @property
    def cash(self) -> float:
        return self._cash

    @property
    def positions(self) -> dict[str, Position]:
        return self._positions

    def account(self) -> AccountState:
        pv = sum(p.market_value(self.last_price.get(p.symbol)) for p in self._positions.values())
        return AccountState(cash=self._cash, equity=self._equity or (self._cash + pv), positions_value=pv,
                            buying_power=self._buying_power or self._cash)

    def hydrate(self, cash: float, positions: list[Position]) -> None:
        # Local knowledge (strategy, stop/target) layered over the venue's truth in sync().
        self._positions = {p.symbol: p for p in positions}
        self._cash = float(cash)

    def _is_crypto(self, symbol: str) -> bool:
        return self.asset_classes.get(symbol, 'stock') == 'crypto'

    @staticmethod
    def _sym(symbol: str) -> str:
        return symbol  # Alpaca accepts 'BTC/USD' for crypto and 'AAPL' for stocks

    # --- reconciliation ---------------------------------------------------
    def sync(self) -> dict:
        acct = self.client.get_account()
        self._cash = float(acct.cash)
        self._equity = float(acct.equity)
        self._buying_power = float(acct.buying_power)
        venue = {}
        for p in self.client.get_all_positions():
            symbol = p.symbol if '/' in p.symbol or not self._looks_crypto(p.symbol) else self._crypto_symbol(p.symbol)
            qty = float(p.qty) * (-1 if str(p.side).lower().endswith('short') else 1)
            venue[symbol] = (qty, float(p.avg_entry_price), float(p.current_price or p.avg_entry_price))
        adopted, closed = [], []
        for symbol, (qty, avg, cur) in venue.items():
            local = self._positions.get(symbol)
            if local is None:
                self._positions[symbol] = Position(symbol=symbol, qty=qty, avg_price=avg, entry_ts=datetime.now(UTC),
                                                   external=True, last_price=cur)
                adopted.append(symbol)
            else:
                local.qty, local.avg_price, local.last_price = qty, avg, cur
            self.last_price[symbol] = cur
        for symbol in list(self._positions):
            if symbol not in venue:
                closed.append(symbol)
                del self._positions[symbol]
        self._sync_closed_orders()
        return {'adopted': adopted, 'closed': closed, 'cash': self._cash, 'equity': self._equity}

    def _looks_crypto(self, symbol: str) -> bool:
        return symbol.endswith('USD') and len(symbol) > 4 and symbol[:-3] + '/USD' in self.asset_classes

    def _crypto_symbol(self, symbol: str) -> str:
        return symbol[:-3] + '/USD'

    def _sync_closed_orders(self) -> None:
        """Turn fills the venue reports into Fill/Trade events for the recorder."""
        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest
            req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=self._last_order_sync, limit=200, nested=True)
            closed = self.client.get_orders(req)
        except Exception as exc:
            log.warning('order sync failed: %s', exc)
            return
        self._last_order_sync = datetime.now(UTC)
        for o in closed:
            self._absorb_closed(o)
            for leg in (getattr(o, 'legs', None) or []):
                self._absorb_closed(leg, parent=o)

    def _absorb_closed(self, o, parent=None) -> None:
        cid = str(o.client_order_id or '')
        if str(o.status).lower().split('.')[-1] != 'filled' or not o.filled_avg_price:
            return
        local = self.orders.get(cid)
        if local is None:
            if parent is None or str(parent.client_order_id or '') not in self.orders:
                return
            # A bracket leg: register it as an exit on our books.
            p = self.orders[str(parent.client_order_id)]
            reason = 'target' if str(o.type).lower().endswith('limit') else 'stop'
            local = OrderReq(id=cid or f'{p.id}-{reason}', symbol=p.symbol, side='sell' if p.side == 'buy' else 'buy',
                             qty=float(o.filled_qty or o.qty), leg='exit', strategy_key=p.strategy_key,
                             reason=reason, exit_reason=reason, bar_ts=None)
            self.orders[local.id] = local
        if local.status == 'filled':
            return
        price = float(o.filled_avg_price)
        qty = float(o.filled_qty or local.qty)
        ts = getattr(o, 'filled_at', None) or datetime.now(UTC)
        local.status, local.filled_qty, local.filled_avg_price, local.filled_ts = 'filled', qty, price, ts
        local.broker_order_id = str(o.id)
        slip = None
        if local.decision_price:
            signed = (price - local.decision_price) / local.decision_price * 1e4
            slip = signed if local.side == 'buy' else -signed
        fill = Fill(local.id, local.symbol, ts, local.side, qty, price, 0.0, slip)
        self._events.append(('fill', fill, local))
        if local.leg == 'exit':
            self._record_trade(local, price, qty, ts)

    def _record_trade(self, order: OrderReq, price: float, qty: float, ts: datetime) -> None:
        pos = self._closed_snapshot.pop(order.symbol, None) if hasattr(self, '_closed_snapshot') else None
        if pos is None:
            pos = self._positions.get(order.symbol)
        if pos is None:
            return
        pnl = (price - pos.avg_price) * qty * (1 if pos.qty > 0 else -1)
        cost = pos.avg_price * qty
        tr = TradeRecord(symbol=order.symbol, strategy_key=pos.strategy_key, side=pos.side, qty=qty,
                         entry_ts=pos.entry_ts, exit_ts=ts, entry_price=pos.avg_price, exit_price=price,
                         pnl=pnl, pnl_pct=(pnl / cost * 100) if cost else 0.0, fees=0.0, bars_held=pos.bars_held,
                         exit_reason=order.exit_reason, entry_order_id=pos.entry_order_id, exit_order_id=order.id)
        self.trades.append(tr)
        self._events.append(('trade', tr, order))

    # --- orders -----------------------------------------------------------
    def submit(self, order: OrderReq) -> OrderReq:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest
        if order.id in self.orders and self.orders[order.id].status not in ('rejected',):
            order.status, order.error = 'rejected', 'duplicate client order id'
            return order
        crypto = self._is_crypto(order.symbol)
        kwargs = dict(symbol=self._sym(order.symbol), qty=order.qty,
                      side=OrderSide.BUY if order.side == 'buy' else OrderSide.SELL,
                      time_in_force=TimeInForce.GTC if crypto else TimeInForce.DAY,
                      client_order_id=order.id)
        if order.leg == 'entry' and not crypto and order.stop and order.target:
            kwargs.update(order_class=OrderClass.BRACKET,
                          take_profit=TakeProfitRequest(limit_price=round(order.target, 2)),
                          stop_loss=StopLossRequest(stop_price=round(order.stop, 2)))
        try:
            resp = self.client.submit_order(MarketOrderRequest(**kwargs))
        except Exception as exc:
            order.status, order.error = 'rejected', str(exc)[:300]
            self.orders[order.id] = order
            log.warning('submit rejected %s: %s', order.id, exc)
            return order
        order.status = 'accepted'
        order.broker_order_id = str(resp.id)
        self.orders[order.id] = order
        self._await_fill(order)
        return order

    def _await_fill(self, order: OrderReq, timeout_s: float = 8.0) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                o = self.client.get_order_by_client_id(order.id)
            except Exception as exc:
                log.warning('poll failed %s: %s', order.id, exc)
                break
            status = str(o.status).lower().split('.')[-1]
            if status == 'filled' and o.filled_avg_price:
                price, qty = float(o.filled_avg_price), float(o.filled_qty)
                ts = getattr(o, 'filled_at', None) or datetime.now(UTC)
                order.status, order.filled_qty, order.filled_avg_price, order.filled_ts = 'filled', qty, price, ts
                slip = None
                if order.decision_price:
                    signed = (price - order.decision_price) / order.decision_price * 1e4
                    slip = signed if order.side == 'buy' else -signed
                self._events.append(('fill', Fill(order.id, order.symbol, ts, order.side, qty, price, 0.0, slip), order))
                self._apply_fill(order, qty, price, ts)
                return
            if status in ('canceled', 'rejected', 'expired'):
                order.status = status
                return
            time.sleep(0.5)

    def _apply_fill(self, order: OrderReq, qty: float, price: float, ts: datetime) -> None:
        signed = qty if order.side == 'buy' else -qty
        pos = self._positions.get(order.symbol)
        if order.leg == 'entry' or pos is None:
            self._positions[order.symbol] = Position(symbol=order.symbol, qty=signed, avg_price=price, entry_ts=ts,
                                                     strategy_key=order.strategy_key, stop=order.stop, target=order.target,
                                                     entry_bar_ts=order.bar_ts, last_price=price, entry_order_id=order.id)
            self._cash -= signed * price
            return
        self._record_trade(order, price, qty, ts)
        self._cash -= signed * price
        pos.qty += signed
        if abs(pos.qty) < 1e-9:
            del self._positions[order.symbol]

    def cancel_open_orders(self, symbol: str | None = None) -> int:
        try:
            if symbol is None:
                res = self.client.cancel_orders()
                return len(res or [])
            n = 0
            for o in self.client.get_orders():
                if o.symbol == self._sym(symbol):
                    self.client.cancel_order_by_id(o.id)
                    n += 1
            return n
        except Exception as exc:
            log.warning('cancel failed: %s', exc)
            return 0

    def on_bar(self, symbol: str, bar, ts: datetime) -> list:
        self.last_price[symbol] = float(bar.close)
        pos = self._positions.get(symbol)
        if pos is None or pos.qty == 0 or pos.external:
            return []
        pos.bars_held += 1
        pos.last_price = float(bar.close)
        # Crypto (no bracket at the venue): manage exits here.
        if self._is_crypto(symbol):
            hit = evaluate_exit(pos, bar)
            if hit is not None:
                reason, level = hit
                self.close_position(symbol, level, ts, reason, f'{pos.entry_order_id or symbol}-{reason}-{int(ts.timestamp())}')
        return []

    def close_position(self, symbol: str, price: float, ts: datetime, reason: str, order_id: str) -> OrderReq | None:
        pos = self._positions.get(symbol)
        if pos is None or pos.qty == 0:
            return None
        if not self._is_crypto(symbol):
            self.cancel_open_orders(symbol)  # drop bracket legs before flattening
        order = OrderReq(id=order_id, symbol=symbol, side='sell' if pos.qty > 0 else 'buy', qty=abs(pos.qty), leg='exit',
                         strategy_key=pos.strategy_key, reason=reason, decision_price=price, bar_ts=ts,
                         submitted_ts=ts, exit_reason=reason)
        return self.submit(order)

    def drain_events(self) -> list:
        ev, self._events = self._events, []
        return ev
