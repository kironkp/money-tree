"""Comparing our books with the venue's, and keeping the answer.

Two parts of this are built and not yet fed. `fees_charged` is not exposed by
either existing adapter, so the fee comparison is inert today; and
`record_movement` has no production caller, so `moved` is always zero. Both are
requirements on the forex adapter that does not exist yet rather than working
machinery, and they are named in the readiness report as such. The cost of
writing them now is nil; the cost of discovering they were missing while
reconciling a funded account is not.

What existed compared positions and nothing else, then wrote the verdict over
the previous one. Two consequences: a cash balance could drift indefinitely
without anyone noticing, and there was no way to answer when a disagreement
started, which is the first question anyone asks.

This compares the three things that must agree — positions, cash, equity — plus
the fees the venue actually charged against the fees we modelled, and writes
every comparison as a row, agreements included. A long run of clean rows is the
evidence that the books can be trusted; one row is just an opinion.

Tolerances are absolute and small. Floating point and rounding produce pennies;
anything larger is a real disagreement and is meant to be loud.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from main_app.models import Account, CashMovement, Order, Position, ReconciliationSnapshot

log = logging.getLogger(__name__)

CASH_TOLERANCE = Decimal('0.05')
EQUITY_TOLERANCE = Decimal('0.05')
FEE_TOLERANCE = Decimal('0.01')
QTY_TOLERANCE = 1e-8


def _d(x) -> Decimal | None:
    return None if x is None else Decimal(str(round(float(x), 4)))


def compare(account: Account, broker, now=None) -> ReconciliationSnapshot:
    """One full comparison. Records a row whether or not anything disagreed."""
    now = now or timezone.now()
    disc: list[dict] = []

    # --- positions
    try:
        theirs = {s: float(p.qty) for s, p in broker.positions.items()}
    except Exception as exc:
        return ReconciliationSnapshot.objects.create(
            account=account, ts=now, ok=False,
            discrepancies=[{'what': 'broker', 'error': repr(exc)}],
            note=f'could not read the broker: {exc!r}'[:300])
    ours = {p.instrument.symbol: float(p.qty)
            for p in Position.objects.filter(account=account).select_related('instrument')}
    for sym in sorted(set(ours) | set(theirs)):
        a, b = ours.get(sym, 0.0), theirs.get(sym, 0.0)
        if abs(a - b) > QTY_TOLERANCE:
            disc.append({'what': f'position {sym}', 'ours': a, 'theirs': b, 'delta': a - b})

    # --- cash and equity
    state = broker.account()
    our_cash, their_cash = _d(account.cash), _d(getattr(state, 'cash', None))
    our_eq, their_eq = _d(account.equity), _d(getattr(state, 'equity', None))
    if their_cash is not None and abs(our_cash - their_cash) > CASH_TOLERANCE:
        disc.append({'what': 'cash', 'ours': float(our_cash), 'theirs': float(their_cash),
                     'delta': float(our_cash - their_cash)})
    if their_eq is not None and abs(our_eq - their_eq) > EQUITY_TOLERANCE:
        disc.append({'what': 'equity', 'ours': float(our_eq), 'theirs': float(their_eq),
                     'delta': float(our_eq - their_eq)})

    # --- fees: what we modelled against what the venue charged.
    #
    # NOT YET LIVE. No adapter exposes `fees_charged` — SimBroker is its own
    # venue and the Alpaca adapter reports fees per order rather than as a
    # running total — so this branch is inert against both, and only a test
    # double exercises it. It is here because a forex adapter must supply it to
    # pass conformance, and the comparison has to exist before the number does.
    fee_q = Order.objects.filter(account=account)
    if account.epoch_started_at:
        fee_q = fee_q.filter(submitted_at__gte=account.epoch_started_at)
    our_fees = _d(fee_q.aggregate(s=Sum('fees'))['s'] or 0)
    their_fees = _d(getattr(broker, 'fees_charged', None))
    if their_fees is not None and abs(our_fees - their_fees) > FEE_TOLERANCE:
        disc.append({'what': 'fees', 'ours': float(our_fees), 'theirs': float(their_fees),
                     'delta': float(our_fees - their_fees)})

    # --- cash that moved without a trade behind it
    unexplained, provenance_checked = _unexplained_cash(account, state)
    if unexplained is not None:
        disc.append(unexplained)

    snap = ReconciliationSnapshot.objects.create(
        account=account, ts=now, ok=not disc,
        our_cash=our_cash, broker_cash=their_cash,
        our_equity=our_eq, broker_equity=their_eq,
        our_fees=our_fees, broker_fees=their_fees,
        positions_checked=len(set(ours) | set(theirs)),
        discrepancies=disc,
        note=('; '.join(f'{d["what"]}: ours {d.get("ours")} vs {d.get("theirs")}' for d in disc)[:300]
              if disc else (f'{len(set(ours) | set(theirs))} positions, cash and equity agree'
                            + ('' if provenance_checked else
                               ' (cash provenance not checked — the book is open)'))))
    if disc:
        log.warning('reconciliation diverged on %s: %s', account.market, snap.note)
    return snap


def _unexplained_cash(account: Account, state) -> tuple[dict | None, bool]:
    """Cash the venue holds that no trade and no recorded movement accounts for.

    starting_cash + recorded movements + realised P&L should be the balance. A
    gap means money arrived or left with nothing to account for it, which is the
    thing a ledger exists to make impossible to miss.

    Everything on the right-hand side is scoped to the SCORING EPOCH, because
    `reset_epoch` rewrites starting_cash and cash while deliberately keeping
    every trade. Summing trades over all time against a reset balance left the
    identity permanently wrong by the whole lifetime P&L — and on a paper or
    live lane that runs every tick, so the first reset after going to paper
    would have blocked new entries forever and halted the lane with a
    reconciliation finding that could never clear.

    Returns (discrepancy, checked). An open book legitimately holds cash outside
    the balance, so the check stands down — but it says so, because a clean row
    that cannot distinguish "agreed" from "not looked at" is how a gap hides.
    """
    their_cash = getattr(state, 'cash', None)
    if their_cash is None:
        return None, False
    if Position.objects.filter(account=account).exclude(qty=0).exists():
        return None, False
    from main_app.models import Trade
    epoch = account.epoch_started_at
    movements = CashMovement.objects.filter(account=account)
    trades = Trade.objects.filter(account=account)
    if epoch:
        movements = movements.filter(ts__gte=epoch)
        trades = trades.filter(exit_ts__gte=epoch)
    moved = movements.aggregate(s=Sum('amount'))['s'] or Decimal('0')
    realised = trades.aggregate(s=Sum('pnl'))['s'] or Decimal('0')
    expected = Decimal(str(account.starting_cash)) + moved + realised
    gap = Decimal(str(round(float(their_cash), 4))) - expected
    if abs(gap) <= CASH_TOLERANCE:
        return None, True
    return {'what': 'unexplained cash', 'ours': float(expected), 'theirs': float(their_cash),
            'delta': float(gap), 'epoch': str(epoch) if epoch else 'all time',
            'note': 'the balance is not starting cash plus recorded movements plus realised P&L'}, True


def record_movement(account: Account, kind: str, amount, *, ts=None, currency: str = 'USD',
                    broker_ref: str = '', source: str = 'local', note: str = '',
                    from_currency: str = '', from_amount=None, rate=None) -> CashMovement | None:
    """Idempotent on broker_ref: re-reading a statement must not double a deposit."""
    if broker_ref:
        existing = CashMovement.objects.filter(account=account, broker_ref=broker_ref).first()
        if existing is not None:
            return existing
    return CashMovement.objects.create(
        account=account, ts=ts or timezone.now(), kind=kind, amount=Decimal(str(amount)),
        currency=currency, broker_ref=broker_ref, source=source, note=note[:300],
        from_currency=from_currency,
        from_amount=None if from_amount is None else Decimal(str(from_amount)),
        rate=None if rate is None else Decimal(str(rate)))
