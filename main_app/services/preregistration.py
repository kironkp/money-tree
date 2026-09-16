"""Freeze the experiment before it collects anything.

A threshold chosen after seeing the data is not a threshold, and the cheapest way
to stop that happening by accident is to make it impossible to happen quietly. So
everything that could change the result — the model, the exact prompt text, the
schema, the act thresholds, every risk limit, the kill rule — is hashed into one
fingerprint. If the fingerprint moves, the running evaluation is superseded and a
new one opens at n=0 rather than inheriting evidence gathered under other rules.

That is a deliberately annoying property. It means a prompt tweak costs the
weeks of data already collected, which is exactly the cost that stops a prompt
being tweaked until the numbers look better.

WHY FIXED CHECKPOINTS RATHER THAN A CONFIDENCE SEQUENCE
The paired daily series is serially dependent — volatility clusters, and one
dossier can influence several consecutive days. A time-uniform confidence
sequence that assumes independence is not valid here, and implementing a
correctly dependence-robust one is easy to get subtly wrong in ways that only
show up as false promotions. So this uses a small number of preregistered
checkpoints with O'Brien-Fleming alpha spending, evaluated on a circular block
bootstrap that handles the dependence directly. Fewer looks, each one honest.
"""
from __future__ import annotations

import hashlib
import json
import logging

from django.utils import timezone

from main_app.models import AgentConfig, Evaluation

log = logging.getLogger('moneytree.preregistration')

# --- the hypothesis, in numbers ---------------------------------------------
# Delta_min is the smallest improvement worth having, in net ATR per day, and it
# is an ECONOMIC hurdle rather than a statistical one. The desk risks ~0.5% of a
# $10,000 account per trade and the North Star is weekly after-cost profit, so an
# improvement that does not move a week's P&L by a noticeable amount is not worth
# the $5.88/month and the extra failure surface. 0.15 ATR/day across five days is
# about three quarters of one winning trade a week — small, but not noise, and
# not zero.
DELTA_MIN_ATR_PER_DAY = 0.15
ALPHA = 0.05
BETA = 0.20                     # 80% power
CHECKPOINTS = [20, 40, 60, 90, 120]     # trading days
KILL_RULE = ('demote when a checkpoint upper bound falls below zero, or immediately on any '
             'hard limit: daily loss, weekly loss, ten consecutive losses, slippage over 2x')

METHOD = 'fixed-checkpoints/obrien-fleming/circular-block-bootstrap'


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def current_fingerprint() -> tuple[str, dict]:
    """Everything that could change the answer, hashed into one string."""
    from . import dossier as dz
    from .news_agent import ACT_SOURCES, MODEL as NEWS_MODEL
    cfg = AgentConfig.get()
    frozen = {
        'dossier_model': dz.MODEL,
        'dossier_tier': dz.SERVICE_TIER,
        'dossier_max_searches': dz.MAX_SEARCHES,
        'dossier_researched': list(dz.RESEARCHED),
        'dossier_catalyst_max_age_hours': dz.CATALYST_MAX_AGE_HOURS,
        'prompt_sha': _sha(dz.SYSTEM),
        'schema_sha': _sha(json.dumps(dz._schema(), sort_keys=True)),
        'news_model': NEWS_MODEL,
        'act_sources': ACT_SOURCES,
        'risk': {
            'correlated_exposure_pct': str(cfg.news_max_correlated_exposure_pct),
            'same_direction_positions': cfg.news_max_same_direction_positions,
            'daily_loss_pct': str(cfg.news_daily_loss_pct),
            'weekly_loss_pct': str(cfg.news_weekly_loss_pct),
            'consecutive_losses': cfg.news_max_consecutive_losses,
            'slippage_trip_multiple': str(cfg.news_slippage_trip_multiple),
            'max_hold_minutes': cfg.max_hold_minutes,
            'risk_per_trade_pct': str(cfg.risk_per_trade_pct),
            'max_position_pct': str(cfg.max_position_pct),
            'min_reward_to_cost': str(cfg.min_reward_to_cost),
        },
        'hypothesis': {
            'primary': ('paired difference in total net ATR per day, complete research policy '
                        'minus headline-only policy, including no-trade days'),
            'secondary_confirmatory': ('catalyst-only versus catalyst plus context/thesis; '
                                       'until this passes, the size multiplier is capped at 1.0'),
            'delta_min': DELTA_MIN_ATR_PER_DAY,
            'alpha': ALPHA, 'beta': BETA, 'checkpoints': CHECKPOINTS, 'method': METHOD,
            'multiplicity': 'Holm across the two confirmatory claims; Benjamini-Hochberg is '
                            'exploratory only and can never trigger promotion',
        },
        'kill_rule': KILL_RULE,
    }
    return _sha(json.dumps(frozen, sort_keys=True)), frozen


def current(kind: str = 'promotion') -> Evaluation:
    """The open evaluation, opening or superseding one if the rules have moved."""
    fingerprint, frozen = current_fingerprint()
    open_now = Evaluation.objects.filter(status='collecting', kind=kind).first()
    if open_now is not None and open_now.fingerprint == fingerprint:
        return open_now
    if open_now is not None:
        open_now.status = 'superseded'
        open_now.closed_at = timezone.now()
        open_now.note = (f'rules changed: {open_now.fingerprint} -> {fingerprint}')[:300]
        open_now.save(update_fields=['status', 'closed_at', 'note'])
        log.warning('evaluation %s superseded: the frozen configuration changed',
                    open_now.identifier)
    return open_evaluation(kind=kind, predecessor=open_now)


def open_evaluation(kind: str = 'promotion', predecessor=None) -> Evaluation:
    fingerprint, frozen = current_fingerprint()
    stamp = timezone.now()
    n = Evaluation.objects.filter(opened_at__date=stamp.date()).count() + 1
    return Evaluation.objects.create(
        identifier=f'{kind[:4]}-{stamp:%Y%m%d}-{n}', kind=kind, fingerprint=fingerprint,
        frozen=frozen, delta_min=DELTA_MIN_ATR_PER_DAY, alpha=ALPHA, beta=BETA,
        checkpoints=list(CHECKPOINTS), method=METHOD, kill_rule=KILL_RULE,
        predecessor=predecessor)


def describe(ev: Evaluation) -> str:
    """The preregistration, as a human would read it back."""
    f = ev.frozen or {}
    h = f.get('hypothesis', {})
    lines = [
        f'{ev.identifier} — {ev.get_status_display()}, opened {ev.opened_at:%Y-%m-%d %H:%M}',
        f'fingerprint {ev.fingerprint}',
        '',
        'PRIMARY (confirmatory, one hypothesis):',
        f'  {h.get("primary", "")}',
        f'  hurdle {ev.delta_min:+.3f} ATR/day, alpha {ev.alpha}, power {1 - ev.beta:.0%}',
        f'  checkpoints at {", ".join(str(c) for c in ev.checkpoints)} trading days',
        f'  method: {ev.method}',
        '',
        'SECONDARY (confirmatory, its own gate):',
        f'  {h.get("secondary_confirmatory", "")}',
        '',
        f'MULTIPLICITY: {h.get("multiplicity", "")}',
        f'KILL RULE: {ev.kill_rule}',
        '',
        'FROZEN:',
        f'  dossier {f.get("dossier_model")} / {f.get("dossier_tier")}, '
        f'<= {f.get("dossier_max_searches")} searches',
        f'  prompt {f.get("prompt_sha")}, schema {f.get("schema_sha")}',
        f'  news agent {f.get("news_model")}',
        f'  risk {json.dumps(f.get("risk", {}), sort_keys=True)}',
    ]
    return '\n'.join(lines)
