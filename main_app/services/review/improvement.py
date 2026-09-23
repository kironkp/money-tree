"""Is what the machine was told to do any good?

Runs daily. Reads net performance AFTER every cost, looks for patterns in the
mistakes, and — when something concrete warrants it — records a sourced
hypothesis, tests it on history with a held-out period, and forward-tests the
survivors on paper.

Three rules hold this cycle honest, all of them learned from watching a strategy
get tuned into looking good and then lose money anyway:

1. A hypothesis with no source is not recorded. "The model suggested" is not
   evidence. A citation, a URL, or the exact in-app measurement is.
2. The split is fixed before the search, and the held-out period is looked at
   ONCE. The candidate that wins on the search half is not the candidate that
   ships; the candidate that survives the held-out half is, and usually nothing
   does.
3. A separate challenger tries to reject every surviving candidate, and the
   default answer is rejection. It is deterministic before it is clever: hard
   statistical rules first, an optional model critique second. A challenger that
   can be talked round is not a challenger.

Nothing here applies anything. The cycle proposes; the owner disposes.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date, timedelta

from django.utils import timezone

from main_app.models import Account, Hypothesis, ReviewFinding, ReviewRun, Trade
from main_app.services.report import lane_costs

log = logging.getLogger(__name__)

# What the challenger demands before anything may be called an improvement.
MIN_TEST_TRADES = 30          # out-of-sample trades, below which nothing is measurable
MIN_EDGE_PER_DAY = 0.0        # held-out mean must clear this AFTER every cost
REQUIRE_LOWER_BOUND = True    # and its bootstrap lower bound must clear zero
MAX_TRAIN_TEST_DECAY = 0.60   # a candidate keeping < 40% of its train edge is a fit


# ---------------------------------------------------------------- daily read
def analyse(account: Account, rec, now=None) -> dict:
    """Net-after-cost performance and the shape of the mistakes."""
    now = now or timezone.now()
    c = lane_costs(account)
    since = now - timedelta(days=7)
    trades = list(Trade.objects.filter(account=account, exit_ts__gte=since).select_related('instrument'))
    out = {'market': account.market, 'costs': c, 'week_trades': len(trades)}
    if not trades:
        return out

    exits = Counter(t.exit_reason for t in trades)
    out['exit_mix'] = dict(exits)
    stops = exits.get('stop', 0)
    if len(trades) >= 20 and stops / len(trades) > 0.60:
        rec.record('mistake_pattern', ReviewFinding.INFO,
                   f'{account.market}: {stops}/{len(trades)} exits in the last week were stops',
                   'A stop rate this high usually means the stop is inside the noise band rather than '
                   'outside it, so the trade is being taken out by ordinary movement before the idea '
                   'has a chance to be right or wrong.',
                   account=account, evidence={'exit_mix': dict(exits), 'trades': len(trades)},
                   fp_parts=('mistake_pattern', account.pk, 'stop_rate', now.date().isoformat()))

    by_strategy = {}
    for t in trades:
        s = by_strategy.setdefault(t.strategy_key, {'n': 0, 'net': 0.0, 'fees': 0.0})
        s['n'] += 1
        s['net'] += float(t.pnl)
        s['fees'] += float(t.fees)
    out['by_strategy'] = by_strategy
    for key, s in by_strategy.items():
        if s['n'] >= 15 and s['net'] < 0:
            rec.record('strategy_losing', ReviewFinding.WARN,
                       f'{account.market}/{key}: {s["net"]:+.2f} after costs over {s["n"]} trades this week',
                       f'Fees alone were {s["fees"]:.2f}. This is a candidate for the same train/test '
                       f'treatment that retired ORB, not for a parameter tweak.',
                       account=account, evidence=dict(s, strategy=key),
                       fp_parts=('strategy_losing', account.pk, key, now.date().isoformat()))

    # The cost finding the operational cycle raises as a fault, restated here as
    # a question about the strategy rather than the plumbing.
    if c['trades'] >= 10 and c['cost_share'] >= 60:
        rec.record('cost_dominates', ReviewFinding.WARN,
                   f'{account.market}: costs take {c["cost_share"]:.0f}% of gross',
                   f'Gross {c["gross"]:+,.2f}, costs {c["costs"]:,.2f} ({c["fees"]:,.2f} fees + '
                   f'{c["slippage"]:,.2f} slippage), net {c["net"]:+,.2f}. Any improvement that does not '
                   f'move this number is cosmetic.',
                   account=account, evidence={k: c[k] for k in ('gross', 'fees', 'slippage', 'net', 'cost_share')},
                   fp_parts=('cost_dominates', account.pk, int(c['cost_share'] // 10)))
    return out


# --------------------------------------------------------------- hypotheses
def propose(title: str, claim: str, source: str, market: str = 'forex', rationale: str = '',
            run: ReviewRun | None = None) -> Hypothesis:
    """Record an idea. Refuses to record one with no source."""
    if not (source or '').strip():
        raise ValueError('a hypothesis without a source is not evidence and is not recorded')
    return Hypothesis.objects.create(title=title[:200], claim=claim, source=source.strip(),
                                     rationale=rationale, market=market, run=run)


def evaluate(h: Hypothesis, runner, train: tuple, test: tuple) -> Hypothesis:
    """Search on TRAIN, look at TEST exactly once.

    `runner(window) -> dict` is supplied by the caller so this module never
    reaches into the backtester itself; it must return at least
    {trades, net, per_day, ci_low, ci_high}.
    """
    h.train_result = dict(runner(train), window=[str(train[0]), str(train[1])])
    h.test_result = dict(runner(test), window=[str(test[0]), str(test[1])])
    h.status = Hypothesis.BACKTESTED
    h.save(update_fields=['train_result', 'test_result', 'status'])
    return h


def challenge(h: Hypothesis, llm_second_opinion=None) -> dict:
    """Try to reject. Rejection is the default and the burden is on the evidence.

    Deterministic before clever: every rule here is arithmetic on numbers already
    recorded, so the challenger cannot be argued with, cannot hallucinate a
    reason to approve, and gives the same verdict twice. An optional model
    critique is recorded alongside and can only ADD objections — it is never
    allowed to clear one.
    """
    reasons, tr, te = [], h.train_result or {}, h.test_result or {}
    if not te:
        reasons.append('no out-of-sample result at all')
    else:
        n = te.get('trades', 0)
        if n < MIN_TEST_TRADES:
            reasons.append(f'only {n} out-of-sample trades, below the {MIN_TEST_TRADES} needed to measure anything')
        if te.get('per_day') is None or te['per_day'] <= MIN_EDGE_PER_DAY:
            reasons.append(f'held-out edge {te.get("per_day")} does not clear {MIN_EDGE_PER_DAY} after costs')
        if REQUIRE_LOWER_BOUND and (te.get('ci_low') is None or te['ci_low'] <= 0):
            reasons.append(f'held-out lower bound {te.get("ci_low")} does not clear zero — '
                           'the result is consistent with no edge')
        if tr.get('per_day', 0) > 0 and te.get('per_day') is not None:
            decay = 1 - (te['per_day'] / tr['per_day'])
            if decay > MAX_TRAIN_TEST_DECAY:
                reasons.append(f'{decay * 100:.0f}% of the edge disappeared out of sample, which is a fit, '
                               'not a finding')
    if not (h.source or '').strip():
        reasons.append('no source')

    verdict = {'rejected': bool(reasons), 'reasons': reasons, 'checked_at': timezone.now().isoformat(),
               'rules': {'min_test_trades': MIN_TEST_TRADES, 'min_edge_per_day': MIN_EDGE_PER_DAY,
                         'require_lower_bound': REQUIRE_LOWER_BOUND,
                         'max_train_test_decay': MAX_TRAIN_TEST_DECAY}}
    if llm_second_opinion is not None:
        try:
            extra = llm_second_opinion(h) or {}
            verdict['second_opinion'] = extra
            # Additive only. A model may raise an objection and may not clear one.
            for r in extra.get('objections', []) or []:
                reasons.append(f'second opinion: {r}')
            verdict['rejected'] = bool(reasons)
            verdict['reasons'] = reasons
        except Exception as exc:
            verdict['second_opinion'] = {'error': repr(exc)}
    h.challenge = verdict
    h.status = Hypothesis.REJECTED if verdict['rejected'] else Hypothesis.FORWARD
    h.decided_at = timezone.now()
    h.decision_note = ('rejected: ' + '; '.join(reasons))[:2000] if reasons else \
        'survived the challenge — cleared for paper forward testing, not for live'
    h.save(update_fields=['challenge', 'status', 'decided_at', 'decision_note'])
    return verdict
