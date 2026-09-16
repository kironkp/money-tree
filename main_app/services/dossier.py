"""Per-company research, in shadow.

The News Agent scores headlines. This scores a COMPANY: the stories clustered
together, the filed and vendor numbers underneath them, where the price already
sits, and a two-sided case. That is the unit of analysis the owner asked for when
he compared our 4/10 on "Gene Munster Says iPhone 18 Pre-Order Wait Times Are
Climbing" against a sourced research note on Apple.

Named `dossier` and not `research`: that word already means walk-forward
parameter search in this codebase, across `views/research.py`, `manage.py
auto_research` and a nightly launchd job.

What this is careful about
--------------------------
STRICT JSON GUARANTEES STRUCTURE, NOT TRUTH. A schema will happily accept a
fabricated number in the right shape, and FinanceBench found GPT-4-Turbo with
retrieval wrong or refusing on 81% of financial questions. So every number the
model introduces must arrive with a verbatim quote and a URL, and anything
without both is dropped before it is written. The numbers that matter most —
revenue, EPS, the multiple, where price sits in its range — are supplied by us
from EDGAR, yfinance and our own bars, and the model is asked to reason about
them rather than to recall them.

IT IS ASKED FOR PROBABILITIES, NOT FOR A MAGNITUDE. `expected_move_bps` is never
a trading input: letting the model set the target makes the cost gate evaluate
the model's own claim instead of the market's volatility. Instead it forecasts
the barrier race the strategy actually runs — target before stop, stop before
target, or neither inside the hold — which sums to one and can be scored. The
1-10 scores are human-facing summaries and are never given a Brier score,
because a rating is not a probability.

IT RUNS IN SHADOW. Dossiers are written, scored, displayed and graded. They emit
no instruction and touch no order until a preregistered gate says the research
arm beats the headline arm on a paired daily comparison.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from main_app.models import AgentConfig, Instrument, Market, NewsItem, SymbolDossier

log = logging.getLogger('moneytree.dossier')

MODEL = 'gpt-5.6-terra'
SERVICE_TIER = 'flex'
MAX_SEARCHES = 3
MAX_OUTPUT_TOKENS = 25_000

# The seven names with real fundamentals. SPY and QQQ are regime context and get
# no company dossier: Yahoo returns a P/E for them and nothing else, and an index
# has no earnings to read. Forex and the altcoins have no fundamentals at all.
RESEARCHED = ('AAPL', 'NVDA', 'TSLA', 'AMD', 'MSFT', 'AMZN', 'META')

# A hard ceiling a web edit cannot raise. AgentConfig.research_budget_usd_per_day
# is the owner's knob and lives below this.
HARD_CEILING_USD_PER_DAY = 0.40
# Measured on the verification call 2026-09-17: a search costs its $0.01 fee PLUS
# roughly 10k input tokens of retrieved content, so ~$0.02 all-in on terra/flex.
# The reservation is deliberately pessimistic — a reservation that is too small
# lets the day's spend drift past the ceiling before anyone notices.
ESTIMATED_USD_PER_DOSSIER = 0.09

CATALYST_MAX_AGE_HOURS = 24
STORIES_PER_DOSSIER = 12

SYSTEM = """You are the research analyst for a small automated trading desk. You are given one
company, everything it has been in the news for lately, and a block of hard numbers that have
already been fetched for you from SEC filings, a market-data vendor and the desk's own price bars.

Your job is to produce a dossier a human can read and a machine can act on.

THE THREE TIERS. Separate what you know into three kinds of thing, because only one of them can
move a position on a desk that closes every trade within 240 minutes:
  CATALYST  a dated, firm-specific event that happened or will happen within about a day. An
            earnings release, a production cut, a regulatory decision, a product launch, a large
            order. It must have a timestamp and a source. This is the only tier that may trade.
  CONTEXT   guidance, estimate revisions, positioning, valuation relative to the recent range.
            Lives over days and weeks. May shrink a position or veto it. It may NEVER enlarge one.
  THESIS    the multi-month picture: growth, margins, product cycle, structural risk. Display only.

NUMBERS. Do not recall figures from memory and do not calculate. Every number you state must either
come from the SUPPLIED FACTS block, in which case cite its label, or come from a page you searched,
in which case you must return it in `evidence` with a verbatim quote of the sentence containing it
and the URL you read it on. A number without a quote and a URL will be discarded before anyone sees
it, and a dossier that discards half its numbers is worse than one that made fewer claims.

THE FORECAST. The desk's trade, if it took one, is: enter at the last close, stop 1.5 ATR away,
target 3 ATR away, and close at 240 minutes whatever has happened. Forecast that race:
  p_target_first  the target is touched before the stop
  p_stop_first    the stop is touched before the target
  p_timeout       neither, and it closes on time
These three must sum to 1. You are given the MEASURED base rate for this exact geometry on this
exact symbol — what the tape does with no news at all. Start there and move only as far as your
evidence justifies. Departing from the base rate is a claim, and a large departure is a large claim.
Separately, give p_positive_net: the chance the trade ends positive AFTER the round-trip cost you
are told. It is not a function of the other three.

SCORES. Give a 1-10 rating for each tier. These are summaries for a human reader, not probabilities.
Be harsh: 5 means "I would put money on this", not "this is interesting". Most news is in the price
before it is published.

DIRECTION. buy, short, or none. Shorts are entirely your decision. Say none when the honest answer is
that there is nothing to do today, which will usually be the case.

SIZE. size_multiplier between 0.25 and 1.0. It may only shrink a position. If CONTEXT or THESIS
argues against the catalyst — a stretched multiple, estimates being cut, the stock already having
run — say so and shrink. There is no way to ask for a bigger position and you should not try.

VETO. If the desk should not open a position in this name at all today, say why in veto_reason.

TRIGGERS. What would change your mind, expressed so a machine can watch for it: a condition, a
direction, and where possible a metric and threshold. "Moves up if delivery times extend on demand
rather than supply" is useful. "Moves up if things go well" is not.

Write the narrative for a person who will read it once, quickly, and wants to know what is going on
and what you would do. Plain English, concrete, no hedging filler."""


def _schema() -> dict:
    score = {'type': 'integer', 'enum': list(range(1, 11))}
    return {
        'type': 'object', 'additionalProperties': False,
        'required': ['narrative', 'catalyst', 'bull', 'bear', 'evidence', 'scores', 'direction',
                     'forecast', 'size_multiplier', 'veto_reason', 'triggers'],
        'properties': {
            'narrative': {'type': 'string'},
            'catalyst': {
                'type': 'object', 'additionalProperties': False,
                'required': ['present', 'headline', 'url', 'happened_at', 'why'],
                'properties': {
                    'present': {'type': 'boolean'},
                    'headline': {'type': 'string'},
                    'url': {'type': 'string'},
                    'happened_at': {'type': 'string',
                                    'description': 'ISO-8601 UTC, or empty if none'},
                    'why': {'type': 'string'},
                },
            },
            'bull': _case_schema(),
            'bear': _case_schema(),
            'evidence': {
                'type': 'array',
                'description': 'numbers you found by searching; each needs a quote and a URL',
                'items': {
                    'type': 'object', 'additionalProperties': False,
                    'required': ['label', 'value', 'unit', 'period', 'quote', 'url'],
                    'properties': {
                        'label': {'type': 'string'},
                        'value': {'type': ['number', 'null']},
                        'unit': {'type': 'string'},
                        'period': {'type': 'string'},
                        'quote': {'type': 'string',
                                  'description': 'the sentence containing the number, verbatim'},
                        'url': {'type': 'string'},
                    },
                },
            },
            'scores': {
                'type': 'object', 'additionalProperties': False,
                'required': ['catalyst', 'context', 'thesis'],
                'properties': {'catalyst': score, 'context': score, 'thesis': score},
            },
            'direction': {'type': 'string', 'enum': ['buy', 'short', 'none']},
            'forecast': {
                'type': 'object', 'additionalProperties': False,
                'required': ['p_target_first', 'p_stop_first', 'p_timeout', 'p_positive_net',
                             'p_low', 'p_high'],
                'properties': {
                    'p_target_first': {'type': 'number'},
                    'p_stop_first': {'type': 'number'},
                    'p_timeout': {'type': 'number'},
                    'p_positive_net': {'type': 'number'},
                    'p_low': {'type': 'number'},
                    'p_high': {'type': 'number'},
                },
            },
            'size_multiplier': {'type': 'number'},
            'veto_reason': {'type': 'string'},
            'triggers': {
                'type': 'array',
                'items': {
                    'type': 'object', 'additionalProperties': False,
                    'required': ['condition', 'direction', 'metric', 'comparator', 'threshold',
                                 'window_hours'],
                    'properties': {
                        'condition': {'type': 'string'},
                        'direction': {'type': 'string', 'enum': ['bullish', 'bearish']},
                        'metric': {'type': 'string'},
                        'comparator': {'type': 'string', 'enum': ['gt', 'lt', 'eq', 'any']},
                        'threshold': {'type': ['number', 'null']},
                        'window_hours': {'type': ['number', 'null']},
                    },
                },
            },
        },
    }


def _case_schema() -> dict:
    return {
        'type': 'array',
        'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['claim', 'facts'],
            'properties': {
                'claim': {'type': 'string'},
                'facts': {'type': 'array', 'items': {'type': 'string'},
                          'description': 'labels from the supplied facts or your evidence'},
            },
        },
    }


# --- what earns a dossier ---------------------------------------------------

def candidates(now=None, limit: int = 4) -> list[str]:
    """Which names to research, ranked, under the cap.

    Deliberately a RANKING and not a threshold. "Three or more novel stories in
    24 hours" sounds like a trigger but selects eleven of the fifteen researchable
    names on an ordinary day — a daily sweep of the whole equity lane wearing a
    trigger's clothes. Ranking by story count and taking the top few spends the
    budget on the busiest names and costs the same on a quiet day as on a loud one.

    Free: pure Python over rows we already hold, no model call.
    """
    now = now or timezone.now()
    since = now - timedelta(hours=24)
    counts: dict[str, int] = {}
    rows = NewsItem.objects.filter(published_at__gte=since, novel=True).values_list('symbols', flat=True)
    for symbols in rows:
        for sym in (symbols or []):
            if sym in RESEARCHED:
                counts[sym] = counts.get(sym, 0) + 1
    ranked = sorted(counts, key=lambda s: (-counts[s], s))
    return ranked[:limit]


def _stories(symbol: str, now) -> list[NewsItem]:
    """The cluster this dossier is about. Novel only — a rewrite is not a second
    event, and twelve outlets on one story should not look like twelve stories."""
    since = now - timedelta(hours=36)
    rows = NewsItem.objects.filter(published_at__gte=since, novel=True).order_by('-published_at')[:200]
    return [n for n in rows if symbol in (n.symbols or [])][:STORIES_PER_DOSSIER]


def evidence_for(symbol: str, now=None) -> dict:
    """Everything we can establish without asking a model anything."""
    from .research.context import barrier_base_rate, price_context, regime
    from .research.edgar import filed_facts
    from .research.facts import FactSheet
    from .research.fundamentals import fundamentals

    now = now or timezone.now()
    sheet = FactSheet(symbol=symbol)
    price_context(symbol, sheet)
    fundamentals(symbol, sheet)
    filed = filed_facts(symbol)
    sheet.facts.extend(filed.facts)
    sheet.missing.extend(filed.missing)
    return {
        'sheet': sheet,
        'regime': regime(),
        'base_rate': barrier_base_rate(symbol),
        'stories': _stories(symbol, now),
    }


def _geometry(sheet, market: str) -> dict:
    """The trade the forecast is about, in the units the desk actually uses."""
    from .risk import RiskConfig
    inst = Instrument.objects.filter(symbol=sheet.symbol).first()
    asset_class = inst.asset_class if inst else 'stock'
    rc = RiskConfig.from_model(AgentConfig.get(), market or Market.STOCKS)
    price, atr = sheet.value('last_close'), sheet.value('atr')
    cost_pct = rc.round_trip_cost_pct(asset_class)
    return {
        'price': price, 'atr': atr, 'hold_minutes': rc.max_hold_minutes,
        'stop_atr': 1.5, 'target_atr': 3.0, 'round_trip_cost_pct': cost_pct,
        'cost_in_atr': (cost_pct / 100 * price / atr) if (price and atr) else None,
    }


def _prompt(symbol: str, ev: dict, geom: dict, now) -> str:
    sheet, base = ev['sheet'], ev['base_rate']
    parts = [f'COMPANY: {symbol}', f'TIME NOW: {now:%Y-%m-%d %H:%M}Z', '']

    parts.append('SUPPLIED FACTS (already fetched and sourced — cite these by label, do not restate '
                 'them as your own findings):')
    for f in sheet.facts:
        bits = [f'  {f.label} = {f.value:,.4g} {f.unit}'.rstrip()]
        if f.period:
            bits.append(f'period {f.period}')
        bits.append(f'[{f.tier}: {f.source}]')
        if f.accession:
            bits.append(f'accession {f.accession} {f.form} tag {f.xbrl_tag}')
        if f.note:
            bits.append(f'— {f.note}')
        parts.append(' '.join(bits))
    if sheet.missing:
        parts.append(f'  NOT AVAILABLE: {", ".join(sorted(set(sheet.missing)))}. Do not invent these.')
    for err in sheet.errors:
        parts.append(f'  NOTE: {err}')

    parts += ['', 'MARKET REGIME (context only, never a company view):']
    for f in ev['regime'].facts:
        parts.append(f'  {f.label} = {f.value:,.2f} {f.unit}')

    parts += ['', 'THE TRADE YOUR FORECAST IS ABOUT:']
    g = geom
    if g['price'] and g['atr']:
        parts.append(f'  enter near {g["price"]:,.2f}; 1 ATR = {g["atr"]:,.4g} '
                     f'({g["atr"] / g["price"] * 100:.2f}% of price)')
        parts.append(f'  stop {g["stop_atr"]} ATR away, target {g["target_atr"]} ATR away, '
                     f'closed after {g["hold_minutes"]} minutes whatever happens')
        parts.append(f'  round-trip cost {g["round_trip_cost_pct"]:.3f}% of notional '
                     f'= {g["cost_in_atr"]:.2f} ATR, which comes straight off any win')
    if base:
        parts.append(f'  MEASURED BASE RATE on {symbol} for this exact geometry, {base["n"]} samples '
                     f'of {base["timeframe"]} bars: target first {base["p_target_first"]:.1%}, '
                     f'stop first {base["p_stop_first"]:.1%}, neither {base["p_timeout"]:.1%}. '
                     f'This is what the tape does with no news at all.')
    else:
        parts.append('  BASE RATE UNAVAILABLE: not enough bars held. Say so and stay near 1/3.')

    parts += ['', f'STORIES IN THE LAST 36 HOURS ({len(ev["stories"])}):']
    for n in ev['stories']:
        parts.append(f'  [{n.pk}] ({n.first_public:%b %-d %H:%M}Z, {int(n.age_minutes)} min old, '
                     f'{n.kind or "?"}, magnitude {n.magnitude}, confidence {n.confidence}) '
                     f'{n.headline}')
        if n.url:
            parts.append(f'        {n.url}')
        body = (n.content or n.summary or '').strip()
        if body:
            parts.append(f'        {" ".join(body.split())[:900]}')
    if not ev['stories']:
        parts.append('  NONE. Say so, score low, and do not manufacture a catalyst.')

    parts += ['', 'You may run up to %d web searches. Spend them on what the numbers above cannot '
              'tell you: whether a dated catalyst is real and confirmed, and what the market has '
              'already done about it.' % MAX_SEARCHES]
    return '\n'.join(parts)


# --- validation: structure is not truth -------------------------------------

def validate_evidence(raw: list, supplied_labels: set) -> tuple[list, list]:
    """Keep only numbers a human could check. Return (kept, rejected_reasons).

    A quote without a URL is an assertion. A URL without a quote is a gesture at
    a page. A number with neither is exactly the fabrication the whole grounding
    layer exists to prevent, and strict JSON will accept all three happily.
    """
    kept, rejected = [], []
    for item in (raw or []):
        label = str(item.get('label', '')).strip()[:80]
        quote = str(item.get('quote', '')).strip()
        url = str(item.get('url', '')).strip()
        value = item.get('value')
        if not label:
            continue
        if label in supplied_labels:
            rejected.append(f'{label}: restates a supplied fact')
            continue
        if value is None:
            rejected.append(f'{label}: no value')
            continue
        if not url.startswith(('http://', 'https://')):
            rejected.append(f'{label}: no usable source URL')
            continue
        if len(quote) < 20:
            rejected.append(f'{label}: no verbatim quote to check the number against')
            continue
        kept.append({
            'label': label, 'value': float(value), 'unit': str(item.get('unit', ''))[:24],
            'period': str(item.get('period', ''))[:40], 'quote': quote[:400], 'url': url[:500],
            'tier': 'searched', 'source': 'web_search',
        })
    return kept, rejected


def validate_forecast(f: dict) -> tuple[dict, str]:
    """The three barrier outcomes must be a distribution. Anything else is noise."""
    def num(key, lo=0.0, hi=1.0):
        try:
            v = float(f.get(key))
        except (TypeError, ValueError):
            return None
        return v if lo <= v <= hi else None

    out = {k: num(k) for k in ('p_target_first', 'p_stop_first', 'p_timeout',
                               'p_positive_net', 'p_low', 'p_high')}
    parts = [out['p_target_first'], out['p_stop_first'], out['p_timeout']]
    if any(p is None for p in parts):
        return out, 'the barrier forecast is missing or out of range'
    total = sum(parts)
    if abs(total - 1.0) > 0.02:
        return out, f'the barrier forecast sums to {total:.3f}, not 1'
    # Renormalise the small residual so downstream arithmetic is exact.
    for key in ('p_target_first', 'p_stop_first', 'p_timeout'):
        out[key] = out[key] / total
    return out, ''


def _iso(value):
    from datetime import datetime
    text = str(value or '').strip().replace('Z', '+00:00')
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        from datetime import UTC
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


# --- the budget -------------------------------------------------------------

def budget_left(cfg=None) -> Decimal:
    """What may still be spent on dossiers today. Raises rather than guessing."""
    from .spend import spent_today
    cfg = cfg or AgentConfig.get()
    allowed = min(Decimal(str(cfg.research_budget_usd_per_day)),
                  Decimal(str(HARD_CEILING_USD_PER_DAY)))
    return allowed - spent_today('dossier')


# --- building one -----------------------------------------------------------

def build(symbol: str, now=None, model: str = MODEL, tier: str = SERVICE_TIER) -> SymbolDossier:
    """One company, researched. Always returns a row, even when it fails.

    Fails CLOSED in every direction that matters: an unreadable budget, a tier
    the API could not serve, a forecast that is not a distribution. None of those
    produce a quiet fallback — a dossier that cannot be trusted is recorded as
    one that failed, because the alternative is a plausible row nobody can tell
    apart from a good one.
    """
    from openai import OpenAI

    from .spend import BudgetError, abandon, cost_of, record, reserve, search_calls_in, settle

    now = now or timezone.now()
    started = time.time()
    inst = Instrument.objects.filter(symbol=symbol).first()
    d = SymbolDossier(symbol=symbol, market=(inst.market if inst else Market.STOCKS),
                      as_of=now, research_started_at=now, model=model, arm='shadow')

    if symbol not in RESEARCHED:
        d.error = f'{symbol} is not a researched name'
        d.save()
        return d
    if not settings.OPENAI_API_KEY:
        d.error = 'no OPENAI_API_KEY configured'
        d.save()
        return d
    try:
        left = budget_left()
    except BudgetError as exc:
        # The ledger could not answer. Treat silence as no headroom: a budget
        # check that reads a dropped row as $0 spent is worse than no check.
        d.error = f'budget unreadable, refusing to spend: {exc}'
        d.save()
        return d
    if left < Decimal(str(ESTIMATED_USD_PER_DOSSIER)):
        d.error = f'daily research budget used up (${left:.3f} left)'
        d.save()
        return d

    ev = evidence_for(symbol, now)
    geom = _geometry(ev['sheet'], d.market)
    base = ev['base_rate']
    d.base_rate = base['p_target_first'] if base else None
    d.stories = [{'news_id': n.pk, 'headline': n.headline, 'url': n.url,
                  'first_public_at': n.first_public.isoformat(),
                  'age_minutes': round(n.age_minutes, 1),
                  'weight': 'primary' if n.magnitude >= 3 else 'supporting'}
                 for n in ev['stories']]
    d.thin_evidence = len(ev['stories']) < 3
    supplied = [f.as_dict() for f in ev['sheet'].facts]
    supplied_labels = {f['label'] for f in supplied}

    key = f'dossier:{symbol}:{now:%Y%m%d%H%M}'
    reserve(key, model=model, provider='openai', purpose='dossier', service_tier=tier,
            estimated_usd=ESTIMATED_USD_PER_DOSSIER, note=f'{symbol} dossier')

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    try:
        resp = client.responses.create(
            model=model, service_tier=tier,
            input=[{'role': 'developer', 'content': SYSTEM},
                   {'role': 'user', 'content': _prompt(symbol, ev, geom, now)}],
            tools=[{'type': 'web_search'}], tool_choice='required',
            max_tool_calls=MAX_SEARCHES, max_output_tokens=MAX_OUTPUT_TOKENS,
            text={'format': {'type': 'json_schema', 'name': 'dossier', 'strict': True,
                             'schema': _schema()}},
        )
    except Exception as exc:                          # noqa: BLE001
        log.exception('dossier %s failed', symbol)
        abandon(key, str(exc)[:120])
        d.error, d.duration_s = str(exc)[:300], time.time() - started
        d.save()
        return d

    served = str(getattr(resp, 'service_tier', '') or '')
    usage = resp.usage
    searches = search_calls_in(resp)
    reasoning = int(getattr(getattr(usage, 'output_tokens_details', None), 'reasoning_tokens', 0) or 0)
    cached = int(getattr(getattr(usage, 'input_tokens_details', None), 'cached_tokens', 0) or 0)
    settle(key, model=model, input_tokens=usage.input_tokens - cached,
           output_tokens=usage.output_tokens, cached_tokens=cached, reasoning_tokens=reasoning,
           search_calls=searches, service_tier=served,
           note=f'{symbol} dossier, {searches} search(es)')
    d.service_tier, d.searches = served, searches
    d.input_tokens, d.output_tokens, d.reasoning_tokens = usage.input_tokens, usage.output_tokens, reasoning
    d.cost_usd = cost_of(model, usage.input_tokens - cached, usage.output_tokens, cached,
                         service_tier=served, search_calls=searches)

    if served and tier and served != tier:
        # A silent fallback to a dearer tier is an unbudgeted overspend, and a
        # Flex request that could not be served may also be a slow one. Record it
        # and refuse the result rather than trading on a surprise.
        d.error = f'requested service tier {tier!r} but the API served {served!r}'
        d.duration_s = time.time() - started
        d.save()
        return d
    if resp.status != 'completed':
        d.error = f'response {resp.status} — output truncated, not trusted'
        d.duration_s = time.time() - started
        d.save()
        return d

    try:
        data = json.loads(resp.output_text)
    except Exception as exc:                          # noqa: BLE001
        d.error = f'unparseable reply: {exc}'
        d.duration_s = time.time() - started
        d.save()
        return d

    if searches == 0:
        # tool_choice='required' should make this impossible; if it ever happens
        # the dossier is a fluent, structurally valid, entirely ungrounded essay,
        # and the only tell is this counter.
        d.error = 'the model answered without searching; the dossier is ungrounded'
        d.duration_s = time.time() - started
        d.save()
        return d

    _apply(d, data, supplied, supplied_labels, now, started)
    return d


def _apply(d: SymbolDossier, data: dict, supplied: list, supplied_labels: set, now, started) -> None:
    kept, rejected = validate_evidence(data.get('evidence'), supplied_labels)
    d.facts = supplied + kept
    if rejected:
        d.refused_reason = ('dropped: ' + '; '.join(rejected))[:300]

    forecast, problem = validate_forecast(data.get('forecast') or {})
    for key, value in forecast.items():
        setattr(d, key, value)
    if problem:
        d.error = problem
        d.direction = 'none'

    scores = data.get('scores') or {}
    d.score_catalyst = max(0, min(10, int(scores.get('catalyst') or 0)))
    d.score_context = max(0, min(10, int(scores.get('context') or 0)))
    d.score_thesis = max(0, min(10, int(scores.get('thesis') or 0)))

    cat = data.get('catalyst') or {}
    happened = _iso(cat.get('happened_at'))
    fresh = happened is not None and happened >= now - timedelta(hours=CATALYST_MAX_AGE_HOURS)
    if cat.get('present') and fresh:
        d.catalyst_headline = str(cat.get('headline', ''))[:500]
        d.catalyst_url = str(cat.get('url', ''))[:500]
        d.catalyst_at = happened
    elif cat.get('present'):
        # A catalyst without a date, or older than a day, is CONTEXT. Saying so is
        # the whole point of the tier; letting it through would be the horizon
        # mismatch the design exists to prevent.
        d.refused_reason = (d.refused_reason + ' | catalyst undated or stale, demoted to context')[:300]

    if not d.has_catalyst:
        d.direction = 'none'
    else:
        d.direction = data.get('direction') if data.get('direction') in ('buy', 'short') else 'none'

    d.narrative = str(data.get('narrative', ''))[:8000]
    d.bull = [x for x in (data.get('bull') or []) if x.get('claim')][:6]
    d.bear = [x for x in (data.get('bear') or []) if x.get('claim')][:6]
    d.triggers = [x for x in (data.get('triggers') or []) if x.get('condition')][:8]
    d.veto_reason = str(data.get('veto_reason', ''))[:300]
    # Conviction may only shrink. There is no path here that enlarges a position.
    try:
        d.size_multiplier = max(0.25, min(1.0, float(data.get('size_multiplier', 1.0))))
    except (TypeError, ValueError):
        d.size_multiplier = 1.0

    d.research_completed_at = timezone.now()
    # A shadow trade may not be priced before this moment. Research latency is
    # part of the strategy's performance and is never removed from grading.
    d.decision_eligible_at = d.research_completed_at
    d.duration_s = time.time() - started
    d.save()


def refresh(limit: int = 4, now=None) -> list[SymbolDossier]:
    """One sweep. Stops as soon as the budget says so rather than part-way through."""
    now = now or timezone.now()
    out = []
    for symbol in candidates(now, limit=limit):
        d = build(symbol, now=now)
        out.append(d)
        if d.error and 'budget' in d.error:
            log.warning('dossier sweep stopped: %s', d.error)
            break
    return out
