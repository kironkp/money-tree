"""What the operator must see in ten seconds: is this real money, is the
agent alive, is the data fresh, are the books reconciled, and is it safe to
trade right now (with the blockers spelled out)."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from main_app.models import Account, AgentConfig, Market, Mode, RiskEvent, Strategy

from .data import calendar as cal
from .timeframes import tf_delta

MODE_LABEL = {Mode.SIM: 'SIM · fake money', Mode.PAPER: 'PAPER · Alpaca paper account',
              Mode.LIVE: 'LIVE — REAL MONEY', Mode.REPLAY: 'REPLAY · stored bars'}


def _age(ts):
    return None if ts is None else (timezone.now() - ts).total_seconds()


def portfolio_risk(account: Account, cfg: AgentConfig) -> dict:
    positions = list(account.positions.select_related('instrument'))
    at_risk = sum((p.risk_dollars for p in positions), Decimal('0'))
    exposure = sum((abs(p.market_value) for p in positions), Decimal('0'))
    equity = account.equity or Decimal('1')
    budget = account.day_start_equity * cfg.max_daily_loss_pct / 100
    used = max(Decimal('0'), -account.day_pnl)
    unprotected = [p.instrument.symbol for p in positions if p.stop_price is None or p.protection == 'none']
    return {'at_risk': at_risk, 'exposure': exposure, 'exposure_pct': (exposure / equity * 100) if equity else Decimal('0'),
            'budget': budget, 'used': used, 'remaining': max(Decimal('0'), budget - used),
            'used_pct': float(used / budget * 100) if budget else 0.0, 'unprotected': unprotected,
            'positions': len(positions)}


def build_status(account: Account, cfg: AgentConfig, run) -> dict:
    now = timezone.now()
    tf = tf_delta(cfg.timeframe_for(account.market))
    health = run.health if run else 'off'
    blockers, notes = [], []
    if account.mode == Mode.LIVE:
        notes.append('REAL MONEY')
    # agent
    if run is None:
        blockers.append('no agent running for this account')
    elif health in ('stale', 'disconnected'):
        blockers.append(f'agent is {health} (last heartbeat {int(run.heartbeat_age_s)} s ago)')
    # data freshness
    bar_age = None
    if run and run.last_bar_ts:
        bar_age = (now - (run.last_bar_ts + tf)).total_seconds()
    market_open = account.is_open_at(now)
    if run and market_open and (bar_age is None or bar_age > 2 * tf.total_seconds()):
        blockers.append('market data is stale' if bar_age is not None else 'no completed bar seen yet')
    # reconciliation
    rec_age = _age(account.last_reconcile_at)
    if account.mode in (Mode.PAPER, Mode.LIVE):
        if not account.reconcile_ok:
            blockers.append(f'books not reconciled with the broker: {account.reconcile_note}')
        elif rec_age is None or rec_age > 2 * tf.total_seconds() + 120:
            blockers.append('broker reconciliation is stale')
    # controls
    if cfg.kill_switch:
        blockers.append('kill switch is ON')
    if not cfg.trading_enabled:
        blockers.append('trading is disabled in Settings')
    if account.day_halted:
        blockers.append(f'halted for the day: {account.day_halted_reason or "daily loss limit"}')
    if not Strategy.objects.filter(enabled=True, market=account.market).exists():
        blockers.append(f'no {account.market} strategy is enabled')
    if RiskEvent.objects.filter(account=account, kind='config_changed', acknowledged_at__isnull=True).exists():
        blockers.append('strategy configuration changed — the running agent needs a restart')
    if not market_open:
        nxt = cal.next_open(now, account.lane_asset_class)
        notes.append(f'{"forex" if account.market == Market.FOREX else "stock"} market closed — opens {nxt.astimezone(cal.ET):%a %H:%M} ET')
    return {
        'mode': account.mode, 'mode_label': MODE_LABEL.get(account.mode, account.mode), 'real_money': account.mode == Mode.LIVE,
        'health': health, 'run': run,
        'data_source': (run.data_source if run else '') or ('stored bars' if account.mode == Mode.REPLAY else '—'),
        'bar_age': bar_age, 'last_bar_ts': run.last_bar_ts if run else None,
        'reconcile': {'applies': account.mode in (Mode.PAPER, Mode.LIVE), 'ok': account.reconcile_ok, 'age': rec_age,
                      'note': account.reconcile_note, 'at': account.last_reconcile_at},
        'trading_enabled': cfg.trading_enabled, 'kill_switch': cfg.kill_switch, 'halted': account.day_halted,
        'blockers': blockers, 'notes': notes, 'safe': not blockers,
        'now': (run.message if run else 'not running'), 'state': (run.state if run else 'off'),
        'next_action_at': (run.next_action_at if run else None), 'market_open': market_open,
    }
