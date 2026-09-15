"""The News Agent: read everything new, score each story out of 10, act above 5.

Every four hours it takes the stories that arrived since its last sitting, plus
the wide-view lane briefings, and answers one question per story: out of ten,
what is the chance that acting on this makes money TODAY. Anything at or above
NewsVerdict.ACT_THRESHOLD becomes an instruction — BUY AAPL, SHORT NZD/USD —
which the lane's trading agent picks up on its next bar and puts through the
normal risk gate. Everything below is still written down, because a record of
what it declined is what makes the record of what it took mean anything.

Two design choices worth defending:

It scores in ONE call, not one per story. Stories interact — "Japan's economy is
slowing" and "the dollar is bid" are the same trade seen twice — and a model that
sees them together can say so, map a macro story onto the currency pair we
actually trade, and avoid recommending the same position four times. It is also
far cheaper.

It can only name symbols we can actually trade. The universe is handed to it
explicitly and anything else is discarded, because a confident call on a ticker
with no market behind it is worse than no call: it reads like an opportunity and
cannot become one.

Honest note on what this is. The published evidence for trading retail news
sentiment is weak, and what survives lives in small-cap shorts rather than the
megacaps here. This is built because the owner asked for it, on fake money, with
every verdict scored against price afterwards. That scoreboard, not the model's
confidence, is what should eventually decide whether it keeps trading.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from main_app.models import Briefing, Instrument, Market, NewsItem, NewsSession, NewsVerdict

log = logging.getLogger('moneytree.news_agent')

MODEL = 'gpt-5.6-sol'          # the account's flagship tier
MAX_STORIES = 40               # per sitting, newest first
LOOKBACK_HOURS = 5             # overlaps the 4-hour cadence so nothing is missed
PRICE_PER_M = (1.25, 10.0)     # input, output USD per million — used only to log spend

SYSTEM = """You are the news analyst for a small automated trading desk. Every four hours you read
what has happened and decide, story by story, whether it can be traded TODAY.

For each story you answer one question: out of 10, what is the chance that acting on this news makes
money today? Be calibrated and be harsh. Most news is already in the price by the time it is
published. A rating of 5 or more becomes a real order, so treat 5 as "I would put money on this",
not "this is interesting".

Guidance for the scale:
  1-2  noise, commentary, opinion, a listicle, something already widely known
  3-4  real but small, or real but already priced in, or too slow to matter today
  5-6  a genuine edge worth a small position
  7-8  a clear, confirmed, market-moving event with an obvious direction
  9-10 reserve for something extraordinary and unambiguous

Rules you must follow:
- You may only name a symbol from the tradable universe given to you. If a story is about something
  we cannot trade, either map it to a symbol we CAN trade and say so in the thesis, or score it low
  with direction "none".
- Map macro stories onto the instrument that actually expresses them. A story about the Japanese
  economy is not tradable as "Japan" here; if it implies dollar strength, the expression is a short
  in a USD pair we trade. Say that reasoning out loud in the thesis.
- If two stories are the same trade, give the stronger one the score and mark the other "none" with
  a thesis explaining it is a duplicate expression.
- Crypto and altcoins cannot be sold short on this desk. For those, a bearish story can only be
  "none" — say so rather than pretending.
- Direction "buy" means we expect it up today, "short" means down today, "none" means do nothing.
- The thesis is read by a human. One or two sentences, plain English, concrete. Say what happened,
  what you expect, and why. No hedging filler, no "could potentially".

Also write one short paragraph, the "narrative", describing what is going on across markets right
now as you see it — the through-line a person would want before reading the individual calls."""


def _schema(n: int) -> dict:
    return {
        'type': 'object',
        'additionalProperties': False,
        'required': ['narrative', 'verdicts'],
        'properties': {
            'narrative': {'type': 'string'},
            'verdicts': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'additionalProperties': False,
                    'required': ['id', 'score', 'direction', 'symbol', 'thesis', 'horizon'],
                    'properties': {
                        'id': {'type': 'integer', 'description': 'the story id given to you'},
                        'score': {'type': 'integer', 'description': '1 to 10'},
                        'direction': {'type': 'string', 'enum': ['buy', 'short', 'none']},
                        'symbol': {'type': 'string', 'description': 'a symbol from the universe, or empty'},
                        'thesis': {'type': 'string'},
                        'horizon': {'type': 'string', 'enum': ['hours', 'today', 'days', 'weeks']},
                    },
                },
            },
        },
    }


def universe() -> dict[str, str]:
    """Symbol -> lane, for everything this desk can actually trade."""
    return {s: m for s, m in Instrument.objects.filter(active=True, in_watchlist=True)
            .values_list('symbol', 'market')}


def _shortable(market: str) -> bool:
    # Spot crypto cannot be sold short on this desk.
    return market not in (Market.CRYPTO, Market.DEGEN)


def gather(since) -> list[NewsItem]:
    """Stories this sitting should consider: classified, novel, newest first."""
    return list(NewsItem.objects.filter(published_at__gte=since, novel=True,
                                        classified_at__isnull=False)
                .exclude(verdicts__isnull=False)
                .order_by('-published_at')[:MAX_STORIES])


def _prompt(stories: list[NewsItem], uni: dict[str, str]) -> str:
    lanes: dict[str, list[str]] = {}
    for sym, market in sorted(uni.items()):
        lanes.setdefault(market, []).append(sym)
    parts = ['TRADABLE UNIVERSE (you may only name these):']
    for market, syms in lanes.items():
        note = '' if _shortable(market) else '  [LONG ONLY — no shorts possible]'
        parts.append(f'  {market}{note}: {", ".join(syms)}')

    briefs = []
    for market in lanes:
        b = Briefing.objects.filter(market=market, error='').order_by('-ts').first()
        if b and b.headline:
            briefs.append(f'  {market}: {b.headline}')
    if briefs:
        parts += ['', 'THE WIDER PICTURE right now (from a separate search):'] + briefs

    parts += ['', f'STORIES TO SCORE ({len(stories)}). Return one verdict for every id.']
    for s in stories:
        tags = ', '.join(s.symbols) or 'untagged'
        parts.append(f'  [id {s.pk}] ({s.published_at:%b %-d %H:%M}Z, {s.kind or "?"}, tagged {tags}) '
                     f'{s.headline}')
        if s.summary:
            parts.append(f'        {s.summary[:220]}')
    return '\n'.join(parts)


def run_session(since=None, model: str = MODEL, act: bool = True) -> NewsSession:
    """One sitting. Always returns a session row, even when it fails."""
    from openai import OpenAI

    from .spend import record

    started = time.time()
    session = NewsSession.objects.create(model=model)
    since = since or timezone.now() - timedelta(hours=LOOKBACK_HOURS)
    uni = universe()
    stories = gather(since)
    session.considered = len(stories)
    if not stories:
        session.narrative = 'Nothing new to read since the last sitting.'
        session.finished_at = timezone.now()
        session.duration_s = time.time() - started
        session.save()
        return session
    if not settings.OPENAI_API_KEY:
        session.error = 'no OPENAI_API_KEY configured'
        session.finished_at = timezone.now()
        session.save()
        return session

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    try:
        resp = client.chat.completions.create(
            model=model, max_completion_tokens=16000,
            messages=[{'role': 'system', 'content': SYSTEM},
                      {'role': 'user', 'content': _prompt(stories, uni)}],
            response_format={'type': 'json_schema',
                             'json_schema': {'name': 'verdicts', 'strict': True,
                                             'schema': _schema(len(stories))}},
        )
        data = json.loads(resp.choices[0].message.content)
    except Exception as exc:
        log.exception('news agent session failed')
        session.error = str(exc)[:300]
        session.finished_at = timezone.now()
        session.duration_s = time.time() - started
        session.save()
        return session

    usage = resp.usage
    cost = ((usage.prompt_tokens * PRICE_PER_M[0] + usage.completion_tokens * PRICE_PER_M[1]) / 1e6)
    record(model, provider='openai', project='moneytree', purpose='news_agent',
           input_tokens=usage.prompt_tokens, output_tokens=usage.completion_tokens,
           cost_usd=round(cost, 6), note=f'news agent session #{session.pk}, {len(stories)} stories')

    by_id = {s.pk: s for s in stories}
    verdicts = []
    for raw in data.get('verdicts', []):
        story = by_id.get(int(raw.get('id', 0) or 0))
        if story is None:
            continue
        symbol = str(raw.get('symbol', '')).strip().upper()
        market = uni.get(symbol, '')
        direction = str(raw.get('direction', 'none'))
        score = max(0, min(10, int(raw.get('score', 0) or 0)))
        # Guardrails the model is told about but must not be trusted to honour.
        if symbol and not market:
            direction, blocked = 'none', f'{symbol} is not in the tradable universe'
        elif direction == 'short' and market and not _shortable(market):
            direction, blocked = 'none', 'spot crypto cannot be sold short'
        else:
            blocked = ''
        verdicts.append(NewsVerdict(
            session=session, news=story, headline=story.headline[:500], url=story.url,
            symbol=symbol if market else '', market=market, score=score, direction=direction,
            thesis=str(raw.get('thesis', ''))[:2000], horizon=str(raw.get('horizon', ''))[:12],
            tradable=bool(market), blocked_reason=blocked[:200],
        ))
    NewsVerdict.objects.bulk_create(verdicts)

    session.narrative = str(data.get('narrative', ''))[:4000]
    session.actionable = sum(1 for v in verdicts if v.actionable)
    session.cost_usd = round(cost, 5)
    session.finished_at = timezone.now()
    session.duration_s = time.time() - started
    session.save()

    for v in session.verdicts.all():
        if v.actionable:
            v.price_at_verdict = _price(v.symbol)
            v.save(update_fields=['price_at_verdict'])
    return session


def _price(symbol: str) -> float | None:
    from .data.store import load_frame
    inst = Instrument.objects.filter(symbol=symbol).first()
    if inst is None:
        return None
    for tf in ('5Min', '15Min', '1Hour', '4Hour'):
        df = load_frame(inst, tf, limit=1)
        if len(df):
            return float(df['close'].iloc[-1])
    return None


# --- what the trading lanes read ------------------------------------------

# How long an instruction stays live. Measured in WALL time but sized per lane,
# because a lane that is shut cannot act. A verdict written at 8pm on a US stock
# has to survive the overnight close or it can never be traded at all — the
# 4-hour window that suits a 24/7 crypto lane silently discarded every evening
# call on stocks. Twenty hours reaches the next open with room to spare.
ORDER_WINDOW_MINUTES = {
    Market.STOCKS: 20 * 60,
    Market.FOREX: 12 * 60,      # shut only at the weekend
    Market.CRYPTO: 8 * 60,      # 4-hour bars, so it needs two chances to act
    Market.DEGEN: 4 * 60,
}
DEFAULT_ORDER_WINDOW = 4 * 60


def pending_for(symbol: str, now=None) -> NewsVerdict | None:
    """The live instruction for this symbol, if there is one.

    Read by the news_catalyst strategy on each bar. One verdict can only ever
    produce one order: `acted` is set the moment the lane takes it.
    """
    now = now or timezone.now()
    candidates = (NewsVerdict.objects
                  .filter(symbol=symbol, acted=False, tradable=True,
                          score__gte=NewsVerdict.ACT_THRESHOLD,
                          direction__in=('buy', 'short'))
                  .order_by('-score', '-created_at'))
    for v in candidates[:5]:
        window = ORDER_WINDOW_MINUTES.get(v.market, DEFAULT_ORDER_WINDOW)
        if v.created_at >= now - timedelta(minutes=window):
            return v
    return None


def mark_acted(verdict: NewsVerdict, blocked: str = '') -> None:
    verdict.acted = True
    verdict.acted_at = timezone.now()
    if blocked:
        verdict.blocked_reason = blocked[:200]
    verdict.save(update_fields=['acted', 'acted_at', 'blocked_reason'])


# --- scoring the calls ------------------------------------------------------

def score_verdicts(max_items: int = 100) -> dict:
    """Was the call right? Signed for the direction it actually called."""
    from .data.store import load_frame
    now = timezone.now()
    due = list(NewsVerdict.objects.filter(outcome_at__isnull=True, price_at_verdict__isnull=False,
                                          created_at__lte=now - timedelta(hours=24))
               .exclude(symbol='')[:max_items])
    done = 0
    for v in due:
        inst = Instrument.objects.filter(symbol=v.symbol).first()
        if inst is None:
            continue
        after = None
        for tf in ('5Min', '15Min', '1Hour', '4Hour'):
            df = load_frame(inst, tf, start=v.created_at + timedelta(hours=24),
                            end=v.created_at + timedelta(hours=30), limit=1)
            if len(df):
                after = float(df['close'].iloc[0])
                break
        if after is None or not v.price_at_verdict:
            continue
        move = (after / v.price_at_verdict - 1) * 100
        v.outcome_pct = round(move if v.direction == 'buy' else -move, 3)
        v.outcome_at = now
        v.save(update_fields=['outcome_pct', 'outcome_at'])
        done += 1
    return {'scored': done}


def scoreboard(days: int = 30) -> list[dict]:
    """Does a score of 8 actually beat a score of 5? The only question that matters."""
    since = timezone.now() - timedelta(days=days)
    rows = NewsVerdict.objects.filter(created_at__gte=since, outcome_at__isnull=False)
    buckets: dict[int, list] = {}
    for v in rows:
        buckets.setdefault(v.score, []).append(v.outcome_pct)
    out = []
    for score, moves in sorted(buckets.items(), reverse=True):
        hits = sum(1 for m in moves if m > 0)
        out.append({'score': score, 'n': len(moves), 'hit_rate': hits / len(moves) * 100,
                    'avg_move_pct': sum(moves) / len(moves)})
    return out
