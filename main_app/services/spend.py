"""The spend ledger: what the paid APIs cost, and which project spent it.

Every model call should land here at the moment it happens, priced with the
rate in force at that moment. A cost reconstructed later from a log is a guess;
a row written at call time is a fact.

`record()` is deliberately forgiving — it never raises into the caller, because
a spend ledger that can break a trading loop or a chat response is worse than
no ledger at all.

Other apps on this machine can post here too (see `record` and the `project`
field), so one table answers "where is the API money going" instead of four
provider dashboards and a guess.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from calendar import monthrange
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal

from django.utils import timezone

from main_app.models import ApiUsage

from .data import calendar as cal

log = logging.getLogger('moneytree.spend')

# USD per MILLION tokens: (input, output). Cached input is billed at
# CACHE_READ_SHARE of the input rate.
PRICES: dict[str, tuple[float, float]] = {
    # Anthropic
    'claude-opus-5': (15.0, 75.0),
    'claude-fable-5-1': (15.0, 75.0),
    'claude-sonnet-5': (3.0, 15.0),
    'claude-haiku-4-5-20251001': (0.80, 4.0),
    # OpenAI text
    'gpt-4o': (2.50, 10.0),
    'gpt-4o-mini': (0.15, 0.60),
    'text-embedding-3-small': (0.02, 0.0),
    'text-embedding-3-large': (0.13, 0.0),
    # OpenAI realtime (audio tokens) — by far the most expensive thing a small
    # app can run, which is why it is listed explicitly rather than defaulted.
    'gpt-4o-realtime-preview': (40.0, 80.0),
    'gpt-4o-mini-realtime-preview': (10.0, 20.0),
    # Non-token providers are priced per call via UNIT_PRICES.
}
CACHE_READ_SHARE = 0.1
DEFAULT_PRICE = (5.0, 25.0)      # unknown model: assume expensive, so it stands out
UNIT_PRICES: dict[str, float] = {'serper': 0.001, 'resend': 0.0}   # USD per call


def price_for(model: str) -> tuple[float, float]:
    if model in PRICES:
        return PRICES[model]
    for key, val in PRICES.items():        # tolerate dated suffixes: gpt-4o-2024-11-20
        if model.startswith(key):
            return val
    return DEFAULT_PRICE


def cost_of(model: str, input_tokens: int = 0, output_tokens: int = 0, cached_tokens: int = 0) -> Decimal:
    pin, pout = price_for(model)
    usd = (input_tokens * pin + cached_tokens * pin * CACHE_READ_SHARE + output_tokens * pout) / 1e6
    return Decimal(str(round(usd, 6)))


def record(model: str, *, provider: str = 'anthropic', project: str = 'moneytree', purpose: str = '',
           input_tokens: int = 0, output_tokens: int = 0, cached_tokens: int = 0,
           calls: int = 1, cost_usd: Decimal | float | None = None, note: str = '') -> ApiUsage | None:
    """Write one call to the ledger. Never raises."""
    try:
        if cost_usd is None:
            if provider in UNIT_PRICES and not (input_tokens or output_tokens):
                cost_usd = Decimal(str(UNIT_PRICES[provider] * calls))
            else:
                cost_usd = cost_of(model, input_tokens, output_tokens, cached_tokens)
        return ApiUsage.objects.create(
            provider=provider, project=project, model=model, purpose=purpose or 'unknown',
            input_tokens=int(input_tokens), output_tokens=int(output_tokens),
            cached_tokens=int(cached_tokens), calls=int(calls),
            cost_usd=Decimal(str(cost_usd)), note=note[:200],
        )
    except Exception:
        log.exception('spend: could not record %s/%s', provider, model)
        return None


def record_anthropic(model: str, usage, purpose: str, project: str = 'moneytree', note: str = ''):
    """Record from an Anthropic SDK usage object."""
    return record(
        model, provider='anthropic', project=project, purpose=purpose, note=note,
        input_tokens=getattr(usage, 'input_tokens', 0) or 0,
        output_tokens=getattr(usage, 'output_tokens', 0) or 0,
        cached_tokens=(getattr(usage, 'cache_read_input_tokens', 0) or 0),
    )


def record_openai(model: str, usage, purpose: str, project: str = 'moneytree', note: str = ''):
    """Record from an OpenAI SDK usage object (dict or attribute style)."""
    def g(*names):
        for n in names:
            v = getattr(usage, n, None)
            if v is None and isinstance(usage, dict):
                v = usage.get(n)
            if v is not None:
                return v
        return 0
    cached = 0
    details = getattr(usage, 'prompt_tokens_details', None) or (usage.get('prompt_tokens_details') if isinstance(usage, dict) else None)
    if details is not None:
        cached = getattr(details, 'cached_tokens', None) or (details.get('cached_tokens') if isinstance(details, dict) else 0) or 0
    return record(model, provider='openai', project=project, purpose=purpose, note=note,
                  input_tokens=int(g('input_tokens', 'prompt_tokens')) - int(cached),
                  output_tokens=int(g('output_tokens', 'completion_tokens')),
                  cached_tokens=int(cached))


# --- reading it back -------------------------------------------------------

def _bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, dtime(0, 0), tzinfo=cal.ET)
    return start, start + timedelta(days=1)


def day_spend(d: date | None = None) -> dict:
    """One day's spend, broken down the three ways that matter."""
    d = d or cal.session_date(timezone.now())
    a, b = _bounds(d)
    rows = list(ApiUsage.objects.filter(ts__gte=a, ts__lt=b))
    return _summarise(rows, {'date': d})


def range_spend(days: int = 30) -> dict:
    since = timezone.now() - timedelta(days=days)
    rows = list(ApiUsage.objects.filter(ts__gte=since))
    out = _summarise(rows, {'days': days})
    out['per_day'] = _per_day(rows)
    return out


def _summarise(rows, base: dict) -> dict:
    total = sum(float(r.cost_usd) for r in rows)
    by = {k: defaultdict(lambda: {'cost': 0.0, 'calls': 0, 'in': 0, 'out': 0}) for k in ('project', 'provider', 'model', 'purpose')}
    for r in rows:
        for key, val in (('project', r.project), ('provider', r.provider), ('model', r.model), ('purpose', r.purpose)):
            cell = by[key][val]
            cell['cost'] += float(r.cost_usd)
            cell['calls'] += r.calls
            cell['in'] += r.input_tokens + r.cached_tokens
            cell['out'] += r.output_tokens
    return {**base, 'total': total, 'calls': sum(r.calls for r in rows),
            **{f'by_{k}': dict(sorted(v.items(), key=lambda kv: -kv[1]['cost'])) for k, v in by.items()}}


def _per_day(rows) -> dict:
    out: dict[str, float] = defaultdict(float)
    for r in rows:
        out[r.ts.astimezone(cal.ET).date().isoformat()] += float(r.cost_usd)
    return dict(sorted(out.items()))


def projected_monthly(days: int = 30) -> float:
    """What the last `days` imply for a month, if nothing changes."""
    s = range_spend(days)
    seen = len(s.get('per_day') or {}) or 1
    return s['total'] / seen * 30.0


# --- navigable windows: 1 day / 7 days / 30 days, stepped by period ---------
#
# Boundaries are LOCAL calendar boundaries in market time, not "N x 24h ago": a
# week starts on Monday and a month is a real month. A late-evening call
# otherwise lands in the wrong day.

PERIODS = ('day', 'week', 'month')


def _local_midnight(y: int, m: int, d: int) -> datetime:
    """Midnight ET for a possibly out-of-range date, normalised by arithmetic."""
    while m > 12:
        y, m = y + 1, m - 12
    while m < 1:
        y, m = y - 1, m + 12
    base = datetime(y, m, 1, tzinfo=cal.ET)
    return base + timedelta(days=d - 1)


def spend_window(period: str = 'day', offset: int = 0, now: datetime | None = None) -> dict:
    """The window for a period at an offset. Offset 0 is current, -1 the previous."""
    period = period if period in PERIODS else 'day'
    offset = min(0, int(offset))
    now = (now or timezone.now()).astimezone(cal.ET)
    if period == 'day':
        start = _local_midnight(now.year, now.month, now.day + offset)
        end = start + timedelta(days=1)
        label = 'Today' if offset == 0 else ('Yesterday' if offset == -1 else f'{start:%a %b %-d}')
    elif period == 'week':
        back_to_monday = now.weekday()          # Monday = 0, which is how a working week reads
        start = _local_midnight(now.year, now.month, now.day - back_to_monday + offset * 7)
        end = start + timedelta(days=7)
        last = end - timedelta(days=1)
        label = ('This week' if offset == 0 else 'Last week' if offset == -1
                 else f'{start:%b %-d} – {last:%b %-d}')
    else:
        start = _local_midnight(now.year, now.month + offset, 1)
        days_in = monthrange(start.year, start.month)[1]
        end = start + timedelta(days=days_in)
        label = 'This month' if offset == 0 else f'{start:%B %Y}'
    return {'period': period, 'offset': offset, 'start': start, 'end': end, 'label': label,
            'has_next': offset < 0, 'days': max(1, round((end - start).total_seconds() / 86400))}


def _bucket(rows, key) -> list[dict]:
    out: dict[str, dict] = {}
    for r in rows:
        cell = out.setdefault(key(r), {'key': key(r), 'calls': 0, 'usd': 0.0,
                                       'input_tokens': 0, 'output_tokens': 0, 'estimated': False})
        cell['calls'] += r.calls
        cell['usd'] += float(r.cost_usd)
        cell['input_tokens'] += r.input_tokens + r.cached_tokens
        cell['output_tokens'] += r.output_tokens
        # A price we had to guess is an upper bound, and the panel says so.
        if r.model not in PRICES and not any(r.model.startswith(k) for k in PRICES):
            cell['estimated'] = True
    return sorted(out.values(), key=lambda c: -c['usd'])


def spend_report(window: dict | None = None) -> dict:
    """Everything the spend panel renders for one window."""
    w = window or spend_window()
    rows = list(ApiUsage.objects.filter(ts__gte=w['start'], ts__lt=w['end']))
    total = sum(float(r.cost_usd) for r in rows)
    calls = sum(r.calls for r in rows)
    days = w['days']

    daily: list[dict] = []
    if w['period'] != 'day':
        per: dict[str, float] = {}
        for r in rows:
            per[r.ts.astimezone(cal.ET).date().isoformat()] = per.get(
                r.ts.astimezone(cal.ET).date().isoformat(), 0.0) + float(r.cost_usd)
        for i in range(days):                 # every day, including the empty ones — a gap is information
            d = (w['start'] + timedelta(days=i)).date().isoformat()
            daily.append({'day': d, 'usd': round(per.get(d, 0.0), 6)})

    biggest = sorted(rows, key=lambda r: -float(r.cost_usd))[:5]
    return {
        'window': {**w, 'start': w['start'].isoformat(), 'end': w['end'].isoformat()},
        'days': days, 'total_usd': total, 'calls': calls,
        'per_day_usd': total / days if days else 0.0,
        'monthly_run_rate_usd': total / days * 30 if days else 0.0,
        'by_kind': _bucket(rows, lambda r: r.purpose or 'other'),
        'by_model': _bucket(rows, lambda r: r.model or 'unknown'),
        'by_project': _bucket(rows, lambda r: r.project or 'unknown'),
        'daily': daily,
        'biggest': [{'id': r.pk, 'kind': r.purpose or 'other', 'model': r.model,
                     'project': r.project, 'usd': float(r.cost_usd),
                     'input_tokens': r.input_tokens + r.cached_tokens, 'output_tokens': r.output_tokens,
                     'at': r.ts.isoformat()} for r in biggest],
        'any_estimated': any(b['estimated'] for b in _bucket(rows, lambda r: r.model or 'unknown')),
    }


def all_time() -> dict:
    rows = ApiUsage.objects.all()
    return {'usd': float(sum(r.cost_usd for r in rows)), 'calls': sum(r.calls for r in rows)}


KIND_LABEL = {
    'coach': 'Coach reviews', 'assistant': 'Assistant', 'research': 'Research',
    'embedding': 'Embeddings', 'voice': 'Voice calls', 'transcribe': 'Dictation',
    'chat': 'Chat', 'unknown': 'Unattributed', 'other': 'Other',
}
