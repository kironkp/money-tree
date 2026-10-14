"""What a research row reports about a list of trades, shared by every study.

Moved verbatim out of turnover_research (MT-A003) so trend_research (MT-A008)
prices its rows with the same arithmetic rather than a second cost model.
"""
from __future__ import annotations


def fmt(v):
    return '-' if v is None else f'{v:.2f}'


def pf(trades) -> float | None:
    won = sum(float(t.pnl) for t in trades if float(t.pnl) > 0)
    lost = -sum(float(t.pnl) for t in trades if float(t.pnl) <= 0)
    return round(won / lost, 3) if lost else None


def stats(trades, slip) -> dict:
    """Net profit factor, not gross: the question is what the desk keeps."""
    if not trades:
        return {'trades': 0, 'gross': 0.0, 'fees': 0.0, 'slippage': 0.0, 'net': 0.0,
                'net_pf': None, 'bps_captured': None, 'notional': 0.0}
    net = sum(float(t.pnl) for t in trades)
    fees = sum(float(t.fees) for t in trades)
    notional = sum(float(t.entry_price) * float(t.qty) for t in trades)
    legs = sum(1 + (0 if t.exit_reason == 'target' else 1) for t in trades)
    slipc = notional / len(trades) * legs * slip / 1e4
    gross = net + fees + slipc
    return {'trades': len(trades), 'gross': round(gross, 2), 'fees': round(fees, 2),
            'slippage': round(slipc, 2), 'net': round(net, 2), 'net_pf': pf(trades),
            'notional': round(notional, 2),
            'bps_captured': round(gross / notional * 1e4, 2) if notional else None}


def at2x(trades) -> dict:
    return {'net_2x': round(sum(float(t.pnl) for t in trades), 2),
            'net_pf_2x': pf(trades), 'trades_2x': len(trades)}
