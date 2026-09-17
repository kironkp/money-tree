"""The news arm's own limits, preregistered and machine-enforced.

These are deliberately separate from `RiskManager`. The risk manager protects the
ACCOUNT and knows nothing about which strategy is asking; these protect the
EXPERIMENT, and they exist because a single model prediction must never be able
to create uncapped exposure. Seven megacap names are one bet wearing seven hats,
so the limit that matters is the aggregate, not the per-position one.

Every value here is frozen as of 2026-09-17 and lives on AgentConfig. Changing
any of them starts a new evaluation identifier rather than extending the running
one — a limit tuned mid-experiment is a result chosen after seeing the data.

Nothing here asks a human anything. A breach halts the arm and the arm re-arms
itself: a daily breach clears with the next session, a weekly one persists until
the week turns. That is the whole point of writing the rules down in advance.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone

from main_app.models import AgentConfig, RiskEvent, Trade

from .data import calendar as cal

log = logging.getLogger('moneytree.news_risk')

STRATEGY_KEY = 'news_catalyst'
HALT_KIND = 'news_halt'


def _day_start(now) -> datetime:
    return datetime.combine(cal.session_date(now), time(), tzinfo=cal.ET)


def _week_start(now) -> datetime:
    """Monday 00:00 ET — the same week boundary the spend report uses."""
    d = cal.session_date(now)
    return datetime.combine(d - timedelta(days=d.weekday()), time(), tzinfo=cal.ET)


def _pnl_since(account, since) -> Decimal:
    rows = Trade.objects.filter(account=account, strategy_key=STRATEGY_KEY, exit_ts__gte=since)
    return sum((t.pnl for t in rows), Decimal('0'))


def _consecutive_losses(account) -> int:
    n = 0
    for t in Trade.objects.filter(account=account, strategy_key=STRATEGY_KEY).order_by('-exit_ts')[:40]:
        if t.pnl >= 0:
            break
        n += 1
    return n


def _halted_this_week(account, now) -> RiskEvent | None:
    """A weekly or slippage breach is sticky until the week turns."""
    return (RiskEvent.objects
            .filter(account=account, kind=HALT_KIND, ts__gte=_week_start(now))
            .exclude(data__sticky=False)
            .order_by('-ts').first())


def _halt(account, reason: str, sticky: bool) -> str:
    RiskEvent.objects.create(account=account, kind=HALT_KIND, message=reason[:300],
                             data={'sticky': sticky, 'arm': STRATEGY_KEY})
    log.warning('news arm halted: %s', reason)
    return reason


def check(account, symbol: str, direction: str, positions: dict, now=None, cfg=None,
          equity: float | None = None) -> str:
    """A reason the news arm may not open this position right now, or ''.

    Order matters: the cheapest checks first, and the sticky ones before the
    ones that clear on their own, so the message a human reads is the one that
    will still be true tomorrow.
    """
    now = now or timezone.now()
    cfg = cfg or AgentConfig.get()
    # `account` must be the persisted Account row — these limits read trade
    # history. Equity comes from the broker's live view, passed in separately,
    # because the two are different objects and conflating them is what silently
    # vetoed every signal on 2026-09-17.
    if equity is None:
        equity = float(getattr(account, 'equity', 0) or 0)
    equity = Decimal(str(equity))
    if equity <= 0:
        return ''

    sticky = _halted_this_week(account, now)
    if sticky is not None:
        return f'news arm halted for the week: {sticky.message}'

    # Weekly loss — sticky, because a bad week is a signal about the arm and not
    # about the day.
    weekly = _pnl_since(account, _week_start(now))
    weekly_cap = -equity * cfg.news_weekly_loss_pct / 100
    if weekly <= weekly_cap:
        return _halt(account, f'weekly loss {weekly:+,.2f} reached the '
                              f'{cfg.news_weekly_loss_pct}% limit ({weekly_cap:,.2f})', sticky=True)

    # Consecutive losses — sticky. Ten in a row is not variance being unkind, it
    # is the cost model or the signal being wrong.
    losses = _consecutive_losses(account)
    if losses >= cfg.news_max_consecutive_losses:
        return _halt(account, f'{losses} losing news trades in a row', sticky=True)

    # Daily loss — clears with the next session, no human involved.
    day_start = _day_start(now)
    daily = _pnl_since(account, day_start)
    daily_cap = -equity * cfg.news_daily_loss_pct / 100
    if daily <= daily_cap:
        return _halt(account, f'daily loss {daily:+,.2f} reached the '
                              f'{cfg.news_daily_loss_pct}% limit ({daily_cap:,.2f})', sticky=False)

    # Correlated exposure. This is the limit the seven-megacap universe actually
    # needs: each position can sit inside every per-position cap while the book
    # as a whole is one leveraged bet on large-cap tech.
    want = 1 if direction == 'buy' else -1
    same, count = Decimal('0'), 0
    for sym, pos in positions.items():
        qty = getattr(pos, 'qty', 0) or 0
        if not qty or (1 if qty > 0 else -1) != want:
            continue
        same += Decimal(str(abs(pos.market_value())))
        count += 1
    if count >= cfg.news_max_same_direction_positions:
        return (f'{count} positions already open on the same side '
                f'(limit {cfg.news_max_same_direction_positions})')
    cap = equity * cfg.news_max_correlated_exposure_pct / 100
    if same >= cap:
        return (f'correlated exposure {same:,.0f} of {cap:,.0f} '
                f'({cfg.news_max_correlated_exposure_pct}% of equity) is used')
    return ''


def slippage_breach(account, cfg=None) -> str:
    """Is realised slippage running at more than the configured multiple?

    The configured 3 bps is an assumption whose only evidence is circular — the
    simulator measures the fills it produced. If the real number is twice that,
    every cost gate and every graded outcome downstream is wrong, and trading on
    is spending money to collect bad evidence.
    """
    from main_app.models import Fill
    cfg = cfg or AgentConfig.get()
    rows = [abs(float(f.realized_slippage_bps or 0))
            for f in Fill.objects.filter(order__account=account,
                                         order__strategy_key=STRATEGY_KEY).order_by('-ts')[:60]]
    if len(rows) < 20:
        return ''                      # not enough fills to say anything
    rows.sort()
    median = rows[len(rows) // 2]
    limit = float(cfg.slippage_bps) * float(cfg.news_slippage_trip_multiple)
    if median > limit:
        return _halt(account, f'realised slippage {median:.1f} bps is over {limit:.1f} bps '
                              f'({cfg.news_slippage_trip_multiple}x configured)', sticky=True)
    return ''
