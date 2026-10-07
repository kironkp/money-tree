"""Fix a failing sim strategy instead of stopping it (owner's policy, 2026-10-06).

A walk-forward proposes parameters. Selection, in order:

1. The candidate passes the existing promotion gate (auto_research's
   `comparable_verdict`): promote it normally. The caller does this.
2. Otherwise compare the SELECTION PROCEDURE's clean adaptive out-of-sample
   result (each test window traded by its own preceding training winner) with
   the CURRENT parameters, fixed, under the same regime on the same test
   windows; the current parameters are out of sample on every one of them. If
   the procedure beats current on both net and profit factor, install its
   latest pick (the final window's winner) as a NEW sim version. The installed
   params are the procedure's latest pick, not separately validated.

   Not used: replaying the latest pick fixed over every window. Its training
   data overlaps the earlier test windows, so that replay is partly in-sample
   (forex vwap 2026-10-06: +596 fixed vs +97 adaptive).
3. Otherwise keep the current version trading and record "no fix found".
   Nothing is ever disabled.

The v1.31 rule (never fall back to a least-bad configuration) still governs
qualification and promotion toward paper and live. A version installed here is
unproven, carries `evidence.kind = 'sim_fix'` rather than held-out validation,
and must earn fresh forward evidence like any other.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from main_app.models import Account, Strategy

from .backtest import load_frames, spec_from_models
from .optimize import evaluate_fixed_params, serialize_window, walk_forward_windows, with_risk_overrides
from .promotion import _subset, live_stats, promote
from .strategies import get_strategy_class

# Churn guard: a retry version is not replaced until it has had a fair run.
RETRY_MIN_TRADES = 30
RETRY_MIN_DAYS = 14


def retry_due(row: Strategy, account: Account | None, now=None) -> tuple[bool, str]:
    """Whether the nightly fix loop may replace this strategy's current version."""
    if not row.retry_pending:
        return False, 'not failing'
    now = now or timezone.now()
    trades = live_stats(row, account)['trades'] if account is not None else 0
    if trades >= RETRY_MIN_TRADES:
        return True, f'{trades} forward trades on v{row.version}'
    if row.evidence_since is None or now - row.evidence_since >= timedelta(days=RETRY_MIN_DAYS):
        return True, f'v{row.version} has run {RETRY_MIN_DAYS}+ days'
    days = (now - row.evidence_since).days
    return False, (f'v{row.version} too young to replace: {trades} of {RETRY_MIN_TRADES} trades, '
                   f'{days} of {RETRY_MIN_DAYS} days')


def sim_candidate(exp) -> dict:
    """The final training window's winner, even when it failed the after-cost
    training gate. Sim only: a failing strategy must not sit idle for want of a
    perfect candidate, and step 2 still requires it to beat what is trading."""
    if exp.best_params:
        return dict(exp.best_params)
    rows = (exp.summary or {}).get('windows') or []
    return dict(rows[-1].get('params') or {}) if rows else {}


def traded_windows(exp) -> list[dict]:
    """The test windows the selection procedure actually traded — the walk-forward
    rows it did not skip. Its adaptive OOS covers exactly these, so the current
    params are replayed on exactly these too: like for like."""
    rows = (exp.summary or {}).get('windows') or []
    traded = {int(r['n']) for r in rows if 'skipped' not in r and 'n' in r}
    w = exp.windows or {}
    return [serialize_window(x) for x in walk_forward_windows(
        exp.start, exp.end, int(w.get('train_days', 60)), int(w.get('test_days', 20)),
        int(w.get('step_days', 0)) or None) if x['n'] in traded]


def own_timeframe(row: Strategy, base: str) -> str:
    """The row's strategy timeframe as a spec wants it: '' when it is the base."""
    tf = (row.timeframe or '').strip()
    return '' if tf in ('', base) else tf


def compare_on_oos(exp, current_params: dict, cfg, current_timeframe: str | None = None) -> dict:
    """The procedure's adaptive OOS against the current params, fixed, under the
    same regime on the same windows: the ones the procedure traded. The current
    params run at the CURRENT strategy timeframe, which may differ from the
    candidate's: the question is whether to replace what is trading."""
    windows = traded_windows(exp)
    procedure = (exp.summary or {}).get('oos') or {}
    if not windows:
        return {'current': {}, 'procedure': _subset(procedure), 'beats': False, 'windows': 0}
    tf = exp.strategy_timeframe if current_timeframe is None else current_timeframe
    spec = with_risk_overrides(spec_from_models(exp.strategy_key, {}, exp.symbols, exp.timeframe, cfg,
                                                strategy_timeframe=tf), exp.risk_overrides)
    frames = load_frames(exp.symbols, exp.timeframe, exp.start, exp.end)
    current = evaluate_fixed_params(spec, frames, windows, current_params)['metrics']
    beats = (float(procedure.get('net_pnl', 0) or 0) > float(current.get('net_pnl', 0) or 0)
             and float(procedure.get('profit_factor', 0) or 0) > float(current.get('profit_factor', 0) or 0))
    return {'current': _subset(current), 'procedure': _subset(procedure), 'beats': beats,
            'windows': len(windows)}


def install_if_better(row: Strategy, exp, cfg, current_params: dict | None = None,
                      dry_run: bool = False, note: str = '') -> tuple[str, dict]:
    """Step 2 and 3 of the selection. Returns (verdict, comparison)."""
    candidate = sim_candidate(exp)
    # Defaults first: stored params predate newer knobs such as entry_session.
    current = {**get_strategy_class(row.key).defaults(),
               **(row.params if current_params is None else current_params)}
    if not candidate:
        return 'no fix found — the walk-forward produced no candidate', {}
    current_tf = own_timeframe(row, exp.timeframe)
    new_tf = exp.strategy_timeframe or ''
    cmp = compare_on_oos(exp, current, cfg, current_timeframe=current_tf)
    c, k = cmp['procedure'], cmp['current']
    line = (f'selection procedure net {c.get("net_pnl", 0):+,.2f} PF {c.get("profit_factor", 0):.2f} '
            f'(adaptive OOS, {c.get("trades", 0)} trades) vs current net {k.get("net_pnl", 0):+,.2f} '
            f'PF {k.get("profit_factor", 0):.2f} ({k.get("trades", 0)} trades) on the same '
            f'{cmp["windows"]} traded test windows')
    if new_tf != current_tf:
        line += f'; candidate on {new_tf or exp.timeframe}, current on {current_tf or exp.timeframe}'
    if new_tf == current_tf and dict(candidate) == {key: current.get(key) for key in candidate}:
        return f'no fix found — the search re-found the current params ({line})', cmp
    if not cmp['beats']:
        return f'no fix found — {line}', cmp
    if dry_run:
        return f'WOULD INSTALL v{row.version + 1} in sim — {line}', cmp
    promote(row, candidate, source=f'sim fix loop, experiment #{exp.pk}', metrics=c,
            note=('selection procedure beat current out-of-sample; the installed params are its latest '
                  'pick, not separately validated. Not a gated promotion.' + (f' {note}' if note else '')),
            evidence={'kind': 'sim_fix', 'experiment': exp.pk, 'windows': cmp['windows'],
                      'current': k, 'previous_params': current, 'previous_timeframe': current_tf,
                      'timeframe': new_tf, 'risk_overrides': exp.risk_overrides or {}})
    if new_tf != current_tf:
        row.timeframe = new_tf          # '' = follow the lane's base; explicit only for a coarse choice
        row.save(update_fields=['timeframe'])
    return f'INSTALLED v{row.version} in sim — {line}', cmp


def regime_key(timeframe: str, min_reward_to_cost: float, max_hold_minutes: int) -> str:
    return f'{timeframe}|mrc={float(min_reward_to_cost):g}|hold={int(max_hold_minutes)}'


def choose_regime(combined_net: dict[str, float], current: str) -> tuple[str, str]:
    """The lane leaves its current regime only for a better combined OOS net.

    `combined_net` maps regime_key -> the sum of the lane's strategies' adaptive
    out-of-sample net, each at its own best params for that regime."""
    if current not in combined_net:
        raise ValueError(f'current regime {current} was not measured')
    best = max(combined_net, key=lambda k: combined_net[k])
    if best != current and combined_net[best] > combined_net[current]:
        return best, (f'{best} combined OOS net {combined_net[best]:+,.2f} beats current {current} '
                      f'{combined_net[current]:+,.2f}')
    return current, f'kept {current}: no regime beat its combined OOS net {combined_net[current]:+,.2f}'
