"""Is the machine doing what it was told?

Runs after fills and on a short schedule. Every check compares something the
desk *intended* against something that is *true* — an order it thinks it sent
against the broker's list, a limit it believes it is under against the
positions it actually holds, a cost it modelled against the cost it paid.

Checks return findings; they never fix anything. The only action this module
takes is halting new entries on a lane, and only on a critical finding.

Adding a check: write a `_check_*` function taking (ctx, rec) and add it to
CHECKS. It must be safe to run every fifteen minutes forever, and its findings
must carry a fingerprint that identifies the problem rather than the sighting,
or it will spam the operator into ignoring all of it.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone

from main_app.models import (Account, Fill, Order, Position, ReviewFinding, ReviewRun, RiskEvent,
                             Signal, Trade)
from main_app.services.report import lane_costs

log = logging.getLogger(__name__)

# How far the measured cost may drift above the configured model before it is a
# finding. Set from the only thing that matters: a lane handing over most of its
# gross is an expensive strategy whether or not each individual fee was correct.
COST_SHARE_WARN = 60.0
COST_SHARE_CRITICAL = 90.0
# Staleness is judged in BARS, not minutes. Thirty minutes is right for a
# five-minute lane and absurd for a four-hour one — the first version of this
# check halted the crypto lane for a bar that was perfectly on time.
STALE_BARS_ALLOWED = 3
RECONCILE_MAX_AGE_MIN = 30     # paper/live only
REPEATED_ERROR_WINDOW_H = 6
REPEATED_ERROR_LIMIT = 5


@dataclass
class Ctx:
    account: Account
    now: object
    broker: object = None      # None when the reviewer runs out of process
    lookback_h: int = 24
    notes: list = field(default_factory=list)


# --------------------------------------------------------------------- checks
def _check_duplicate_orders(ctx: Ctx, rec) -> None:
    """Two live orders for one symbol and leg, or a client_order_id used twice.

    client_order_id is unique in the database, so a true duplicate cannot be
    stored — which means the failure shows up as two DIFFERENT ids for the same
    (symbol, bar, leg), the shape the four QQQ shorts took on 2026-09-16.
    """
    since = ctx.now - timedelta(hours=ctx.lookback_h)
    rows = (Order.objects.filter(account=ctx.account, submitted_at__gte=since)
            .values('instrument__symbol', 'bar_ts', 'leg', 'strategy_key')
            .annotate(n=Count('id')).filter(n__gt=1))
    for r in rows:
        if not r['bar_ts']:
            continue
        sym = r['instrument__symbol']
        rec.record('duplicate_order', ReviewFinding.CRITICAL,
                   f'{r["n"]} {r["leg"]} orders for {sym} on one bar',
                   f'{r["strategy_key"]} produced {r["n"]} separate {r["leg"]} orders for {sym} at '
                   f'{r["bar_ts"]:%Y-%m-%d %H:%M}. One instruction must become one order.',
                   account=ctx.account, evidence={'symbol': sym, 'bar_ts': str(r['bar_ts']),
                                                  'leg': r['leg'], 'count': r['n']},
                   fp_parts=('duplicate_order', ctx.account.pk, sym, r['bar_ts'], r['leg']))


def _check_intent_vs_broker(ctx: Ctx, rec) -> None:
    """A signal the desk acted on that produced no order, or an order with no fill."""
    since = ctx.now - timedelta(hours=ctx.lookback_h)
    orphan = (Signal.objects.filter(account=ctx.account, ts__gte=since, acted=True, order__isnull=True)
              .select_related('instrument'))
    for sg in orphan:
        # Two very different faults wear the same shape. If an order for that
        # symbol and bar exists, the trade happened and only the audit link is
        # missing — bad books, not a lost instruction. If no order exists at all,
        # the desk believed it traded and did not, which is the serious one.
        # Grading both CRITICAL would halt the desk for a bookkeeping gap.
        # Match the LEG the signal asked for. An exit signal produces an exit
        # order and an entry signal an entry order, and a protective stop often
        # carries the same bar_ts as an unrelated entry — matching any leg would
        # let that stop vouch for an entry that never went out, turning a
        # critical fault into a bookkeeping note.
        leg = 'exit' if sg.action == 'close' else 'entry'
        executed = Order.objects.filter(account=ctx.account, instrument=sg.instrument,
                                        strategy_key=sg.strategy_key, bar_ts=sg.ts, leg=leg).first()
        if executed is not None:
            rec.record('signal_order_unlinked', ReviewFinding.WARN,
                       f'{sg.instrument.symbol}: signal and its order are not linked',
                       f'Signal {sg.pk} ({sg.strategy_key} {sg.action} at {sg.ts:%Y-%m-%d %H:%M}) was acted on and '
                       f'order {executed.client_order_id} exists for the same bar, but the two are not joined. '
                       f'The trade happened; the audit trail from intent to execution does not.',
                       account=ctx.account,
                       evidence={'symbol': sg.instrument.symbol, 'signal': sg.pk,
                                 'client_order_id': executed.client_order_id},
                       fp_parts=('signal_order_unlinked', ctx.account.pk, sg.pk))
            continue
        rec.record('intent_without_order', ReviewFinding.CRITICAL,
                   f'{sg.instrument.symbol}: acted on and no order was ever placed',
                   f'Signal {sg.pk} ({sg.strategy_key} {sg.action}) is marked acted at {sg.ts:%Y-%m-%d %H:%M} and no '
                   f'order exists for that symbol and bar. The desk believes it traded and it did not.',
                   account=ctx.account, evidence={'symbol': sg.instrument.symbol, 'signal': sg.pk},
                   fp_parts=('intent_without_order', ctx.account.pk, sg.pk))

    stuck = (Order.objects.filter(account=ctx.account, submitted_at__gte=since,
                                  status__in=('new', 'accepted', 'partial'),
                                  submitted_at__lt=ctx.now - timedelta(minutes=30))
             .select_related('instrument'))
    for o in stuck:
        rec.record('order_stuck_open', ReviewFinding.WARN,
                   f'{o.instrument.symbol}: {o.leg} order open for over 30 minutes',
                   f'Order {o.client_order_id} has been {o.status} since {o.submitted_at:%H:%M}. '
                   f'{o.filled_qty} of {o.qty} filled.',
                   account=ctx.account, evidence={'symbol': o.instrument.symbol,
                                                  'client_order_id': o.client_order_id, 'status': o.status},
                   fp_parts=('order_stuck_open', ctx.account.pk, o.client_order_id))


def _check_position_vs_broker(ctx: Ctx, rec) -> None:
    """Our positions against the broker's. Only meaningful with a broker."""
    if ctx.broker is None:
        ctx.notes.append('position_vs_broker: skipped, no broker handle in this process')
        return
    try:
        theirs = {s: float(p.qty) for s, p in ctx.broker.positions.items()}
    except Exception as exc:
        rec.record('broker_unreachable', ReviewFinding.CRITICAL,
                   'the broker could not be read',
                   f'Reading positions raised {exc!r}. Treat every position number as unverified.',
                   account=ctx.account, evidence={'error': repr(exc)},
                   fp_parts=('broker_unreachable', ctx.account.pk))
        return
    ours = {p.instrument.symbol: float(p.qty)
            for p in Position.objects.filter(account=ctx.account).select_related('instrument')}
    for sym in set(ours) | set(theirs):
        a, b = ours.get(sym, 0.0), theirs.get(sym, 0.0)
        if abs(a - b) > 1e-8:
            rec.record('position_divergence', ReviewFinding.CRITICAL,
                       f'{sym}: our books say {a:g}, the broker says {b:g}',
                       'Position quantities disagree with the venue. The venue is right by definition; '
                       'new entries are halted until this is reconciled.',
                       account=ctx.account, evidence={'symbol': sym, 'ours': a, 'broker': b},
                       fp_parts=('position_divergence', ctx.account.pk, sym))


def _check_limits(ctx: Ctx, rec) -> None:
    """Limits the desk believes it is under, against what it is actually holding."""
    from main_app.models import AgentConfig
    from main_app.services.risk import RiskConfig
    cfg = RiskConfig.from_model(AgentConfig.get(), ctx.account.market)
    live = list(Position.objects.filter(account=ctx.account).exclude(qty=0).select_related('instrument'))
    if len(live) > cfg.max_open_positions:
        rec.record('limit_open_positions', ReviewFinding.CRITICAL,
                   f'{len(live)} positions open, limit is {cfg.max_open_positions}',
                   'More positions are open than the risk config allows. A limit that can be exceeded '
                   'is not a limit.',
                   account=ctx.account,
                   evidence={'open': len(live), 'limit': cfg.max_open_positions,
                             'symbols': [p.instrument.symbol for p in live]},
                   fp_parts=('limit_open_positions', ctx.account.pk, len(live)))

    equity = float(ctx.account.equity)
    if equity > 0 and cfg.max_directional_exposure_pct > 0:
        for direction, name in ((1, 'long'), (-1, 'short')):
            gross = sum(abs(float(p.qty) * float(p.avg_price)) for p in live
                        if (1 if float(p.qty) > 0 else -1) == direction)
            pct = gross / equity * 100
            if pct > cfg.max_directional_exposure_pct * 1.01:      # 1% tolerance for marking
                rec.record('limit_directional_exposure', ReviewFinding.CRITICAL,
                           f'{name} exposure {pct:.0f}% of equity, cap is {cfg.max_directional_exposure_pct:g}%',
                           f'Gross {name} notional is {gross:,.0f} against equity of {equity:,.0f}.',
                           account=ctx.account,
                           evidence={'direction': name, 'pct': round(pct, 2),
                                     'cap': float(cfg.max_directional_exposure_pct)},
                           fp_parts=('limit_directional_exposure', ctx.account.pk, name))

    # A breached daily loss that did not halt the day is a control that did not fire.
    if ctx.account.day_start_equity and cfg.max_daily_loss_pct > 0:
        start = float(ctx.account.day_start_equity)
        limit = start * float(cfg.max_daily_loss_pct) / 100.0
        loss = start - equity
        if loss > limit and not ctx.account.day_halted:
            rec.record('limit_daily_loss_not_halted', ReviewFinding.CRITICAL,
                       f'down {loss:,.2f} against a {limit:,.2f} daily limit, and not halted',
                       'The daily loss limit was passed and the day is not marked halted. The control '
                       'exists and did not fire.',
                       account=ctx.account,
                       evidence={'loss': round(loss, 2), 'limit': round(limit, 2),
                                 'day_start_equity': start, 'equity': equity},
                       fp_parts=('limit_daily_loss_not_halted', ctx.account.pk,
                                 str(ctx.account.day_start_date)))


def _check_stale_data(ctx: Ctx, rec) -> None:
    """Bars that stopped arriving while the market was open."""
    from main_app.models import Bar
    from main_app.services.data import calendar as cal
    ac = ctx.account.lane_asset_class
    if not cal.is_open(ctx.now, ac):
        ctx.notes.append('stale_data: market closed, not checked')
        return
    newest = (Bar.objects.filter(instrument__market=ctx.account.market)
              .order_by('-ts').values_list('ts', 'instrument__symbol').first())
    if newest is None:
        rec.record('stale_data', ReviewFinding.CRITICAL, 'no bars at all for this lane',
                   'The lane has no price history. Nothing may be traded on it.',
                   account=ctx.account, fp_parts=('stale_data', ctx.account.pk, 'none'))
        return
    age_min = (ctx.now - newest[0]).total_seconds() / 60
    tfm = _lane_bar_minutes(ctx.account)
    allowed = tfm * STALE_BARS_ALLOWED
    if age_min > allowed:
        rec.record('stale_data', ReviewFinding.CRITICAL,
                   f'newest bar is {age_min:.0f} min old on a {tfm:g}-minute lane while the market is open',
                   f'Latest bar {newest[1]} at {newest[0]:%Y-%m-%d %H:%M}, which is {age_min / tfm:.1f} bars ago '
                   f'against an allowance of {STALE_BARS_ALLOWED}. Decisions taken on data this old are taken '
                   f'on a price that no longer exists.',
                   account=ctx.account,
                   evidence={'age_min': round(age_min, 1), 'bars_late': round(age_min / tfm, 1),
                             'timeframe_min': tfm, 'symbol': newest[1]},
                   fp_parts=('stale_data', ctx.account.pk, int(age_min // max(1, allowed))))


def _lane_bar_minutes(account: Account) -> float:
    from main_app.models import AgentConfig
    from main_app.services.timeframes import tf_minutes
    return float(tf_minutes(AgentConfig.get().timeframe_for(account.market)))


def _check_reconciliation(ctx: Ctx, rec) -> None:
    if ctx.account.mode not in ('paper', 'live'):
        ctx.notes.append('reconciliation: simulator is its own venue, not checked')
        return
    if not ctx.account.reconcile_ok:
        rec.record('reconcile_failed', ReviewFinding.CRITICAL, 'books disagree with the broker',
                   ctx.account.reconcile_note or 'reconcile_ok is false with no note.',
                   account=ctx.account, evidence={'note': ctx.account.reconcile_note},
                   fp_parts=('reconcile_failed', ctx.account.pk, ctx.account.reconcile_note[:60]))
    last = ctx.account.last_reconcile_at
    if last is None or (ctx.now - last) > timedelta(minutes=RECONCILE_MAX_AGE_MIN):
        age = 'never' if last is None else f'{(ctx.now - last).total_seconds() / 60:.0f} min ago'
        rec.record('reconcile_stale', ReviewFinding.CRITICAL, f'last reconciled {age}',
                   'Position and balance agreement with the broker is unverified.',
                   account=ctx.account, evidence={'last_reconcile_at': str(last)},
                   fp_parts=('reconcile_stale', ctx.account.pk))


def _check_costs(ctx: Ctx, rec) -> None:
    """What the tolls actually took, against gross — in both directions.

    A profitable lane that hands over most of what it earns is the failure this
    check exists for, and it is invisible to any check that only looks at net.
    """
    c = lane_costs(ctx.account)
    if c['trades'] < 10:
        ctx.notes.append(f'costs: only {c["trades"]} trades this epoch, not judged')
        return
    share = c['cost_share']
    if share >= COST_SHARE_CRITICAL:
        sev = ReviewFinding.CRITICAL
    elif share >= COST_SHARE_WARN:
        sev = ReviewFinding.WARN
    else:
        return
    rec.record('abnormal_costs', sev,
               f'costs are {share:.0f}% of gross on {ctx.account.market}',
               f'Gross {c["gross"]:+,.2f} over {c["trades"]} trades. Fees {c["fees"]:,.2f} plus slippage '
               f'{c["slippage"]:,.2f} leaves {c["net"]:+,.2f}. Slippage is '
               f'{"measured from fills" if c["slippage_measured"] else "ASSUMED, not measured"}.',
               account=ctx.account,
               evidence={k: c[k] for k in ('trades', 'gross', 'fees', 'slippage', 'net',
                                           'cost_share', 'cost_bps', 'slippage_measured')},
               fp_parts=('abnormal_costs', ctx.account.pk, int(share // 10)))


def _check_repeated_errors(ctx: Ctx, rec) -> None:
    since = ctx.now - timedelta(hours=REPEATED_ERROR_WINDOW_H)
    counts = Counter(RiskEvent.objects.filter(account=ctx.account, ts__gte=since)
                     .values_list('kind', flat=True))
    for kind, n in counts.items():
        if n < REPEATED_ERROR_LIMIT:
            continue
        rec.record('repeated_errors', ReviewFinding.WARN,
                   f'{n} "{kind}" risk events in {REPEATED_ERROR_WINDOW_H}h',
                   f'The same condition fired {n} times. One is an event; {n} is a pattern nobody acted on.',
                   account=ctx.account, evidence={'kind': kind, 'count': n},
                   fp_parts=('repeated_errors', ctx.account.pk, kind,
                             ctx.now.strftime('%Y-%m-%d')))


def _check_unfilled_protection(ctx: Ctx, rec) -> None:
    """An open position with no stop recorded is the one that ends a desk."""
    naked = [p for p in Position.objects.filter(account=ctx.account).exclude(qty=0)
             .select_related('instrument') if not p.stop_price]
    for p in naked:
        rec.record('position_without_stop', ReviewFinding.CRITICAL,
                   f'{p.instrument.symbol}: open position carries no stop',
                   f'{p.qty:g} {p.instrument.symbol} at {p.avg_price} has no stop recorded. '
                   f'Its downside is the whole account.',
                   account=ctx.account, evidence={'symbol': p.instrument.symbol, 'qty': float(p.qty)},
                   fp_parts=('position_without_stop', ctx.account.pk, p.instrument.symbol))


def _check_missed_quarantine(ctx: Ctx, rec) -> None:
    """A strategy the desk's own promotion rules have already failed, still trading.

    `qualification_assessment` is the existing verdict machinery. It is consulted
    when somebody opens a page and when the nightly job runs, so a strategy can
    sit at "measured no edge" for days while continuing to take positions,
    because nothing compares that verdict against the stored state.

    The reviewer raises it and stops there. Quarantining a strategy is a change
    to strategy configuration, which review cycles may not make — readonly_config
    would crash this run if it tried. Deciding is the owner's.
    """
    from main_app.models import Strategy as SModel
    from main_app.services.promotion import qualification_assessment
    for row in SModel.objects.filter(market=ctx.account.market):
        try:
            a = qualification_assessment(row, ctx.account)
        except Exception as exc:
            log.warning('assessment failed for %s/%s: %r', ctx.account.market, row.key, exc)
            continue
        if a.get('state') != 'quarantine' or row.qualification == 'quarantine':
            continue
        stats = a.get('stats') or {}
        rec.record('missed_quarantine',
                   ReviewFinding.CRITICAL if row.enabled else ReviewFinding.WARN,
                   f'{ctx.account.market}/{row.key} is marked {row.qualification} but assesses as quarantine',
                   f'{a.get("reason", "")} The strategy is '
                   f'{"ENABLED and trading" if row.enabled else "disabled"}. The desk\'s own promotion '
                   f'rules have already failed it and nothing acted on that. Quarantining it is a '
                   f'configuration change, which this reviewer may not make.',
                   account=ctx.account,
                   evidence={'strategy': row.key, 'stored': row.qualification,
                             'assessed': a.get('state'), 'enabled': row.enabled,
                             'reason': a.get('reason', ''),
                             'stats': {k: stats.get(k) for k in
                                       ('trades', 'profit_factor', 'expectancy', 'net_pnl')}},
                   fp_parts=('missed_quarantine', ctx.account.pk, row.key))


CHECKS = {
    'duplicate_order': _check_duplicate_orders,
    'intent_vs_broker': _check_intent_vs_broker,
    'position_vs_broker': _check_position_vs_broker,
    'limits': _check_limits,
    'stale_data': _check_stale_data,
    'reconciliation': _check_reconciliation,
    'costs': _check_costs,
    'repeated_errors': _check_repeated_errors,
    'protection': _check_unfilled_protection,
    'missed_quarantine': _check_missed_quarantine,
}

# Which critical findings stop new entries.
#
# The split is acute versus chronic. A duplicate order, books that disagree with
# the venue, a breached limit, a position with no stop, prices that stopped
# arriving — each means the next order may be wrong, so the next order does not
# go out. Costs eating 92% of gross is just as serious and is not that: it is a
# standing property of the strategy, it will still be true tomorrow, and halting
# on it would freeze the lane permanently while destroying the only thing that
# could resolve it, which is more evidence. Chronic findings alert, stay open,
# and block promotion; they do not stop the simulator.
HALTING_CHECKS = {
    'duplicate_order', 'intent_without_order', 'position_divergence', 'broker_unreachable',
    'limit_open_positions', 'limit_directional_exposure', 'limit_daily_loss_not_halted',
    'stale_data', 'reconcile_failed', 'reconcile_stale', 'position_without_stop', 'check_crashed',
}

# Which findings each check can raise, so sweep_resolved can close the right ones.
RAISES = {
    'duplicate_order': ['duplicate_order'],
    'intent_vs_broker': ['intent_without_order', 'signal_order_unlinked', 'order_stuck_open'],
    'position_vs_broker': ['position_divergence', 'broker_unreachable'],
    'limits': ['limit_open_positions', 'limit_directional_exposure', 'limit_daily_loss_not_halted'],
    'stale_data': ['stale_data'],
    'reconciliation': ['reconcile_failed', 'reconcile_stale'],
    'costs': ['abnormal_costs'],
    'repeated_errors': ['repeated_errors'],
    'protection': ['position_without_stop'],
    'missed_quarantine': ['missed_quarantine'],
}
