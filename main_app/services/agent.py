"""The agent loop: live (sim / paper / live) and replay.

One process per account, guarded by a file lock. Each tick:
  reconcile with the broker (paper/live) → sync the newest COMPLETED bars →
  engine.process_bar per new bar → persist the ledger and the risk state →
  equity snapshot → heartbeat.
Between ticks the loop polls its controls every 2 s: stop requests, the kill
switch, trading on/off, risk-setting changes and operator approvals, so
"kill" means now, not next bar. Catch-up after a laptop sleep evaluates stops
on the missed bars without opening anything new. An operator Stop flattens
sim/paper; an infrastructure signal preserves positions for a clean restart.
Live follows LIVE_FLATTEN_ON_EXIT.
"""
from __future__ import annotations

import fcntl
import logging
import os
import signal
import time
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta

from django.conf import settings
from django.utils import timezone

from main_app.models import (Account, AgentConfig, AgentRun, FeedEvent, Instrument, Market, Mode, Qualification,
                             RiskEvent, Stage, Strategy, TradeCard)

from .backtest import date_bounds
from .broker.sim import SimBroker
from .data import calendar as cal
from .data import get_provider
from .data.store import complete_bars_only, load_frame, quality_gate, synthetic_symbols, upsert_bars
from .engine import Engine, EngineConfig
from .indicators import minutes_to_close as _mtc
from .ledger import D as D_, DBRecorder, hydrate_broker, hydrate_cards, open_orders_from_db, persist_broker
from .narrator import Narrator
from .risk import RiskConfig, RiskManager
from .strategies import make_strategy
from .timeframes import floor_to_bar, tf_delta, tf_minutes

log = logging.getLogger('moneytree.agent')

WINDOW_BARS = 420          # trailing bars loaded per symbol each tick
# Seconds after a bar closes before it is read. Probe 2026-09-02: Alpaca IEX
# 5-minute bars were final 5 s after the close; 10 s leaves a margin. Yahoo is
# simply late.
GRACE_S = {'alpaca': 10, 'yahoo': 20, 'synthetic': 1}
REFRESH_BARS = 3           # re-fetch this many recent bars each tick so revisions replace partial ones
SNAPSHOT_EVERY = timedelta(minutes=5)
CONTROL_POLL_S = 2
STRATEGY_CHECK_S = 60
STAGE_FOR_MODE = {
    Mode.SIM: (Stage.SPROUT, Stage.SAPLING, Stage.TREE),
    Mode.PAPER: (Stage.SAPLING, Stage.TREE),
    Mode.LIVE: (Stage.TREE,),
    Mode.REPLAY: (Stage.SEED, Stage.SPROUT, Stage.SAPLING, Stage.TREE),
}


def eligible_strategy_rows(mode: str, market: str):
    """Strategies this execution mode may load; broker modes require proof."""
    rows = Strategy.objects.filter(enabled=True, stage__in=STAGE_FOR_MODE[mode], market=market)
    if mode in (Mode.PAPER, Mode.LIVE):
        return rows.filter(qualification=Qualification.QUALIFIED)
    if mode != Mode.REPLAY:
        return rows.exclude(qualification=Qualification.QUARANTINED)
    return rows


class AgentStop(Exception):
    pass


class Agent:
    def __init__(self, mode: str = 'sim', replay_date: date | None = None, speed: float = 30.0,
                 once: bool = False, provider_name: str | None = None, quiet: bool = False,
                 market: str = Market.STOCKS):
        self.mode = Mode.REPLAY if replay_date else mode
        self.market = market
        self.replay_date = replay_date
        self.speed = max(1.0, float(speed))
        self.once = once
        self.provider_name = provider_name
        self.quiet = quiet
        self.stop_requested = False
        self._operator_stop_requested = False
        self.last_tick: datetime | None = None
        self.last_snapshot: datetime | None = None
        self.last_processed: dict[str, datetime] = {}
        self.last_bar_ts: datetime | None = None
        self.eod_done: date | None = None
        self.run_row: AgentRun | None = None
        self._lock_fh = None
        self._last_control_poll = 0.0
        self._last_strategy_check = 0.0
        self._config_flagged = False
        self.exclude_sources: list = []
        self.provider = None
        self._last_pulse = 0.0
        self._pulse_prices: dict[str, float] = {}
        self._pulse_ref: dict[str, float] = {}
        self._pulse_ref_at = None
        self._pulse_failures = 0
        self.journal_done: date | None = None

    # --- setup --------------------------------------------------------------
    def setup(self) -> None:
        self.cfg = AgentConfig.get()
        if self.mode == Mode.LIVE and not (settings.LIVE_TRADING_ARMED and self.cfg.mode == Mode.LIVE):
            raise RuntimeError('live mode refused: set LIVE_TRADING_ARMED=1 in .env AND arm live mode in Settings')
        if self.mode == Mode.PAPER and not settings.ALPACA_ENABLED:
            raise RuntimeError('paper mode needs Alpaca keys in .env')
        if self.market == Market.FOREX and self.mode in (Mode.PAPER, Mode.LIVE):
            raise RuntimeError('the forex lane trades on the simulator only for now — no forex broker adapter yet')
        self._acquire_lock()
        self.account = Account.for_mode(self.mode, self.market)
        self.lane_asset_class = self.account.lane_asset_class
        if self.mode == Mode.REPLAY:
            self.account.reset()
        self.narrator = Narrator(self.account, echo=not self.quiet)
        self.timeframe = self.cfg.timeframe_for(self.market)
        self.step = tf_delta(self.timeframe)
        self.instruments = {i.symbol: i for i in Instrument.objects.filter(in_watchlist=True, active=True, market=self.market)}
        if not self.instruments:
            raise RuntimeError(f'no {self.market} symbols on the watchlist — add some at /data/')
        self.asset_classes = {s: i.asset_class for s, i in self.instruments.items()}
        self.qty_increments = {s: float(i.qty_increment) for s, i in self.instruments.items()}
        # Simulator observation is how an idea earns evidence. Broker-backed
        # execution is reserved for immutable versions that cleared it.
        rows = list(eligible_strategy_rows(self.mode, self.market))
        self.strategies = []
        self.strategy_symbols: dict[str, set] = {}
        allocations = {}
        for row in rows:
            strat = make_strategy(row.key, row.params)
            strat.name = row.name
            self.strategies.append(strat)
            syms = set(row.symbols or self.instruments)
            self.strategy_symbols[row.key] = {s for s in syms if s in self.instruments and strat.supports(self.asset_classes[s])}
            allocations[row.key] = float(row.allocation_pct)
        self.strategy_snapshot = self._strategy_snapshot()
        risk_cfg = RiskConfig.from_model(self.cfg, self.market)
        self.risk = RiskManager(risk_cfg, self.qty_increments)
        self.risk.kill_switch = self.cfg.kill_switch
        self.risk.trading_enabled = self.cfg.trading_enabled
        if self.mode != Mode.REPLAY:
            # Alpaca has no forex: that lane quotes from Yahoo (to the minute, no volume).
            default_provider = 'yahoo' if self.market == Market.FOREX else None
            self.provider = get_provider(self.provider_name or default_provider, live_feed=True)
            self.exclude_sources = [] if self.provider.name == 'synthetic' else ['synthetic']
            # Live decisions use the feed the loop trades on (IEX for stocks), so
            # relative volume and every other indicator compare like with like.
            self.live_source = {ac: getattr(self.provider, 'source_label', lambda a: self.provider.name)(ac)
                                for ac in ('stock', 'etf', 'crypto', 'forex')}
        data_source = 'stored bars' if self.mode == Mode.REPLAY else self.provider.name
        engine_cfg = EngineConfig(timeframe=self.timeframe, mode=self.mode, asset_classes=self.asset_classes, risk=risk_cfg,
                                  allocations=allocations, data_source=data_source,
                                  confirm_entries=(self.mode == Mode.LIVE and self.cfg.live_confirm_orders),
                                  confirm_minutes=int(self.cfg.live_confirm_minutes))
        fee_bps = self.cfg.fee_bps()
        if self.mode in (Mode.SIM, Mode.REPLAY):
            self.broker = SimBroker(float(self.account.cash), immediate_fills=(self.mode == Mode.SIM),
                                    slippage_bps=risk_cfg.slippage_bps, fee_bps=fee_bps,
                                    liquidity_cap_pct=float(self.cfg.liquidity_cap_pct),
                                    asset_classes=self.asset_classes, qty_increments=self.qty_increments,
                                    leverage=risk_cfg.leverage)
            if self.mode == Mode.SIM:
                n = hydrate_broker(self.account, self.broker)
                stale = open_orders_from_db(self.account)
                log.info('hydrated %d positions, canceled %d stale orders', n, stale)
        else:
            from .broker.alpaca import AlpacaBroker
            self.broker = AlpacaBroker(paper=(self.mode == Mode.PAPER), asset_classes=self.asset_classes,
                                       qty_increments=self.qty_increments, mode_is_live=(self.cfg.mode == Mode.LIVE))
            hydrate_broker(self.account, self.broker)
        self.recorder = DBRecorder(self.account, self.instruments)
        self.engine = Engine(self.strategies, self.broker, engine_cfg, self.recorder, self.risk, narrator=self.narrator)
        # Risk state survives restarts: pick the day up where the last process left it.
        today = cal.trading_day(timezone.now(), self.lane_asset_class)
        if self.mode != Mode.REPLAY and self.account.day_start_date == today:
            self.risk.restore(today, float(self.account.day_start_equity), self.account.day_entries,
                              self.account.day_halted, self.account.day_halted_reason)
            self.engine.current_day = today
        # A restart between the close and midnight must not re-run end of day: that
        # would re-flatten, rewrite the journal and pay for a second coach review of a
        # day already closed. If today's journal exists, treat the day as done.
        if self.mode != Mode.REPLAY:
            from main_app.models import JournalEntry
            if JournalEntry.objects.filter(account=self.account, kind='auto_eod', date=today).exists():
                self.eod_done = today
                self.journal_done = today
        n_cards = hydrate_cards(self.account, self.engine)
        if self.mode in (Mode.PAPER, Mode.LIVE):
            self.reconcile(timezone.now(), announce=True)
        persist_broker(self.account, self.broker, self.instruments, risk=self.risk)
        self.run_row = AgentRun.objects.create(account=self.account, mode=self.mode, market=self.market, pid=os.getpid(),
                                               replay_date=self.replay_date, speed=self.speed, message='starting',
                                               expected_interval_s=int(self.step.total_seconds()) + 60,
                                               state='starting', data_source=data_source)
        AgentRun.objects.filter(account=self.account, status='running').exclude(pk=self.run_row.pk).update(
            status='stopped', stopped_at=timezone.now(), message='superseded')
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        names = ', '.join(f'{s.key} ({len(self.strategy_symbols[s.key])} symbols)' for s in self.strategies) or 'none enabled'
        log.info('MoneyTree agent up: mode=%s market=%s account=%s equity=%.2f strategies=%s', self.mode, self.market,
                 self.account.name, float(self.account.equity), names)
        FeedEvent.prune()
        self.say('system', f'{self.market.capitalize()} agent up in {self.mode} mode — equity ${float(self.account.equity):,.2f}, '
                 f'{len(self.instruments)} symbols ({", ".join(self.instruments)}), {self.timeframe} bars. Strategies: {names}.'
                 + (f' Restored today\'s risk state: {self.risk.day.entries} entries so far'
                    + (f', HALTED ({self.risk.day.halted_reason})' if self.risk.day.halted else '') + '.'
                    if self.account.day_start_date == today and self.mode != Mode.REPLAY else '')
                 + (f' {n_cards} open trade card(s) picked up.' if n_cards else ''))
        if not self.strategies:
            requirement = ('enabled and statistically qualified' if self.mode in (Mode.PAPER, Mode.LIVE)
                           else 'enabled, stage-eligible, and not quarantined')
            self.say('risk', f'No strategy is {requirement} for this market — I will watch bars but never trade. '
                     'Open Strategies to see the evidence gate.', phase='alert')
        if self.mode != Mode.REPLAY:
            fake = synthetic_symbols(self.instruments.values(), self.timeframe)
            if fake:
                self.say('risk', f'Stored history for {", ".join(fake)} is synthetic (a seeded random walk). I ignore it for '
                         'live decisions, so indicators warm up from real bars only — sync real history on the Data page.',
                         phase='alert')
        self.narrator.flush()

    def _strategy_snapshot(self) -> dict:
        rows = Strategy.objects.filter(market=self.market)
        return {r.key: (r.version, r.enabled, r.stage, r.qualification,
                        tuple(sorted(r.symbols or [])), float(r.allocation_pct)) for r in rows}

    def _acquire_lock(self) -> None:
        settings.RUN_DIR.mkdir(exist_ok=True)
        path = settings.RUN_DIR / f'agent-{self.mode}-{self.market}.lock'
        self._lock_fh = open(path, 'w')
        try:
            fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError(f'another agent already holds {path} — one agent per account') from None
        self._lock_fh.write(str(os.getpid()))
        self._lock_fh.flush()

    def _on_signal(self, signum, frame):
        log.warning('signal %s received — stopping after this tick', signum)
        self.stop_requested = True

    # --- run ----------------------------------------------------------------
    def run(self) -> None:
        try:
            self.setup()
            if self.mode == Mode.REPLAY:
                self.run_replay()
            else:
                self.run_live()
        except AgentStop:
            pass
        finally:
            self.shutdown()

    def say(self, level: str, text: str, **kw) -> None:
        self.narrator.say(level, text, **kw)

    def heartbeat(self, message: str = '', state: str | None = None, next_action_at: datetime | None = None) -> None:
        self.narrator.flush()
        if self.run_row is None:
            return
        self.run_row.last_tick_at = timezone.now()
        self.run_row.ticks += 1
        if message:
            self.run_row.message = message[:300]
        if state:
            self.run_row.state = state
        if next_action_at is not None:
            self.run_row.next_action_at = next_action_at
        if self.last_bar_ts is not None:
            self.run_row.last_bar_ts = self.last_bar_ts
        self.run_row.save(update_fields=['last_tick_at', 'ticks', 'message', 'state', 'next_action_at', 'last_bar_ts'])

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + max(0.0, seconds)
        while True:
            if self.stop_requested:
                raise AgentStop()
            if time.monotonic() - self._last_control_poll >= CONTROL_POLL_S:
                self.poll_controls(timezone.now())
            if self._pulse_enabled() and time.monotonic() - self._last_pulse >= self.pulse_interval():
                self.pulse(timezone.now())
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(1.0, remaining))
        if self.stop_requested:
            raise AgentStop()

    # --- the pulse: live prices between bars ---------------------------------
    def _pulse_enabled(self) -> bool:
        if self.mode == Mode.REPLAY or not self.cfg.pulse_seconds or self.provider is None \
                or not hasattr(self.provider, 'latest_prices'):
            return False
        return cal.is_open(timezone.now(), self.lane_asset_class)

    def pulse_interval(self) -> float:
        """Yahoo rate-limits by IP, so its pulse is never faster than every 30 s."""
        base = float(self.cfg.pulse_seconds or 0)
        if self.provider is not None and self.provider.name == 'yahoo':
            base = max(base, 30.0)
        return base * (4 if self._pulse_failures else 1)

    def pulse(self, now: datetime) -> None:
        """Every few seconds: last prices, open P&L, distance to stop/target and
        to the nearest trigger — the 'thinking out loud' between bar decisions."""
        self._last_pulse = time.monotonic()
        try:
            prices = self.provider.latest_prices(list(self.instruments), self.lane_asset_class)
        except Exception as exc:
            self._pulse_failures += 1
            self.say('pulse', f'pulse: price check failed ({exc!r}) — backing off'[:200], phase='observe')
            self.narrator.flush()
            return
        self._pulse_failures = 0
        if not prices:
            return
        self.broker.mark(prices) if hasattr(self.broker, 'mark') else None
        parts = []
        for sym, px in prices.items():
            ref = self._pulse_ref.get(sym)
            chg = f' {((px / ref) - 1) * 100:+.2f}%/min' if ref else ''
            parts.append(f'{sym.split("/")[0]} {px:,.6g}{chg}')
        held = []
        for sym, pos in self.broker.positions.items():
            if not pos.qty or sym not in prices:
                continue
            px = prices[sym]
            up = pos.unrealized(px)
            d_stop = f', stop {((pos.stop / px) - 1) * 100:+.2f}% away' if pos.stop else ''
            d_tgt = f', target {((pos.target / px) - 1) * 100:+.2f}% away' if pos.target else ''
            held.append(f'{sym}: {up:+,.2f}{d_stop}{d_tgt}')
        nearest = self._nearest_trigger(prices)
        text = 'pulse — ' + ', '.join(parts)
        if held:
            text += ' · holding ' + '; '.join(held)
        if nearest:
            text += ' · ' + nearest
        self.say('pulse', text[:600], phase='observe', data={'prices': prices})
        self._pulse_prices = prices
        if not self._pulse_ref or (now - self._pulse_ref_at).total_seconds() >= 60:
            self._pulse_ref = dict(prices)
            self._pulse_ref_at = now
        try:
            from main_app.models import SymbolState
            for sym, px in prices.items():
                SymbolState.objects.filter(account=self.account, symbol=sym).update(price=px, ts=now)
            self.account.equity = D_(self.broker.account().equity)
            self.account.save(update_fields=['equity'])
        except Exception:
            log.exception('pulse persist failed')
        self.narrator.flush()

    def _nearest_trigger(self, prices: dict) -> str:
        """The symbol closest to firing, from the latest rule evaluation."""
        try:
            from main_app.models import SymbolState
            best, best_gap = None, None
            for st in SymbolState.objects.filter(account=self.account, symbol__in=list(prices)):
                for r in st.rules or []:
                    if r.get('ok') or r.get('value') is None or r.get('threshold') is None:
                        continue
                    if r['rule'] in ('move', 'breakout', 'stretch'):
                        gap = abs(float(r['threshold']) - float(r['value']))
                        if best_gap is None or gap < best_gap:
                            best, best_gap = (st.symbol, r), gap
            if best is None:
                return ''
            sym, r = best
            return f'nearest trigger: {sym} ({r["strategy"]} {r["rule"]}: {r["value"]:.4g} vs {r["threshold"]:.4g})'
        except Exception:
            return ''

    # --- controls (polled every 2 s, even while sleeping) --------------------
    def poll_controls(self, now: datetime) -> None:
        self._last_control_poll = time.monotonic()
        try:
            if self.run_row is not None:
                self.run_row.refresh_from_db(fields=['stop_requested'])
                if self.run_row.stop_requested:
                    self._operator_stop_requested = True
                    self.stop_requested = True
                    return
            cfg = AgentConfig.get()
            if cfg.kill_switch and not self.risk.kill_switch:
                self.risk.kill_switch = True
                n = self.engine.flatten_all(now, 'kill')
                self.recorder.on_risk_event('kill_switch', f'kill switch engaged — flattened {n} positions', now)
                self.say('risk', f'KILL SWITCH engaged — flattened {n} position(s) immediately; no entries until it is reset.',
                         phase='alert')
                if self.mode in (Mode.PAPER, Mode.LIVE):
                    self.reconcile(now, announce=True)
                persist_broker(self.account, self.broker, self.instruments, risk=self.risk)
            elif not cfg.kill_switch and self.risk.kill_switch:
                self.risk.kill_switch = False
                self.say('system', 'Kill switch reset — entries allowed again.')
            if cfg.trading_enabled != self.risk.trading_enabled:
                self.risk.trading_enabled = cfg.trading_enabled
                self.say('system', 'Trading ' + ('enabled' if cfg.trading_enabled else 'disabled') + ' in Settings.')
            new_risk = RiskConfig.from_model(cfg, self.market)
            if new_risk != self.risk.cfg:
                old = asdict(self.risk.cfg)
                changes = [f'{k} {old[k]} → {v}' for k, v in asdict(new_risk).items() if old.get(k) != v]
                self.risk.cfg = new_risk
                self.engine.cfg.risk = new_risk
                self.say('system', 'Risk settings updated (applied between bars): ' + '; '.join(changes)[:400])
            self.cfg = cfg
            self._process_approvals(now)
            if time.monotonic() - self._last_strategy_check >= STRATEGY_CHECK_S:
                self._last_strategy_check = time.monotonic()
                if not self._config_flagged and self._strategy_snapshot() != self.strategy_snapshot:
                    self._config_flagged = True
                    self.risk.blocks['config_changed'] = 'strategy configuration changed — restart required before new entries'
                    self.recorder.on_risk_event('config_changed', 'strategy configuration changed — restart the agent to apply it', now)
                    self.say('risk', 'Strategy configuration changed on the Strategies page. New entries are blocked now; '
                             'existing positions remain managed. Restart the agent to load the new version.', phase='alert')
            self.narrator.flush()
        except AgentStop:
            raise
        except Exception:
            log.exception('control poll failed')

    def _process_approvals(self, now: datetime) -> None:
        if not self.engine.cfg.confirm_entries:
            return
        expired = self.engine.expire_cards(now)
        approved = TradeCard.objects.filter(account=self.account, status='approved').exclude(approved_by='')
        for row in approved:
            card = self.engine.cards.get(row.entry_order_id)
            if card is None or card.status != 'awaiting_approval':
                continue
            self.say('order', f'Operator {row.approved_by} approved the {card.symbol} entry.', symbol=card.symbol,
                     strategy_key=card.strategy_key, phase='submit', card_id=card.id)
            card.status = 'approved'
            self.engine.submit_card(card.id, now)
        rejected = TradeCard.objects.filter(account=self.account, status='rejected', error='rejected by operator')
        for row in rejected:
            card = self.engine.cards.get(row.entry_order_id)
            if card is not None and card.status == 'awaiting_approval':
                card.status, card.error = 'rejected', 'rejected by operator'
                self.engine.card_by_symbol.pop(card.symbol, None)
                self.say('order', f'Operator rejected the {card.symbol} entry.', symbol=card.symbol, phase='submit', card_id=card.id)
        if expired or approved or rejected:
            persist_broker(self.account, self.broker, self.instruments, risk=self.risk)

    # --- broker reconciliation (paper/live) -----------------------------------
    def reconcile(self, now: datetime, announce: bool = False) -> bool:
        if self.mode not in (Mode.PAPER, Mode.LIVE):
            return True
        try:
            info = self.broker.sync()
        except Exception as exc:
            self.account.reconcile_ok = False
            self.account.reconcile_note = f'sync failed: {exc!r}'[:300]
            self.account.save(update_fields=['reconcile_ok', 'reconcile_note'])
            self.risk.blocks['reconcile'] = 'broker reconciliation failed — no new entries until it succeeds'
            self.recorder.on_risk_event('reconcile', f'broker sync failed: {exc!r}'[:300], now)
            self.say('error', f'Could not reconcile with Alpaca: {exc!r} — entries blocked until it works.'[:400], phase='alert')
            return False
        for symbol in info.get('adopted', []):
            self.recorder.on_risk_event('external_position', f'{symbol}: position found at the broker but not on our books — adopted as external', now)
            self.say('risk', f'{symbol} is open at the broker but not on my books — adopted as external, I will not touch it.',
                     symbol=symbol, phase='alert')
        diverged = info.get('diverged', [])
        if diverged:
            note = '; '.join(f'{s}: ours {a:g} vs broker {b:g}' for s, a, b in diverged)
            self.risk.blocks['reconcile'] = 'books diverged from the broker — no new entries until reconciled'
            self.recorder.on_risk_event('reconcile', f'books diverged: {note}'[:300], now, {'diverged': diverged})
            self.say('risk', f'RECONCILIATION: my books disagreed with Alpaca ({note}). I adopted the broker\'s quantities and '
                     'paused new entries.', phase='alert')
        else:
            self.risk.blocks.pop('reconcile', None)
        self.account.last_reconcile_at = now
        self.account.reconcile_ok = not diverged
        self.account.reconcile_note = ('diverged: ' + note)[:300] if diverged else \
            f'{len(self.broker.positions)} positions, {info.get("open_orders", 0)} open orders, {info.get("fills", 0)} new fills'
        self.account.save(update_fields=['last_reconcile_at', 'reconcile_ok', 'reconcile_note'])
        self.engine._emit_broker_events()
        if announce or info.get('fills') or info.get('closed'):
            self.say('system', f'Reconciled with Alpaca: equity {info.get("equity", 0):,.2f}, cash {info.get("cash", 0):,.2f}, '
                     f'{len(self.broker.positions)} position(s), {info.get("open_orders", 0)} open order(s), '
                     f'{info.get("fills", 0)} new fill(s).', phase='manage')
        return not diverged

    # --- live loop ---------------------------------------------------------------
    def strategies_for(self, symbol: str) -> list:
        return [s for s in self.strategies if symbol in self.strategy_symbols[s.key]]

    def run_live(self) -> None:
        provider = self.provider
        grace = GRACE_S.get(provider.name, 20)
        lane_ac = self.lane_asset_class
        symbols = list(self.instruments)
        step = self.step
        lane = {'stocks': 'US stocks', 'crypto': 'BTC/ETH', 'degen': 'altcoins (the high-risk sandbox: fake money, expected to bleed)',
                'forex': 'forex majors (USD-quoted pairs, traded on margin)'}[self.market]
        tfm = tf_minutes(self.timeframe)
        cadence = f'{tfm} minutes' if tfm < 60 else ('hour' if tfm == 60 else f'{tfm // 60} hours')
        log.info('live loop: provider=%s timeframe=%s lane=%s symbols=%d', provider.name, self.timeframe, lane_ac, len(symbols))
        feed_note = {'alpaca': 'Alpaca (IEX real-time)', 'yahoo': 'Yahoo (free, to the minute, rate-limited)',
                     'synthetic': 'SYNTHETIC random walk'}.get(provider.name, provider.name)
        pulse_note = f' Between bars I check prices every {self.pulse_interval():.0f} s (the pulse).' if self._pulse_enabled() or self.account.is_24x7 else ''
        hours = {
            'stock': f'US stocks trade 09:30–16:00 ET Mon–Fri (early closes 13:00 ET); I evaluate every {cadence} during the session and sleep outside it.',
            'crypto': f'This lane trades around the clock; I evaluate every {cadence} ({self.timeframe} bars), {grace} s after each bar closes.',
            'forex': f'Forex trades Sunday 17:00 to Friday 17:00 ET; I evaluate every {cadence} ({self.timeframe} bars), {grace} s after '
                     f'each bar closes, and close everything before the Friday close. Positions may exceed the account '
                     f'({self.risk.cfg.leverage:g}× leverage), as at a forex broker.',
        }[lane_ac]
        self.say('system', f'Lane: {lane}. Quotes from {feed_note}. ' + hours + pulse_note)
        self.narrator.flush()
        closed_note_at = None
        announced_bar = None
        while True:
            now = timezone.now()
            self.poll_controls(now)
            lane_open = cal.is_open(now, lane_ac)
            self._daily_roll(now, lane_open)
            active = symbols if lane_open else []
            if not active:
                nxt = cal.next_open(now, lane_ac)
                if lane_ac == 'forex' and self.last_tick is not None and any(p.qty for p in self.broker.positions.values()):
                    n = self.engine.flatten_all(now, 'weekend')
                    self.recorder.on_risk_event('flatten', f'forex week closed: flattened {n} leftover positions', now)
                    persist_broker(self.account, self.broker, self.instruments, risk=self.risk)
                if closed_note_at is None or now - closed_note_at > timedelta(hours=1):
                    self.say('system', f'Market closed — next open {nxt.astimezone(cal.ET):%a %b %-d %H:%M} ET '
                             f'({(nxt - now).total_seconds() / 3600:.1f} h). Sleeping until then; controls are still polled every 2 s.')
                    closed_note_at = now
                if self.run_row.expected_interval_s != 120:
                    self.run_row.expected_interval_s = 120
                    self.run_row.save(update_fields=['expected_interval_s'])
                self.heartbeat(f'market closed — next open {nxt.astimezone(cal.ET):%a %H:%M} ET', state='sleeping', next_action_at=nxt)
                self.last_tick = None  # the overnight gap is a schedule, not a missed tick
                self._sleep(min(60.0, max(1.0, (nxt - now).total_seconds())))
                if self.once:
                    return
                continue
            closed_note_at = None
            expected = int(step.total_seconds()) + 60
            if self.run_row.expected_interval_s != expected:
                self.run_row.expected_interval_s = expected
                self.run_row.save(update_fields=['expected_interval_s'])
            wake = floor_to_bar(now, self.timeframe) + step + timedelta(seconds=grace)
            if announced_bar != wake:
                self.say('system', f'Waiting for the {(wake - step - timedelta(seconds=grace)).astimezone(cal.ET):%H:%M} ET bar to close; '
                         f'next evaluation {wake.astimezone(cal.ET):%H:%M:%S} ET.')
                announced_bar = wake
            self.heartbeat(f'waiting for the {wake.astimezone(cal.ET):%H:%M:%S} ET bar', state='waiting', next_action_at=wake)
            self._sleep((wake - now).total_seconds())
            now = timezone.now()
            try:
                self.tick(now, provider, active, grace)
            except AgentStop:
                raise
            except Exception as exc:
                log.exception('tick failed')
                self.recorder.on_risk_event('error', f'tick failed: {exc!r}'[:300], now)
                self.say('error', f'Tick failed: {exc!r} — retrying next bar.'[:400], phase='alert')
                self.heartbeat(f'error: {exc!r}'[:300], state='waiting')
                self._sleep(10)
            if self.once:
                return

    def tick(self, now: datetime, provider, symbols: list, grace: int) -> None:
        step = self.step
        self.heartbeat('evaluating', state='ticking')
        missed = self.last_tick is not None and (now - self.last_tick) > 2 * step
        if missed:
            self.recorder.on_risk_event('missed_ticks', f'no tick for {(now - self.last_tick).total_seconds() / 60:.0f} min — '
                                        'catch-up: stops evaluated, no new entries from stale bars', now)
            self.say('risk', f'I missed {(now - self.last_tick).total_seconds() / 60:.0f} minutes (laptop asleep?). Catching up: '
                     'stops and targets are checked on the missed bars, but no new entries from stale data.', phase='alert')
        if self.mode in (Mode.PAPER, Mode.LIVE):
            self.reconcile(now)
        by_class: dict[str, list] = {}
        for s in symbols:
            by_class.setdefault(self.asset_classes[s], []).append(s)
        for ac, syms in by_class.items():
            since_candidates = [self.last_processed[s] for s in syms if s in self.last_processed]
            since = (min(since_candidates) - REFRESH_BARS * step) if since_candidates else (now - 6 * step)
            since = max(since, now - timedelta(days=3))
            try:
                frames = provider.latest_bars(syms, self.timeframe, since, ac)
            except Exception as exc:
                log.warning('provider %s failed for %s: %s', provider.name, ac, exc)
                self.recorder.on_risk_event('error', f'data fetch failed ({provider.name}, {ac}): {exc!r}'[:300], now)
                self.say('error', f'Could not fetch {ac} bars from {provider.name}: {exc!r}'[:400], phase='alert')
                continue
            source = getattr(provider, 'source_label', lambda a: provider.name)(ac)
            fetched = []
            for s, df in frames.items():
                df = complete_bars_only(df, self.timeframe, now, grace)
                df, rep = quality_gate(df, self.timeframe, ac)
                if len(df):
                    added = upsert_bars(self.instruments[s], self.timeframe, df, source)
                    if added:
                        fetched.append(f'{s} {float(df["close"].iloc[-1]):,.2f}')
                        self.last_bar_ts = max(self.last_bar_ts or df.index[-1].to_pydatetime(), df.index[-1].to_pydatetime())
            age = f'{(now - self.last_bar_ts - step).total_seconds():.0f} s after the bar closed' if self.last_bar_ts else ''
            self.say('system', f'{now.astimezone(cal.ET):%H:%M:%S} — {len(fetched)} new {ac} bar(s) from {provider.name}'
                     + (f' ({age})' if age and fetched else '')
                     + (': ' + ', '.join(fetched) if fetched else ' (nothing new yet — the feed lags a little)'), phase='observe')
        processed = 0
        for s in symbols:
            strategies = self.strategies_for(s)
            inst = self.instruments[s]
            window = load_frame(inst, self.timeframe, limit=WINDOW_BARS, exclude_sources=self.exclude_sources,
                                source=self.live_source.get(self.asset_classes[s]))
            window = complete_bars_only(window, self.timeframe, now, grace)
            if len(window) == 0:
                continue
            last = self.last_processed.get(s)
            if last is None:
                new_positions = [len(window) - 1]
            else:
                new_positions = [i for i, ts in enumerate(window.index) if ts.to_pydatetime() > last]
            if not new_positions:
                continue
            ac = self.asset_classes[s]
            need = max((st.warmup_bars for st in strategies), default=0)
            if strategies and len(window) < need:
                self.say('bar', f'{s}: {len(window)}/{need} {self.live_source.get(self.asset_classes[s], "")} bars stored — '
                         'indicators still warming up, no decisions yet (sync this feed\'s history on the Data page).', symbol=s,
                         phase='observe')
            prepared = {st.key: st.prepare(window, ac, self.timeframe) for st in strategies}
            rows = {k: list(df.itertuples(index=True)) for k, df in prepared.items()}
            base_rows = list(window.itertuples(index=True))
            mtc = _mtc(window.index, ac).tolist()
            stale = new_positions[:-1] if missed else []
            for i in new_positions:
                ts = window.index[i].to_pydatetime()
                if i in stale:
                    self.risk.blocks['catchup'] = 'catching up on missed bars — no entries from stale data'
                    try:
                        self.engine.process_bar(s, ts, base_rows[i], {k: r[i] for k, r in rows.items()}, prepared, i, mtc[i])
                    finally:
                        self.risk.blocks.pop('catchup', None)
                else:
                    self.engine.process_bar(s, ts, base_rows[i], {k: r[i] for k, r in rows.items()}, prepared, i, mtc[i])
                self.last_processed[s] = ts
                processed += 1
        if self.mode in (Mode.PAPER, Mode.LIVE):
            self.reconcile(now)
        persist_broker(self.account, self.broker, self.instruments, risk=self.risk)
        if self.last_snapshot is None or now - self.last_snapshot >= SNAPSHOT_EVERY:
            self.engine.record_equity(now)
            self.last_snapshot = now
        a = self.broker.account()
        n_pos = len([p for p in self.broker.positions.values() if p.qty])
        risk_open = sum(abs(p.qty) * abs(p.avg_price - p.stop) for p in self.broker.positions.values() if p.qty and p.stop)
        nxt = floor_to_bar(now, self.timeframe) + step + timedelta(seconds=grace)
        if self.risk.cfg.leverage > 1:
            # A margin lane: cash swings by the notional on every entry, so report what matters instead.
            exposure = sum(abs(p.market_value(self.broker.last_price.get(p.symbol))) for p in self.broker.positions.values() if p.qty)
            money = f'exposure ${exposure:,.0f} ({exposure / a.equity if a.equity else 0:.1f}× equity, buying power ${a.buying_power:,.0f} left)'
        else:
            money = f'cash ${a.cash:,.2f}'
        self.say('system', f'Equity ${a.equity:,.2f} (day {self.risk.day_pnl(a.equity):+,.2f}), {money}, '
                 f'{n_pos} open position(s), ${risk_open:,.2f} at risk to stops. Next evaluation {nxt.astimezone(cal.ET):%H:%M:%S} ET.',
                 phase='manage')
        self.heartbeat(f'tick ok — {processed} bars, equity {a.equity:,.2f}, {n_pos} positions', state='waiting', next_action_at=nxt)
        self.last_tick = now
        if not self.quiet:
            log.info('tick %s: %d bars processed, equity %.2f', now.astimezone(cal.ET).strftime('%H:%M:%S'), processed, a.equity)

    def _daily_roll(self, now: datetime, lane_open: bool) -> None:
        """Once a day: the stocks lane flattens at the bell and writes its journal;
        the round-the-clock lanes write yesterday's journal at midnight ET and
        keep their positions (their exits are stops, targets and max hold)."""
        if self.last_tick is None:
            return
        today = cal.session_date(now)
        if self.lane_asset_class == 'stock':
            s = cal.session_for(today)
            if not lane_open and s and now > s.close_utc and self.eod_done != today:
                self.end_of_day(now, today, flatten=True)
        else:
            yesterday = today - timedelta(days=1)
            if self.journal_done is None:
                self.journal_done = yesterday  # never write a journal for a day this process did not see
            elif self.journal_done < yesterday:
                self.end_of_day(now, yesterday, flatten=False)

    def end_of_day(self, now: datetime, d: date | None = None, flatten: bool = True) -> None:
        from .journal import write_eod_journal
        d = d or cal.session_date(now)
        self.eod_done = d
        self.journal_done = d
        n = self.engine.flatten_all(now, 'eod') if flatten else 0
        if n:
            self.recorder.on_risk_event('flatten', f'end of day: flattened {n} leftover positions', now)
        if self.mode in (Mode.PAPER, Mode.LIVE):
            self.reconcile(now)
        persist_broker(self.account, self.broker, self.instruments, risk=self.risk)
        self.engine.record_equity(now)
        try:
            entry = write_eod_journal(self.account, d)
            log.info('journal written: %s', entry.title)
            self.say('journal', f'END OF DAY {d}: {entry.title}. ' + entry.body.split('\n\n')[0][:300], phase='system')
        except Exception:
            log.exception('journal failed')
            self.say('error', 'Journal failed — see the agent log.', phase='alert')
        FeedEvent.prune()
        if settings.COACH_ENABLED:
            try:
                from .coach import coach_review
                coach_review(self.account, d)
            except Exception:
                log.exception('coach failed')
        self.heartbeat(f'end of day {d} done', state='sleeping')

    # --- replay -------------------------------------------------------------
    def run_replay(self) -> None:
        from .journal import write_eod_journal
        d = self.replay_date
        a, b = date_bounds(d, d)
        warm_start = a - timedelta(days=5 if tf_minutes(self.timeframe) < 60 else 30)
        frames = {}
        for s, inst in self.instruments.items():
            df = load_frame(inst, self.timeframe, warm_start, b)
            df, _ = quality_gate(df, self.timeframe, inst.asset_class)
            if len(df[df.index >= a]):
                frames[s] = df
        if not frames:
            raise RuntimeError(f'no {self.timeframe} bars stored for {d} — sync history first (/data/ or `manage.py sync_bars`)')
        prepared = {s: {st.key: st.prepare(df, self.asset_classes[s], self.timeframe) for st in self.strategies_for(s)}
                    for s, df in frames.items()}
        rows = {s: {k: list(df.itertuples(index=True)) for k, df in per.items()} for s, per in prepared.items()}
        base_rows = {s: list(df.itertuples(index=True)) for s, df in frames.items()}
        mtc = {s: _mtc(df.index, self.asset_classes[s]).tolist() for s, df in frames.items()}
        events = []
        for s, df in frames.items():
            for i, ts in enumerate(df.index):
                if ts >= a:
                    events.append((ts.to_pydatetime(), s, i))
        events.sort(key=lambda e: (e[0], e[1]))
        pause = self.step.total_seconds() / self.speed
        log.info('replay %s: %d bars across %d symbols at %.0fx (%.1fs per bar)', d, len(events), len(frames), self.speed, pause)
        self.say('system', f'REPLAY of {d:%A %b %-d}: {len(events)} bars across {len(frames)} symbols at {self.speed:.0f}× '
                 f'({pause:.1f} s per bar). Same engine as live; fills at the next bar\'s open.')
        self.run_row.expected_interval_s = int(min(pause, 10.0)) + 60
        self.run_row.save(update_fields=['expected_interval_s'])
        self.heartbeat(f'replaying {d} at {self.speed:.0f}x', state='ticking')
        last_ts = None
        for ts, s, i in events:
            if last_ts is not None and ts != last_ts:
                persist_broker(self.account, self.broker, self.instruments, risk=self.risk)
                self.engine.record_equity(last_ts)
                acct = self.broker.account()
                self.last_bar_ts = last_ts
                self.heartbeat(f'replay {ts.astimezone(cal.ET):%H:%M} ET — equity {acct.equity:,.2f}', state='ticking')
                self._sleep(min(pause, 10.0))
            self.engine.process_bar(s, ts, base_rows[s][i], {k: r[i] for k, r in rows[s].items()}, prepared[s], i, mtc[s][i])
            last_ts = ts
        if last_ts is not None:
            self.engine.flatten_all(last_ts, 'end')
            persist_broker(self.account, self.broker, self.instruments, risk=self.risk)
            self.engine.record_equity(last_ts)
        entry = write_eod_journal(self.account, d)
        self.say('journal', f'REPLAY DONE — {entry.title}. ' + entry.body.split('\n\n')[0][:300], phase='system')
        self.heartbeat(f'replay {d} finished', state='stopping')
        log.info('replay finished: equity %.2f, %d trades', self.broker.account().equity, len(self.broker.trades))

    # --- teardown -----------------------------------------------------------
    def _operator_requested_stop(self) -> bool:
        """Distinguish the Stop button from launchd/deploy restart signals."""
        if self._operator_stop_requested or self.run_row is None:
            return self._operator_stop_requested
        try:
            self.run_row.refresh_from_db(fields=['stop_requested'])
            self._operator_stop_requested = bool(self.run_row.stop_requested)
        except Exception:
            log.exception('could not verify stop source')
        return self._operator_stop_requested

    def _should_flatten_on_stop(self, operator_stop: bool) -> bool:
        if self.mode == Mode.LIVE:
            return settings.LIVE_FLATTEN_ON_EXIT
        if self.mode == Mode.REPLAY:
            return True
        return operator_stop

    def shutdown(self) -> None:
        now = timezone.now()
        try:
            if hasattr(self, 'engine'):
                operator_stop = self._operator_requested_stop() if self.stop_requested else False
                flatten = self._should_flatten_on_stop(operator_stop)
                if self.stop_requested and flatten:
                    n = self.engine.flatten_all(now, 'manual')
                    if n:
                        self.recorder.on_risk_event('flatten', f'agent stopped — flattened {n} positions', now)
                    self.say('system', f'Stopped by request — {n} position(s) closed. Bye.')
                elif self.stop_requested and self.mode == Mode.LIVE:
                    self.recorder.on_risk_event('restart', 'agent stopped — LIVE positions LEFT OPEN '
                                                '(LIVE_FLATTEN_ON_EXIT=0); broker-side stops remain', now)
                    self.say('risk', 'Agent stopped — LIVE positions left OPEN on purpose (LIVE_FLATTEN_ON_EXIT=0). '
                             'Their broker-side stops still stand; the watchdog will not touch them while they are protected.', phase='alert')
                elif self.stop_requested:
                    n = len([p for p in self.broker.positions.values() if p.qty])
                    self.recorder.on_risk_event('restart', f'process restart signal — preserved {n} position(s)', now)
                    self.say('system', f'Process restart signal — preserved {n} position(s) instead of realizing an '
                             'off-strategy exit. They will be hydrated and managed when the agent returns.')
                else:
                    self.say('system', 'Agent exiting.')
                if self.mode in (Mode.PAPER, Mode.LIVE):
                    try:
                        self.reconcile(now)
                    except Exception:
                        log.exception('final reconcile failed')
                persist_broker(self.account, self.broker, self.instruments, risk=self.risk)
        except Exception:
            log.exception('shutdown flatten failed')
        if hasattr(self, 'narrator'):
            self.narrator.flush()
        if self.run_row is not None:
            self.run_row.status = 'stopped'
            self.run_row.state = 'stopped'
            self.run_row.stopped_at = now
            self.run_row.save(update_fields=['status', 'state', 'stopped_at'])
        if self._lock_fh is not None:
            try:
                fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
                self._lock_fh.close()
            except OSError:
                pass
        log.info('agent stopped')
