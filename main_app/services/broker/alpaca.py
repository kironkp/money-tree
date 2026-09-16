"""Alpaca broker adapter (paper and live share it; the keys differ).

Alpaca is the source of truth. `sync()` reconciles account, positions, open
orders and closed fills against our books and reports divergences; the agent
calls it before every decision. Closing is one coordinated lifecycle: cancel
protective orders, confirm, close the remaining venue quantity, ignore
duplicates. Stocks carry bracket legs at the venue; crypto (no brackets) gets
a resting stop order after the entry fills, so a stop survives our process.

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


def _status(o) -> str:
    return str(getattr(o, 'status', '')).lower().split('.')[-1]


class AlpacaBroker(Broker):
    name = 'alpaca'
    immediate_fills = True

    def __init__(self, paper: bool = True, asset_classes: dict | None = None, qty_increments: dict | None = None,
                 mode_is_live: bool = False, client=None, poll_s: float = 8.0):
        if client is None:
            if not paper:
                if not (settings.LIVE_TRADING_ARMED and mode_is_live):
                    raise LiveTradingRefused('live trading is not armed (LIVE_TRADING_ARMED=1 and mode=live are both required)')
                key, secret = settings.ALPACA_LIVE_API_KEY, settings.ALPACA_LIVE_SECRET_KEY
            else:
                key, secret = settings.ALPACA_API_KEY, settings.ALPACA_SECRET_KEY
            if not (key and secret):
                raise RuntimeError('Alpaca keys missing for this mode')
            from alpaca.trading.client import TradingClient
            client = TradingClient(key, secret, paper=paper)
        self.client = client
        self.paper = paper
        self.poll_s = poll_s
        self.asset_classes = asset_classes or {}
        self.qty_increments = qty_increments or {}
        self._positions: dict[str, Position] = {}
        self._cash = 0.0
        self._equity = 0.0
        self._buying_power = 0.0
        self.last_price: dict[str, float] = {}
        self.orders: dict[str, OrderReq] = {}
        self._open_orders: list[dict] = []
        self._events: list = []
        self._last_order_sync: datetime | None = None
        self.trades: list[TradeRecord] = []
        self.last_sync: dict = {}

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
        # Local knowledge (strategy, stop/target, protection) layered over the venue's truth in sync().
        self._positions = {p.symbol: p for p in positions}
        self._cash = float(cash)

    def _is_crypto(self, symbol: str) -> bool:
        return self.asset_classes.get(symbol, 'stock') == 'crypto'

    def _venue_symbol(self, symbol: str) -> str:
        return symbol.replace('/', '') if self._is_crypto(symbol) else symbol

    def _our_symbol(self, venue_symbol: str) -> str:
        if '/' in venue_symbol:
            return venue_symbol
        for s in self.asset_classes:
            if s.replace('/', '') == venue_symbol:
                return s
        return venue_symbol

    def _increment(self, symbol: str) -> float:
        return float(self.qty_increments.get(symbol, 1.0 if not self._is_crypto(symbol) else 0.0001))

    def open_orders_for(self, symbol: str | None = None) -> list:
        return [o for o in self._open_orders if symbol is None or o['symbol'] == symbol]

    # --- reconciliation ---------------------------------------------------
    def can_short(self, symbol: str) -> tuple[bool, str]:
        """Ask the venue, every time, immediately before the order.

        Shortability and borrow availability are venue state that moves during
        the day: a name can be shortable at the open and hard-to-borrow by
        lunchtime. A cached yes is how an automated desk discovers it is short
        something it cannot cover. Spot crypto has no borrow at all.

        On any failure this returns False. An unanswered question about borrow is
        a no, not a yes — failing closed here costs one missed trade, and failing
        open costs a position nobody can close.
        """
        if self.asset_classes.get(symbol) == 'crypto':
            return False, 'spot crypto cannot be sold short'
        try:
            asset = self.client.get_asset(symbol)
        except Exception as exc:                       # noqa: BLE001
            log.warning('shortability check failed for %s: %r', symbol, exc)
            return False, f'could not confirm {symbol} is shortable ({exc.__class__.__name__})'
        if not getattr(asset, 'tradable', True):
            return False, f'{symbol} is not tradable at the venue right now'
        if not getattr(asset, 'shortable', False):
            return False, f'{symbol} is not shortable at the venue right now'
        if not getattr(asset, 'easy_to_borrow', False):
            return False, f'{symbol} is hard to borrow; the borrow fee is not modelled'
        return True, ''

    def sync(self) -> dict:
        """Compare our books with the venue and adopt the venue's truth.

        Returns adopted (positions we did not know), closed (positions the venue
        no longer has), diverged [(symbol, ours, venue)], open_orders, fills."""
        acct = self.client.get_account()
        self._cash = float(acct.cash)
        self._equity = float(acct.equity)
        self._buying_power = float(acct.buying_power)
        venue: dict[str, tuple[float, float, float]] = {}
        for p in self.client.get_all_positions():
            symbol = self._our_symbol(str(p.symbol))
            qty = float(p.qty) * (-1 if str(getattr(p, 'side', '')).lower().endswith('short') else 1)
            venue[symbol] = (qty, float(p.avg_entry_price), float(getattr(p, 'current_price', None) or p.avg_entry_price))
        adopted, closed, diverged = [], [], []
        for symbol, (qty, avg, cur) in venue.items():
            local = self._positions.get(symbol)
            if local is None:
                self._positions[symbol] = Position(symbol=symbol, qty=qty, avg_price=avg, entry_ts=datetime.now(UTC),
                                                   external=True, last_price=cur, protection='none')
                adopted.append(symbol)
            else:
                tolerance = max(self._increment(symbol) / 2, abs(qty) * (0.003 if self._is_crypto(symbol) else 0.0))
                if abs(local.qty - qty) > tolerance:
                    diverged.append((symbol, local.qty, qty))
                local.qty, local.avg_price, local.last_price = qty, avg, cur
            self.last_price[symbol] = cur
        for symbol in list(self._positions):
            if symbol not in venue:
                closed.append(symbol)
        fills = self._sync_closed_orders()
        for symbol in closed:
            self._positions.pop(symbol, None)
        self._open_orders = self._fetch_open_orders()
        # Orphans: a resting exit/stop for a symbol we no longer hold can only do harm.
        orphans = []
        for o in list(self._open_orders):
            if o['symbol'] not in self._positions and (o['type'] in ('stop', 'stop_limit', 'limit') or o['client_order_id'].endswith('-stop')):
                try:
                    self.client.cancel_order_by_id(o['id'])
                    orphans.append(o['client_order_id'] or o['id'])
                    self._open_orders.remove(o)
                except Exception as exc:
                    log.warning('orphan cancel failed %s: %s', o['id'], exc)
        self.last_sync = {'adopted': adopted, 'closed': closed, 'diverged': diverged, 'cash': self._cash,
                          'equity': self._equity, 'open_orders': len(self._open_orders), 'fills': fills,
                          'orphans': orphans, 'at': datetime.now(UTC)}
        return self.last_sync

    def _fetch_open_orders(self) -> list[dict]:
        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest
            rows = self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True, limit=200))
        except Exception as exc:
            log.warning('open-order fetch failed: %s', exc)
            return self._open_orders
        out = []
        for o in rows:
            out.append({'id': str(o.id), 'client_order_id': str(o.client_order_id or ''), 'symbol': self._our_symbol(str(o.symbol)),
                        'side': str(getattr(o, 'side', '')).lower().split('.')[-1], 'qty': float(o.qty or 0),
                        'type': str(getattr(o, 'type', '')).lower().split('.')[-1], 'status': _status(o),
                        'order_class': str(getattr(o, 'order_class', '')).lower().split('.')[-1],
                        'legs': len(getattr(o, 'legs', None) or [])})
        return out

    def _sync_closed_orders(self) -> int:
        """Turn fills the venue reports into Fill/Trade events for the recorder."""
        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest
            req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=self._last_order_sync, limit=200, nested=True)
            closed = self.client.get_orders(req)
        except Exception as exc:
            log.warning('order sync failed: %s', exc)
            return 0
        self._last_order_sync = datetime.now(UTC)
        n = 0
        for o in closed:
            n += self._absorb_closed(o)
            for leg in (getattr(o, 'legs', None) or []):
                n += self._absorb_closed(leg, parent=o)
        return n

    def _absorb_closed(self, o, parent=None) -> int:
        cid = str(o.client_order_id or '')
        if _status(o) != 'filled' or not o.filled_avg_price:
            return 0
        local = self.orders.get(cid)
        if local is None:
            if parent is None or str(parent.client_order_id or '') not in self.orders:
                return 0
            p = self.orders[str(parent.client_order_id)]
            reason = 'target' if str(getattr(o, 'type', '')).lower().endswith('limit') else 'stop'
            local = OrderReq(id=cid or f'{p.id}-{reason}', symbol=p.symbol, side='sell' if p.side == 'buy' else 'buy',
                             qty=float(o.filled_qty or o.qty), leg='exit', strategy_key=p.strategy_key,
                             reason=reason, exit_reason=reason, bar_ts=None)
            self.orders[local.id] = local
        if local.status == 'filled':
            return 0
        price = float(o.filled_avg_price)
        qty = float(o.filled_qty or local.qty)
        ts = getattr(o, 'filled_at', None) or datetime.now(UTC)
        local.status, local.filled_qty, local.filled_avg_price, local.filled_ts = 'filled', qty, price, ts
        local.broker_order_id = str(o.id)
        slip = None
        if local.decision_price:
            signed = (price - local.decision_price) / local.decision_price * 1e4
            slip = signed if local.side == 'buy' else -signed
        self._events.append(('fill', Fill(local.id, local.symbol, ts, local.side, qty, price, 0.0, slip), local))
        if local.leg == 'exit':
            self._record_trade(local, price, qty, ts)
        return 1

    def _record_trade(self, order: OrderReq, price: float, qty: float, ts: datetime) -> None:
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
        self._positions.pop(order.symbol, None)

    # --- orders -----------------------------------------------------------
    def submit(self, order: OrderReq) -> OrderReq:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest
        if order.id in self.orders and self.orders[order.id].status not in ('rejected',):
            order.status, order.error = 'rejected', 'duplicate client order id'
            return order
        crypto = self._is_crypto(order.symbol)
        if order.leg == 'exit':
            pos = self._positions.get(order.symbol)
            if pos is None or pos.qty == 0:
                order.status, order.error = 'rejected', 'no position to close'
                return order
            if pos.closing:
                order.status, order.error = 'rejected', 'an exit is already in flight (duplicate ignored)'
                return order
            # Exits go through the coordinated close so protective orders come off first.
            res = self.close_position(order.symbol, order.decision_price or self.last_price.get(order.symbol, 0.0),
                                      order.submitted_ts or datetime.now(UTC), order.exit_reason, order.id)
            return res or order
        kwargs = dict(symbol=self._venue_symbol(order.symbol), qty=order.qty,
                      side=OrderSide.BUY if order.side == 'buy' else OrderSide.SELL,
                      time_in_force=TimeInForce.GTC if crypto else TimeInForce.DAY,
                      client_order_id=order.id)
        if not crypto and order.stop and order.target:
            ref = self.last_price.get(order.symbol) or order.decision_price or 0.0
            bad = (order.side == 'buy' and not (order.stop < ref < order.target)) or \
                  (order.side == 'sell' and not (order.target < ref < order.stop))
            if bad:
                order.status, order.error = 'rejected', f'bracket levels do not straddle the price {ref:,.2f} (stop {order.stop}, target {order.target})'
                self.orders[order.id] = order
                return order
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

    def _await_fill(self, order: OrderReq) -> None:
        deadline = time.time() + self.poll_s
        while time.time() < deadline:
            try:
                o = self.client.get_order_by_client_id(order.id)
            except Exception as exc:
                log.warning('poll failed %s: %s', order.id, exc)
                break
            status = _status(o)
            filled_qty = float(o.filled_qty or 0)
            if status == 'filled' and o.filled_avg_price:
                self._record_fill(order, float(o.filled_avg_price), filled_qty, getattr(o, 'filled_at', None) or datetime.now(UTC), 'filled')
                return
            if status == 'partially_filled' and filled_qty > order.filled_qty and o.filled_avg_price:
                self._record_fill(order, float(o.filled_avg_price), filled_qty, datetime.now(UTC), 'partially_filled')
            if status in ('canceled', 'rejected', 'expired'):
                order.status = status
                order.error = f'venue: {status}'
                return
            time.sleep(0.5)

    def _record_fill(self, order: OrderReq, price: float, filled_qty: float, ts, status: str) -> None:
        new_qty = filled_qty - order.filled_qty
        if new_qty <= 0:
            return
        order.status = status
        order.filled_qty = filled_qty
        order.filled_avg_price = price
        order.filled_ts = ts
        slip = None
        if order.decision_price:
            signed = (price - order.decision_price) / order.decision_price * 1e4
            slip = signed if order.side == 'buy' else -signed
        self._events.append(('fill', Fill(order.id, order.symbol, ts, order.side, new_qty, price, 0.0, slip), order))
        self._apply_fill(order, new_qty, price, ts)

    def _apply_fill(self, order: OrderReq, qty: float, price: float, ts: datetime) -> None:
        signed = qty if order.side == 'buy' else -qty
        pos = self._positions.get(order.symbol)
        if order.leg == 'entry':
            if pos is None or pos.qty == 0:
                pos = Position(symbol=order.symbol, qty=signed, avg_price=price, entry_ts=ts, strategy_key=order.strategy_key,
                               stop=order.stop, target=order.target, entry_bar_ts=order.bar_ts, last_price=price,
                               entry_order_id=order.id, protection='none')
                self._positions[order.symbol] = pos
            else:
                total = pos.qty + signed
                pos.avg_price = (pos.avg_price * pos.qty + price * signed) / total
                pos.qty = total
            self._cash -= signed * price
            if not self._is_crypto(order.symbol) and order.stop and order.target:
                pos.protection, pos.protection_order_id = 'bracket', order.broker_order_id
            elif self._is_crypto(order.symbol) and order.status == 'filled':
                # Alpaca deducts the crypto fee from the coins received, so the
                # position is slightly smaller than the order. Adopt the venue's
                # quantity, book the difference as the fee, protect what we hold.
                try:
                    venue = self.client.get_open_position(self._venue_symbol(order.symbol))
                    actual = abs(float(venue.qty)) * (1 if pos.qty > 0 else -1)
                    fee_qty = abs(pos.qty) - abs(actual)
                    if fee_qty > 0:
                        fee = fee_qty * price
                        order.fees += fee
                        pos.entry_fees += fee
                        self._events.append(('fee', fee, order))
                    pos.qty = actual
                except Exception as exc:
                    log.warning('post-fill position read failed for %s: %s', order.symbol, exc)
                if order.stop:
                    self._place_protection(pos)
            return
        self._record_trade(order, price, qty, ts)
        self._cash -= signed * price

    def _place_protection(self, pos: Position) -> None:
        """Crypto has no brackets: rest a stop order at the venue so the stop
        survives our process, the laptop lid and the internet."""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopLimitOrderRequest
        if pos.stop is None:
            return
        try:
            req = StopLimitOrderRequest(symbol=self._venue_symbol(pos.symbol), qty=abs(pos.qty),
                                        side=OrderSide.SELL if pos.qty > 0 else OrderSide.BUY,
                                        time_in_force=TimeInForce.GTC, stop_price=round(pos.stop, 2),
                                        limit_price=round(pos.stop * (0.995 if pos.qty > 0 else 1.005), 2),
                                        client_order_id=f'{pos.entry_order_id}-stop')
            resp = self.client.submit_order(req)
            pos.protection, pos.protection_order_id = 'stop_order', str(resp.id)
            self.orders[f'{pos.entry_order_id}-stop'] = OrderReq(id=f'{pos.entry_order_id}-stop', symbol=pos.symbol,
                                                               side='sell' if pos.qty > 0 else 'buy', qty=abs(pos.qty),
                                                               order_type='stop', leg='exit', strategy_key=pos.strategy_key,
                                                               reason='stop', exit_reason='stop', status='accepted',
                                                               broker_order_id=str(resp.id))
        except Exception as exc:
            log.warning('protective stop failed for %s: %s', pos.symbol, exc)
            pos.protection = 'none'

    def cancel_open_orders(self, symbol: str | None = None) -> int:
        try:
            if symbol is None:
                res = self.client.cancel_orders()
                self._open_orders = []
                return len(res or [])
            n = 0
            for o in self.open_orders_for(symbol) or self._fetch_open_orders():
                if o['symbol'] == symbol:
                    self.client.cancel_order_by_id(o['id'])
                    n += 1
            self._open_orders = [o for o in self._open_orders if o['symbol'] != symbol]
            return n
        except Exception as exc:
            log.warning('cancel failed: %s', exc)
            return 0

    def _confirm_canceled(self, order_ids: list[str]) -> bool:
        deadline = time.time() + 5.0
        pending = set(order_ids)
        while pending and time.time() < deadline:
            for oid in list(pending):
                try:
                    o = self.client.get_order_by_id(oid)
                except Exception:
                    pending.discard(oid)
                    continue
                if _status(o) in ('canceled', 'filled', 'expired', 'rejected', 'done_for_day'):
                    pending.discard(oid)
            if pending:
                time.sleep(0.4)
        return not pending

    def on_bar(self, symbol: str, bar, ts: datetime) -> list:
        self.last_price[symbol] = float(bar.close)
        pos = self._positions.get(symbol)
        if pos is None or pos.qty == 0 or pos.external:
            return []
        pos.bars_held += 1
        pos.last_price = float(bar.close)
        # Targets on crypto are engine-managed (the venue only holds the stop).
        if self._is_crypto(symbol) and not pos.closing:
            hit = evaluate_exit(pos, bar)
            if hit is not None and hit[0] == 'target':
                self.close_position(symbol, hit[1], ts, 'target', f'{pos.entry_order_id or symbol}-target-{int(ts.timestamp())}')
        return []

    def close_position(self, symbol: str, price: float, ts: datetime, reason: str, order_id: str) -> OrderReq | None:
        """One coordinated close: cancel protection → confirm → close the venue quantity."""
        pos = self._positions.get(symbol)
        if pos is None or pos.qty == 0:
            return None
        if pos.closing:
            return None
        pos.closing = True
        try:
            protective = [o['id'] for o in self.open_orders_for(symbol)]
            if pos.protection_order_id and pos.protection_order_id not in protective:
                protective.append(pos.protection_order_id)
            for oid in protective:
                try:
                    self.client.cancel_order_by_id(oid)
                except Exception as exc:
                    log.warning('cancel %s failed: %s', oid, exc)
            if protective and not self._confirm_canceled(protective):
                log.warning('%s: protective orders not confirmed canceled — closing anyway', symbol)
            # The venue's remaining quantity is what we close (a leg may have filled meanwhile).
            try:
                venue = self.client.get_open_position(self._venue_symbol(symbol))
                remaining = abs(float(venue.qty))
            except Exception:
                remaining = 0.0
            if remaining <= 0:
                self._sync_closed_orders()   # a leg closed it: the trade is recorded from the fill
                self._positions.pop(symbol, None)
                return None
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import MarketOrderRequest
            order = OrderReq(id=order_id, symbol=symbol, side='sell' if pos.qty > 0 else 'buy', qty=remaining, leg='exit',
                             strategy_key=pos.strategy_key, reason=reason, decision_price=price, bar_ts=ts,
                             submitted_ts=ts, exit_reason=reason)
            resp = self.client.submit_order(MarketOrderRequest(
                symbol=self._venue_symbol(symbol), qty=remaining, side=OrderSide.SELL if pos.qty > 0 else OrderSide.BUY,
                time_in_force=TimeInForce.GTC if self._is_crypto(symbol) else TimeInForce.DAY, client_order_id=order.id))
            order.status, order.broker_order_id = 'accepted', str(resp.id)
            self.orders[order.id] = order
            self._await_fill(order)
            # Belt and braces: nothing protective may survive the close.
            leftovers = [o for o in self._fetch_open_orders() if o['symbol'] == symbol]
            for o in leftovers:
                try:
                    self.client.cancel_order_by_id(o['id'])
                except Exception as exc:
                    log.warning('leftover cancel failed %s: %s', o['id'], exc)
            if leftovers:
                self._confirm_canceled([o['id'] for o in leftovers])
            return order
        finally:
            if symbol in self._positions:
                self._positions[symbol].closing = False

    def mark(self, prices: dict[str, float]) -> None:
        for s, p in prices.items():
            self.last_price[s] = p
            if s in self._positions:
                self._positions[s].last_price = p

    def drain_events(self) -> list:
        ev, self._events = self._events, []
        return ev
