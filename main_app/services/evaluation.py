"""Deciding whether the research arm has earned the right to trade.

The primary question is deliberately singular and deliberately policy-level:

    the paired difference in total net ATR PER DAY between the complete research
    trading policy and the current headline-only policy, counting the days each
    chose not to trade.

Per-day and not per-trade, because a richer dossier mechanically produces higher
scores and therefore more trades. An arm whose per-trade edge is unchanged but
which trades three times as often loses three times as much money while every
per-trade metric looks flat or better. Counting no-trade days is what makes
"it declined to trade today" a result rather than a missing observation.

THE STATISTICS, AND WHY THESE ONES
Serial dependence is real here: volatility clusters, and one dossier can shape
several consecutive days. So the daily paired differences are NOT independent,
and the two consequences are taken seriously rather than assumed away.

  Variance     comes from a circular block bootstrap over whole days, which
               preserves within-block dependence, plus a non-overlapping
               batch-means long-run sigma for the power calculation. Not the
               naive standard error, which would be optimistic by roughly the
               square root of the dependence.

  Looking      happens only at preregistered checkpoints, with O'Brien-Fleming
               alpha spending across them. A scheduled job that re-checks an
               ordinary interval and promotes the first time one passes promotes
               noise with probability approaching one — the single most likely way
               this project produces a confident wrong answer.

A time-uniform confidence sequence would allow continuous monitoring, but a
correct dependence-robust one is easy to get subtly wrong and the failure mode is
invisible. Fewer looks, each one honest.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import timedelta

from django.utils import timezone

from main_app.models import Evaluation, NewsVerdict, SymbolDossier

from .data import calendar as cal

log = logging.getLogger('moneytree.evaluation')

BLOCK_DAYS = 5          # one trading week per bootstrap block
BOOTSTRAP_DRAWS = 4000
MIN_DAYS_FOR_STATS = 10


# --- the paired daily series ------------------------------------------------

def daily_series(ev: Evaluation, now=None) -> list[dict]:
    """One row per trading day: what each policy earned, and the difference.

    A day on which neither arm traded is still a day, and it is kept. Dropping
    flat days would silently compare "days the research arm chose to act" against
    "all days", which flatters whichever arm is pickier.
    """
    now = now or timezone.now()
    headline: dict = defaultdict(float)
    research: dict = defaultdict(float)
    catalyst_only: dict = defaultdict(float)
    days: set = set()

    verdicts = (NewsVerdict.objects
                .filter(provenance='contemporaneous', outcome_at__isnull=False,
                        created_at__gte=ev.opened_at, arm='headline')
                .exclude(outcome_atr_net=None))
    for v in verdicts:
        d = cal.session_date(v.created_at)
        days.add(d)
        if v.score >= NewsVerdict.ACT_THRESHOLD and v.direction in ('buy', 'short'):
            headline[d] += v.outcome_atr_net

    dossiers = SymbolDossier.objects.filter(outcome_at__isnull=False, as_of__gte=ev.opened_at)
    for d_row in dossiers:
        d = cal.session_date(d_row.as_of)
        days.add(d)
        if d_row.net_atr_combined is not None:
            research[d] += d_row.net_atr_combined
        if d_row.net_atr_catalyst_only is not None:
            catalyst_only[d] += d_row.net_atr_catalyst_only

    # Days the dossier sweep ran and found nothing worth trading are real zeros.
    for d_row in SymbolDossier.objects.filter(as_of__gte=ev.opened_at).only('as_of'):
        days.add(cal.session_date(d_row.as_of))

    return [{'date': d, 'headline': headline[d], 'research': research[d],
             'catalyst_only': catalyst_only[d],
             'diff': research[d] - headline[d],
             'diff_ablation': catalyst_only[d] - research[d]}
            for d in sorted(days)]


# --- variance under dependence ----------------------------------------------

def batch_means_sigma(series: list[float], block: int = BLOCK_DAYS) -> float | None:
    """Long-run standard deviation of the daily mean, via non-overlapping batches.

    The naive standard deviation treats consecutive days as independent and is
    therefore optimistic. Batching to whole weeks absorbs the within-week
    dependence into each batch, so what is left between batches is much closer to
    independent.
    """
    n = len(series)
    if n < block * 3:
        return None
    k = n // block
    means = [sum(series[i * block:(i + 1) * block]) / block for i in range(k)]
    grand = sum(means) / k
    var = sum((m - grand) ** 2 for m in means) / (k - 1) if k > 1 else 0.0
    # Variance of the daily observation implied by the batch variance.
    return math.sqrt(max(var, 0.0) * block)


def circular_block_bootstrap(series: list[float], alpha: float, draws: int = BOOTSTRAP_DRAWS,
                             block: int = BLOCK_DAYS, seed: int = 20260917) -> dict | None:
    """One-sided bounds on the mean that survive serial dependence.

    Blocks are resampled whole and wrapped around the end, which keeps runs of
    good and bad days intact instead of shuffling them into a false calm.
    """
    import random
    n = len(series)
    if n < MIN_DAYS_FOR_STATS:
        return None
    rng = random.Random(seed)
    need = math.ceil(n / block)
    means = []
    for _ in range(draws):
        total = 0.0
        for _b in range(need):
            start = rng.randrange(n)
            for j in range(block):
                total += series[(start + j) % n]
        means.append(total / (need * block))
    means.sort()

    def q(p):
        idx = min(len(means) - 1, max(0, int(round(p * (len(means) - 1)))))
        return means[idx]

    return {'mean': sum(series) / n, 'lower': q(alpha), 'upper': q(1 - alpha),
            'draws': draws, 'block': block, 'n': n}


# --- spending alpha across a fixed number of looks ---------------------------

def obrien_fleming(k: int, total: int, alpha: float) -> float:
    """Alpha available at look k of `total`. Early looks are almost free.

    O'Brien-Fleming spends very little early and most at the end, which is the
    right shape here: an early promotion on twenty days of a noisy daily series
    would be the expensive mistake, and a late one costs only patience.
    """
    if total <= 0:
        return alpha
    t = max(1e-6, min(1.0, k / total))
    z = _z(1 - alpha / 2)
    return 2 * (1 - _phi(z / math.sqrt(t)))


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _z(p: float) -> float:
    """Inverse normal CDF, Acklam's rational approximation. Plenty for alpha levels."""
    if p <= 0 or p >= 1:
        raise ValueError('p must be in (0, 1)')
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def required_days(sigma_lr: float, delta_min: float, alpha: float, beta: float) -> int | None:
    """How many trading days this needs, with power, from the long-run variance.

    Paired design, so there is one series and one sigma. No events-per-day design
    effect is applied: the observation IS the daily difference, so within-day
    clustering is already aggregated away and inflating for it again would double
    count.
    """
    if not sigma_lr or delta_min <= 0:
        return None
    z = _z(1 - alpha) + _z(1 - beta)
    return int(math.ceil((z * sigma_lr / delta_min) ** 2))


def holm(pvalues: dict[str, float], alpha: float) -> dict[str, bool]:
    """Holm-Bonferroni across the confirmatory family. Two claims, not twenty."""
    ordered = sorted(pvalues.items(), key=lambda kv: kv[1])
    m = len(ordered)
    out, still = {}, True
    for i, (name, p) in enumerate(ordered):
        threshold = alpha / (m - i)
        still = still and p <= threshold
        out[name] = still
    return out


# --- calibration of the forecasts -------------------------------------------

def brier(pairs: list[tuple[float, bool]]) -> dict | None:
    """Brier score and skill against the base rate.

    Applied to the PROBABILITIES only. The 1-10 scores never appear here: a
    rating is not a probability and scoring it as one would be inventing a number.
    """
    if len(pairs) < 5:
        return None
    n = len(pairs)
    score = sum((p - (1.0 if hit else 0.0)) ** 2 for p, hit in pairs) / n
    rate = sum(1 for _, hit in pairs if hit) / n
    ref = sum((rate - (1.0 if hit else 0.0)) ** 2 for _, hit in pairs) / n
    return {'n': n, 'brier': score, 'base_rate': rate,
            'skill': (1 - score / ref) if ref > 0 else None}


def reliability(pairs: list[tuple[float, bool]], bins: int = 5) -> list[dict]:
    """Does 40% mean 40%? Binned forecast against realised frequency."""
    buckets: dict[int, list] = defaultdict(list)
    for p, hit in pairs:
        buckets[min(bins - 1, int(p * bins))].append((p, hit))
    out = []
    for b in sorted(buckets):
        rows = buckets[b]
        out.append({'from': b / bins, 'to': (b + 1) / bins, 'n': len(rows),
                    'forecast': sum(p for p, _ in rows) / len(rows),
                    'realised': sum(1 for _, hit in rows if hit) / len(rows)})
    return out


def forecast_pairs(ev: Evaluation) -> dict[str, list]:
    rows = SymbolDossier.objects.filter(outcome_at__isnull=False, as_of__gte=ev.opened_at,
                                        error='').exclude(outcome_kind='')
    target, positive = [], []
    for d in rows:
        if d.p_target_first is not None:
            target.append((d.p_target_first, d.outcome_kind == 'target'))
        if d.p_positive_net is not None and d.net_atr_catalyst_only is not None:
            positive.append((d.p_positive_net, d.net_atr_catalyst_only > 0))
    return {'p_target_first': target, 'p_positive_net': positive}


# --- the decision -----------------------------------------------------------

def assess(ev: Evaluation, now=None) -> dict:
    """Everything the scoreboard shows, and everything the decision needs."""
    rows = daily_series(ev, now)
    diffs = [r['diff'] for r in rows]
    ablation = [r['diff_ablation'] for r in rows]
    n_days = len(rows)
    sigma = batch_means_sigma(diffs)
    done = len(ev.checkpoints_done or [])
    total = len(ev.checkpoints or []) or 1
    spent = obrien_fleming(done + 1, total, ev.alpha)

    primary = circular_block_bootstrap(diffs, spent) if n_days >= MIN_DAYS_FOR_STATS else None
    second = circular_block_bootstrap(ablation, spent) if n_days >= MIN_DAYS_FOR_STATS else None
    return {
        'evaluation': ev,
        'days': rows,
        'n_days': n_days,
        'sigma_lr': sigma,
        'required_days': required_days(sigma, ev.delta_min, ev.alpha, ev.beta) if sigma else None,
        'alpha_spent_next': spent,
        'next_checkpoint': ev.next_checkpoint,
        'primary': primary,
        'ablation': second,
        'delta_min': ev.delta_min,
        'forecasts': {key: {'brier': brier(pairs), 'reliability': reliability(pairs)}
                      for key, pairs in forecast_pairs(ev).items()},
        'outcomes': _outcome_mix(ev),
    }


def _outcome_mix(ev: Evaluation) -> dict:
    rows = SymbolDossier.objects.filter(outcome_at__isnull=False, as_of__gte=ev.opened_at)
    mix = {'target': 0, 'stop': 0, 'timeout': 0, 'vetoed': 0}
    for d in rows:
        if d.veto_reason:
            mix['vetoed'] += 1
        if d.outcome_kind in mix:
            mix[d.outcome_kind] += 1
    return mix


def decide(ev: Evaluation, now=None, apply: bool = False) -> dict:
    """Promote, demote, or keep collecting. Checkpoints only — never on demand.

    This is the whole defence against optional stopping. `n_days` has to have
    REACHED a preregistered checkpoint; being near one, or being past the last
    one, is not a look. A scheduled job may call this as often as it likes and it
    will still only ever decide at the days that were written down in advance.
    """
    now = now or timezone.now()
    a = assess(ev, now)
    result = {'decision': 'collecting', 'reason': '', 'assessment': a}
    if ev.status != 'collecting':
        result['decision'], result['reason'] = ev.status, 'this evaluation is closed'
        return result

    checkpoint = ev.next_checkpoint
    if checkpoint is None:
        result['reason'] = 'every preregistered checkpoint has been used'
        return result
    if a['n_days'] < checkpoint:
        result['reason'] = (f'{a["n_days"]} of {checkpoint} trading days to the next '
                            f'preregistered look')
        return result
    if a['primary'] is None:
        result['reason'] = 'not enough days for a dependence-robust interval'
        return result

    spent = a['alpha_spent_next']
    lower, upper = a['primary']['lower'], a['primary']['upper']
    # Holm across the confirmatory family. The primary claim decides promotion;
    # the ablation decides only whether the size multiplier may ever exceed 1.0.
    pvals = {'primary': _one_sided_p(a['primary'], ev.delta_min),
             'ablation': _one_sided_p(a['ablation'], 0.0) if a['ablation'] else 1.0}
    passed = holm(pvals, spent)

    if lower > ev.delta_min and passed.get('primary'):
        decision = 'promote'
        reason = (f'lower bound {lower:+.3f} clears the {ev.delta_min:+.3f} ATR/day hurdle at '
                  f'alpha {spent:.4f} after {a["n_days"]} days')
    elif upper < 0:
        decision = 'demote'
        reason = f'upper bound {upper:+.3f} is below zero after {a["n_days"]} days'
    else:
        decision = 'collecting'
        reason = (f'checkpoint {checkpoint}: {lower:+.3f} to {upper:+.3f} ATR/day straddles the '
                  f'{ev.delta_min:+.3f} hurdle; no decision')

    result.update(decision=decision, reason=reason, checkpoint=checkpoint,
                  alpha_spent=spent, holm=passed, pvalues=pvals,
                  ablation_passed=bool(passed.get('ablation')))
    if apply:
        _record(ev, checkpoint, spent, a, decision, reason, passed)
    return result


def _one_sided_p(boot: dict | None, hurdle: float) -> float:
    """A bootstrap p-value for mean <= hurdle, from the resampled distribution."""
    if not boot:
        return 1.0
    spread = max(1e-9, (boot['upper'] - boot['lower']) / 2)
    z = (boot['mean'] - hurdle) / spread
    return max(0.0, min(1.0, 1 - _phi(z)))


def _record(ev: Evaluation, checkpoint: int, spent: float, a: dict, decision: str,
            reason: str, passed: dict) -> None:
    ev.checkpoints_done = list(ev.checkpoints_done or []) + [{
        'n': checkpoint, 'alpha_spent': round(spent, 5),
        'mean': round(a['primary']['mean'], 4), 'lower': round(a['primary']['lower'], 4),
        'upper': round(a['primary']['upper'], 4), 'decision': decision,
        'holm': passed, 'at': timezone.now().isoformat(),
    }]
    fields = ['checkpoints_done']
    if decision in ('promote', 'demote'):
        ev.status = 'promoted' if decision == 'promote' else 'demoted'
        ev.closed_at = timezone.now()
        ev.note = reason[:300]
        fields += ['status', 'closed_at', 'note']
    ev.save(update_fields=fields)
    if decision == 'promote':
        # A3: promotion opens a NEW epoch at n=0. An all-history sequence lets a
        # good first month outvote a strategy that is failing right now.
        from .preregistration import open_evaluation
        epoch = open_evaluation(kind='decay', predecessor=ev)
        log.warning('promoted %s; decay monitor %s opens at n=0', ev.identifier, epoch.identifier)
