"""Parameter search: grid, random, and walk-forward.

Walk-forward is the honest one: choose params on a training window, judge
them on the following test window, roll forward, and report the
in-sample → out-of-sample decay plus how stable the winner's neighbourhood is.
"""
from __future__ import annotations

import itertools
import logging
import os
import random
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import date, timedelta

import pandas as pd

from .backtest import BacktestResult, BacktestSpec, date_bounds, run_backtest
from .metrics import compute_metrics, downsample_equity, objective_value
from .strategies import get_strategy_class

log = logging.getLogger('moneytree.optimize')

MAX_GRID = 60
MAX_RANDOM = 40
WORKERS = 2

# Set before forking; workers read them.
_SPEC: BacktestSpec | None = None
_FRAMES: dict | None = None
_BENCH: pd.DataFrame | None = None


def grid_from_schema(strategy_key: str, overrides: dict | None = None, max_points: int = 4) -> dict:
    """{param: [values]} — numeric/choice params spread across their range;
    bools stay at their default unless overridden."""
    cls = get_strategy_class(strategy_key)
    grid = {}
    overrides = overrides or {}
    for p in cls.params:
        if p.name in overrides and overrides[p.name] not in (None, '', []):
            vals = overrides[p.name]
            grid[p.name] = [p.coerce(v) for v in (vals if isinstance(vals, (list, tuple)) else [vals])]
        elif p.type == 'bool':
            grid[p.name] = [p.default]
        else:
            grid[p.name] = p.grid(max_points)
    return grid


def enumerate_combos(grid: dict, method: str = 'grid', max_combos: int | None = None, seed: int = 7) -> list[dict]:
    names = list(grid)
    if method == 'random':
        max_combos = max_combos or MAX_RANDOM
        rng = random.Random(seed)
        seen, out = set(), []
        total = 1
        for n in names:
            total *= max(1, len(grid[n]))
        target = min(max_combos, total)
        while len(out) < target:
            combo = {n: rng.choice(grid[n]) for n in names}
            key = tuple(combo[n] for n in names)
            if key in seen:
                continue
            seen.add(key)
            out.append(combo)
        return out
    max_combos = max_combos or MAX_GRID
    combos = [dict(zip(names, vals)) for vals in itertools.product(*[grid[n] for n in names])]
    if len(combos) > max_combos:
        idx = [round(i * (len(combos) - 1) / (max_combos - 1)) for i in range(max_combos)]
        combos = [combos[i] for i in idx]
    return combos


def slice_frames(frames: dict, start: date, end: date) -> dict:
    a, b = date_bounds(start, end)
    return {s: df[(df.index >= a) & (df.index < b)] for s, df in frames.items()}


def walk_forward_windows(start: date, end: date, train_days: int, test_days: int, step_days: int | None = None) -> list[dict]:
    step_days = step_days or test_days
    if step_days < test_days:
        raise ValueError('walk-forward test windows must not overlap')
    out, n = [], 1
    t0 = start
    while True:
        t1 = t0 + timedelta(days=train_days - 1)
        s0 = t1 + timedelta(days=1)
        s1 = s0 + timedelta(days=test_days - 1)
        if s1 > end:
            break
        out.append({'n': n, 'train_start': t0, 'train_end': t1, 'test_start': s0, 'test_end': s1})
        t0 += timedelta(days=step_days)
        n += 1
    return out


def _eval(params: dict) -> tuple[dict, dict, list, list, int, int]:
    spec = replace(_SPEC, params=params)
    r = run_backtest(spec, _FRAMES, _BENCH)
    return params, r.metrics, r.trades, r.equity, r.bars_seen, r.bars_with_position


def evaluate_all(spec: BacktestSpec, frames: dict, combos: list[dict], bench=None, progress=None,
                 workers: int = WORKERS) -> list[tuple]:
    """Run every combo; returns [(params, metrics, trades, equity, bars_seen, bars_with_position)]."""
    global _SPEC, _FRAMES, _BENCH
    _SPEC, _FRAMES, _BENCH = spec, frames, bench
    results = []
    use_pool = workers > 1 and len(combos) > 2 and os.name == 'posix'
    if use_pool:
        try:
            from django.db import connections
            connections.close_all()
        except Exception:
            pass
        import multiprocessing as mp
        try:
            ctx = mp.get_context('fork')
            with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
                for i, res in enumerate(pool.map(_eval, combos, chunksize=1)):
                    results.append(res)
                    if progress:
                        progress(i + 1, len(combos))
            return results
        except Exception as exc:  # fall back to in-process
            log.warning('process pool failed (%s); running sequentially', exc)
            results = []
    for i, c in enumerate(combos):
        results.append(_eval(c))
        if progress:
            progress(i + 1, len(combos))
    return results


def rank(results: list[tuple], objective: str, min_trades: int) -> list[dict]:
    rows = []
    for params, m, *_ in results:
        rows.append({'params': params, 'objective': objective_value(m, objective, min_trades), 'trades': m['trades'],
                     'net_pnl': m['net_pnl'], 'sharpe': m['sharpe'], 'profit_factor': m['profit_factor'],
                     'win_rate': m['win_rate'], 'max_drawdown_pct': m['max_drawdown_pct'], 'expectancy': m['expectancy']})
    rows.sort(key=lambda r: r['objective'], reverse=True)
    return rows


def training_candidate_is_viable(candidate: dict, min_trades: int) -> bool:
    """A least-bad training result is not an actionable recommendation."""
    return (
        bool(candidate)
        and candidate.get('objective') != float('-inf')
        and int(candidate.get('trades', 0) or 0) >= int(min_trades)
        and float(candidate.get('profit_factor', 0) or 0) > 1.0
        and float(candidate.get('net_pnl', 0) or 0) > 0
        and float(candidate.get('expectancy', 0) or 0) > 0
    )


def stability(ranked: list[dict], grid: dict) -> dict:
    """How the best combo's one-step neighbours fare: mean neighbour objective
    over the best objective. Near 1 = robust plateau; near 0 = lonely spike."""
    if not ranked or ranked[0]['objective'] == float('-inf'):
        return {'score': None, 'neighbours': 0}
    best = ranked[0]
    by_key = {tuple(sorted(r['params'].items())): r for r in ranked}
    neighbours = []
    for name, values in grid.items():
        if len(values) < 2 or name not in best['params']:
            continue
        try:
            idx = values.index(best['params'][name])
        except ValueError:
            continue
        for j in (idx - 1, idx + 1):
            if 0 <= j < len(values):
                p = dict(best['params'])
                p[name] = values[j]
                r = by_key.get(tuple(sorted(p.items())))
                if r is not None and r['objective'] != float('-inf'):
                    neighbours.append(r['objective'])
    if not neighbours:
        return {'score': None, 'neighbours': 0}
    mean_n = sum(neighbours) / len(neighbours)
    score = (mean_n / best['objective']) if best['objective'] > 0 else None
    return {'score': None if score is None else round(max(-1.0, min(1.5, score)), 3), 'neighbours': len(neighbours),
            'mean_neighbour_objective': round(mean_n, 4)}


def chain_oos(segments: list[tuple[list, list, int, int]], starting_cash: float) -> tuple[list, list, int, int]:
    """Stitch per-window OOS results into one trade list and equity curve."""
    trades, equity = [], []
    bars_seen = bars_pos = 0
    offset = 0.0
    for seg_trades, seg_equity, bs, bp in segments:
        trades.extend(seg_trades)
        for ts, cash, pv, eq, dp in seg_equity:
            equity.append((ts, cash + offset, pv, eq + offset, dp))
        if seg_equity:
            offset += seg_equity[-1][3] - starting_cash
        bars_seen += bs
        bars_pos += bp
    return trades, equity, bars_seen, bars_pos


def serialize_window(win: dict) -> dict:
    """Stable, JSON-safe boundaries for an auditable research window."""
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in win.items()}


def deserialize_window(win: dict) -> dict:
    """Turn persisted ISO boundaries back into dates for an exact replay."""
    out = dict(win)
    for key in ('train_start', 'train_end', 'test_start', 'test_end'):
        if isinstance(out.get(key), str):
            out[key] = date.fromisoformat(out[key])
    return out


def evaluate_fixed_params(spec: BacktestSpec, frames: dict, windows: list[dict], params: dict,
                          bench: pd.DataFrame | None = None) -> dict:
    """Replay one immutable parameter set over explicit test windows.

    This is deliberately separate from adaptive walk-forward results, where a
    different training winner may trade each test window. Only a fixed result
    can describe the configuration that would actually be installed.
    """
    segments, rows = [], []
    for raw in windows:
        win = deserialize_window(raw)
        test = slice_frames(frames, win['test_start'], win['test_end'])
        test_bench = (slice_frames({'benchmark': bench}, win['test_start'], win['test_end'])['benchmark']
                      if bench is not None else None)
        result = run_backtest(replace(spec, params=dict(params)), test, test_bench)
        segments.append((result.trades, result.equity, result.bars_seen, result.bars_with_position))
        rows.append({'window': serialize_window(win), 'metrics': result.metrics})
    trades, equity, bars_seen, bars_pos = chain_oos(segments, spec.starting_cash)
    metrics = compute_metrics(trades, equity, spec.starting_cash, bars_seen=bars_seen,
                              bars_with_position=bars_pos)
    return {'params': dict(params), 'metrics': metrics, 'windows': rows,
            'equity': downsample_equity(equity, 400)}


# --- Django side ---------------------------------------------------------

def run_experiment(exp) -> None:
    from django.utils import timezone

    from main_app.models import AgentConfig, BacktestRun

    from .backtest import load_frames, spec_from_models

    cfg = AgentConfig.get()
    exp.status, exp.pid = 'running', os.getpid()
    exp.save(update_fields=['status', 'pid'])
    last_progress = [0.0]

    def progress(done, total, base=0, scale=1.0):
        exp.done_runs = base + done
        exp.progress = min(100.0, (base + done * scale) / max(1, exp.total_runs) * 100)
        if time.time() - last_progress[0] > 2:
            exp.save(update_fields=['done_runs', 'progress'])
            last_progress[0] = time.time()

    try:
        spec = spec_from_models(exp.strategy_key, {}, exp.symbols, exp.timeframe, cfg)
        frames = load_frames(exp.symbols, exp.timeframe, exp.start, exp.end)
        bench = None
        if spec.benchmark_symbol not in frames:
            bench = load_frames([spec.benchmark_symbol], exp.timeframe, exp.start, exp.end).get(spec.benchmark_symbol)
            if bench is not None and len(bench) == 0:
                bench = None
        grid = exp.param_grid or grid_from_schema(exp.strategy_key)
        combos = enumerate_combos(grid, 'random' if exp.method == 'random' else 'grid')
        summary = {'objective': exp.objective, 'min_trades': exp.min_trades, 'grid': grid, 'combos': len(combos)}
        if exp.method in ('grid', 'random'):
            exp.total_runs = len(combos)
            exp.save(update_fields=['total_runs'])
            results = evaluate_all(spec, frames, combos, bench, progress)
            ranked = rank(results, exp.objective, exp.min_trades)
            for params, m, trades, equity, *_ in results:
                BacktestRun.objects.create(
                    strategy_key=exp.strategy_key, params=params, symbols=exp.symbols, timeframe=exp.timeframe,
                    start=exp.start, end=exp.end, starting_cash=spec.starting_cash, status='done', metrics=m,
                    equity_curve=downsample_equity(equity, 300), experiment=exp, window_label='',
                    finished_at=timezone.now())
            summary.update({'ranked': ranked[:60], 'stability': stability(ranked, grid)})
            exp.best_params = ranked[0]['params'] if ranked and ranked[0]['objective'] != float('-inf') else {}
        else:
            w = exp.windows or {}
            windows = walk_forward_windows(exp.start, exp.end, int(w.get('train_days', 60)), int(w.get('test_days', 20)),
                                           int(w.get('step_days', 0)) or None)
            if not windows:
                raise ValueError('date range too short for the chosen train/test windows')
            # One grid and one adaptive test per window, followed by a fixed
            # replay of the final candidate on every valid test window.
            exp.total_runs = len(windows) * (len(combos) + 2)
            exp.save(update_fields=['total_runs'])
            segments, rows, chosen, valid_windows = [], [], [], []
            recommended = {}
            base = 0
            for win in windows:
                train = slice_frames(frames, win['train_start'], win['train_end'])
                test = slice_frames(frames, win['test_start'], win['test_end'])
                tb = slice_frames({'b': bench}, win['train_start'], win['train_end'])['b'] if bench is not None else None
                sb = slice_frames({'b': bench}, win['test_start'], win['test_end'])['b'] if bench is not None else None
                results = evaluate_all(spec, train, combos, tb, lambda d, t, base=base: progress(d, t, base))
                base += len(combos)
                ranked = rank(results, exp.objective, exp.min_trades)
                if not ranked or ranked[0]['objective'] == float('-inf'):
                    rows.append({**{k: str(v) for k, v in win.items()}, 'skipped': 'no combo reached min_trades'})
                    base += 1
                    continue
                best = ranked[0]
                if not training_candidate_is_viable(best, exp.min_trades):
                    rows.append({
                        **{k: str(v) for k, v in win.items()},
                        'skipped': ('no positive training edge after costs — best combo: '
                                    f'{best["trades"]} trades, PF {best["profit_factor"]:.2f}, '
                                    f'net {best["net_pnl"]:+,.2f}, expectancy {best["expectancy"]:+,.2f}'),
                        'params': best['params'], 'train_objective': best['objective'],
                        'train_trades': best['trades'], 'train_net_pnl': best['net_pnl'],
                        'train_profit_factor': best['profit_factor'], 'train_expectancy': best['expectancy'],
                    })
                    base += 1
                    continue
                r_test = run_backtest(replace(spec, params=best['params']), test, sb)
                base += 1
                progress(0, 1, base)
                segments.append((r_test.trades, r_test.equity, r_test.bars_seen, r_test.bars_with_position))
                chosen.append(best['params'])
                valid_windows.append(win)
                if win['n'] == windows[-1]['n']:
                    recommended = best['params']
                for label, m, eq, params in (('train', next(x[1] for x in results if x[0] == best['params']),
                                              next(x[3] for x in results if x[0] == best['params']), best['params']),
                                             ('test', r_test.metrics, r_test.equity, best['params'])):
                    BacktestRun.objects.create(
                        strategy_key=exp.strategy_key, params=params, symbols=exp.symbols, timeframe=exp.timeframe,
                        start=win['train_start'] if label == 'train' else win['test_start'],
                        end=win['train_end'] if label == 'train' else win['test_end'],
                        starting_cash=spec.starting_cash, status='done', metrics=m,
                        equity_curve=downsample_equity(eq, 300), experiment=exp,
                        window_label=f'W{win["n"]} {label}', finished_at=timezone.now())
                rows.append({
                    'n': win['n'], 'train': f'{win["train_start"]} → {win["train_end"]}',
                    'test': f'{win["test_start"]} → {win["test_end"]}', 'params': best['params'],
                    'train_objective': best['objective'], 'train_trades': best['trades'], 'train_net_pnl': best['net_pnl'],
                    'test_objective': objective_value(r_test.metrics, exp.objective, 0),
                    'test_trades': r_test.metrics['trades'], 'test_net_pnl': r_test.metrics['net_pnl'],
                    'test_sharpe': r_test.metrics['sharpe'], 'test_profit_factor': r_test.metrics['profit_factor'],
                    'test_max_dd': r_test.metrics['max_drawdown_pct'],
                })
            trades, equity, bs, bp = chain_oos(segments, spec.starting_cash)
            oos = compute_metrics(trades, equity, spec.starting_cash, bars_seen=bs, bars_with_position=bp)
            valid = [r for r in rows if 'skipped' not in r]
            train_avg = sum(r['train_objective'] for r in valid) / len(valid) if valid else 0.0
            test_avg = sum(r['test_objective'] for r in valid) / len(valid) if valid else 0.0
            freq: dict[str, int] = {}
            for p in chosen:
                k = ', '.join(f'{a}={b}' for a, b in sorted(p.items()))
                freq[k] = freq.get(k, 0) + 1
            # Only the winner from the final chronological training window is
            # eligible for the final holdout. Never fall back to stale params
            # when the latest window found no positive after-cost edge.
            fixed = (evaluate_fixed_params(spec, frames, valid_windows, recommended, bench)
                     if recommended and valid_windows else {})
            validation = {}
            if fixed:
                last = fixed['windows'][-1]
                validation = {
                    'window': last['window'],
                    'candidate': last['metrics'],
                    'candidate_params': recommended,
                    'purpose': 'untouched final test window; this is the promotion evidence',
                }
            summary.update({'windows': rows, 'oos': oos, 'train_avg_objective': train_avg, 'test_avg_objective': test_avg,
                            'decay': (test_avg / train_avg) if train_avg else None, 'param_frequency': freq,
                            'oos_equity': downsample_equity(equity, 400),
                            'oos_kind': 'adaptive policy: each test window uses its own preceding training winner',
                            'candidate_static_oos': fixed.get('metrics'),
                            'candidate_static_equity': fixed.get('equity', []),
                            'validation': validation,
                            'recommendation_basis': (
                                'winner of the final chronological training window after its positive after-cost gate'
                                if recommended else
                                'none — the final chronological training window produced no positive after-cost candidate'
                            )})
            exp.best_params = recommended
        exp.summary = summary
        exp.status = 'done'
        exp.progress = 100.0
        exp.done_runs = exp.total_runs
        exp.finished_at = timezone.now()
        exp.save()
    except Exception as exc:
        log.exception('experiment %s failed', exp.pk)
        exp.status = 'failed'
        exp.error = repr(exc)[:2000]
        exp.finished_at = timezone.now()
        exp.save()
        raise
