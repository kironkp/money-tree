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
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from main_app.models import Briefing, Instrument, Market, NewsItem, NewsSession, NewsVerdict

log = logging.getLogger('moneytree.news_agent')

MODEL = 'gpt-5.6-sol'          # the account's flagship tier
MAX_STORIES = 40               # per sitting, newest first
LOOKBACK_HOURS = 5             # overlaps the 4-hour cadence so nothing is missed
# Prices live in spend.PRICES and nowhere else. This module used to carry its own
# PRICE_PER_M = (1.25, 10.0) for a model that bills $4/$20, so every sitting was
# recorded at 46% of what it cost and the owner's mental budget was built on it.

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

    from .spend import cost_of, record

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
    tier = str(getattr(resp, 'service_tier', '') or '')
    cost = cost_of(model, usage.prompt_tokens, usage.completion_tokens, service_tier=tier)
    record(model, provider='openai', project='moneytree', purpose='news_agent',
           input_tokens=usage.prompt_tokens, output_tokens=usage.completion_tokens,
           cost_usd=cost, note=f'news agent session #{session.pk}, {len(stories)} stories')

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
            # The unit of action is the EVENT, not the row. Twelve outlets rewrite
            # one wire story and four sittings can each see it afresh; without this
            # they became four separate QQQ shorts into a rising market.
            event_key=event_key(symbol, story.headline),
            provenance='contemporaneous', arm='headline',
        ))
    NewsVerdict.objects.bulk_create(verdicts)

    session.narrative = str(data.get('narrative', ''))[:4000]
    session.actionable = sum(1 for v in verdicts if v.actionable)
    session.cost_usd = round(cost, 5)
    session.finished_at = timezone.now()
    session.duration_s = time.time() - started
    session.save()

    _stamp_prices(session.verdicts.all())
    return session


def _stamp_prices(verdicts) -> int:
    """Record price and ATR on every verdict, whatever it scored.

    This used to run only for the calls it acted on, which meant the scoreboard
    had no control group and could never answer its own question: six of 266 rows
    were ever priced. A 3/10 that would have lost money is the evidence that a
    6/10 was worth taking, and it costs nothing but a lookup to keep it.
    """
    rows = [v for v in verdicts if v.symbol and v.price_at_verdict is None]
    cache: dict[str, tuple] = {}
    done = 0
    for v in rows:
        # Rebuilt rows are priced at their own moment; live rows at this one.
        at = v.created_at if v.provenance == 'reconstructed' else None
        key = (v.symbol, at)
        if key not in cache:
            cache[key] = _price_and_atr(v.symbol, at)
        price, atr = cache[key]
        if price is None:
            continue
        v.price_at_verdict, v.atr_at_verdict = price, atr
        v.save(update_fields=['price_at_verdict', 'atr_at_verdict'])
        done += 1
    return done


def event_key(symbol: str, headline: str) -> str:
    """Identity of the underlying event, not of the row that reported it."""
    from .news import story_key
    return f'{symbol or "-"}:{story_key(headline)}'[:64]


def _feed_frame(inst, timeframe: str, *, start=None, end=None, limit=None):
    """Bars from a feed that actually covers the window being asked about.

    `store.best_source` ranks feeds by overall quality, not by whether they hold
    the days in question. SIP is ranked first and is the right answer for a
    backtest — but the local SIP copy stops where the last history sync stopped,
    while the live loop keeps writing IEX. On 2026-09-17 that gap was two weeks,
    so every price this module stamped came from 1 September and every grading
    window came back empty. Freshest covering feed wins, priority breaks ties.
    """
    from django.db.models import Max

    from main_app.models import Bar

    from .data.store import SOURCE_PRIORITY, empty_frame, load_frame
    qs = Bar.objects.filter(instrument=inst, timeframe=timeframe)
    if start is not None:
        qs = qs.filter(ts__gte=start)
    if end is not None:
        qs = qs.filter(ts__lt=end)
    # One aggregate to choose the feed, then one load. Loading every feed and
    # throwing the losers away turned a 4-hourly job into a minutes-long one.
    rows = list(qs.values('source').annotate(last=Max('ts')))
    if not rows:
        return empty_frame()

    def rank(src):
        return SOURCE_PRIORITY.index(src) if src in SOURCE_PRIORITY else len(SOURCE_PRIORITY)

    best = max(rows, key=lambda r: (r['last'], -rank(r['source'])))
    return load_frame(inst, timeframe, start=start, end=end, limit=limit, source=best['source'])


def _price_and_atr(symbol: str, at=None) -> tuple[float | None, float | None]:
    """Close and ATR — the geometry a call is really betting on.

    `at` is what makes the rebuilt history honest: a verdict written on Monday
    has to be priced at Monday's tape, not at today's. Live callers pass None
    and get the latest bar, which is the same thing said at the right moment.
    """
    from .indicators import atr as atr_of
    inst = Instrument.objects.filter(symbol=symbol).first()
    if inst is None:
        return None, None
    for tf in ('5Min', '15Min', '1Hour', '4Hour'):
        df = _feed_frame(inst, tf, end=at, limit=60)
        if len(df) >= 15:
            a = float(atr_of(df, 14).iloc[-1])
            return float(df['close'].iloc[-1]), (a if a == a and a > 0 else None)
        if len(df):
            return float(df['close'].iloc[-1]), None
    return None, None


def _price(symbol: str) -> float | None:
    return _price_and_atr(symbol)[0]


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
# A claim that is neither released nor consumed is a crash, not a position. It
# returns to the pool rather than locking the event out forever.
LEASE_MINUTES = 10
# One event may reach the book once per lane-hold. Without this the same story
# seen by four sittings became four positions, opened at rising prices.
COOLDOWN_MINUTES = {Market.STOCKS: 240, Market.CRYPTO: 240, Market.FOREX: 240, Market.DEGEN: 180}


def _window_q(now):
    """The freshness window, expressed in SQL rather than after the ordering.

    It used to be applied in Python to the top five rows by score, so a stale 7
    could sit in front of a live 6 and consume its slot.
    """
    q = Q()
    for market, mins in ORDER_WINDOW_MINUTES.items():
        q |= Q(market=market, created_at__gte=now - timedelta(minutes=mins))
    q |= (~Q(market__in=list(ORDER_WINDOW_MINUTES))
          & Q(created_at__gte=now - timedelta(minutes=DEFAULT_ORDER_WINDOW)))
    return q


def _cooling_off(symbol: str, now) -> bool:
    """Did this symbol already send an instruction recently?"""
    recent = (NewsVerdict.objects.filter(symbol=symbol, lease_state='consumed',
                                         acted_at__isnull=False)
              .order_by('-acted_at').first())
    if recent is None:
        return False
    mins = COOLDOWN_MINUTES.get(recent.market, 240)
    return recent.acted_at >= now - timedelta(minutes=mins)


def pending_for(symbol: str, now=None) -> NewsVerdict | None:
    """The live instruction for this symbol, if there is one.

    Read by the news_catalyst strategy on each bar. Freshest first, not
    highest-scoring first: a thesis is a perishable good, and the measured
    failure was a 15.8-hour-old call being taken ahead of a fresh one.
    """
    now = now or timezone.now()
    if _cooling_off(symbol, now):
        return None
    return (NewsVerdict.objects
            .filter(_window_q(now), symbol=symbol, tradable=True, lease_state='available',
                    score__gte=NewsVerdict.ACT_THRESHOLD, direction__in=('buy', 'short'))
            .order_by('-created_at', '-score')
            .first())


def claim(verdict: NewsVerdict, owner: str = '') -> bool:
    """Take the lease. One event, one order, enforced by the database.

    Returns False if someone else holds it — including another process that got
    there a millisecond earlier, which a Python-side check could never catch.
    """
    expires = timezone.now() + timedelta(minutes=LEASE_MINUTES)
    try:
        with transaction.atomic():
            taken = (NewsVerdict.objects
                     .filter(pk=verdict.pk, lease_state='available')
                     .update(lease_state='leased', lease_expires_at=expires,
                             lease_owner=(owner or 'agent')[:64]))
    except IntegrityError:
        # The unique index on (event_key) where leased did its job: this exact
        # event is already in flight somewhere else.
        return False
    if taken:
        verdict.lease_state, verdict.lease_expires_at = 'leased', expires
    return bool(taken)


def release(verdict: NewsVerdict, reason: str = '') -> None:
    """Risk said no. Give the instruction back rather than destroying it.

    68% of signals are blocked, and this used to burn the verdict every time:
    the agent forgot a thesis because the account happened to be at its position
    limit that minute.
    """
    NewsVerdict.objects.filter(pk=verdict.pk, lease_state='leased').update(
        lease_state='available', lease_expires_at=None, lease_owner='',
        blocked_reason=(reason or verdict.blocked_reason)[:200])
    verdict.lease_state = 'available'


def consume(verdict: NewsVerdict, trade=None) -> None:
    """The broker acknowledged it. Now, and only now, is the event spent."""
    now = timezone.now()
    NewsVerdict.objects.filter(pk=verdict.pk).update(
        lease_state='consumed', acted=True, acted_at=now, trade=trade,
        lease_expires_at=None)
    verdict.lease_state, verdict.acted, verdict.acted_at = 'consumed', True, now
    # Collapse the stack: every other live call on the same symbol and side is
    # the same opinion wearing a different headline.
    (NewsVerdict.objects
     .filter(symbol=verdict.symbol, direction=verdict.direction, lease_state='available')
     .exclude(pk=verdict.pk)
     .update(lease_state='consumed', blocked_reason='superseded by a fresher call on the same side'))


def mark_acted(verdict: NewsVerdict, blocked: str = '') -> None:
    """Back-compatible shim. Prefer claim/release/consume."""
    if blocked:
        release(verdict, blocked)
    else:
        consume(verdict)


def expire_leases(now=None) -> int:
    """Return abandoned claims to the pool. A crash is not a position."""
    now = now or timezone.now()
    return (NewsVerdict.objects
            .filter(lease_state='leased', lease_expires_at__lt=now)
            .update(lease_state='available', lease_expires_at=None, lease_owner=''))


# --- scoring the calls ------------------------------------------------------
#
# Grading replays the barrier race the trade would actually have run, on the
# clock the desk actually trades. The old version marked every call at +24h to
# +30h while the position it drove is force-closed after 240 minutes, so it was
# measuring a different bet from the one that was placed.

GRADE_STOP_ATR = 1.5      # mirrors news_catalyst's stop_atr_mult
GRADE_RR = 2.0            # mirrors news_catalyst's rr → a 3-ATR target
GRADE_SLACK_HOURS = 6     # how far past the hold we will look for bars
ATR_WARMUP_DAYS = 4       # enough history before the verdict to warm a 14-period ATR


def _hold_minutes(market: str) -> int:
    from main_app.models import AgentConfig
    cfg = AgentConfig.get()
    return {Market.DEGEN: int(cfg.degen_max_hold_minutes),
            Market.FOREX: int(cfg.forex_max_hold_minutes)}.get(market, int(cfg.max_hold_minutes))


def _atr_at(df, ts) -> float | None:
    """The ATR of the bar the trade would have entered on."""
    from .indicators import atr as atr_of
    try:
        series = atr_of(df, 14)
        a = float(series.loc[:ts].iloc[-1])
    except Exception:
        return None
    return a if a == a and a > 0 else None


def _cost_atr(v: NewsVerdict, entry_px: float, atr: float) -> float:
    """The round trip, expressed in the ATRs the outcome is measured in."""
    from main_app.models import AgentConfig
    from .risk import RiskConfig
    inst = Instrument.objects.filter(symbol=v.symbol).first()
    asset_class = inst.asset_class if inst else 'stock'
    rc = RiskConfig.from_model(AgentConfig.get(), v.market or Market.STOCKS)
    pct = rc.round_trip_cost_pct(asset_class) / 100.0
    return (pct * float(entry_px) / float(atr)) if atr else 0.0


def _replay(v: NewsVerdict, df, entry: float, atr: float) -> dict:
    """Walk the bars and see which barrier the price touched first."""
    long = v.direction == 'buy'
    stop = entry - GRADE_STOP_ATR * atr if long else entry + GRADE_STOP_ATR * atr
    target = entry + GRADE_RR * GRADE_STOP_ATR * atr if long else entry - GRADE_RR * GRADE_STOP_ATR * atr
    mfe = mae = 0.0
    for bar in df.itertuples():
        up, down = (float(bar.high) - entry) / atr, (float(bar.low) - entry) / atr
        mfe = max(mfe, up if long else -down)
        mae = min(mae, down if long else -up)
        hit_stop = float(bar.low) <= stop if long else float(bar.high) >= stop
        hit_target = float(bar.high) >= target if long else float(bar.low) <= target
        # Both inside one bar: assume the adverse one came first. OHLC cannot say,
        # and the flattering assumption is how a backtest lies to itself.
        if hit_stop:
            return {'kind': 'stop', 'gross_atr': -GRADE_STOP_ATR, 'mfe': mfe, 'mae': mae,
                    'exit': stop}
        if hit_target:
            return {'kind': 'target', 'gross_atr': GRADE_RR * GRADE_STOP_ATR, 'mfe': mfe,
                    'mae': mae, 'exit': target}
    last = float(df['close'].iloc[-1])
    gross = (last - entry) / atr
    return {'kind': 'timeout', 'gross_atr': gross if long else -gross, 'mfe': mfe, 'mae': mae,
            'exit': last}


def score_verdicts(max_items: int = 100) -> dict:
    """Was the call right? Replayed over the geometry it actually implied."""
    now = timezone.now()
    due = list(NewsVerdict.objects
               .filter(outcome_at__isnull=True, price_at_verdict__isnull=False,
                       created_at__lte=now - timedelta(minutes=min(COOLDOWN_MINUTES.values())))
               .exclude(symbol='')[:max_items])
    graded = priced_only = 0
    for v in due:
        hold = _hold_minutes(v.market)
        if v.created_at > now - timedelta(minutes=hold):
            continue                       # the race it implied has not finished yet
        inst = Instrument.objects.filter(symbol=v.symbol).first()
        if inst is None:
            continue
        # Look across the instruction's whole lifetime, not just the hold. A call
        # written at 19:15 ET has its entire 240-minute hold while the exchange is
        # shut; the trade it implies opens at the next bell. Anchoring the replay
        # at `created_at` graded a race that was never run and silently dropped
        # every evening call on stocks.
        window = ORDER_WINDOW_MINUTES.get(v.market, DEFAULT_ORDER_WINDOW)
        df = None
        for tf in ('5Min', '15Min', '1Hour', '4Hour'):
            # Reach back before the verdict so the ATR has its warm-up. The strategy
            # sizes its stop from the ATR of the bar it enters on, so grading must
            # too: an evening call's pre-close ATR is a fraction of the next
            # morning's, and using it graded a stop ten times tighter than the one
            # the trade would actually have carried.
            frame = _feed_frame(inst, tf, start=v.created_at - timedelta(days=ATR_WARMUP_DAYS),
                                end=v.created_at + timedelta(minutes=window + hold)
                                + timedelta(hours=GRADE_SLACK_HOURS))
            if len(frame[frame.index >= v.created_at]):
                df = frame
                break
        if df is None:
            continue
        after = df[df.index >= v.created_at]
        if not len(after):
            continue

        # The entry is the first bar the lane was actually open for, and its price
        # is the one the trade would have paid — latency included, not removed.
        entry_ts = after.index[0]
        entry_px = float(after['close'].iloc[0])
        entry_atr = _atr_at(df, entry_ts)
        end = entry_ts + timedelta(minutes=hold)
        move = (float(after['close'].iloc[-1]) / entry_px - 1) * 100
        if v.direction in ('buy', 'short') and entry_atr:
            held = after[after.index <= end]
            r = _replay(v, held if len(held) else after, entry_px, entry_atr)
            v.outcome_kind = r['kind']
            v.outcome_atr_net = round(r['gross_atr'] - _cost_atr(v, entry_px, entry_atr), 4)
            v.mfe_atr, v.mae_atr = round(r['mfe'], 4), round(r['mae'], 4)
            signed = (r['exit'] / entry_px - 1) * 100
            v.outcome_pct = round(signed if v.direction == 'buy' else -signed, 3)
            graded += 1
        else:
            # No direction, or no ATR to build a barrier from. Record what the
            # price did so the row is not lost, but do not pretend it was a trade.
            v.outcome_pct = round(move, 3)
            priced_only += 1
        v.outcome_at = now
        v.save(update_fields=['outcome_pct', 'outcome_at', 'outcome_kind', 'outcome_atr_net',
                              'mfe_atr', 'mae_atr'])
    return {'scored': graded + priced_only, 'graded': graded, 'priced_only': priced_only}


def scoreboard(days: int = 30, provenance: str = 'contemporaneous') -> list[dict]:
    """Does a score of 8 actually beat a score of 5?

    Contemporaneous rows only by default. Forecasts recorded before the fact and
    outcomes rebuilt from bars afterwards are different kinds of evidence, and a
    table that pools them is not answering the question it prints at the top.
    """
    since = timezone.now() - timedelta(days=days)
    rows = NewsVerdict.objects.filter(created_at__gte=since, outcome_at__isnull=False,
                                      direction__in=('buy', 'short'))
    if provenance:
        rows = rows.filter(provenance=provenance)
    buckets: dict[int, list] = {}
    for v in rows:
        if v.outcome_atr_net is None:
            continue
        buckets.setdefault(v.score, []).append(v)
    out = []
    for score, items in sorted(buckets.items(), reverse=True):
        nets = [v.outcome_atr_net for v in items]
        hits = sum(1 for v in items if v.outcome_kind == 'target')
        out.append({
            'score': score, 'n': len(items),
            'hit_rate': hits / len(items) * 100,
            'avg_move_pct': sum(v.outcome_pct or 0 for v in items) / len(items),
            'avg_atr_net': sum(nets) / len(nets),
            'targets': hits,
            'stops': sum(1 for v in items if v.outcome_kind == 'stop'),
            'timeouts': sum(1 for v in items if v.outcome_kind == 'timeout'),
        })
    return out
