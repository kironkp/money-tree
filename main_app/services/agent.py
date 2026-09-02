"""The agent loop: live (sim / paper / live) and replay.

One process per account, guarded by a file lock. Each tick:
  sync the newest COMPLETED bars → engine.process_bar per new bar →
  persist the ledger → equity snapshot → heartbeat.
Catch-up after a laptop sleep evaluates stops on the missed bars without
opening anything new. SIGTERM/SIGINT flatten (sim/paper) and stop cleanly.
"""
from __future__ import annotations

import fcntl
import logging
import os
import signal
import time
from datetime import UTC, date, datetime, timedelta

from django.conf import settings
from django.utils import timezone

from main_app.models import (Account, AgentConfig, AgentRun, FeedEvent, Instrument, Market, Mode, RiskEvent, Stage,
                             Strategy)

from .backtest import date_bounds
from .broker.sim import SimBroker
from .data import calendar as cal
from .data import get_provider
from .data.store import complete_bars_only, load_frame, quality_gate, synthetic_symbols, upsert_bars
from .engine import Engine, EngineConfig
from .indicators import minutes_to_close as _mtc
from .ledger import DBRecorder, hydrate_broker, open_orders_from_db, persist_broker
from .narrator import Narrator
from .risk import RiskConfig, RiskManager
from .strategies import make_strategy
from .timeframes import floor_to_bar, tf_delta

log = logging.getLogger('moneytree.agent')

WINDOW_BARS = 420          # trailing bars loaded per symbol each tick (≈ 5 sessions of 5-min)
SNAPSHOT_EVERY = timedelta(minutes=5)
STAGE_FOR_MODE = {
    Mode.SIM: (Stage.SPROUT, Stage.SAPLING, Stage.TREE),
    Mode.PAPER: (Stage.SAPLING, Stage.TREE),
    Mode.LIVE: (Stage.TREE,),
    Mode.REPLAY: (Stage.SEED, Stage.SPROUT, Stage.SAPLING, Stage.TREE),
}


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
        self.last_tick: datetime | None = None
        self.last_snapshot: datetime | None = None
        self.last_processed: dict[str, datetime] = {}
        self.eod_done: date | None = None
        self.run_row: AgentRun | None = None
        self._lock_fh = None

    # --- setup --------------------------------------------------------------
    def setup(self) -> None:
        self.cfg = AgentConfig.get()
        if self.mode == Mode.LIVE and not (settings.LIVE_TRADING_ARMED and self.cfg.mode == Mode.LIVE):
            raise RuntimeError('live mode refused: set LIVE_TRADING_ARMED=1 in .env AND arm live mode in Settings')
        if self.mode in (Mode.PAPER, Mode.LIVE) and not settings.ALPACA_ENABLED and self.mode == Mode.PAPER:
            raise RuntimeError('paper mode needs Alpaca keys in .env')
        self._acquire_lock()
        self.account = Account.for_mode(self.mode, self.market)
        if self.mode == Mode.REPLAY:
            self.account.reset()
        self.narrator = Narrator(self.account, echo=not self.quiet)
        self.timeframe = self.cfg.timeframe_for(self.market)
        self.instruments = {i.symbol: i for i in Instrument.objects.filter(in_watchlist=True, active=True,
                                                                         asset_class__in=self.account.asset_classes)}
        if not self.instruments:
            raise RuntimeError(f'no {self.market} symbols on the watchlist — add some at /data/')
        self.asset_classes = {s: i.asset_class for s, i in self.instruments.items()}
        self.qty_increments = {s: float(i.qty_increment) for s, i in self.instruments.items()}
        stages = STAGE_FOR_MODE[self.mode]
        rows = list(Strategy.objects.filter(enabled=True, stage__in=stages, market=self.market))
        self.strategies = []
        self.strategy_symbols: dict[str, set] = {}
        for row in rows:
            strat = make_strategy(row.key, row.params)
            self.strategies.append(strat)
            syms = set(row.symbols or self.instruments)
            self.strategy_symbols[row.key] = {s for s in syms if s in self.instruments and strat.supports(self.asset_classes[s])}
        risk_cfg = RiskConfig.from_model(self.cfg)
        self.risk = RiskManager(risk_cfg, self.qty_increments)
        self.risk.kill_switch = self.cfg.kill_switch
        self.risk.trading_enabled = self.cfg.trading_enabled
        engine_cfg = EngineConfig(timeframe=self.timeframe, mode=self.mode, asset_classes=self.asset_classes, risk=risk_cfg)
        fee_bps = {'stock': float(self.cfg.fee_bps_stock), 'etf': float(self.cfg.fee_bps_stock), 'crypto': float(self.cfg.fee_bps_crypto)}
        if self.mode in (Mode.SIM, Mode.REPLAY):
            self.broker = SimBroker(float(self.account.cash), immediate_fills=(self.mode == Mode.SIM),
                                    slippage_bps=float(self.cfg.slippage_bps), fee_bps=fee_bps,
                                    liquidity_cap_pct=float(self.cfg.liquidity_cap_pct),
                                    asset_classes=self.asset_classes, qty_increments=self.qty_increments)
            if self.mode == Mode.SIM:
                n = hydrate_broker(self.account, self.broker)
                stale = open_orders_from_db(self.account)
                log.info('hydrated %d positions, canceled %d stale orders', n, stale)
        else:
            from .broker.alpaca import AlpacaBroker
            self.broker = AlpacaBroker(paper=(self.mode == Mode.PAPER), asset_classes=self.asset_classes,
                                       qty_increments=self.qty_increments, mode_is_live=(self.cfg.mode == Mode.LIVE))
            hydrate_broker(self.account, self.broker)
            info = self.broker.sync()
            log.info('alpaca sync: %s', info)
            for sym in info.get('adopted', []):
                RiskEvent.objects.create(account=self.account, kind='external_position',
                                         message=f'{sym}: position found at the broker but not on our books — adopted as external')
        self.recorder = DBRecorder(self.account, self.instruments)
        self.engine = Engine(self.strategies, self.broker, engine_cfg, self.recorder, self.risk, narrator=self.narrator)
        persist_broker(self.account, self.broker, self.instruments)
        self.run_row = AgentRun.objects.create(account=self.account, mode=self.mode, market=self.market, pid=os.getpid(),
                                               replay_date=self.replay_date, speed=self.speed,
                                               message='starting')
        AgentRun.objects.filter(account=self.account, status='running').exclude(pk=self.run_row.pk).update(
            status='stopped', stopped_at=timezone.now(), message='superseded')
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        names = ', '.join(f'{s.key} ({len(self.strategy_symbols[s.key])} symbols)' for s in self.strategies) or 'none enabled'
        log.info('MoneyTree agent up: mode=%s market=%s account=%s equity=%.2f strategies=%s', self.mode, self.market,
                 self.account.name, float(self.account.equity), names)
        FeedEvent.prune()
        self.say('system', f'{self.market.capitalize()} agent up in {self.mode} mode — seed ${float(self.account.equity):,.2f}, '
                 f'{len(self.instruments)} symbols ({", ".join(self.instruments)}), {self.timeframe} bars. Strategies: {names}.')
        if not self.strategies:
            self.say('risk', 'No strategy is enabled for this market at a stage this mode allows — I will watch bars '
                     'but never trade. Enable one on the Strategies page.')
        if self.mode != Mode.REPLAY:
            fake = synthetic_symbols(self.instruments.values(), self.timeframe)
            if fake:
                self.say('risk', f'Stored history for {", ".join(fake)} is synthetic (a seeded random walk). I ignore it for '
                         'live decisions, so indicators warm up from real bars only — sync real history on the Data page.')
        self.narrator.flush()

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

    def heartbeat(self, message: str = '') -> None:
        self.narrator.flush()
        if self.run_row is None:
            return
        self.run_row.last_tick_at = timezone.now()
        self.run_row.ticks += 1
        if message:
            self.run_row.message = message[:300]
        self.run_row.save(update_fields=['last_tick_at', 'ticks', 'message'])

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < end:
            if self.stop_requested:
                raise AgentStop()
            time.sleep(min(1.0, end - time.monotonic()))
        if self.stop_requested:
            raise AgentStop()

    def refresh_flags(self, now: datetime) -> None:
        cfg = AgentConfig.get()
        if cfg.kill_switch and not self.risk.kill_switch:
            n = self.engine.flatten_all(now, 'kill')
            self.recorder.on_risk_event('kill_switch', f'kill switch engaged from Settings — flattened {n} positions', now)
            self.say('risk', f'KILL SWITCH engaged from the dashboard — flattened {n} position(s); no entries until it is reset.')
            persist_broker(self.account, self.broker, self.instruments)
        elif not cfg.kill_switch and self.risk.kill_switch:
            self.say('system', 'Kill switch reset — entries allowed again.')
        if cfg.trading_enabled != self.risk.trading_enabled:
            self.say('system', 'Trading ' + ('enabled' if cfg.trading_enabled else 'disabled') + ' in Settings.')
        self.risk.kill_switch = cfg.kill_switch
        self.risk.trading_enabled = cfg.trading_enabled
        self.cfg = cfg

    def strategies_for(self, symbol: str) -> list:
        return [s for s in self.strategies if symbol in self.strategy_symbols[s.key]]

    def run_live(self) -> None:
        provider = get_provider(self.provider_name, live_feed=True)
        self.exclude_sources = [] if provider.name == 'synthetic' else ['synthetic']
        grace = 5 if provider.name == 'alpaca' else 20
        stock_syms = [s for s, ac in self.asset_classes.items() if ac != 'crypto']
        crypto_syms = [s for s, ac in self.asset_classes.items() if ac == 'crypto']
        step = tf_delta(self.timeframe)
        log.info('live loop: provider=%s timeframe=%s stocks=%d crypto=%d', provider.name, self.timeframe,
                 len(stock_syms), len(crypto_syms))
        feed_note = {'alpaca': 'Alpaca (IEX real-time)', 'yahoo': 'Yahoo (free, ~20 s late, rate-limited)',
                     'synthetic': 'SYNTHETIC random walk'}.get(provider.name, provider.name)
        self.say('system', f'Quotes from {feed_note}. ' + (
            'Crypto trades around the clock; I check every 5 minutes, a few seconds after each bar closes.'
            if self.market == Market.CRYPTO else
            'US stocks trade 09:30–16:00 ET Mon–Fri (early closes 13:00 ET); I sleep outside the session.'))
        self.narrator.flush()
        closed_note_at = None
        announced_bar = None
        while True:
            now = timezone.now()
            self.refresh_flags(now)
            stocks_open = bool(stock_syms) and cal.is_open(now)
            if not stocks_open and self.eod_done != cal.session_date(now) and cal.session_for(cal.session_date(now)) \
                    and now > cal.session_for(cal.session_date(now)).close_utc and self.last_tick is not None:
                self.end_of_day(now)
            active = (stock_syms if stocks_open else []) + crypto_syms
            if not active:
                nxt = cal.next_open(now)
                if closed_note_at is None or now - closed_note_at > timedelta(hours=1):
                    self.say('system', f'Market closed — next open {nxt.astimezone(cal.ET):%a %b %-d %H:%M} ET '
                             f'({(nxt - now).total_seconds() / 3600:.1f} h). Sleeping; the crypto agent is the one that never sleeps.')
                    closed_note_at = now
                self.heartbeat(f'market closed — next open {nxt.astimezone(cal.ET):%a %H:%M} ET')
                self._sleep(min(60.0, max(1.0, (nxt - now).total_seconds())))
                if self.once:
                    return
                continue
            closed_note_at = None
            wake = floor_to_bar(now, self.timeframe) + step + timedelta(seconds=grace)
            if announced_bar != wake:
                self.say('system', f'Waiting for the {(wake - step - timedelta(seconds=grace)).astimezone(cal.ET):%H:%M} ET bar to close '
                         f'(I read it at {wake.astimezone(cal.ET):%H:%M:%S}).')
                announced_bar = wake
            self.heartbeat(f'waiting for the {wake.astimezone(cal.ET):%H:%M:%S} ET bar')
            self._sleep((wake - now).total_seconds())
            now = timezone.now()
            try:
                self.tick(now, provider, active, grace)
            except AgentStop:
                raise
            except Exception as exc:
                log.exception('tick failed')
                self.recorder.on_risk_event('error', f'tick failed: {exc!r}'[:300], now)
                self.say('error', f'Tick failed: {exc!r} — retrying next bar.'[:400])
                self.heartbeat(f'error: {exc!r}'[:300])
                self._sleep(10)
            if self.once:
                return

    def tick(self, now: datetime, provider, symbols: list, grace: int) -> None:
        step = tf_delta(self.timeframe)
        missed = self.last_tick is not None and (now - self.last_tick) > 2 * step
        if missed:
            self.recorder.on_risk_event('missed_ticks', f'no tick for {(now - self.last_tick).total_seconds() / 60:.0f} min — '
                                        'catch-up: stops evaluated, no new entries from stale bars', now)
            self.say('risk', f'I missed {(now - self.last_tick).total_seconds() / 60:.0f} minutes (laptop asleep?). Catching up: '
                     'stops and targets are checked on the missed bars, but no new entries from stale data.')
        by_class: dict[str, list] = {}
        for s in symbols:
            by_class.setdefault(self.asset_classes[s], []).append(s)
        for ac, syms in by_class.items():
            since_candidates = [self.last_processed[s] for s in syms if s in self.last_processed]
            since = (min(since_candidates) - step) if since_candidates else (now - 6 * step)
            since = max(since, now - timedelta(days=3))
            try:
                frames = provider.latest_bars(syms, self.timeframe, since, ac)
            except Exception as exc:
                log.warning('provider %s failed for %s: %s', provider.name, ac, exc)
                self.recorder.on_risk_event('error', f'data fetch failed ({provider.name}, {ac}): {exc!r}'[:300], now)
                self.say('error', f'Could not fetch {ac} bars from {provider.name}: {exc!r}'[:400])
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
            self.say('system', f'{now.astimezone(cal.ET):%H:%M:%S} — {len(fetched)} new {ac} bar(s) from {provider.name}'
                     + (': ' + ', '.join(fetched) if fetched else ' (nothing new yet — the feed lags a little)'))
        processed = 0
        for s in symbols:
            strategies = self.strategies_for(s)
            inst = self.instruments[s]
            window = load_frame(inst, self.timeframe, limit=WINDOW_BARS, exclude_sources=self.exclude_sources)
            window = complete_bars_only(window, self.timeframe, now, grace)
            if len(window) == 0:
                continue
            last = self.last_processed.get(s)
            if last is None:
                # First sight of this symbol: act on the newest bar only.
                new_positions = [len(window) - 1]
            else:
                new_positions = [i for i, ts in enumerate(window.index) if ts.to_pydatetime() > last]
            if not new_positions:
                continue
            ac = self.asset_classes[s]
            need = max((st.warmup_bars for st in strategies), default=0)
            if strategies and len(window) < need:
                self.say('bar', f'{s}: {len(window)}/{need} real bars stored — indicators still warming up, no decisions yet.', symbol=s)
            prepared = {st.key: st.prepare(window, ac, self.timeframe) for st in strategies}
            rows = {k: list(df.itertuples(index=True)) for k, df in prepared.items()}
            base_rows = list(window.itertuples(index=True))
            mtc = _mtc(window.index, ac).tolist()
            stale = new_positions[:-1] if missed else []
            for i in new_positions:
                ts = window.index[i].to_pydatetime()
                if i in stale:
                    was = self.risk.trading_enabled
                    self.risk.trading_enabled = False
                    try:
                        self.engine.process_bar(s, ts, base_rows[i], {k: r[i] for k, r in rows.items()}, prepared, i, mtc[i])
                    finally:
                        self.risk.trading_enabled = was
                else:
                    self.engine.process_bar(s, ts, base_rows[i], {k: r[i] for k, r in rows.items()}, prepared, i, mtc[i])
                self.last_processed[s] = ts
                processed += 1
        persist_broker(self.account, self.broker, self.instruments)
        if self.last_snapshot is None or now - self.last_snapshot >= SNAPSHOT_EVERY:
            self.engine.record_equity(now)
            self.last_snapshot = now
        a = self.broker.account()
        n_pos = len([p for p in self.broker.positions.values() if p.qty])
        self.say('system', f'Equity ${a.equity:,.2f} (day {self.risk.day_pnl(a.equity):+,.2f}), cash ${a.cash:,.2f}, '
                 f'{n_pos} open position(s). Next check in {tf_delta(self.timeframe).seconds // 60} min.')
        self.heartbeat(f'tick ok — {processed} bars, equity {a.equity:,.2f}, {n_pos} positions')
        self.last_tick = now
        if not self.quiet:
            log.info('tick %s: %d bars processed, equity %.2f', now.astimezone(cal.ET).strftime('%H:%M:%S'), processed, a.equity)

    def end_of_day(self, now: datetime) -> None:
        from .journal import write_eod_journal
        d = cal.session_date(now)
        self.eod_done = d
        n = self.engine.flatten_all(now, 'eod')
        if n:
            self.recorder.on_risk_event('flatten', f'end of day: flattened {n} leftover positions', now)
        persist_broker(self.account, self.broker, self.instruments)
        self.engine.record_equity(now)
        try:
            entry = write_eod_journal(self.account, d)
            log.info('journal written: %s', entry.title)
            self.say('journal', f'END OF DAY {d}: {entry.title}. ' + entry.body.split('\n\n')[0][:300])
        except Exception:
            log.exception('journal failed')
            self.say('error', 'Journal failed — see the agent log.')
        FeedEvent.prune()
        if settings.COACH_ENABLED:
            try:
                from .coach import coach_review
                coach_review(self.account, d)
            except Exception:
                log.exception('coach failed')
        self.heartbeat(f'end of day {d} done')

    # --- replay -------------------------------------------------------------
    def run_replay(self) -> None:
        from .journal import write_eod_journal
        d = self.replay_date
        session = cal.session_for(d)
        a, b = date_bounds(d, d)
        warm_start = a - timedelta(days=5)
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
        pause = tf_delta(self.timeframe).total_seconds() / self.speed
        log.info('replay %s: %d bars across %d symbols at %.0fx (%.1fs per bar)', d, len(events), len(frames), self.speed, pause)
        self.say('system', f'REPLAY of {d:%A %b %-d}: {len(events)} bars across {len(frames)} symbols at {self.speed:.0f}× '
                 f'({pause:.1f} s per bar). Same engine as live; fills at the next bar\'s open.')
        self.heartbeat(f'replaying {d} at {self.speed:.0f}x')
        last_ts = None
        for ts, s, i in events:
            if last_ts is not None and ts != last_ts:
                persist_broker(self.account, self.broker, self.instruments)
                self.engine.record_equity(last_ts)
                acct = self.broker.account()
                self.heartbeat(f'replay {ts.astimezone(cal.ET):%H:%M} ET — equity {acct.equity:,.2f}')
                self._sleep(min(pause, 10.0))
            self.engine.process_bar(s, ts, base_rows[s][i], {k: r[i] for k, r in rows[s].items()}, prepared[s], i, mtc[s][i])
            last_ts = ts
        if last_ts is not None:
            self.engine.flatten_all(last_ts, 'end')
            persist_broker(self.account, self.broker, self.instruments)
            self.engine.record_equity(last_ts)
        entry = write_eod_journal(self.account, d)
        self.say('journal', f'REPLAY DONE — {entry.title}. ' + entry.body.split('\n\n')[0][:300])
        self.heartbeat(f'replay {d} finished')
        log.info('replay finished: equity %.2f, %d trades', self.broker.account().equity, len(self.broker.trades))

    # --- teardown -----------------------------------------------------------
    def shutdown(self) -> None:
        now = timezone.now()
        try:
            if hasattr(self, 'engine'):
                flatten = self.mode in (Mode.SIM, Mode.PAPER, Mode.REPLAY) or settings.LIVE_FLATTEN_ON_EXIT
                if self.stop_requested and flatten:
                    n = self.engine.flatten_all(now, 'manual')
                    if n:
                        self.recorder.on_risk_event('flatten', f'agent stopped — flattened {n} positions', now)
                    self.say('system', f'Stopped by request — {n} position(s) closed. Bye.')
                elif self.stop_requested:
                    self.broker.cancel_open_orders()
                    self.recorder.on_risk_event('flatten', 'agent stopped — live positions LEFT OPEN (LIVE_FLATTEN_ON_EXIT=0)', now)
                    self.say('risk', 'Stopped by request — LIVE positions left open (LIVE_FLATTEN_ON_EXIT=0).')
                else:
                    self.say('system', 'Agent exiting.')
                persist_broker(self.account, self.broker, self.instruments)
        except Exception:
            log.exception('shutdown flatten failed')
        if hasattr(self, 'narrator'):
            self.narrator.flush()
        if self.run_row is not None:
            self.run_row.status = 'stopped'
            self.run_row.stopped_at = now
            self.run_row.save(update_fields=['status', 'stopped_at'])
        if self._lock_fh is not None:
            try:
                fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
                self._lock_fh.close()
            except OSError:
                pass
        log.info('agent stopped')
