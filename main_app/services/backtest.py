"""Backtest runner. `run_backtest()` is pure (frames in, result out) so the
optimizer's worker processes can call it without Django; `persist_run()` and
`run_backtest_for_model()` are the Django side."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time as dtime, timedelta

import pandas as pd

from .broker.sim import SimBroker
from .data import calendar as cal
from .engine import Engine, EngineConfig, MemoryRecorder
from .metrics import compute_metrics, downsample_equity
from .risk import RiskConfig, RiskManager
from .strategies import make_strategy


@dataclass
class BacktestSpec:
    strategy_key: str
    params: dict
    symbols: list
    timeframe: str = '5Min'
    starting_cash: float = 10000.0
    risk: RiskConfig = field(default_factory=RiskConfig)
    fee_bps: dict = field(default_factory=lambda: {'stock': 0.5, 'etf': 0.5, 'crypto': 25.0})
    liquidity_cap_pct: float = 1.0
    asset_classes: dict = field(default_factory=dict)
    qty_increments: dict = field(default_factory=dict)
    benchmark_symbol: str = 'SPY'
    act_from: object = None  # warm-up cutoff (datetime) — bars before it feed indicators only


@dataclass
class BacktestResult:
    metrics: dict
    trades: list
    equity: list
    signals: list
    risk_events: list
    duration_s: float
    bars_seen: int = 0
    bars_with_position: int = 0


def run_backtest(spec: BacktestSpec, frames: dict[str, pd.DataFrame],
                 benchmark_frame: pd.DataFrame | None = None) -> BacktestResult:
    t0 = time.time()
    strat = make_strategy(spec.strategy_key, spec.params)
    frames = {s: df for s, df in frames.items() if len(df) and strat.supports(spec.asset_classes.get(s, 'stock'))}
    broker = SimBroker(spec.starting_cash, immediate_fills=False, slippage_bps=spec.risk.slippage_bps,
                       fee_bps=spec.fee_bps, liquidity_cap_pct=spec.liquidity_cap_pct,
                       asset_classes=spec.asset_classes, qty_increments=spec.qty_increments)
    cfg = EngineConfig(timeframe=spec.timeframe, mode='bt', asset_classes=spec.asset_classes, risk=spec.risk)
    rec = MemoryRecorder()
    engine = Engine([strat], broker, cfg, rec, RiskManager(spec.risk, spec.qty_increments))
    engine.run_frames(frames, act_from=spec.act_from)
    bench = None
    bdf = benchmark_frame if benchmark_frame is not None else frames.get(spec.benchmark_symbol)
    if bdf is None and frames:
        bdf = next(iter(frames.values()))
    bench_symbol = spec.benchmark_symbol if (benchmark_frame is not None or spec.benchmark_symbol in frames) else (next(iter(frames)) if frames else '')
    if bdf is not None and len(bdf):
        bench = (float(bdf['close'].iloc[0]), float(bdf['close'].iloc[-1]))
    metrics = compute_metrics(rec.trades, rec.equity, spec.starting_cash, bars_seen=engine.bars_seen,
                              bars_with_position=engine.bars_with_position, benchmark=bench,
                              benchmark_symbol=bench_symbol)
    metrics['signals'] = len(rec.signals)
    metrics['blocked_signals'] = sum(1 for s in rec.signals if s[2] is not None and not s[2].allowed)
    metrics['blocked_reasons'] = _count_blocked(rec.signals)
    return BacktestResult(metrics, rec.trades, rec.equity, rec.signals, rec.risk_events, time.time() - t0,
                          engine.bars_seen, engine.bars_with_position)


def _count_blocked(signals) -> dict:
    out: dict[str, int] = {}
    for sig, key, decision, order in signals:
        if decision is not None and not decision.allowed:
            reason = decision.reason.split(' (')[0]
            out[reason] = out.get(reason, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def date_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """UTC bounds covering [start, end] as ET calendar days."""
    a = datetime.combine(start, dtime(0, 0), tzinfo=cal.ET).astimezone(UTC)
    b = datetime.combine(end + timedelta(days=1), dtime(0, 0), tzinfo=cal.ET).astimezone(UTC)
    return a, b


# --- Django side ---------------------------------------------------------

def spec_from_models(strategy_key: str, params: dict, symbols: list, timeframe: str, cfg,
                     starting_cash=None) -> BacktestSpec:
    from main_app.models import Instrument
    instruments = {i.symbol: i for i in Instrument.objects.filter(symbol__in=symbols)}
    return BacktestSpec(
        strategy_key=strategy_key, params=params or {}, symbols=list(symbols), timeframe=timeframe,
        starting_cash=float(starting_cash if starting_cash is not None else cfg.starting_cash),
        risk=RiskConfig.from_model(cfg),
        fee_bps={'stock': float(cfg.fee_bps_stock), 'etf': float(cfg.fee_bps_stock), 'crypto': float(cfg.fee_bps_crypto)},
        liquidity_cap_pct=float(cfg.liquidity_cap_pct),
        asset_classes={s: instruments[s].asset_class for s in symbols if s in instruments},
        qty_increments={s: float(instruments[s].qty_increment) for s in symbols if s in instruments},
    )


def load_frames(symbols: list, timeframe: str, start: date, end: date) -> dict[str, pd.DataFrame]:
    from main_app.models import Instrument
    from .data.store import load_frame, quality_gate
    a, b = date_bounds(start, end)
    frames = {}
    for inst in Instrument.objects.filter(symbol__in=symbols):
        df = load_frame(inst, timeframe, a, b)
        df, _ = quality_gate(df, timeframe, inst.asset_class)
        frames[inst.symbol] = df
    return frames


def run_backtest_for_model(run) -> None:
    """Execute a BacktestRun row in-process and persist the result."""
    from django.utils import timezone

    from main_app.models import AgentConfig, BacktestTrade

    cfg = AgentConfig.get()
    run.status = 'running'
    run.save(update_fields=['status'])
    try:
        spec = spec_from_models(run.strategy_key, run.params, run.symbols, run.timeframe, cfg, run.starting_cash)
        frames = load_frames(run.symbols, run.timeframe, run.start, run.end)
        bench = None
        if spec.benchmark_symbol not in frames:
            bframes = load_frames([spec.benchmark_symbol], run.timeframe, run.start, run.end)
            bench = bframes.get(spec.benchmark_symbol)
            if bench is not None and len(bench) == 0:
                bench = None
        result = run_backtest(spec, frames, bench)
        persist_result(run, spec, result)
    except Exception as exc:
        run.status = 'failed'
        run.error = repr(exc)
        run.finished_at = timezone.now()
        run.save(update_fields=['status', 'error', 'finished_at'])
        raise


def persist_result(run, spec: BacktestSpec, result: BacktestResult, max_trades: int = 5000) -> None:
    from django.utils import timezone

    from main_app.models import BacktestTrade

    run.metrics = result.metrics
    run.equity_curve = downsample_equity(result.equity)
    run.config_snapshot = {'risk': spec.risk.as_dict(), 'fee_bps': spec.fee_bps,
                           'liquidity_cap_pct': spec.liquidity_cap_pct, 'starting_cash': spec.starting_cash}
    run.status = 'done'
    run.duration_s = result.duration_s
    run.finished_at = timezone.now()
    run.save()
    run.trades.all().delete()
    BacktestTrade.objects.bulk_create([
        BacktestTrade(run=run, symbol=t.symbol, side=t.side, qty=t.qty, entry_ts=t.entry_ts, exit_ts=t.exit_ts,
                      entry_price=t.entry_price, exit_price=t.exit_price, pnl=t.pnl, pnl_pct=t.pnl_pct,
                      fees=t.fees, bars_held=t.bars_held, exit_reason=t.exit_reason)
        for t in result.trades[:max_trades]
    ], batch_size=1000)
