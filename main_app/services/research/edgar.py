"""Filed figures, from the SEC, with enough identity to find them again.

This is the only source in the app that may be called authoritative, and only for
the exact number a company put in a filing. It is free, needs no key and no
signup — just a declared User-Agent, which the SEC requires and which is a
condition of use rather than a formality.

A filed fact carries its accession number, form type, fiscal period, unit, XBRL
tag and filing timestamp. That is not bookkeeping: it is what lets a human open
the filing and check the number against the dossier that quoted it, which is the
whole difference between a citation and a claim.

Deliberately narrow. `companyfacts` returns 3.8 MB for Apple — far too much to
hand a model — so this fetches one concept at a time and only the handful that
matter. Nothing here is required for a dossier to exist; when EDGAR is slow or
unreachable the fact is simply missing, and missing is an honest answer.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from urllib.request import Request, urlopen

from django.utils import timezone

from .facts import Fact, FactSheet

log = logging.getLogger('moneytree.research.edgar')

# The SEC asks for a declaring User-Agent on every automated request and throttles
# to ~10/s. We make a handful an hour, so the limit is never the constraint.
USER_AGENT = 'MoneyTree research (kironkp@gmail.com)'
TIMEOUT_S = 8

# CIKs for the seven names this desk researches. Hard-coded because the mapping
# file is 1 MB, these never change, and a lookup that can fail is one more way a
# dossier can fail for no good reason.
CIK = {
    'AAPL': '0000320193', 'NVDA': '0001045810', 'TSLA': '0001318605',
    'AMD': '0000002488', 'MSFT': '0000789019', 'AMZN': '0001018724',
    'META': '0001326801',
}

# The three figures worth cross-checking against the filing itself.
CONCEPTS = (
    ('revenue', 'RevenueFromContractWithCustomerExcludingAssessedTax', 'usd'),
    ('net_income', 'NetIncomeLoss', 'usd'),
    ('eps_diluted', 'EarningsPerShareDiluted', 'usd'),
)


def _get(url: str) -> dict | None:
    try:
        req = Request(url, headers={'User-Agent': USER_AGENT, 'Accept-Encoding': 'gzip, deflate'})
        with urlopen(req, timeout=TIMEOUT_S) as resp:         # noqa: S310 - fixed SEC host
            raw = resp.read()
            if resp.headers.get('Content-Encoding') == 'gzip':
                import gzip
                raw = gzip.decompress(raw)
            return json.loads(raw)
    except Exception as exc:                                  # noqa: BLE001
        log.info('edgar fetch failed %s: %r', url, exc)
        return None


def _latest_quarter(units: list[dict]) -> dict | None:
    """The most recently FILED quarterly figure.

    Sorted by filing date, not by period: a restatement filed last week is newer
    information about an older quarter, and it is the filing we want to cite.
    Entries without a form are annual roll-ups EDGAR includes for convenience.
    """
    quarters = [u for u in units
                if u.get('form') in ('10-Q', '10-K') and u.get('start') and u.get('end')
                and (date.fromisoformat(u['end']) - date.fromisoformat(u['start'])).days <= 100]
    if not quarters:
        return None
    return max(quarters, key=lambda u: (u.get('filed', ''), u.get('end', '')))


def concept(symbol: str, tag: str, label: str, unit: str = 'usd') -> Fact | None:
    """One filed figure, complete with the identity needed to find it again."""
    cik = CIK.get(symbol)
    if not cik:
        return None
    data = _get(f'https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json')
    if not data:
        return None
    for unit_key, entries in (data.get('units') or {}).items():
        row = _latest_quarter(entries)
        if row is None:
            continue
        filed = row.get('filed')
        return Fact(
            label=label, value=float(row['val']), unit=unit,
            period=f"{row['start']}..{row['end']}", tier='filed', source='sec-edgar',
            as_of=datetime.fromisoformat(row['end']).replace(tzinfo=UTC),
            fetched_at=timezone.now(),
            filed_at=datetime.fromisoformat(filed).replace(tzinfo=UTC) if filed else None,
            accession=str(row.get('accn', '')), form=str(row.get('form', '')),
            xbrl_tag=tag,
            url=f'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}'
                f'&type={row.get("form", "10-Q")}&dateb=&owner=include&count=10',
            note=f'as filed, {unit_key}')
    return None


def filed_facts(symbol: str, sheet: FactSheet | None = None) -> FactSheet:
    """The cross-check. Slow-ish and optional; a dossier stands without it."""
    sheet = sheet or FactSheet(symbol=symbol)
    if symbol not in CIK:
        return sheet
    for label, tag, unit in CONCEPTS:
        sheet.add(concept(symbol, tag, f'filed_{label}', unit), f'filed_{label}')
    return sheet
