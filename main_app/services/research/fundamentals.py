"""What yfinance can tell us about a company, with the caveats attached.

yfinance is an unofficial scraper of a free Yahoo endpoint. It has no contract,
no uptime guarantee, no support, and it covers only the seven real stocks on this
desk — SPY and QQQ return a P/E and nothing else, and forex and the altcoins
return nothing at all. So everything here is `tier='vendor'`: useful for putting
a number in front of a model, never a citation, and never described as confirmed.

Measured 2026-09-16: all nine equity symbols returned a populated `.info` in 1.7
seconds with zero failures, and `eps_revisions` carried AAPL at 7 upgrades against
14 downgrades over 30 days — which is the "estimates are being cut" signal that no
headline ever states outright.

Every call is wrapped. A source that can vanish without warning must never be able
to take the dossier down with it; a missing fact is recorded as missing and the
sheet is returned anyway.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from django.utils import timezone

from .facts import Fact, FactSheet

log = logging.getLogger('moneytree.research.fundamentals')

VENDOR = 'yfinance'
# Symbols Yahoo actually has fundamentals for. Everything else gets price context
# only, and the dossier says so rather than producing an empty-looking company.
COVERED = {'AAPL', 'NVDA', 'TSLA', 'AMD', 'MSFT', 'AMZN', 'META'}

# label -> (info key, unit). Ratios stay ratios; percentages are converted once,
# here, so nothing downstream has to guess whether 0.164 means 16.4% or 0.164%.
INFO_FIELDS = (
    ('trailing_pe', 'trailingPE', 'ratio'),
    ('forward_pe', 'forwardPE', 'ratio'),
    ('market_cap', 'marketCap', 'usd'),
    ('revenue_growth_yoy', 'revenueGrowth', 'pct'),
    ('earnings_growth_yoy', 'earningsGrowth', 'pct'),
    ('gross_margin', 'grossMargins', 'pct'),
    ('profit_margin', 'profitMargins', 'pct'),
    ('analyst_target_mean', 'targetMeanPrice', 'usd'),
    ('analyst_count', 'numberOfAnalystOpinions', 'count'),
    ('short_pct_of_float', 'shortPercentOfFloat', 'pct'),
    ('beta', 'beta', 'ratio'),
)
PCT_FIELDS = {label for label, _, unit in INFO_FIELDS if unit == 'pct'}


def _ticker(symbol: str):
    import yfinance as yf
    return yf.Ticker(symbol)


def _fact(label, value, unit, *, now, period='', note='') -> Fact | None:
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value:                     # NaN
        return None
    if label in PCT_FIELDS:
        value *= 100.0
    return Fact(label=label, value=value, unit=unit, period=period, tier='vendor',
                source=VENDOR, fetched_at=now, as_of=now, note=note)


def fundamentals(symbol: str, sheet: FactSheet | None = None) -> FactSheet:
    """One company's vendor fundamentals. Never raises; gaps are recorded."""
    sheet = sheet or FactSheet(symbol=symbol)
    now = timezone.now()
    if symbol not in COVERED:
        sheet.errors.append(f'{symbol}: no vendor fundamentals exist for this instrument')
        return sheet

    url = f'https://finance.yahoo.com/quote/{symbol}'
    try:
        t = _ticker(symbol)
        info = dict(t.info or {})
    except Exception as exc:               # noqa: BLE001
        log.warning('yfinance info failed for %s: %r', symbol, exc)
        sheet.errors.append(f'{symbol}: vendor fundamentals unavailable ({exc.__class__.__name__})')
        return sheet

    for label, key, unit in INFO_FIELDS:
        f = _fact(label, info.get(key), unit, now=now)
        if f is not None:
            f.url = url
        sheet.add(f, label)

    _earnings_date(t, sheet, now, url)
    _revisions(t, sheet, now, url)
    return sheet


def _earnings_date(t, sheet: FactSheet, now, url: str) -> None:
    """The single most important 'do not hold into this' input."""
    try:
        cal = t.calendar or {}
        dates = cal.get('Earnings Date') or []
        when = dates[0] if dates else None
        if when is None:
            sheet.missing.append('next_earnings_date')
            return
        when = datetime(when.year, when.month, when.day, tzinfo=UTC)
        sheet.facts.append(Fact(
            label='next_earnings_date', value=when.timestamp(), unit='date', tier='vendor',
            source=VENDOR, as_of=now, fetched_at=now, url=url,
            note=f'{when:%Y-%m-%d}; a scheduled release the desk must not hold through'))
        sheet.facts.append(Fact(
            label='days_to_earnings', value=(when - now).total_seconds() / 86400, unit='count',
            tier='vendor', source=VENDOR, as_of=now, fetched_at=now, url=url))
    except Exception as exc:               # noqa: BLE001
        log.info('yfinance calendar failed for %s: %r', sheet.symbol, exc)
        sheet.missing.append('next_earnings_date')


def _revisions(t, sheet: FactSheet, now, url: str) -> None:
    """Which way the analysts are moving — a signal no headline states outright."""
    try:
        rev = t.eps_revisions
        row = rev.loc['0q']
        up = float(row.get('upLast30days') or 0)
        down = float(row.get('downLast30days') or 0)
    except Exception as exc:               # noqa: BLE001
        log.info('yfinance eps_revisions failed for %s: %r', sheet.symbol, exc)
        sheet.missing.extend(['eps_revisions_up_30d', 'eps_revisions_down_30d'])
        return
    for label, value in (('eps_revisions_up_30d', up), ('eps_revisions_down_30d', down)):
        sheet.facts.append(Fact(label=label, value=value, unit='count', period='current quarter',
                                tier='vendor', source=VENDOR, as_of=now, fetched_at=now, url=url))
    if up + down > 0:
        sheet.facts.append(Fact(
            label='eps_revision_balance', value=(up - down) / (up + down) * 100, unit='pct',
            period='current quarter', tier='vendor', source=VENDOR, as_of=now, fetched_at=now,
            url=url, note='positive means estimates are being raised'))
