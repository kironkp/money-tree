"""One engine for backtest, replay and live.

`Engine.process_bar()` is the whole trading decision for one completed bar of
one symbol: broker bookkeeping → strategy signals → risk gate → orders → time
exits → marks. `run_frames()` drives it over historical frames; the agent
loop drives it one bar at a time. Nothing in here imports Django.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from .broker.base import Broker, OrderReq, Position
from .data import calendar as cal
from .indicators import minutes_to_close as _mtc
from .risk import Decision, RiskConfig, RiskManager
from .strategies.base import Context, PositionView, Signal, Strategy
from .timeframes import tf_minutes

log = logging.getLogger('moneytree.engine')


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


class MemoryRecorder(Recorder):
    def __init__(self):
        self.signals: list = []
        self.orders: list = []
        self.fills: list = []
        self.trades: list = []
        self.risk_events: list = []
        self.equity: list = []

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


@dataclass
class EngineConfig:
    timeframe: str = '5Min'
    mode: str = 'sim'
    asset_classes: dict = field(default_factory=dict)   # symbol -> stock/etf/crypto
    risk: RiskConfig = field(default_factory=RiskConfig)
    flatten_intraday: bool = True


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

    # --- helpers ----------------------------------------------------------
    def asset_class(self, symbol: str) -> str:
        return self.cfg.asset_classes.get(symbol, 'stock')

    def order_id(self, strategy_key: str, symbol: str, ts: datetime, leg: str) -> str:
        return f'mt-{self.cfg.mode}-{strategy_key}-{symbol.replace("/", "")}-{int(ts.timestamp())}-{leg}'

    def say(self, level: str, text: str, symbol: str = '', strategy_key: str = '', ts=None, data=None) -> None:
        if self.narrator is not None:
            self.narrator.say(level, text, symbol=symbol, strategy_key=strategy_key, ts=ts, data=data)

    def _emit_broker_events(self) -> None:
        for kind, obj, order in self.broker.drain_events():
            if kind == 'fill':
                self.rec.on_fill(obj, order)
                self.rec.on_order(order)
                if self.narrator is not None:
                    slip = f', slippage {obj.slippage_bps:+.1f} bps' if obj.slippage_bps is not None else ''
                    partial = ' (partial — liquidity cap)' if order.status == 'canceled' and order.filled_qty else ''
                    self.say('fill', f'FILLED {order.side} {obj.qty:g} {order.symbol} @ {obj.price:,.2f}'
                             f'{slip}{partial} — {order.leg}: {order.reason}', order.symbol, order.strategy_key, obj.ts,
                             {'qty': obj.qty, 'price': obj.price, 'side': order.side})
            elif kind == 'trade':
                self.rec.on_trade(obj)
                if self.narrator is not None:
                    self.say('trade', f'CLOSED {obj.symbol} {obj.side} {obj.qty:g} @ {obj.entry_price:,.2f} → '
                             f'{obj.exit_price:,.2f} = {obj.pnl:+,.2f} ({obj.pnl_pct:+.2f}%) after {obj.bars_held} bars — '
                             f'{obj.exit_reason}', obj.symbol, obj.strategy_key, obj.exit_ts,
                             {'pnl': obj.pnl, 'exit_reason': obj.exit_reason})

    def _day_of(self, ts: datetime, asset_class: str):
        return cal.session_date(ts) if asset_class != 'crypto' else ts.astimezone(cal.ET).date()

    def start_day_if_new(self, ts: datetime, asset_class: str = 'stock') -> bool:
        d = self._day_of(ts, asset_class)
        if d != self.current_day:
            self.current_day = d
            self.risk.new_day(d, self.broker.account().equity)
            return True
        return False

    def position_view(self, symbol: str) -> PositionView | None:
        pos = self.broker.positions.get(symbol)
        if pos is None or pos.qty == 0 or pos.external:
            return None
        return PositionView(qty=pos.qty, avg_price=pos.avg_price, entry_ts=pos.entry_ts,
                            bars_held=pos.bars_held, stop=pos.stop, target=pos.target)

    # --- the decision for one bar ----------------------------------------
    def process_bar(self, symbol: str, ts: datetime, bar, rows_by_strategy: dict, frames_by_strategy: dict,
                    i: int, minutes_to_close: float | None, act: bool = True) -> None:
        """The whole decision for one completed bar of one symbol.

        `rows_by_strategy[key]` is that strategy's prepared row (an itertuples
        row) and `frames_by_strategy[key]` its prepared frame; `bar` is any
        row with open/high/low/close/volume for the broker.
        """
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
                     'no more entries today', ts=ts)
            self.flatten_all(ts, 'kill')
        mtc_val = None if (minutes_to_close is None or minutes_to_close != minutes_to_close) else float(minutes_to_close)
        thoughts: list[str] = []
        for strat in (self.strategies if act else ()):
            row = rows_by_strategy.get(strat.key)
            if row is None or i < strat.warmup_bars:
                continue
            key = (strat.key, symbol)
            if self.last_acted.get(key) == ts:
                continue  # never act twice on one bar
            self.last_acted[key] = ts
            ctx = Context(symbol=symbol, asset_class=asset_class, timeframe=self.cfg.timeframe, ts=ts,
                          position=self.position_view(symbol), bar_pos=int(getattr(row, 'bar_pos', 0)),
                          minutes_to_close=mtc_val)
            try:
                signals = strat.on_bar(ctx, row, frames_by_strategy[strat.key], i)
            except Exception as exc:  # a strategy bug must not kill the loop
                log.exception('strategy %s failed on %s %s', strat.key, symbol, ts)
                self.rec.on_risk_event('error', f'{strat.key} raised {exc!r} on {symbol}', ts)
                self.say('error', f'{strat.key} crashed on {symbol}: {exc!r}', symbol, strat.key, ts)
                continue
            for sig in signals:
                self.handle_signal(sig, strat, ctx, row)
            if self.narrator is not None and not signals:
                try:
                    note = strat.explain(ctx, row)
                except Exception:
                    note = ''
                if note:
                    thoughts.append(f'{strat.key}: {note}')
        if self.narrator is not None and thoughts:
            self.say('bar', f'{symbol} {float(bar.close):,.2f} · ' + ' · '.join(thoughts), symbol, '', ts)
        self._time_exits(symbol, ts, bar, mtc_val)
        self._emit_broker_events()

    def handle_signal(self, sig: Signal, strat: Strategy, ctx: Context, bar) -> None:
        symbol = sig.symbol
        pos = self.broker.positions.get(symbol)
        if sig.action == 'close':
            if pos is None or pos.qty == 0 or pos.external:
                self.rec.on_signal(sig, strat.key, Decision(False, reason='no position'), None)
                return
            self.say('signal', f'EXIT {symbol} ({strat.key}): {sig.reason}', symbol, strat.key, sig.ts)
            side = 'sell' if pos.qty > 0 else 'buy'
            order = OrderReq(id=self.order_id(strat.key, symbol, sig.ts, 'exit'), symbol=symbol, side=side,
                             qty=abs(pos.qty), leg='exit', strategy_key=strat.key, reason=sig.reason,
                             decision_price=sig.price, bar_ts=sig.ts, submitted_ts=sig.ts, exit_reason='signal')
            self.broker.submit(order)
            self.rec.on_order(order)
            self.rec.on_signal(sig, strat.key, Decision(True, qty=abs(pos.qty), reason=sig.reason), order)
            self._emit_broker_events()
            return
        acct = self.broker.account()
        decision = self.risk.evaluate(sig, ctx, acct, self.broker.positions, ctx.asset_class,
                                      strategy_supports=strat.supports(ctx.asset_class))
        if not decision.allowed:
            self.rec.on_signal(sig, strat.key, decision, None)
            self.say('signal', f'SKIP {sig.action.upper()} {symbol} ({strat.key}): {sig.reason} — blocked: {decision.reason}',
                     symbol, strat.key, sig.ts, {'blocked': decision.reason})
            return
        levels = ''
        if sig.stop is not None:
            levels += f' · stop {sig.stop:,.2f}'
        if sig.target is not None:
            levels += f' · target {sig.target:,.2f}'
        self.say('signal', f'{sig.action.upper()} {symbol} {decision.qty:g} @ ~{sig.price:,.2f} ({strat.key}): {sig.reason}'
                 f'{levels} — sized by {decision.reason}', symbol, strat.key, sig.ts,
                 {'qty': decision.qty, 'price': sig.price, 'stop': sig.stop, 'target': sig.target})
        side = 'buy' if sig.action == 'buy' else 'sell'
        order = OrderReq(id=self.order_id(strat.key, symbol, sig.ts, 'entry'), symbol=symbol, side=side,
                         qty=decision.qty, leg='entry', strategy_key=strat.key, reason=sig.reason,
                         decision_price=sig.price, bar_ts=sig.ts, submitted_ts=sig.ts, stop=sig.stop, target=sig.target)
        self.broker.submit(order)
        if order.status == 'rejected':
            self.rec.on_signal(sig, strat.key, Decision(False, reason=f'broker rejected: {order.error}'), order)
            self.rec.on_order(order)
            self.say('error', f'Broker rejected {sig.action} {symbol}: {order.error}', symbol, strat.key, sig.ts)
            return
        self.risk.record_entry()
        if ctx.asset_class == 'crypto' and self.cfg.risk.max_hold_minutes:
            p = self.broker.positions.get(symbol)
            if p is not None:
                p.max_hold_until = sig.ts + timedelta(minutes=self.cfg.risk.max_hold_minutes)
            order.max_hold_minutes = self.cfg.risk.max_hold_minutes  # for immediate=False brokers, applied on fill
        self.rec.on_order(order)
        self.rec.on_signal(sig, strat.key, decision, order)
        self._emit_broker_events()

    def _time_exits(self, symbol: str, ts: datetime, bar, minutes_to_close: float | None) -> None:
        pos = self.broker.positions.get(symbol)
        if pos is None or pos.qty == 0 or pos.external:
            return
        price = float(bar.close)
        if minutes_to_close is not None:
            if self.cfg.flatten_intraday and minutes_to_close <= self.cfg.risk.flat_before_close_min + self.tfm:
                self.say('order', f'END OF DAY — closing {symbol} at {price:,.2f} ({minutes_to_close:.0f} min to the close)',
                         symbol, pos.strategy_key, ts)
                self.broker.close_position(symbol, price, ts, 'eod', self.order_id(pos.strategy_key, symbol, ts, 'eod'))
        else:
            hold_limit = pos.max_hold_until
            if hold_limit is None and self.cfg.risk.max_hold_minutes:
                hold_limit = pos.entry_ts + timedelta(minutes=self.cfg.risk.max_hold_minutes)
            if hold_limit is not None and ts >= hold_limit:
                self.say('order', f'MAX HOLD reached — closing {symbol} at {price:,.2f}', symbol, pos.strategy_key, ts)
                self.broker.close_position(symbol, price, ts, 'time', self.order_id(pos.strategy_key, symbol, ts, 'time'))
        self._emit_broker_events()

    def flatten_all(self, ts: datetime, reason: str, prices: dict[str, float] | None = None) -> int:
        n = 0
        self.broker.cancel_open_orders()
        live = [p for p in self.broker.positions.values() if p.qty and not p.external]
        if live:
            self.say('order', f'FLATTEN ({reason}): closing {len(live)} position(s): ' + ', '.join(p.symbol for p in live), ts=ts)
        for symbol, pos in list(self.broker.positions.items()):
            if pos.qty == 0 or pos.external:
                continue
            price = (prices or {}).get(symbol, pos.last_price or pos.avg_price)
            if self.broker.close_position(symbol, price, ts, reason, self.order_id(pos.strategy_key, symbol, ts, reason)):
                n += 1
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
        # Global time order across symbols.
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
                # Session rolled over with a position still open (missing bars
                # near the close): flatten at the previous bar's close.
                pos = self.broker.positions.get(symbol)
                if pos is not None and pos.qty != 0 and self.cfg.flatten_intraday and self.asset_class(symbol) != 'crypto':
                    prev_bar = next(iter(rows[symbol].values()))[i - 1]
                    self.broker.close_position(symbol, float(prev_bar.close), prev_bar.Index.to_pydatetime(), 'eod',
                                               self.order_id(pos.strategy_key, symbol, prev_bar.Index.to_pydatetime(), 'eod'))
                    self._emit_broker_events()
                for strat in self.strategies:
                    strat.on_session_end(symbol)
            prev_session[symbol] = sess
            per_strat_rows = rows[symbol]
            first_rows = next(iter(per_strat_rows.values()))
            bar = first_rows[i]
            self.process_bar(symbol, ts, bar, {k: r[i] for k, r in per_strat_rows.items()}, prepared[symbol], i, mtc[symbol][i],
                             act=(act_from is None or ts >= act_from))
            if equity_every_bar and ts != last_ts:
                self.record_equity(ts)
            last_ts = ts
        if last_ts is not None:
            self.flatten_all(last_ts, 'end')
            self.record_equity(last_ts)
