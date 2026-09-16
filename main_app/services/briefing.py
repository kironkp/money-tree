"""One searching prompt per lane, on a schedule: "what is going on out there?"

The headline feed in news.py answers "what happened to a symbol I hold". This
answers the wider question the feed structurally cannot: the stories that move a
whole lane are usually tagged to no ticker at all — a rate decision, a
regulation, an exchange going down, a war. One prompt, one lane, a few web
searches, five lines back.

Measured 2026-09-12 on a real call: 8,174 input and 528 output tokens, six
seconds, $0.0015 of tokens. The search tool bills per SEARCH ACTION on top of
tokens and the usage object never mentions it, so the searches are counted out
of the response itself and priced into ApiUsage. That fee is most of what a
briefing costs: for a long time it was written onto the Briefing row alone, and
the ledger held 12% of the real number.

Deliberately NOT a trading signal. It is the wide view for the owner and for the
evening report. Nothing here reaches the risk manager — only the confirmed,
symbol-tagged events in news.py can stand a trade aside, because those can be
scored against price afterwards and a narrative cannot.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from main_app.models import Briefing, Instrument, Market

log = logging.getLogger('moneytree.briefing')

MODEL = 'gpt-4o-mini'
MAX_PER_DAY = 120          # a hard ceiling, so a scheduling mistake cannot run away


def _tier(resp) -> str:
    """The tier the API says it served, which is the one that gets billed."""
    return str(getattr(resp, 'service_tier', '') or '')

LANE_PROMPT = {
    Market.STOCKS: (
        'What are the most important news stories affecting US STOCKS right now? '
        'A trader holding large-cap US tech (Apple, Nvidia, Microsoft, Amazon, Meta, Tesla, AMD) '
        'plus SPY and QQQ needs: market-moving macro (Fed, inflation prints, jobs), regulation, '
        'earnings or guidance from those names, and anything driving the index today.'),
    Market.CRYPTO: (
        'What are the most important news stories affecting CRYPTO markets right now? '
        'A trader holding Bitcoin or Ether needs: regulation and legislation, ETF flows, '
        'exchange incidents or hacks, large liquidations, and the macro that moves crypto.'),
    Market.DEGEN: (
        'What are the most important news stories affecting ALTCOINS right now? '
        'A trader holding Solana, XRP, Dogecoin, Cardano, Avalanche, Chainlink, and memecoins '
        '(PEPE, SHIB, BONK, WIF, TRUMP) needs: exchange listings and delistings, chain outages, '
        'unlocks and large token transfers, and which narratives money is rotating into.'),
    Market.FOREX: (
        'What are the most important news stories affecting FOREX right now? '
        'A trader in EUR/USD, GBP/USD, AUD/USD and NZD/USD needs: central bank decisions and '
        'speeches (Fed, ECB, BoE, RBA, RBNZ), inflation and jobs releases, and political events '
        'moving the dollar.'),
}

FORMAT = (
    '\n\nAnswer as JSON only, no prose around it:\n'
    '{"headline": "one line, under 140 characters, the single thing that matters most",\n'
    ' "quiet": true|false,   // true if nothing significant is happening\n'
    ' "items": [{"text": "one line, what happened and why it matters", "url": "source url"}]}\n'
    'At most 5 items, newest and most important first. Prefer the last 24 hours. '
    'If nothing significant has happened, set quiet to true, return an empty items list, and say so '
    'in the headline. Do not speculate, do not give trading advice, do not pad the list.'
)


def _json_from(text: str) -> dict:
    """Models wrap JSON in prose or fences more often than they should."""
    text = (text or '').strip()
    fence = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find('{'), text.rfind('}')
    if start == -1 or end <= start:
        raise ValueError('no JSON object in the reply')
    return json.loads(text[start:end + 1])


def brief_lane(market: str) -> Briefing:
    """One searching call for one lane. Always returns a row, even on failure."""
    from openai import OpenAI

    from .spend import SEARCH_CALL_USD, cost_of, record, search_calls_in

    prompt = LANE_PROMPT.get(market)
    if prompt is None:
        raise ValueError(f'no prompt for lane {market!r}')
    if not settings.OPENAI_API_KEY:
        return Briefing.objects.create(market=market, error='no OPENAI_API_KEY configured')

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    try:
        resp = client.responses.create(model=MODEL, input=prompt + FORMAT,
                                       tools=[{'type': 'web_search'}], max_output_tokens=1200)
    except Exception as exc:
        log.warning('briefing %s failed: %r', market, exc)
        return Briefing.objects.create(market=market, model=MODEL, error=str(exc)[:300])

    usage = getattr(resp, 'usage', None)
    tokens_in = getattr(usage, 'input_tokens', 0) or 0
    tokens_out = getattr(usage, 'output_tokens', 0) or 0
    text = resp.output_text or ''
    try:
        data = _json_from(text)
    except Exception:
        # Keep the prose rather than losing a call we already paid for.
        data = {'headline': text.strip().split('\n')[0][:280], 'items': [], 'quiet': False}

    items = [{'text': str(i.get('text', ''))[:400], 'url': str(i.get('url', ''))[:400]}
             for i in (data.get('items') or [])][:5]
    row = Briefing.objects.create(
        market=market, model=MODEL,
        headline=str(data.get('headline', ''))[:300],
        body=text[:8000], items=items, quiet=bool(data.get('quiet')) or not items,
    )
    # The search fee goes into ApiUsage, not just onto this row. It used to live
    # only here, so the ledger held $0.16 of a real $1.33 across 117 briefings —
    # 12% of the true cost — and every budget conversation was fiction.
    searches = search_calls_in(resp)
    cost = cost_of(MODEL, tokens_in, tokens_out, service_tier=_tier(resp), search_calls=searches)
    record(MODEL, provider='openai', project='moneytree', purpose='briefing',
           input_tokens=tokens_in, output_tokens=tokens_out, cost_usd=cost,
           note=f'{market} lane briefing; {searches} web search(es) at ${SEARCH_CALL_USD:.2f}')
    row.cost_usd = cost
    row.save(update_fields=['cost_usd'])
    return row


def brief_all(markets=None) -> list[Briefing]:
    """Brief every lane that has something to trade."""
    today = timezone.now() - timedelta(hours=24)
    if Briefing.objects.filter(ts__gte=today).count() >= MAX_PER_DAY:
        log.warning('briefing: daily ceiling of %d reached, skipping', MAX_PER_DAY)
        return []
    if markets is None:
        markets = [m for m in Market.values
                   if Instrument.objects.filter(market=m, active=True, in_watchlist=True).exists()]
    return [brief_lane(m) for m in markets]


def latest(market: str, within_hours: int = 12) -> Briefing | None:
    return (Briefing.objects.filter(market=market, error='', ts__gte=timezone.now() - timedelta(hours=within_hours))
            .order_by('-ts').first())
