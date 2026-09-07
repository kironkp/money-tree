"""End-of-day journal: what happened, and whether live results are drifting
away from what the backtest promised."""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
from statistics import median

from django.utils import timezone

from main_app.models import (Account, EquitySnapshot, Fill, JournalEntry, RiskEvent, Signal, Strategy, Trade)

from .data import calendar as cal
from .promotion import baseline_metrics, qualification_assessment, refresh_qualification

DRIFT_MIN_TRADES = 20


def _bounds(d: date) -> tuple[datetime, datetime]:
    a = datetime.combine(d, dtime(0, 0), tzinfo=cal.ET)
    return a, a + timedelta(days=1)


def day_summary(account: Account, d: date) -> dict:
    a, b = _bounds(d)
    trades = list(Trade.objects.filter(account=account, exit_ts__gte=a, exit_ts__lt=b).select_related('instrument'))
    pnls = [float(t.pnl) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    per_strategy: dict[str, dict] = {}
    per_symbol: dict[str, dict] = {}
    exits: dict[str, int] = {}
    for t in trades:
        s = per_strategy.setdefault(t.strategy_key or '-', {'trades': 0, 'pnl': 0.0, 'wins': 0})
        s['trades'] += 1
        s['pnl'] += float(t.pnl)
        s['wins'] += 1 if t.pnl > 0 else 0
        y = per_symbol.setdefault(t.instrument.symbol, {'trades': 0, 'pnl': 0.0})
        y['trades'] += 1
        y['pnl'] += float(t.pnl)
        exits[t.exit_reason] = exits.get(t.exit_reason, 0) + 1
    signals = Signal.objects.filter(account=account, ts__gte=a, ts__lt=b)
    blocked: dict[str, int] = {}
    for reason in signals.filter(acted=False).exclude(blocked_reason='').values_list('blocked_reason', flat=True):
        key = reason.split(' (')[0]
        blocked[key] = blocked.get(key, 0) + 1
    risk_events = list(RiskEvent.objects.filter(account=account, ts__gte=a, ts__lt=b).values('kind', 'message'))
    snaps = EquitySnapshot.objects.filter(account=account, ts__gte=a, ts__lt=b).order_by('ts')
    first, last = snaps.first(), snaps.last()
    slips = [float(x) for x in Fill.objects.filter(order__account=account, ts__gte=a, ts__lt=b,
                                                   realized_slippage_bps__isnull=False).values_list('realized_slippage_bps', flat=True)]
    best = max(trades, key=lambda t: t.pnl, default=None)
    worst = min(trades, key=lambda t: t.pnl, default=None)
    return {
        'date': d.isoformat(), 'account': account.name, 'mode': account.mode,
        'trades': len(trades), 'wins': len(wins), 'losses': len(losses),
        'win_rate': (len(wins) / len(trades) * 100) if trades else 0.0,
        'net_pnl': sum(pnls), 'fees': float(sum(t.fees for t in trades)),
        'gross_win': sum(wins), 'gross_loss': -sum(losses),
        'profit_factor': (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else (sum(wins) if wins else 0.0),
        'expectancy': (sum(pnls) / len(pnls)) if pnls else 0.0,
        'best': {'symbol': best.instrument.symbol, 'pnl': float(best.pnl), 'strategy': best.strategy_key} if best else None,
        'worst': {'symbol': worst.instrument.symbol, 'pnl': float(worst.pnl), 'strategy': worst.strategy_key} if worst else None,
        'per_strategy': per_strategy, 'per_symbol': per_symbol, 'exit_reasons': exits,
        'signals': signals.count(), 'acted_signals': signals.filter(acted=True).count(), 'blocked': blocked,
        'risk_events': risk_events,
        'equity_start': float(first.equity) if first else float(account.day_start_equity),
        'equity_end': float(last.equity) if last else float(account.equity),
        'slippage_median_bps': median(slips) if slips else None,
        'open_positions': account.positions.count(),
    }


def drift_check(account: Account, row: Strategy, n: int = DRIFT_MIN_TRADES) -> dict:
    recent = list(Trade.objects.filter(account=account, strategy_key=row.key).order_by('-exit_ts')[:n])
    base = baseline_metrics(row)
    bt_exp = base.get('expectancy')
    live_exp = (sum(float(t.pnl) for t in recent) / len(recent)) if recent else None
    drift = False
    if live_exp is not None and bt_exp and len(recent) >= n:
        drift = live_exp < 0 < bt_exp or (bt_exp > 0 and live_exp < 0.3 * bt_exp)
    return {'strategy': row.key, 'trades': len(recent), 'live_expectancy': live_exp, 'backtest_expectancy': bt_exp,
            'drift': drift}


def write_eod_journal(account: Account, d: date | None = None, auto_disable: bool = True) -> JournalEntry:
    d = d or timezone.localdate()
    s = day_summary(account, d)
    drifts, qualifications = [], []
    for row in Strategy.objects.filter(enabled=True, market=account.market):
        if auto_disable and account.mode != 'replay':
            qa, changed = refresh_qualification(row, account)
        else:
            qa, changed = qualification_assessment(row, account), False
        qualifications.append({'strategy': row.key, **qa})
        if changed:
            RiskEvent.objects.create(
                account=account, kind='qualification',
                message=f'{row.key}: {qa["state"]} — {qa["reason"]}'[:300],
                data={'strategy': row.key, 'state': qa['state'], 'version': row.version},
            )
        dc = drift_check(account, row)
        drifts.append(dc)
        if dc['drift'] and auto_disable and account.mode != 'replay':
            row.enabled = False
            row.notes = (row.notes + '\n' if row.notes else '') + (
                f'{d}: auto-disabled — last {dc["trades"]} live trades expectancy {dc["live_expectancy"]:+.2f} '
                f'vs backtest {dc["backtest_expectancy"]:+.2f}')
            row.save(update_fields=['enabled', 'notes'])
            RiskEvent.objects.create(account=account, kind='drift', message=f'{row.key} auto-disabled: live expectancy '
                                     f'{dc["live_expectancy"]:+.2f} vs backtest {dc["backtest_expectancy"]:+.2f}')
    s['drift'] = drifts
    s['qualification'] = qualifications
    lines = [f"**{s['trades']} trades**, net **{s['net_pnl']:+,.2f}** (fees {s['fees']:,.2f}), "
             f"win rate {s['win_rate']:.0f}%, profit factor {s['profit_factor']:.2f}, expectancy {s['expectancy']:+.2f}/trade.",
             f"Equity {s['equity_start']:,.2f} → {s['equity_end']:,.2f}."]
    if s['best']:
        lines.append(f"Best: {s['best']['symbol']} {s['best']['pnl']:+,.2f} ({s['best']['strategy']}). "
                     f"Worst: {s['worst']['symbol']} {s['worst']['pnl']:+,.2f} ({s['worst']['strategy']}).")
    if s['per_strategy']:
        lines.append('Per strategy: ' + '; '.join(f"{k} {v['trades']} trades {v['pnl']:+,.2f}" for k, v in s['per_strategy'].items()))
    if s['exit_reasons']:
        lines.append('Exits: ' + ', '.join(f'{k} {v}' for k, v in s['exit_reasons'].items()))
    if s['blocked']:
        lines.append('Blocked signals: ' + ', '.join(f'{k} ×{v}' for k, v in s['blocked'].items()))
    if s['risk_events']:
        lines.append('Risk events: ' + '; '.join(f"{e['kind']}: {e['message']}" for e in s['risk_events'][:8]))
    if s['slippage_median_bps'] is not None:
        lines.append(f"Median realized slippage {s['slippage_median_bps']:.1f} bps.")
    for dc in drifts:
        if dc['live_expectancy'] is not None and dc['backtest_expectancy']:
            flag = ' **DRIFT — auto-disabled**' if dc['drift'] else ''
            lines.append(f"{dc['strategy']}: last {dc['trades']} trades expectancy {dc['live_expectancy']:+.2f} "
                         f"vs backtest {dc['backtest_expectancy']:+.2f}{flag}")
    for qa in qualifications:
        lines.append(f"{qa['strategy']} evidence: **{qa['state']}** — {qa['reason']}")
    if s['open_positions']:
        lines.append(f"⚠ {s['open_positions']} position(s) still open after the close.")
    title = f"{account.name}: {s['trades']} trades, {s['net_pnl']:+,.2f}"
    entry, _ = JournalEntry.objects.update_or_create(
        date=d, kind='auto_eod', account=account,
        defaults={'title': title, 'body': '\n\n'.join(lines), 'metrics': s})
    return entry
