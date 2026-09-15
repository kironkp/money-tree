"""Reading the news, hourly, for the stocks and crypto lanes.

Three steps, each cheap and each capped:

  ingest      pull headlines from Alpaca's news feed (Benzinga), which the
              account already pays nothing for, tagged by symbol
  classify    one small-model call per NOVEL headline turns it into a
              structured event: kind, direction, size, confidence, horizon
  score       hours later, record what the price actually did, so an event
              type can be judged instead of believed

What this deliberately does NOT do is place a trade. The research is blunt
about why: every coin Alpaca can trade is already on Coinbase, so the listing
pop is long gone before we could act (across 652 listings since Jan 2025 the
median token sits 82% below its listing price); and the documented edge in
LLM headline sentiment lives in small-cap SHORTS on negative news, which is the
exact opposite of a long-only megacap watchlist. Trading on that would be
paying real spread to harvest someone else's published, decaying result.

So news is used two honest ways: it tells the owner what is happening, and it
lets the risk manager stand aside when a held symbol is in the middle of a
confirmed, market-moving story. Everything is logged with its outcome, so any
event type that turns out to carry edge can earn its way into trading later.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from main_app.models import Instrument, Market, NewsItem

log = logging.getLogger('moneytree.news')

# Caps. The point of a cap is that a bad day costs a known amount.
MAX_INGEST_PER_RUN = 200       # headlines pulled per hourly run
MAX_CLASSIFY_PER_RUN = 40      # model calls per hourly run
# Two providers, and the cheaper one is preferred rather than merely tolerated:
# gpt-4o-mini is $0.15/M input against Haiku's $0.80/M for identical work. The
# fallback also stopped being theoretical on 2026-09-12, when the Anthropic key
# hit a spend cap and three days of headlines went in unclassified while a
# perfectly good OpenAI key sat unused.
CLASSIFY_MODEL_OPENAI = 'gpt-4o-mini'
CLASSIFY_MODEL_ANTHROPIC = 'claude-haiku-4-5-20251001'
LOOKBACK_HOURS = 3             # overlap the hourly cadence so nothing slips between runs

STOPWORDS = {'the', 'a', 'an', 'of', 'to', 'in', 'on', 'for', 'and', 'as', 'at', 'is', 'its',
             'after', 'over', 'with', 'says', 'say', 'new', 'up', 'down', 'more', 'than'}


def _crypto_symbol(tag: str) -> str | None:
    """Alpaca tags crypto news as BTCUSD; the ledger knows it as BTC/USD."""
    m = re.fullmatch(r'([A-Z0-9]{2,10})(USD|USDT)', tag)
    return f'{m.group(1)}/USD' if m else None


def tradable_map() -> dict[str, str]:
    """Every symbol we can actually trade, mapped to its lane."""
    out: dict[str, str] = {}
    for sym, market in Instrument.objects.filter(active=True).values_list('symbol', 'market'):
        out[sym] = market
    return out


def match_symbols(tags: list[str], known: dict[str, str]) -> tuple[list[str], str]:
    """Which tagged symbols we trade, and which lane the story belongs to."""
    hits: list[str] = []
    for tag in tags or []:
        tag = (tag or '').upper()
        for cand in (tag, _crypto_symbol(tag)):
            if cand and cand in known and cand not in hits:
                hits.append(cand)
    markets = {known[s] for s in hits}
    if len(markets) == 1:
        return hits, markets.pop()
    return hits, (Market.STOCKS if any(known[s] == Market.STOCKS for s in hits) else
                  (markets.pop() if markets else ''))


def story_key(headline: str) -> str:
    """A fingerprint for 'the same story from a different outlet'.

    Twelve outlets rewrite one wire story twelve ways; classifying each is
    paying twelve times for one fact. Content words only, sorted, so word order
    and outlet boilerplate do not make two identical stories look different.
    """
    words = sorted({w for w in re.findall(r'[a-z0-9]+', (headline or '').lower())
                    if len(w) > 2 and w not in STOPWORDS})
    return hashlib.sha1(' '.join(words[:12]).encode()).hexdigest()[:16]


# --- 1. ingest --------------------------------------------------------------

def fetch_alpaca_news(since: datetime, limit: int = MAX_INGEST_PER_RUN) -> list[dict]:
    """Headlines from Alpaca's news feed. Free on the account's existing plan."""
    if not settings.ALPACA_ENABLED:
        return []
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest

    client = NewsClient(settings.ALPACA_API_KEY, settings.ALPACA_SECRET_KEY)
    out: list[dict] = []
    token = None
    while len(out) < limit:
        res = client.get_news(NewsRequest(start=since, limit=min(50, limit - len(out)),
                                          include_content=False, page_token=token))
        data = res.data if hasattr(res, 'data') else {}
        items = data.get('news', []) if isinstance(data, dict) else list(res)
        if not items:
            break
        for n in items:
            out.append({
                'external_id': f'alpaca:{getattr(n, "id", "")}',
                'published_at': getattr(n, 'created_at', None),
                'headline': (getattr(n, 'headline', '') or '')[:500],
                'summary': (getattr(n, 'summary', '') or '')[:2000],
                'url': (getattr(n, 'url', '') or '')[:500],
                'symbols': list(getattr(n, 'symbols', []) or []),
                'source': (getattr(n, 'source', '') or 'alpaca')[:40],
            })
        token = getattr(res, 'next_page_token', None) or (data.get('next_page_token') if isinstance(data, dict) else None)
        if not token:
            break
    return out


def ingest(since: datetime | None = None) -> dict:
    """Store new headlines that touch something we trade. Cheap: no model calls."""
    since = since or timezone.now() - timedelta(hours=LOOKBACK_HOURS)
    known = tradable_map()
    raw = fetch_alpaca_news(since)
    seen_ids = set(NewsItem.objects.filter(published_at__gte=since - timedelta(hours=6))
                   .values_list('external_id', flat=True))
    kept = skipped = 0
    for item in raw:
        if not item['external_id'] or item['external_id'] in seen_ids or not item['published_at']:
            continue
        symbols, market = match_symbols(item['symbols'], known)
        if not symbols:
            skipped += 1
            continue                      # a story about nothing we can trade is not our business
        key = story_key(item['headline'])
        twin = NewsItem.objects.filter(
            published_at__gte=item['published_at'] - timedelta(hours=12)
        ).filter(rationale__startswith='').exclude(external_id=item['external_id'])
        prior = next((n for n in twin.only('id', 'headline', 'novel')
                      if story_key(n.headline) == key), None)
        NewsItem.objects.create(
            external_id=item['external_id'], source=item['source'],
            published_at=item['published_at'], headline=item['headline'],
            summary=item['summary'], url=item['url'], symbols=symbols, market=market,
            tradable=True, novel=prior is None, duplicate_of=prior,
        )
        seen_ids.add(item['external_id'])
        kept += 1
    return {'fetched': len(raw), 'stored': kept, 'not_ours': skipped}


# --- 2. classify ------------------------------------------------------------

SYSTEM = """You read market headlines and turn each into one structured event. You are not a trader
and you never give trading advice; you describe what the headline says, factually.

kind: listing (an asset added to an exchange/broker), earnings, guidance, mna, regulatory, macro,
product, partnership, legal, security (hack/breach), personnel, analyst (rating change), rumour, other.

direction: the plausible effect on the named asset's price — bullish, bearish, or neutral. Most
headlines are neutral. Commentary, opinion pieces, "here's why X could" and listicles are neutral.

magnitude 1-5: 1 routine coverage, 3 notable company news, 5 the kind of event that repriced the
asset (major M&A, a halted drug, an exchange hack, a surprise rate decision).

confidence 1-5: 1 rumour or speculation, 3 reported by a credible outlet, 5 confirmed by the company,
a regulator, or an exchange itself.

horizon: minutes, hours, days, or weeks — how long the effect plausibly lasts.

Be sceptical. Headlines are written to be clicked. A question mark, "could", "may", "analyst says",
or a prediction is confidence 1-2 and usually neutral. Only mark magnitude 4-5 for something that
has actually happened, not something someone expects to happen."""

SCHEMA = {
    'type': 'object',
    'properties': {
        'kind': {'type': 'string', 'enum': list(NewsItem.KINDS)},
        'direction': {'type': 'string', 'enum': list(NewsItem.DIRECTIONS)},
        # enum, NOT minimum/maximum: the API rejects numeric bounds with a 400
        # ("properties maximum, minimum are not supported"). Same family of
        # schema trap that silently killed the coach for five days.
        'magnitude': {'type': 'integer', 'enum': [1, 2, 3, 4, 5]},
        'confidence': {'type': 'integer', 'enum': [1, 2, 3, 4, 5]},
        'horizon': {'type': 'string', 'enum': ['minutes', 'hours', 'days', 'weeks']},
        'rationale': {'type': 'string', 'description': 'one short clause, plain English'},
    },
    'required': ['kind', 'direction', 'magnitude', 'confidence', 'horizon', 'rationale'],
    'additionalProperties': False,
}


def _classify_openai(item) -> tuple[dict, object, str]:
    from openai import OpenAI
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model=CLASSIFY_MODEL_OPENAI, max_tokens=400,
        messages=[{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': _user_text(item)}],
        response_format={'type': 'json_schema',
                         'json_schema': {'name': 'event', 'strict': True, 'schema': SCHEMA}},
    )
    return json.loads(resp.choices[0].message.content), resp.usage, CLASSIFY_MODEL_OPENAI


def _classify_anthropic(item) -> tuple[dict, object, str]:
    from anthropic import Anthropic
    client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=CLASSIFY_MODEL_ANTHROPIC, max_tokens=400, system=SYSTEM,
        messages=[{'role': 'user', 'content': _user_text(item)}],
        output_config={'format': {'type': 'json_schema', 'schema': SCHEMA}},
    )
    text = ''.join(b.text for b in resp.content if getattr(b, 'type', '') == 'text')
    return json.loads(text), resp.usage, CLASSIFY_MODEL_ANTHROPIC


def _user_text(item) -> str:
    return (f'Headline: {item.headline}\n'
            f'Summary: {(item.summary or "")[:600]}\n'
            f'Tagged symbols: {", ".join(item.symbols)}')


def classify(limit: int = MAX_CLASSIFY_PER_RUN) -> dict:
    """One model call per novel unclassified headline, newest first.

    Whichever provider is configured and working does the job; a provider that
    starts refusing is abandoned for the rest of the run rather than retried
    forty times, which is what turned one spend cap into forty identical 400s
    in a single hourly run.
    """
    from .spend import record_anthropic, record_openai

    pending = list(NewsItem.objects.filter(classified_at__isnull=True, novel=True)
                   .order_by('-published_at')[:limit])
    if not pending:
        return {'classified': 0, 'skipped': 0}

    providers = []
    if settings.OPENAI_API_KEY:
        providers.append(('openai', _classify_openai, record_openai))
    if settings.ANTHROPIC_API_KEY:
        providers.append(('anthropic', _classify_anthropic, record_anthropic))
    if not providers:
        log.info('news: no model key configured — headlines stored unclassified')
        return {'classified': 0, 'skipped': len(pending), 'reason': 'no api key'}

    done = failed = 0
    used = ''
    for item in pending:
        data = usage = model = None
        for name, call, record in list(providers):
            try:
                data, usage, model = call(item)
                used = name
                break
            except Exception as exc:
                log.warning('news: %s classify failed for %s: %r', name, item.pk, exc)
                # A refusal is about the account, not this headline: stop asking.
                if any(w in str(exc).lower() for w in ('usage limit', 'quota', 'billing',
                                                       'insufficient', 'rate limit')):
                    providers = [p for p in providers if p[0] != name]
                    log.warning('news: dropping %s for the rest of this run', name)
        if data is None:
            failed += 1
            if not providers:
                break
            continue
        recorder = record_openai if used == 'openai' else record_anthropic
        recorder(model, usage, purpose='news', project='moneytree')
        item.kind = str(data.get('kind', 'other'))[:16]
        item.direction = str(data.get('direction', 'neutral'))[:8]
        item.magnitude = max(1, min(5, int(data.get('magnitude', 1))))
        item.confidence = max(1, min(5, int(data.get('confidence', 1))))
        item.horizon = str(data.get('horizon', 'days'))[:12]
        item.rationale = str(data.get('rationale', ''))[:400]
        item.model = model
        item.classified_at = timezone.now()
        item.price_at_news = _prices_for(item.symbols)
        item.save(update_fields=['kind', 'direction', 'magnitude', 'confidence', 'horizon',
                                 'rationale', 'model', 'classified_at', 'price_at_news'])
        done += 1
    return {'classified': done, 'failed': failed, 'provider': used}


def _prices_for(symbols: list[str]) -> dict:
    """The last stored close per symbol, so the outcome can be measured later."""
    from .data.store import load_frame
    out = {}
    for sym in symbols[:6]:
        inst = Instrument.objects.filter(symbol=sym).first()
        if inst is None:
            continue
        for tf in ('5Min', '15Min', '1Hour', '4Hour'):
            df = load_frame(inst, tf, limit=1)
            if len(df):
                out[sym] = float(df['close'].iloc[-1])
                break
    return out


# --- 3. score ---------------------------------------------------------------

def score_outcomes(max_items: int = 60) -> dict:
    """Fill in what the price did after a story. This is what makes it evidence."""
    from .data.store import load_frame
    now = timezone.now()
    due = list(NewsItem.objects.filter(classified_at__isnull=False, outcome_at__isnull=True,
                                       published_at__lte=now - timedelta(hours=24))
               .exclude(price_at_news={})[:max_items])
    scored = 0
    for item in due:
        result = {}
        for sym, before in (item.price_at_news or {}).items():
            inst = Instrument.objects.filter(symbol=sym).first()
            if inst is None or not before:
                continue
            per = {}
            for label, delta in (('60m', timedelta(minutes=60)), ('1d', timedelta(days=1))):
                for tf in ('5Min', '15Min', '1Hour', '4Hour'):
                    df = load_frame(inst, tf, start=item.published_at + delta,
                                    end=item.published_at + delta + timedelta(hours=6), limit=1)
                    if len(df):
                        per[label] = round((float(df['close'].iloc[0]) / before - 1) * 100, 3)
                        break
            if per:
                result[sym] = per
        item.outcome = result
        item.outcome_at = now
        item.save(update_fields=['outcome', 'outcome_at'])
        scored += 1
    return {'scored': scored}


# --- reading it back --------------------------------------------------------

@dataclass
class Digest:
    market: str
    significant: list
    counts: dict
    total: int


def digest(market: str, hours: int = 24) -> Digest:
    """What the lane's news said, for the report and the dashboard."""
    since = timezone.now() - timedelta(hours=hours)
    rows = list(NewsItem.objects.filter(market=market, published_at__gte=since, novel=True))
    counts: dict[str, int] = {}
    for r in rows:
        if r.direction:
            counts[r.direction] = counts.get(r.direction, 0) + 1
    significant = sorted([r for r in rows if r.is_significant],
                         key=lambda r: (-r.magnitude, -r.confidence, r.published_at))[:5]
    return Digest(market=market, significant=significant, counts=counts, total=len(rows))


def event_scoreboard(days: int = 30) -> list[dict]:
    """Has any event type actually predicted anything? The honest answer, by kind.

    This is the table that decides whether news ever gets to place a trade.
    """
    since = timezone.now() - timedelta(days=days)
    rows = NewsItem.objects.filter(published_at__gte=since, outcome_at__isnull=False,
                                   direction__in=('bullish', 'bearish'))
    by: dict[tuple, list] = {}
    for r in rows:
        for sym, per in (r.outcome or {}).items():
            move = per.get('1d')
            if move is None:
                continue
            signed = move if r.direction == 'bullish' else -move
            by.setdefault((r.kind, r.direction), []).append(signed)
    out = []
    for (kind, direction), moves in by.items():
        if not moves:
            continue
        hits = sum(1 for m in moves if m > 0)
        out.append({'kind': kind, 'direction': direction, 'n': len(moves),
                    'hit_rate': hits / len(moves) * 100,
                    'avg_move_pct': sum(moves) / len(moves)})
    return sorted(out, key=lambda r: -r['n'])


# --- the one way news touches trading -------------------------------------

# A confirmed, market-moving story is a reason to stand aside, not a reason to
# buy. Entering into a repricing event means paying spread into the one moment
# the book is thinnest and the next print is least predictable. Standing aside
# costs nothing when the story turns out to be noise, which is most of the time.
NEWS_HALT_MINUTES = 45


def entry_block(symbol: str, now: datetime | None = None) -> str:
    """A reason not to open a position in `symbol` right now, or ''."""
    now = now or timezone.now()
    # Filtered in Python, not by a JSON containment lookup: SQLite has no such
    # lookup, and the window is 45 minutes so the candidate set is tiny anyway.
    candidates = NewsItem.objects.filter(
        published_at__gte=now - timedelta(minutes=NEWS_HALT_MINUTES),
        novel=True, classified_at__isnull=False, magnitude__gte=4, confidence__gte=4,
    ).exclude(direction='neutral').order_by('-magnitude', '-confidence')
    recent = next((n for n in candidates if symbol in (n.symbols or [])), None)
    if recent is None:
        return ''
    age = int((now - recent.published_at).total_seconds() // 60)
    return (f'{recent.kind} news {age} min ago ({recent.direction}): '
            f'{recent.headline[:90]} — standing aside while it reprices')
