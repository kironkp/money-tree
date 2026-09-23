"""Routing macro news to the currency lane, which the wire never does by itself.

The problem this exists for: a census of all 598 shadow verdicts on 2026-09-23
found 2 that concern any FX instrument. The wire is Benzinga via Alpaca, which
tags stories by equity ticker, so a Federal Reserve rate decision arrives
labelled QQQ. It is far more directly a dollar event than a Nasdaq one, and the
forex lane never sees it. That is why news_catalyst has taken zero trades on
forex in its entire life.

The mapping is unusually clean here. Every pair this desk trades is quoted
XXX/USD, so one judgement — is this story dollar-positive or dollar-negative —
moves all four the same way. A stronger dollar sends EUR/USD, GBP/USD, AUD/USD
and NZD/USD all DOWN. (That shared factor is also why the lane has a directional
exposure cap: the four pairs are close to one position wearing four names.)

SHADOW ONLY. Every verdict written here has tradable=False. The point is to
start accumulating a labelled sample from today so the question "does macro news
predict forex" becomes answerable in a few weeks, instead of resting on the two
rows that exist now. Nothing here may place a trade, and a test asserts it.
"""
from __future__ import annotations

import logging
import re

from django.utils import timezone

from main_app.models import Market, NewsItem, NewsVerdict

log = logging.getLogger(__name__)

PAIRS = ('EUR/USD', 'GBP/USD', 'AUD/USD', 'NZD/USD')

# A story has to be about one of these before any dollar reading is attempted.
MACRO = re.compile(
    r'\b(fed|fomc|federal reserve|powell|rate cut|rate hike|raises? rates?|cuts? rates?|'
    r'interest rates?|cpi|inflation|jobs report|payrolls?|unemployment|ecb|bank of england|boe|'
    r'bank of japan|boj|central bank|treasury yields?|10-year yield|bond yields?|'
    r'the dollar|u\.s\. dollar|us dollar|dxy|greenback|currency|currencies|forex|fx market|'
    r'tariffs?|trade war|gdp|recession|quantitative (easing|tightening))\b', re.I)

# Dollar-positive: tighter policy, higher yields, risk-off into USD, strong data.
# Verbs carry an optional plural 's' throughout: the first version missed
# "Yield Hits 19-Year High" because it matched "hit" and not "hits".
UP = re.compile(
    r'\b(rate hikes?|raises? rates?|hikes? rates?|hawkish|tightening|'
    r'yields? \w{0,6} ?(rises?|surges?|jumps?|climbs?|tops?|reclaims?|hits?|soars?)|'
    r'(rises?|surges?|jumps?|climbs?|tops?|reclaims?|hits?) \w{0,12} ?yield|'
    r'inflation (rises?|surges?|hotter|accelerat)|stickier inflation|'
    r'strong(er)? (jobs|payrolls|data)|dollar (rises?|strengthens?|surges?|rallies)|'
    r'safe.haven|risk.off)\b', re.I)

# Dollar-negative: easing, falling yields, weak data, explicit dollar weakness.
DOWN = re.compile(
    r'\b(rate cuts?|cuts? rates?|dovish|easing|'
    r'yields? \w{0,6} ?(falls?|drops?|slides?|retreats?|sinks?|eases?|tumbles?)|'
    r'inflation (cools?|eases?|falls?|slows?)|weak(er)? (jobs|payrolls|data)|'
    r'dollar (falls?|weakens?|slides?|drops?|no bottom)|rally in risk|risk.on)\b', re.I)


def usd_direction(headline: str) -> tuple[str, str]:
    """('up'|'down'|'unclear', why).

    Rules rather than a model, deliberately. The purpose right now is to start a
    clean labelled sample; a rule that abstains when it is unsure produces better
    data than a model that always answers. Rows labelled 'unclear' are still
    recorded, because the story set is the asset and the label can be improved
    later without re-collecting it.
    """
    h = headline or ''
    up, down = UP.search(h), DOWN.search(h)
    if up and not down:
        return 'up', f'dollar-positive: {up.group(0)!r}'
    if down and not up:
        return 'down', f'dollar-negative: {down.group(0)!r}'
    if up and down:
        return 'unclear', f'both readings present: {up.group(0)!r} and {down.group(0)!r}'
    return 'unclear', 'macro, but no directional phrase matched'


def is_macro(headline: str) -> bool:
    return bool(MACRO.search(headline or ''))


def pair_direction(usd: str) -> str:
    """Every pair is XXX/USD, so a stronger dollar is a SELL on all four."""
    return {'up': 'sell', 'down': 'buy'}.get(usd, 'none')


def route(session, stories=None, now=None) -> list[NewsVerdict]:
    """Write one shadow forex verdict per (macro story x pair). Never trades."""
    now = now or timezone.now()
    stories = list(stories if stories is not None else
                   NewsItem.objects.filter(published_at__gte=now - timezone.timedelta(days=1)))
    from main_app.services.news_agent import event_key

    rows = []
    for story in stories:
        if not is_macro(story.headline):
            continue
        usd, why = usd_direction(story.headline)
        direction = pair_direction(usd)
        for pair in PAIRS:
            rows.append(NewsVerdict(
                session=session, news=story, headline=story.headline[:500], url=story.url,
                symbol=pair, market=Market.FOREX, score=0, direction=direction,
                thesis=f'macro->fx: USD {usd} ({why}). {pair} therefore {direction or "unmoved"}.'[:2000],
                horizon='macro',
                # The whole point is that this cannot trade. It is a sample being
                # collected, not a signal being acted on.
                tradable=False,
                blocked_reason='shadow: macro->fx routing is collecting a sample, not trading',
                event_key=event_key(pair, story.headline),
                provenance='contemporaneous', arm='macro_fx'))
    if rows:
        NewsVerdict.objects.bulk_create(rows, ignore_conflicts=True)
    log.info('macro->fx routed %d stories into %d shadow verdicts', len(rows) // len(PAIRS), len(rows))
    return rows
