"""Performance metrics from a trade list and an equity series. Pure Python."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd

from .data import calendar as cal


def _safe(x, default=0.0):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return default
    return float(x)


def daily_equity(equity: list) -> pd.Series:
    """Last equity of each ET calendar day."""
    if not equity:
        return pd.Series(dtype='float64')
    ts = [e[0] for e in equity]
    eq = [e[3] if len(e) > 3 else e[1] for e in equity]
    s = pd.Series(eq, index=pd.DatetimeIndex(ts))
    if s.index.tz is None:
        s.index = s.index.tz_localize('UTC')
    days = s.index.tz_convert(cal.ET).date
    return s.groupby(days).last()


def drawdown_stats(daily: pd.Series) -> dict:
    if len(daily) == 0:
        return {'max_drawdown_pct': 0.0, 'max_dd_days': 0}
    peak = daily.cummax()
    dd = (daily / peak - 1) * 100
    max_dd = float(dd.min())
    # Longest stretch below the previous peak.
    longest = cur = 0
    for v in dd:
        if v < 0:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 0
    return {'max_drawdown_pct': _safe(max_dd), 'max_dd_days': int(longest)}


def compute_metrics(trades: list, equity: list, starting_cash: float, *, bars_seen: int = 0,
                    bars_with_position: int = 0, benchmark: tuple[float, float] | None = None,
                    benchmark_symbol: str = '') -> dict:
    n = len(trades)
    pnls = np.array([t.pnl for t in trades], dtype='float64') if n else np.array([], dtype='float64')
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    gross_win = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.sum()) if len(losses) else 0.0
    final_equity = equity[-1][3] if equity else starting_cash
    net = float(final_equity - starting_cash)
    daily = daily_equity(equity)
    rets = daily.pct_change().dropna() if len(daily) > 1 else pd.Series(dtype='float64')
    sharpe = sortino = 0.0
    if len(rets) > 1 and rets.std() > 0:
        sharpe = float(rets.mean() / rets.std() * math.sqrt(252))
        downside = rets[rets < 0]
        if len(downside) > 0 and downside.std() > 0:
            sortino = float(rets.mean() / downside.std() * math.sqrt(252))
    dd = drawdown_stats(daily)
    exposure = (bars_with_position / bars_seen * 100) if bars_seen else 0.0
    per = {'symbol': defaultdict(list), 'hour': defaultdict(list), 'weekday': defaultdict(list),
           'exit_reason': defaultdict(list), 'strategy': defaultdict(list)}
    hold_minutes = []
    for t in trades:
        per['symbol'][t.symbol].append(t.pnl)
        per['hour'][t.entry_ts.astimezone(cal.ET).strftime('%H:00')].append(t.pnl)
        per['weekday'][t.entry_ts.astimezone(cal.ET).strftime('%a')].append(t.pnl)
        per['exit_reason'][t.exit_reason].append(t.pnl)
        per['strategy'][t.strategy_key or '-'].append(t.pnl)
        hold_minutes.append((t.exit_ts - t.entry_ts).total_seconds() / 60)

    def summarize(groups):
        out = {}
        for k, v in sorted(groups.items()):
            arr = np.array(v)
            out[k] = {'trades': len(arr), 'pnl': _safe(arr.sum()),
                      'win_rate': _safe((arr > 0).mean() * 100) if len(arr) else 0.0,
                      'avg': _safe(arr.mean()) if len(arr) else 0.0}
        return out

    m = {
        'starting_cash': float(starting_cash),
        'final_equity': _safe(final_equity),
        'net_pnl': _safe(net),
        'return_pct': _safe(net / starting_cash * 100) if starting_cash else 0.0,
        'trades': n,
        'wins': int(len(wins)),
        'losses': int(len(losses)),
        'win_rate': _safe(len(wins) / n * 100) if n else 0.0,
        'profit_factor': _safe(gross_win / gross_loss) if gross_loss > 0 else (_safe(gross_win) if gross_win else 0.0),
        'expectancy': _safe(pnls.mean()) if n else 0.0,
        'avg_win': _safe(wins.mean()) if len(wins) else 0.0,
        'avg_loss': _safe(losses.mean()) if len(losses) else 0.0,
        'largest_win': _safe(pnls.max()) if n else 0.0,
        'largest_loss': _safe(pnls.min()) if n else 0.0,
        'fees': _safe(sum(t.fees for t in trades)),
        'avg_bars_held': _safe(np.mean([t.bars_held for t in trades])) if n else 0.0,
        'avg_hold_minutes': _safe(np.mean(hold_minutes)) if hold_minutes else 0.0,
        'sharpe': _safe(sharpe),
        'sortino': _safe(sortino),
        'max_drawdown_pct': dd['max_drawdown_pct'],
        'max_dd_days': dd['max_dd_days'],
        'days': int(len(daily)),
        'best_day_pct': _safe(rets.max() * 100) if len(rets) else 0.0,
        'worst_day_pct': _safe(rets.min() * 100) if len(rets) else 0.0,
        'exposure_pct': _safe(exposure),
        'per_symbol': summarize(per['symbol']),
        'per_hour': summarize(per['hour']),
        'per_weekday': summarize(per['weekday']),
        'per_exit_reason': summarize(per['exit_reason']),
        'per_strategy': summarize(per['strategy']),
    }
    if benchmark and benchmark[0]:
        b_ret = (benchmark[1] / benchmark[0] - 1) * 100
        m['benchmark_symbol'] = benchmark_symbol
        m['benchmark_return_pct'] = _safe(b_ret)
        m['alpha_pct'] = _safe(m['return_pct'] - b_ret)
        m['exposure_adjusted_alpha_pct'] = _safe(m['return_pct'] - b_ret * exposure / 100)
    return m


def downsample_equity(equity: list, max_points: int = 1500) -> list:
    """[[epoch_seconds, equity], ...] thinned evenly."""
    if not equity:
        return []
    step = max(1, len(equity) // max_points)
    out = [[int(e[0].timestamp()), round(float(e[3]), 2)] for e in equity[::step]]
    last = equity[-1]
    if out[-1][0] != int(last[0].timestamp()):
        out.append([int(last[0].timestamp()), round(float(last[3]), 2)])
    return out


def objective_value(metrics: dict, objective: str, min_trades: int = 0) -> float:
    """Scalar used by the optimizer; -inf when the sample is too small."""
    if metrics.get('trades', 0) < min_trades:
        return float('-inf')
    key = {'sharpe': 'sharpe', 'profit_factor': 'profit_factor', 'net_pnl': 'net_pnl',
           'expectancy': 'expectancy', 'return': 'return_pct', 'sortino': 'sortino'}.get(objective, 'sharpe')
    return float(metrics.get(key, 0.0))
