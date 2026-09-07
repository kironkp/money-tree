"""NYSE session calendar.

Pure functions (no Django needed) so the backtest core and optimizer workers
can use them; `MarketSession` rows synced from Alpaca override the computed
values when the app is up. Times are UTC-aware; the exchange runs on ET.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from datetime import UTC
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

# Full-day closures. Kept two years back for backtests and a year ahead.
HOLIDAYS = {
    # 2024
    date(2024, 1, 1), date(2024, 1, 15), date(2024, 2, 19), date(2024, 3, 29), date(2024, 5, 27),
    date(2024, 6, 19), date(2024, 7, 4), date(2024, 9, 2), date(2024, 11, 28), date(2024, 12, 25),
    # 2025 (Jan 9 = national day of mourning)
    date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1), date(2025, 11, 27),
    date(2025, 12, 25),
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31),
    date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24),
}
# 13:00 ET closes.
EARLY_CLOSES = {
    date(2024, 7, 3), date(2024, 11, 29), date(2024, 12, 24),
    date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24),
    date(2026, 11, 27), date(2026, 12, 24),
    date(2027, 11, 26),
}

# Populated from the MarketSession table by `load_overrides()`; date -> Session.
_OVERRIDES: dict[date, 'Session'] = {}


@dataclass(frozen=True)
class Session:
    date: date
    open_utc: datetime
    close_utc: datetime
    early_close: bool = False

    @property
    def minutes(self) -> int:
        return int((self.close_utc - self.open_utc).total_seconds() // 60)

    def contains(self, ts: datetime) -> bool:
        return self.open_utc <= ts < self.close_utc


def _to_utc(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=ET).astimezone(UTC)


def is_trading_day(d: date) -> bool:
    if d in _OVERRIDES:
        return True
    return d.weekday() < 5 and d not in HOLIDAYS


def session_for(d: date) -> Session | None:
    """The stock session on a date, or None when the market is closed."""
    if d in _OVERRIDES:
        return _OVERRIDES[d]
    if not is_trading_day(d):
        return None
    early = d in EARLY_CLOSES
    return Session(d, _to_utc(d, REGULAR_OPEN), _to_utc(d, EARLY_CLOSE if early else REGULAR_CLOSE), early)


def sessions_between(start: date, end: date) -> list[Session]:
    out = []
    d = start
    while d <= end:
        s = session_for(d)
        if s:
            out.append(s)
        d += timedelta(days=1)
    return out


def previous_session(d: date) -> Session | None:
    d -= timedelta(days=1)
    for _ in range(10):
        s = session_for(d)
        if s:
            return s
        d -= timedelta(days=1)
    return None


def session_at(ts: datetime) -> Session | None:
    """The session that contains ts (UTC), else None."""
    d = ts.astimezone(ET).date()
    s = session_for(d)
    if s and s.contains(ts):
        return s
    return None


def is_open(ts: datetime, asset_class: str = 'stock') -> bool:
    if asset_class == 'crypto':
        return True
    if asset_class == 'forex':
        return forex_is_open(ts)
    return session_at(ts) is not None


def next_open(ts: datetime, asset_class: str = 'stock') -> datetime:
    if asset_class == 'crypto':
        return ts
    if asset_class == 'forex':
        return forex_next_open(ts)
    d = ts.astimezone(ET).date()
    for _ in range(15):
        s = session_for(d)
        if s and s.open_utc > ts:
            return s.open_utc
        d += timedelta(days=1)
    raise RuntimeError('no session found in the next two weeks — extend HOLIDAYS')


def session_date(ts: datetime) -> date:
    """Calendar date in ET — the 'trading day' a timestamp belongs to."""
    return ts.astimezone(ET).date()


def trading_day(ts: datetime, asset_class: str = 'stock') -> date:
    """The day a timestamp's risk budget belongs to: the ET date for stocks
    and crypto, the New York-close day (rolls at 17:00 ET) for forex."""
    return forex_day(ts) if asset_class == 'forex' else session_date(ts)


# --- forex: 24/5, the week runs Sunday 17:00 ET → Friday 17:00 ET -----------
FOREX_ROLL = time(17, 0)   # the New York close: the forex "day" and week roll here


def forex_is_open(ts: datetime) -> bool:
    et = ts.astimezone(ET)
    wd, t = et.weekday(), et.time()
    if wd == 5:
        return False
    if wd == 4 and t >= FOREX_ROLL:
        return False
    if wd == 6 and t < FOREX_ROLL:
        return False
    return True


def forex_next_open(ts: datetime) -> datetime:
    if forex_is_open(ts):
        return ts
    et = ts.astimezone(ET)
    d = et.date()
    while d.weekday() != 6:  # the coming Sunday
        d += timedelta(days=1)
    return _to_utc(d, FOREX_ROLL)


def forex_week_close(ts: datetime) -> datetime | None:
    """Friday 17:00 ET of the week containing ts, or None when closed."""
    if not forex_is_open(ts):
        return None
    et = ts.astimezone(ET)
    d = et.date()
    while d.weekday() != 4:
        d += timedelta(days=1)
    return _to_utc(d, FOREX_ROLL)


def forex_day(ts: datetime) -> date:
    """Forex days roll at the New York close: 17:00 ET Sunday already belongs to Monday."""
    return (ts.astimezone(ET) + timedelta(hours=7)).date()


def load_overrides(rows) -> int:
    """rows: iterable of objects with date/open_utc/close_utc/early_close."""
    _OVERRIDES.clear()
    for r in rows:
        _OVERRIDES[r.date] = Session(r.date, r.open_utc, r.close_utc, r.early_close)
    return len(_OVERRIDES)


def load_overrides_from_db() -> int:
    try:
        from main_app.models import MarketSession
        return load_overrides(MarketSession.objects.all())
    except Exception:  # apps not ready, table missing, etc. — computed calendar stands
        return 0
