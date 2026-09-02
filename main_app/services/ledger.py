"""Django side of the engine: the DB recorder and simulator persistence."""
from __future__ import annotations

import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from main_app.models import (Account, EquitySnapshot, Fill, Instrument, Order, OrderStatus, Position, RiskEvent,
                             Signal, Trade)

from .broker.base import Position as SimPosition
from .engine import Recorder

log = logging.getLogger('moneytree.ledger')


def D(x, places='0.0001') -> Decimal:
    if x is None:
        return None
    return Decimal(str(x)).quantize(Decimal(places))


class DBRecorder(Recorder):
    def __init__(self, account: Account, instruments: dict[str, Instrument]):
        self.account = account
        self.instruments = instruments

    def _inst(self, symbol: str) -> Instrument:
        inst = self.instruments.get(symbol)
        if inst is None:
            inst, _ = Instrument.objects.get_or_create(symbol=symbol, defaults={'in_watchlist': False})
            self.instruments[symbol] = inst
        return inst

    def on_order(self, order):
        Order.objects.update_or_create(
            client_order_id=order.id,
            defaults={
                'account': self.account, 'instrument': self._inst(order.symbol), 'strategy_key': order.strategy_key,
                'side': order.side, 'qty': D(order.qty, '0.00000001'), 'order_type': order.order_type,
                'limit_price': D(order.limit_price), 'stop_price': D(order.stop_price),
                'time_in_force': 'gtc' if self._inst(order.symbol).is_crypto else 'day',
                'status': order.status, 'broker_order_id': order.broker_order_id or '', 'leg': order.leg,
                'decision_price': D(order.decision_price), 'bar_ts': order.bar_ts, 'reason': order.reason[:200],
                'submitted_at': order.submitted_ts or timezone.now(), 'filled_at': order.filled_ts,
                'filled_qty': D(order.filled_qty, '0.00000001'), 'filled_avg_price': D(order.filled_avg_price),
                'fees': D(order.fees), 'error': order.error or '',
            },
        )

    def on_signal(self, sig, strategy_key, decision, order):
        order_row = Order.objects.filter(client_order_id=order.id).first() if order is not None else None
        Signal.objects.create(
            account=self.account, strategy_key=strategy_key, instrument=self._inst(sig.symbol), ts=sig.ts,
            action=sig.action, strength=sig.strength, price=D(sig.price), stop_price=D(sig.stop),
            target_price=D(sig.target), reason=sig.reason[:200],
            acted=bool(decision and decision.allowed), blocked_reason=('' if (decision is None or decision.allowed) else decision.reason[:200]),
            order=order_row,
        )

    def on_fill(self, fill, order):
        row = Order.objects.filter(client_order_id=order.id).first()
        if row is None:
            self.on_order(order)
            row = Order.objects.get(client_order_id=order.id)
        Fill.objects.create(order=row, ts=fill.ts, qty=D(fill.qty, '0.00000001'), price=D(fill.price), fee=D(fill.fee),
                            realized_slippage_bps=fill.slippage_bps)

    def on_trade(self, trade):
        Trade.objects.create(
            account=self.account, instrument=self._inst(trade.symbol), strategy_key=trade.strategy_key,
            side=trade.side, qty=D(trade.qty, '0.00000001'), entry_ts=trade.entry_ts, exit_ts=trade.exit_ts,
            entry_price=D(trade.entry_price), exit_price=D(trade.exit_price), pnl=D(trade.pnl, '0.01'),
            pnl_pct=D(trade.pnl_pct, '0.001'), fees=D(trade.fees), bars_held=trade.bars_held,
            exit_reason=trade.exit_reason,
        )

    def on_risk_event(self, kind, message, ts, data=None):
        RiskEvent.objects.create(account=self.account, ts=ts or timezone.now(), kind=kind, message=message[:300],
                                 data=data or {})

    def on_equity(self, ts, cash, positions_value, equity, day_pnl):
        EquitySnapshot.objects.create(account=self.account, ts=ts, cash=D(cash, '0.01'),
                                      positions_value=D(positions_value, '0.01'), equity=D(equity, '0.01'),
                                      day_pnl=D(day_pnl, '0.01'))


def hydrate_broker(account: Account, broker) -> int:
    """Restore the simulator's cash and open positions from the DB."""
    positions = []
    for row in account.positions.select_related('instrument'):
        positions.append(SimPosition(
            symbol=row.instrument.symbol, qty=float(row.qty), avg_price=float(row.avg_price), entry_ts=row.opened_at,
            strategy_key=row.strategy_key, stop=float(row.stop_price) if row.stop_price is not None else None,
            target=float(row.target_price) if row.target_price is not None else None, bars_held=row.bars_held,
            entry_bar_ts=row.entry_bar_ts, entry_fees=float(row.entry_fees),
            last_price=float(row.last_price) if row.last_price is not None else None,
            max_hold_until=row.max_hold_until, external=row.external,
        ))
    broker.hydrate(float(account.cash), positions)
    return len(positions)


@transaction.atomic
def persist_broker(account: Account, broker, instruments: dict[str, Instrument], day_start_reset: bool = False) -> None:
    """Mirror the simulator's ledger into Account/Position rows."""
    acct = broker.account()
    account.cash = D(acct.cash, '0.01')
    account.equity = D(acct.equity, '0.01')
    account.buying_power = D(acct.buying_power, '0.01')
    today = timezone.localdate()
    if account.day_start_date != today or day_start_reset:
        account.day_start_date = today
        account.day_start_equity = account.equity
    account.last_synced_at = timezone.now()
    account.save()
    live = {s: p for s, p in broker.positions.items() if p.qty != 0}
    account.positions.exclude(instrument__symbol__in=list(live)).delete()
    for symbol, p in live.items():
        inst = instruments.get(symbol) or Instrument.objects.get_or_create(symbol=symbol, defaults={'in_watchlist': False})[0]
        Position.objects.update_or_create(
            account=account, instrument=inst,
            defaults={'strategy_key': p.strategy_key, 'qty': D(p.qty, '0.00000001'), 'avg_price': D(p.avg_price),
                      'stop_price': D(p.stop), 'target_price': D(p.target), 'opened_at': p.entry_ts,
                      'entry_bar_ts': p.entry_bar_ts, 'bars_held': p.bars_held, 'max_hold_until': p.max_hold_until,
                      'last_price': D(p.last_price), 'entry_fees': D(p.entry_fees), 'external': p.external},
        )


def open_orders_from_db(account: Account) -> int:
    """Orders left 'accepted' by a crashed run are stale: cancel them."""
    return account.orders.filter(status__in=[OrderStatus.NEW, OrderStatus.ACCEPTED, OrderStatus.PARTIAL]).update(
        status=OrderStatus.CANCELED, error='stale after restart')
