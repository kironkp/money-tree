"""One engine for backtest, replay and live.

`Engine.process_bar()` is the whole trading decision for one completed bar of
one symbol: broker bookkeeping → rule evaluation → signals → risk gate →
order → protection → time exits → marks. Every trade lives on a *card* that
moves approved → submitted → accepted → filled → protected → closing → closed
(or rejected/canceled/expired), and every symbol keeps a *state* (all rules
with values, thresholds and pass/fail) so the screen can say why nothing
happened. Nothing in here imports Django.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from .broker.base import Broker, OrderReq
from .data import calendar as cal
from .indicators import minutes_to_close as _mtc
from .risk import Decision, RiskConfig, RiskManager
from .strategies.base import Context, PositionView, Rule, Signal, Strategy
from .timeframes import tf_minutes

log = logging.getLogger('moneytree.engine')


@dataclass
class CardState:
    id: str                       # entry client order id — the correlation id
    symbol: str
    strategy_key: str
    side: str
    status: str = 'approved'
    bar_ts: datetime | None = None
    decision_price: float | None = None
    planned_entry: float | None = None
    stop: float | None = None
    target: float | None = None
    qty: float = 0.0
    risk_dollars: float = 0.0
    reward_dollars: float = 0.0
    expected_costs: float = 0.0
    reward_risk: float = 0.0
    reason: str = ''
    rules: list = field(default_factory=list)
    broker_order_id: str = ''
    protection: str = 'none'
    protection_order_id: str = ''
    filled_qty: float = 0.0
    avg_fill: float | None = None
    fees: float = 0.0
    slippage_bps: float | None = None
    exit_reason: str = ''
    exit_price: float | None = None
    gross_pnl: float | None = None
    net_pnl: float | None = None
    error: str = ''
    approval_expires_at: datetime | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class Recorder:
    """Sink for everything the engine decides. Override what you need."""

    def on_signal(self, sig: Signal, strategy_key: str, decision: Decision | None, order: OrderReq | None) -> None:
        pass

    def on_order(self, order: OrderReq) -> None:
        pass

    def on_fill(self, fill, order: OrderReq) -> None:
        pass

    def on_trade(self, trade) -> None:
        pass

    def on_risk_event(self, kind: str, message: str, ts: datetime, data: dict | None = None) -> None:
        pass

    def on_equity(self, ts: datetime, cash: float, positions_value: float, equity: float, day_pnl: float) -> None:
        pass

    def on_card(self, card: CardState, event: str) -> None:
        pass

    def on_symbol_state(self, state: dict) -> None:
        pass


class MemoryRecorder(Recorder):
    def __init__(self):
        self.signals: list = []
        self.orders: list = []
        self.fills: list = []
        self.trades: list = []
        self.risk_events: list = []
        self.equity: list = []
        self.cards: dict = {}

    def on_signal(self, sig, strategy_key, decision, order):
        self.signals.append((sig, strategy_key, decision, order))

    def on_order(self, order):
        self.orders.append(order)

    def on_fill(self, fill, order):
        self.fills.append(fill)

    def on_trade(self, trade):
        self.trades.append(trade)

    def on_risk_event(self, kind, message, ts, data=None):
        self.risk_events.append((kind, message, ts, data or {}))

    def on_equity(self, ts, cash, positions_value, equity, day_pnl):
        self.equity.append((ts, cash, positions_value, equity, day_pnl))

    def on_card(self, card, event):
        self.cards[card.id] = card.as_dict()


@dataclass
class EngineConfig:
    timeframe: str = '5Min'
    mode: str = 'sim'
    asset_classes: dict = field(default_factory=dict)   # symbol -> stock/etf/crypto
    risk: RiskConfig = field(default_factory=RiskConfig)
    flatten_intraday: bool = True
    # Per-strategy allocation caps (% of equity) — 100 = uncapped.
    allocations: dict = field(default_factory=dict)
    # Per-strategy symbol lists (portfolio runs); empty = every symbol.
    strategy_symbols: dict = field(default_factory=dict)
    # Live confirmation: entries wait for an operator instead of going straight out.
    confirm_entries: bool = False
    confirm_minutes: int = 3
    data_source: str = ''


class Engine:
    def __init__(self, strategies: list[Strategy], broker: Broker, cfg: EngineConfig,
                 recorder: Recorder | None = None, risk: RiskManager | None = None, narrator=None):
        self.strategies = strategies
        self.broker = broker
        self.cfg = cfg
        self.rec = recorder or Recorder()
        self.risk = risk or RiskManager(cfg.risk)
        self.narrator = narrator  # None → silent (backtests)
        self.tfm = tf_minutes(cfg.timeframe)
        self.current_day = None
        self.bars_with_position = 0
        self.bars_seen = 0
        self.last_acted: dict[tuple[str, str], datetime] = {}  # (strategy, symbol) -> bar ts
        self.cards: dict[str, CardState] = {}
        self.card_by_symbol: dict[str, str] = {}
        self.names = {s.key: getattr(s, 'name', s.key) for s in strategies}

    # --- helpers ----------------------------------------------------------
    @property
    def observing(self) -> bool:
        return self.narrator is not None

    def asset_class(self, symbol: str) -> str:
        return self.cfg.asset_classes.get(symbol, 'stock')

    def order_id(self, strategy_key: str, symbol: str, ts: datetime, leg: str) -> str:
        return f'mt-{self.cfg.mode}-{strategy_key}-{symbol.replace("/", "")}-{int(ts.timestamp())}-{leg}'

    def say(self, level: str, text: str, symbol: str = '', strategy_key: str = '', ts=None, data=None,
            phase: str = '', card: CardState | None = None) -> None:
        if self.narrator is not None:
            self.narrator.say(level, text, symbol=symbol, strategy_key=strategy_key, bar_ts=ts, data=data,
                              phase=phase, card_id=card.id if card else '')

    def _card_event(self, card: CardState, event: str) -> None:
        self.rec.on_card(card, event)

    def _day_of(self, ts: datetime, asset_class: str):
        return cal.session_date(ts) if asset_class != 'crypto' else ts.astimezone(cal.ET).date()

    def start_day_if_new(self, ts: datetime, asset_class: str = 'stock') -> bool:
        d = self._day_of(ts, asset_class)
        if d != self.current_day:
            self.current_day = d
            if self.risk.day.date != d:
                self.risk.new_day(d, self.broker.account().equity)
            return True
        return False

    def position_view(self, symbol: str, strategy_key: str | None = None) -> PositionView | None:
        """A strategy only ever sees its own position."""
        pos = self.broker.positions.get(symbol)
        if pos is None or pos.qty == 0 or pos.external:
            return None
        if strategy_key is not None and pos.strategy_key and pos.strategy_key != strategy_key:
            return None
        return PositionView(qty=pos.qty, avg_price=pos.avg_price, entry_ts=pos.entry_ts,
                            bars_held=pos.bars_held, stop=pos.stop, target=pos.target)

    def strategy_exposure(self, strategy_key: str) -> float:
        return sum(abs(p.market_value()) for p in self.broker.positions.values()
                   if p.qty and p.strategy_key == strategy_key)

    # --- broker events → cards, feed, recorder ------------------------------
    def _emit_broker_events(self) -> None:
        for kind, obj, order in self.broker.drain_events():
            if kind == 'fill':
                self.rec.on_fill(obj, order)
                self.rec.on_order(order)
                self._on_fill(obj, order)
            elif kind == 'trade':
                self.rec.on_trade(obj)
                self._on_trade(obj, order)

    def _on_fill(self, fill, order: OrderReq) -> None:
        card = self.cards.get(order.id) if order.leg == 'entry' else self.cards.get(self.card_by_symbol.get(order.symbol, ''))
        slip = f', slippage {fill.slippage_bps:+.1f} bps' if fill.slippage_bps is not None else ''
        partial = ' (partial — liquidity cap)' if order.status == 'canceled' and order.filled_qty else ''
        remaining = max(0.0, order.qty - order.filled_qty)
        self.say('fill', f'FILLED {order.side} {fill.qty:g} {order.symbol} @ {fill.price:,.2f}{slip}{partial}, fees {fill.fee:,.2f}'
                 + (f', {remaining:g} remaining' if remaining > 1e-9 and order.status not in ('canceled', 'filled') else '')
                 + f' — {order.leg}: {order.reason}', order.symbol, order.strategy_key, fill.ts,
                 {'qty': fill.qty, 'price': fill.price, 'side': order.side, 'fee': fill.fee, 'slippage_bps': fill.slippage_bps,
                  'order_id': order.id, 'broker_order_id': order.broker_order_id}, phase='fill', card=card)
        if card is None or order.leg != 'entry':
            return
        card.filled_qty = order.filled_qty
        card.avg_fill = order.filled_avg_price
        card.fees = order.fees
        card.slippage_bps = fill.slippage_bps
        card.broker_order_id = order.broker_order_id or card.broker_order_id
        if order.status == 'filled' or (order.status == 'canceled' and order.filled_qty > 0):
            card.status = 'filled'
            card.opened_at = fill.ts
            kind, oid = self.broker.protection_for(order.symbol)
            card.protection, card.protection_order_id = kind, oid
            if kind != 'none':
                card.status = 'protected'
                self.say('order', f'PROTECTED {order.symbol}: {self._protection_text(kind)} — stop {card.stop:,.2f}, target '
                         f'{card.target:,.2f}' if card.stop and card.target else f'PROTECTED {order.symbol}: {self._protection_text(kind)}',
                         order.symbol, order.strategy_key, fill.ts, phase='manage', card=card)
            else:
                self.rec.on_risk_event('unprotected', f'{order.symbol} is open with NO exit protection', fill.ts,
                                       {'order_id': order.id})
                self.say('risk', f'UNPROTECTED {order.symbol}: the position is open but no stop is in place', order.symbol,
                         order.strategy_key, fill.ts, phase='alert', card=card)
        else:
            card.status = 'partially_filled'
        self._card_event(card, 'fill')

    @staticmethod
    def _protection_text(kind: str) -> str:
        return {'engine': 'stop and target watched by the engine every bar', 'bracket': 'bracket legs resting at the broker',
                'stop_order': 'stop order resting at the broker'}.get(kind, kind)

    def _on_trade(self, trade, order: OrderReq) -> None:
        card = self.cards.get(trade.entry_order_id) or self.cards.get(self.card_by_symbol.get(trade.symbol, ''))
        gross = trade.pnl + trade.fees
        self.say('trade', f'CLOSED {trade.symbol} {trade.side} {trade.qty:g}: {trade.entry_price:,.2f} → {trade.exit_price:,.2f}, '
                 f'gross {gross:+,.2f}, fees {trade.fees:,.2f}, net {trade.pnl:+,.2f} ({trade.pnl_pct:+.2f}%) after {trade.bars_held} bars — '
                 f'{trade.exit_reason}', trade.symbol, trade.strategy_key, trade.exit_ts,
                 {'pnl': trade.pnl, 'gross': gross, 'fees': trade.fees, 'exit_reason': trade.exit_reason,
                  'exit_price': trade.exit_price}, phase='close', card=card)
        if card is not None:
            card.status = 'closed'
            card.exit_reason = trade.exit_reason
            card.exit_price = trade.exit_price
            card.gross_pnl = gross
            card.net_pnl = trade.pnl
            card.closed_at = trade.exit_ts
            self._card_event(card, 'close')
            self.card_by_symbol.pop(trade.symbol, None)

    # --- the decision for one bar ----------------------------------------
    def process_bar(self, symbol: str, ts: datetime, bar, rows_by_strategy: dict, frames_by_strategy: dict,
                    i: int, minutes_to_close: float | None, act: bool = True) -> None:
        """The whole decision for one completed bar of one symbol."""
        asset_class = self.asset_class(symbol)
        self.start_day_if_new(ts, asset_class)
        self.broker.on_bar(symbol, bar, ts)
        self._emit_broker_events()
        self.bars_seen += 1
        pos = self.broker.positions.get(symbol)
        if pos is not None and pos.qty != 0:
            self.bars_with_position += 1
        acct = self.broker.account()
        if not self.risk.day.halted and self.risk.daily_loss_breached(acct.equity):
            self.risk.halt('daily loss limit')
            self.rec.on_risk_event('daily_loss', f'daily loss limit hit: {self.risk.day_pnl(acct.equity):+.2f}', ts,
                                   {'equity': acct.equity, 'start': self.risk.day.start_equity})
            self.say('risk', f'DAILY LOSS LIMIT hit ({self.risk.day_pnl(acct.equity):+,.2f}) — flattening everything, '
                     'no more entries today', ts=ts, phase='alert')
            self.flatten_all(ts, 'kill')
        mtc_val = None if (minutes_to_close is None or minutes_to_close != minutes_to_close) else float(minutes_to_close)
        evaluated: list[tuple[str, list[Rule]]] = []
        fired = False
        for strat in (self.strategies if act else ()):
            row = rows_by_strategy.get(strat.key)
            if row is None or i < strat.warmup_bars:
                continue
            key = (strat.key, symbol)
            if self.last_acted.get(key) == ts:
                continue  # never act twice on one bar
            self.last_acted[key] = ts
            ctx = Context(symbol=symbol, asset_class=asset_class, timeframe=self.cfg.timeframe, ts=ts,
                          position=self.position_view(symbol, strat.key), bar_pos=int(getattr(row, 'bar_pos', 0)),
                          minutes_to_close=mtc_val)
            rules: list[Rule] = []
            if self.observing:
                try:
                    rules = strat.rules(ctx, row)
                except Exception:
                    rules = []
            try:
                signals = strat.on_bar(ctx, row, frames_by_strategy[strat.key], i)
            except Exception as exc:  # a strategy bug must not kill the loop
                log.exception('strategy %s failed on %s %s', strat.key, symbol, ts)
                self.rec.on_risk_event('error', f'{strat.key} raised {exc!r} on {symbol}', ts)
                self.say('error', f'{strat.key} crashed on {symbol}: {exc!r}', symbol, strat.key, ts, phase='alert')
                continue
            for sig in signals:
                fired = True
                self.handle_signal(sig, strat, ctx, row, rules)
            evaluated.append((strat.key, rules))
        if self.observing and evaluated and not fired:
            self._record_state(symbol, ts, bar, evaluated)
        self._time_exits(symbol, ts, bar, mtc_val)
        self._emit_broker_events()

    # --- symbol state: "why nothing happened" ---------------------------------
    def _record_state(self, symbol: str, ts: datetime, bar, evaluated: list, decision: str = 'wait',
                      blocked: str = '') -> None:
        price = float(bar.close)
        pos = self.broker.positions.get(symbol)
        holding = pos is not None and pos.qty != 0
        parts = []
        rules_out = []
        best = 0.0
        for key, rules in evaluated:
            if not rules:
                continue
            rules_out.extend(r.as_dict(key) for r in rules)
            passed = sum(1 for r in rules if r.ok)
            best = max(best, passed / len(rules))
            parts.append(f'{self.names.get(key, key)}: ' + '; '.join(r.text for r in rules) + '.')
        when = ts.astimezone(cal.ET).strftime('%H:%M')
        head = f'{when} {symbol} bar closed at {price:,.2f}.'
        if holding:
            d_stop = f' stop {pos.stop:,.2f} ({(pos.stop / price - 1) * 100:+.2f}%)' if pos.stop else ''
            d_tgt = f', target {pos.target:,.2f} ({(pos.target / price - 1) * 100:+.2f}%)' if pos.target else ''
            tail = f' Holding {pos.side} {abs(pos.qty):g} from {pos.avg_price:,.2f} ({pos.unrealized(price):+,.2f}):{d_stop}{d_tgt}, {pos.bars_held} bars.'
            decision = 'holding'
        elif blocked:
            tail = f' Blocked: {blocked}.'
            decision = 'blocked'
        else:
            tail = ' No trade.'
        summary = head + ' ' + ' '.join(parts) + tail
        self.say('bar', summary, symbol, '', ts, {'price': price, 'rules': rules_out, 'decision': decision}, phase='evaluate')
        self.rec.on_symbol_state({'symbol': symbol, 'bar_ts': ts, 'price': price, 'source': self.cfg.data_source,
                                  'decision': decision, 'summary': summary[:600], 'rules': rules_out,
                                  'proximity': 1.0 if holding else best})

    # --- signals ---------------------------------------------------------------
    def handle_signal(self, sig: Signal, strat: Strategy, ctx: Context, bar, rules: list | None = None) -> None:
        symbol = sig.symbol
        pos = self.broker.positions.get(symbol)
        if sig.action == 'close':
            if pos is None or pos.qty == 0 or pos.external:
                self.rec.on_signal(sig, strat.key, Decision(False, reason='no position'), None)
                return
            if pos.strategy_key and pos.strategy_key != strat.key:
                self.rec.on_signal(sig, strat.key, Decision(False, reason=f'position belongs to {pos.strategy_key}'), None)
                self.say('bar', f'{strat.key} wanted to close {symbol} but {pos.strategy_key} owns that position — ignored',
                         symbol, strat.key, sig.ts, phase='decide')
                return
            if pos.closing:
                self.rec.on_signal(sig, strat.key, Decision(False, reason='exit already in flight'), None)
                return
            card = self.cards.get(self.card_by_symbol.get(symbol, ''))
            self.say('signal', f'EXIT {symbol} ({strat.key}): {sig.reason} — closing {abs(pos.qty):g} at market', symbol,
                     strat.key, sig.ts, phase='close', card=card)
            side = 'sell' if pos.qty > 0 else 'buy'
            order = OrderReq(id=self.order_id(strat.key, symbol, sig.ts, 'exit'), symbol=symbol, side=side,
                             qty=abs(pos.qty), leg='exit', strategy_key=strat.key, reason=sig.reason,
                             decision_price=sig.price, bar_ts=sig.ts, submitted_ts=sig.ts, exit_reason='signal')
            self.broker.submit(order)
            self.rec.on_order(order)
            self.rec.on_signal(sig, strat.key, Decision(order.status != 'rejected', qty=abs(pos.qty),
                                                        reason=sig.reason if order.status != 'rejected' else order.error), order)
            if card is not None and order.status != 'rejected':
                card.status = 'closing'
                self._card_event(card, 'closing')
            self._emit_broker_events()
            return
        acct = self.broker.account()
        decision = self.risk.evaluate(sig, ctx, acct, self.broker.positions, ctx.asset_class,
                                      strategy_supports=strat.supports(ctx.asset_class),
                                      allocation_pct=float(self.cfg.allocations.get(strat.key, 100.0)),
                                      strategy_exposure=self.strategy_exposure(strat.key))
        if not decision.allowed:
            self.rec.on_signal(sig, strat.key, decision, None)
            self.say('signal', f'BLOCKED {sig.action.upper()} {symbol} ({strat.key}): {sig.reason} — {decision.reason}',
                     symbol, strat.key, sig.ts, {'blocked': decision.reason, 'rules': [r.as_dict(strat.key) for r in (rules or [])]},
                     phase='decide')
            if self.observing:
                self._record_state(symbol, sig.ts, bar, [(strat.key, rules or [])], blocked=decision.reason)
            return
        side_word = 'long' if sig.action == 'buy' else 'short'
        price = float(sig.price)
        stop_dist = abs(price - sig.stop) if sig.stop else price * self.cfg.risk.default_stop_pct / 100
        reward = abs(sig.target - price) * decision.qty if sig.target else 0.0
        notional = price * decision.qty
        costs = notional * self.cfg.risk.round_trip_cost_pct(ctx.asset_class) / 100
        card = CardState(id=self.order_id(strat.key, symbol, sig.ts, 'entry'), symbol=symbol, strategy_key=strat.key,
                         side=side_word, bar_ts=sig.ts, decision_price=price, planned_entry=price, stop=sig.stop,
                         target=sig.target, qty=decision.qty, risk_dollars=stop_dist * decision.qty, reward_dollars=reward,
                         expected_costs=costs, reward_risk=(reward / (stop_dist * decision.qty)) if stop_dist and decision.qty else 0.0,
                         reason=sig.reason, rules=[r.as_dict(strat.key) for r in (rules or [])])
        self.cards[card.id] = card
        self.card_by_symbol[symbol] = card.id
        stop_txt = f'stop {sig.stop:,.2f}' if sig.stop else 'no stop'
        tgt_txt = f'target {sig.target:,.2f}' if sig.target else 'no target'
        self.say('signal', f'{self.names.get(strat.key, strat.key)} entry approved for {symbol} ({side_word}): {sig.reason}. '
                 f'Planned entry {price:,.2f}, {stop_txt}, {tgt_txt}.', symbol, strat.key, sig.ts,
                 {'price': price, 'stop': sig.stop, 'target': sig.target, 'rules': card.rules}, phase='decide', card=card)
        self.say('signal', f'Size: {decision.qty:g} {symbol} (≈ {notional:,.2f}); planned loss at stop {card.risk_dollars:,.2f}, '
                 f'expected reward {reward:,.2f} before costs (≈ {costs:,.2f}); reward:risk {card.reward_risk:.1f}. '
                 f'Sized by {decision.reason}; equity {acct.equity:,.2f}.', symbol, strat.key, sig.ts,
                 {'qty': decision.qty, 'notional': notional, 'risk': card.risk_dollars, 'reward': reward, 'costs': costs,
                  'equity': acct.equity}, phase='size', card=card)
        self.rec.on_signal(sig, strat.key, decision, None)
        if self.cfg.confirm_entries:
            card.status = 'awaiting_approval'
            card.approval_expires_at = sig.ts + timedelta(minutes=self.cfg.confirm_minutes)
            self._card_event(card, 'awaiting_approval')
            self.say('order', f'AWAITING APPROVAL: {symbol} entry needs an operator within {self.cfg.confirm_minutes} min '
                     '(live_confirm_orders is on).', symbol, strat.key, sig.ts, phase='submit', card=card)
            return
        self._card_event(card, 'approved')
        self.submit_card(card.id, sig.ts)

    def submit_card(self, card_id: str, ts: datetime) -> OrderReq | None:
        """Send an approved card's entry to the broker (also used for operator approvals)."""
        card = self.cards.get(card_id)
        if card is None or card.status not in ('approved', 'awaiting_approval'):
            return None
        side = 'buy' if card.side == 'long' else 'sell'
        order = OrderReq(id=card.id, symbol=card.symbol, side=side, qty=card.qty, leg='entry', strategy_key=card.strategy_key,
                         reason=card.reason, decision_price=card.decision_price, bar_ts=card.bar_ts, submitted_ts=ts,
                         stop=card.stop, target=card.target)
        self.broker.submit(order)
        card.broker_order_id = order.broker_order_id
        if order.status == 'rejected':
            card.status, card.error = 'rejected', order.error
            self.rec.on_order(order)
            self._card_event(card, 'rejected')
            self.say('error', f'Broker rejected {side} {card.symbol}: {order.error}', card.symbol, card.strategy_key, ts,
                     phase='submit', card=card)
            self.card_by_symbol.pop(card.symbol, None)
            return order
        self.risk.record_entry()
        if card.status != 'filled':
            card.status = 'submitted' if order.status in ('new', 'accepted') else ('filled' if order.status == 'filled' else card.status)
        if order.status in ('new', 'accepted'):
            self.say('order', f'Order submitted: {side} {card.qty:g} {card.symbol} at market (id {card.id}) — waiting for the broker.',
                     card.symbol, card.strategy_key, ts, {'order_id': card.id, 'broker_order_id': order.broker_order_id},
                     phase='submit', card=card)
        if self.asset_class(card.symbol) == 'crypto' and self.cfg.risk.max_hold_minutes:
            p = self.broker.positions.get(card.symbol)
            if p is not None:
                p.max_hold_until = ts + timedelta(minutes=self.cfg.risk.max_hold_minutes)
        self.rec.on_order(order)
        self._card_event(card, 'submitted')
        self._emit_broker_events()
        return order

    def expire_cards(self, now: datetime) -> int:
        n = 0
        for card in self.cards.values():
            if card.status == 'awaiting_approval' and card.approval_expires_at and now >= card.approval_expires_at:
                card.status = 'expired'
                self._card_event(card, 'expired')
                self.card_by_symbol.pop(card.symbol, None)
                self.say('order', f'Approval window passed for {card.symbol} — entry expired.', card.symbol, card.strategy_key,
                         now, phase='submit', card=card)
                n += 1
        return n

    # --- exits -----------------------------------------------------------------
    def _close(self, symbol: str, price: float, ts: datetime, reason: str, note: str) -> OrderReq | None:
        pos = self.broker.positions.get(symbol)
        if pos is None or pos.qty == 0 or pos.external or pos.closing:
            return None
        card = self.cards.get(self.card_by_symbol.get(symbol, ''))
        self.say('order', note, symbol, pos.strategy_key, ts, phase='close', card=card)
        if card is not None:
            card.status = 'closing'
            self._card_event(card, 'closing')
        return self.broker.close_position(symbol, price, ts, reason, self.order_id(pos.strategy_key, symbol, ts, reason))

    def _time_exits(self, symbol: str, ts: datetime, bar, minutes_to_close: float | None) -> None:
        pos = self.broker.positions.get(symbol)
        if pos is None or pos.qty == 0 or pos.external:
            return
        price = float(bar.close)
        if minutes_to_close is not None:
            if self.cfg.flatten_intraday and minutes_to_close <= self.cfg.risk.flat_before_close_min + self.tfm:
                self._close(symbol, price, ts, 'eod', f'END OF DAY — closing {symbol} at {price:,.2f} ({minutes_to_close:.0f} min to the close)')
        else:
            hold_limit = pos.max_hold_until
            if hold_limit is None and self.cfg.risk.max_hold_minutes:
                hold_limit = pos.entry_ts + timedelta(minutes=self.cfg.risk.max_hold_minutes)
            if hold_limit is not None and ts >= hold_limit:
                self._close(symbol, price, ts, 'time', f'MAX HOLD reached — closing {symbol} at {price:,.2f}')
        self._emit_broker_events()

    def flatten_all(self, ts: datetime, reason: str, prices: dict[str, float] | None = None) -> int:
        n = 0
        self.broker.cancel_open_orders()
        live = [p for p in self.broker.positions.values() if p.qty and not p.external]
        if live:
            self.say('order', f'FLATTEN ({reason}): closing {len(live)} position(s): ' + ', '.join(p.symbol for p in live), ts=ts,
                     phase='close')
        for pos in list(live):
            price = (prices or {}).get(pos.symbol, pos.last_price or pos.avg_price)
            pos.closing = False  # a flatten overrides any in-flight exit
            card = self.cards.get(self.card_by_symbol.get(pos.symbol, ''))
            if card is not None:
                card.status = 'closing'
                self._card_event(card, 'closing')
            if self.broker.close_position(pos.symbol, price, ts, reason, self.order_id(pos.strategy_key, pos.symbol, ts, reason)):
                n += 1
        for card in self.cards.values():
            if card.status in ('awaiting_approval', 'approved'):
                card.status = 'canceled'
                self._card_event(card, 'canceled')
                self.card_by_symbol.pop(card.symbol, None)
        self._emit_broker_events()
        return n

    def record_equity(self, ts: datetime) -> None:
        a = self.broker.account()
        self.rec.on_equity(ts, a.cash, a.positions_value, a.equity, self.risk.day_pnl(a.equity))

    # --- historical driver -------------------------------------------------
    def prepare_frames(self, frames: dict[str, pd.DataFrame]) -> dict[str, dict[str, pd.DataFrame]]:
        out: dict[str, dict[str, pd.DataFrame]] = {}
        for symbol, df in frames.items():
            ac = self.asset_class(symbol)
            out[symbol] = {}
            for strat in self.strategies:
                if not strat.supports(ac):
                    continue
                allowed = self.cfg.strategy_symbols.get(strat.key)
                if allowed and symbol not in allowed:
                    continue
                out[symbol][strat.key] = strat.prepare(df, ac, self.cfg.timeframe)
        return out

    def run_frames(self, frames: dict[str, pd.DataFrame], equity_every_bar: bool = True,
                   act_from: datetime | None = None) -> None:
        """Backtest driver: bars of all symbols in time order, one session at a time.
        Bars before `act_from` are warm-up: they feed indicators and marks but
        no strategy acts on them."""
        prepared = self.prepare_frames(frames)
        rows: dict[str, dict[str, list]] = {}
        mtc: dict[str, list] = {}
        sessions: dict[str, list] = {}
        for symbol, per_strat in prepared.items():
            rows[symbol] = {k: list(df.itertuples(index=True)) for k, df in per_strat.items()}
            base = frames[symbol]
            ac = self.asset_class(symbol)
            mtc[symbol] = _mtc(base.index, ac).tolist()
            sessions[symbol] = [str(x) for x in (base.index.tz_convert(cal.ET).date if ac != 'crypto' else base.index.tz_convert('UTC').date)]
        events = []
        for symbol, df in frames.items():
            if symbol not in prepared or not prepared[symbol]:
                continue
            for i, ts in enumerate(df.index):
                events.append((ts.to_pydatetime(), symbol, i))
        events.sort(key=lambda e: (e[0], e[1]))
        prev_session: dict[str, str] = {}
        last_ts = None
        for ts, symbol, i in events:
            sess = sessions[symbol][i]
            if prev_session.get(symbol) not in (None, sess):
                pos = self.broker.positions.get(symbol)
                if pos is not None and pos.qty != 0 and self.cfg.flatten_intraday and self.asset_class(symbol) != 'crypto':
                    prev_bar = next(iter(rows[symbol].values()))[i - 1]
                    self._close(symbol, float(prev_bar.close), prev_bar.Index.to_pydatetime(), 'eod',
                                f'END OF DAY — closing {symbol} at the last bar')
                    self._emit_broker_events()
                for strat in self.strategies:
                    strat.on_session_end(symbol)
            prev_session[symbol] = sess
            per_strat_rows = rows[symbol]
            bar = next(iter(per_strat_rows.values()))[i]
            self.process_bar(symbol, ts, bar, {k: r[i] for k, r in per_strat_rows.items()}, prepared[symbol], i, mtc[symbol][i],
                             act=(act_from is None or ts >= act_from))
            if equity_every_bar and ts != last_ts:
                self.record_equity(ts)
            last_ts = ts
        if last_ts is not None:
            self.flatten_all(last_ts, 'end')
            self.record_equity(last_ts)
