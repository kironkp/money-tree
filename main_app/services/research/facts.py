"""A fact, and where it came from.

Every number a dossier prints has to carry its provenance, because the three
sources this desk can reach are not equally trustworthy and pretending otherwise
is how a fabricated figure ends up looking like a filed one:

  filed     the company told the SEC. Carries accession, form, period and the
            XBRL tag, so the exact number can be found again by a human.
  vendor    yfinance, which is an unofficial scraper of a free web endpoint with
            no uptime guarantee and no contract. Useful, fast, and never
            authoritative — it is a starting point, not a citation.
  measured  computed here from bars we hold. True by construction about our own
            data, which is not the same as true about the market.

Every field is nullable on purpose. A missing multiple is a missing multiple: the
absence of a number is information, and filling it with a zero or an inference is
the failure this whole module exists to prevent.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime


@dataclass
class Fact:
    label: str
    value: float | None = None
    unit: str = ''                 # usd, pct, ratio, count, date
    period: str = ''               # the fiscal period it describes, if any
    tier: str = 'vendor'           # filed | vendor | measured
    source: str = ''               # yfinance | sec-edgar | bars
    as_of: datetime | None = None  # when the VALUE was true, not when we fetched it
    fetched_at: datetime | None = None
    url: str = ''
    # SEC identity — present only on a filed fact, and complete when it is.
    accession: str = ''
    form: str = ''
    filed_at: datetime | None = None
    xbrl_tag: str = ''
    note: str = ''

    @property
    def is_filed(self) -> bool:
        return self.tier == 'filed' and bool(self.accession and self.xbrl_tag)

    @property
    def citable(self) -> bool:
        """May this number appear in a dossier at all?

        A filed fact needs its identity; anything else needs a source and a time.
        A value with neither cannot be checked by a human and is not evidence.
        """
        if self.value is None:
            return False
        if self.tier == 'filed':
            return self.is_filed
        return bool(self.source and (self.as_of or self.fetched_at))

    def as_dict(self) -> dict:
        d = asdict(self)
        for k in ('as_of', 'fetched_at', 'filed_at'):
            d[k] = d[k].isoformat() if d[k] else None
        return d


@dataclass
class FactSheet:
    """Everything we know about one symbol, with the gaps left visible."""
    symbol: str
    facts: list[Fact] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)   # asked for, not available
    errors: list[str] = field(default_factory=list)

    def add(self, fact: Fact | None, label: str = '') -> None:
        if fact is None or fact.value is None:
            self.missing.append(label or (fact.label if fact else ''))
            return
        self.facts.append(fact)

    def get(self, label: str) -> Fact | None:
        return next((f for f in self.facts if f.label == label), None)

    def value(self, label: str) -> float | None:
        f = self.get(label)
        return f.value if f else None

    @property
    def citable(self) -> list[Fact]:
        return [f for f in self.facts if f.citable]

    def as_dict(self) -> dict:
        return {'symbol': self.symbol, 'facts': [f.as_dict() for f in self.facts],
                'missing': self.missing, 'errors': self.errors}
