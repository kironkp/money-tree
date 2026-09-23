"""The full audit trail, as files a person or an accountant can actually read.

Six tables, joined by broker identifiers so a line can be followed from the
signal that caused it to the cash that resulted: signals, orders, fills
(partial fills included, one row each), trades, cash movements, and the
reconciliation history.

NOT a tax export, and deliberately not labelled one. Producing a return needs
things this desk has never established: a verified cost-basis method, wash-sale
treatment, the §988 versus §1256 election for currency trades, and the venue's
own year-end statement to agree against. Calling the file "tax-ready" would
invite someone to file it. It is an audit trail, which is a different and
honest claim, and it is the right raw material to hand to somebody who does
know the rules.
"""
from __future__ import annotations

import csv
import io
import zipfile

from main_app.models import (Account, CashMovement, Fill, Order, ReconciliationSnapshot, Signal,
                             Trade)

README = """MoneyTree audit export — {account} ({mode})
Generated {generated}
Rows are the complete record for this account: {epoch}

WHAT THIS IS
  An audit trail. Every signal the strategies raised, every order that carried
  one to a venue, every fill including partial ones, every closed round trip,
  every movement of cash, and every comparison of these books against the
  broker's.

  Join them with these keys:
    signals.client_order_id  ->  orders.client_order_id
    orders.client_order_id   ->  fills.client_order_id
    orders.broker_order_id   ->  the venue's own record

WHAT THIS IS NOT
  It is not a tax export and must not be filed as one. A return needs a verified
  cost-basis method, wash-sale treatment, the §988/§1256 election for currency
  trades, and the broker's year-end statement to agree against. None of those
  has been established here. Give these files to someone who knows the rules;
  do not treat them as the answer.

COSTS
  pnl in trades.csv is already NET of fees. Slippage is not a separate column
  anywhere because it is not a separate charge — it is inside the fill price, so
  fills.realized_slippage_bps is the measurement of it, signed against the
  account: positive means the fill was worse than the price the decision was
  made at.

MONEY IS SIMULATED
  This account traded on the simulator. No real order was ever submitted.
"""


def _rows(w, header, qs, fn):
    w.writerow(header)
    for obj in qs.iterator():
        w.writerow(fn(obj))


def write_bundle(account: Account, fh, generated) -> None:
    """Write the whole export as a zip into an open binary file handle."""
    def sheet(name, header, qs, fn):
        buf = io.StringIO()
        _rows(csv.writer(buf), header, qs, fn)
        z.writestr(name, buf.getvalue())

    with zipfile.ZipFile(fh, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('README.txt', README.format(
            account=account.name, mode=account.mode, generated=generated.isoformat(),
            epoch=(f'scoring epoch from {account.epoch_started_at:%Y-%m-%d}'
                   if account.epoch_started_at else 'all time')))

        sheet('signals.csv',
              ['ts', 'symbol', 'strategy', 'action', 'price', 'stop', 'target', 'acted',
               'blocked_reason', 'client_order_id'],
              Signal.objects.filter(account=account).select_related('instrument', 'order').order_by('ts'),
              lambda s: [s.ts.isoformat(), s.instrument.symbol, s.strategy_key, s.action, s.price,
                         s.stop_price, s.target_price, s.acted, s.blocked_reason,
                         s.order.client_order_id if s.order_id else ''])

        sheet('orders.csv',
              ['submitted_at', 'filled_at', 'symbol', 'strategy', 'side', 'leg', 'qty', 'filled_qty',
               'filled_avg_price', 'decision_price', 'status', 'fees', 'client_order_id',
               'broker_order_id', 'error'],
              Order.objects.filter(account=account).select_related('instrument').order_by('submitted_at'),
              lambda o: [o.submitted_at.isoformat(), o.filled_at.isoformat() if o.filled_at else '',
                         o.instrument.symbol, o.strategy_key, o.side, o.leg, o.qty, o.filled_qty,
                         o.filled_avg_price, o.decision_price, o.status, o.fees, o.client_order_id,
                         o.broker_order_id, o.error])

        sheet('fills.csv',
              ['ts', 'symbol', 'side', 'qty', 'price', 'fee', 'realized_slippage_bps',
               'client_order_id', 'broker_order_id', 'partial'],
              Fill.objects.filter(order__account=account).select_related('order', 'order__instrument')
              .order_by('ts'),
              lambda f: [f.ts.isoformat(), f.order.instrument.symbol, f.order.side, f.qty, f.price,
                         f.fee, f.realized_slippage_bps, f.order.client_order_id,
                         f.order.broker_order_id,
                         'yes' if f.qty < f.order.qty else 'no'])

        sheet('trades.csv',
              ['entry_ts', 'exit_ts', 'symbol', 'strategy', 'side', 'qty', 'entry_price',
               'exit_price', 'pnl_net_of_fees', 'pnl_pct', 'fees', 'bars_held', 'exit_reason'],
              Trade.objects.filter(account=account).select_related('instrument').order_by('exit_ts'),
              lambda t: [t.entry_ts.isoformat(), t.exit_ts.isoformat(), t.instrument.symbol,
                         t.strategy_key, t.side, t.qty, t.entry_price, t.exit_price, t.pnl,
                         t.pnl_pct, t.fees, t.bars_held, t.exit_reason])

        sheet('cash_movements.csv',
              ['ts', 'kind', 'amount', 'currency', 'from_currency', 'from_amount', 'rate',
               'broker_ref', 'source', 'note'],
              CashMovement.objects.filter(account=account).order_by('ts'),
              lambda m: [m.ts.isoformat(), m.kind, m.amount, m.currency, m.from_currency,
                         m.from_amount, m.rate, m.broker_ref, m.source, m.note])

        sheet('reconciliations.csv',
              ['ts', 'ok', 'our_cash', 'broker_cash', 'our_equity', 'broker_equity', 'our_fees',
               'broker_fees', 'positions_checked', 'discrepancies'],
              ReconciliationSnapshot.objects.filter(account=account).order_by('ts'),
              lambda r: [r.ts.isoformat(), r.ok, r.our_cash, r.broker_cash, r.our_equity,
                         r.broker_equity, r.our_fees, r.broker_fees, r.positions_checked,
                         r.discrepancies])
