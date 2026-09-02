"""Timeframe helpers shared by providers, the store and the engine."""
from __future__ import annotations

from datetime import datetime, timedelta

TIMEFRAME_MINUTES = {'1Min': 1, '5Min': 5, '15Min': 15, '30Min': 30, '1Hour': 60, '1Day': 1440}
YAHOO_INTERVAL = {'1Min': '1m', '5Min': '5m', '15Min': '15m', '30Min': '30m', '1Hour': '60m', '1Day': '1d'}


def tf_minutes(timeframe: str) -> int:
    try:
        return TIMEFRAME_MINUTES[timeframe]
    except KeyError:
        raise ValueError(f'unknown timeframe {timeframe!r}') from None


def tf_delta(timeframe: str) -> timedelta:
    return timedelta(minutes=tf_minutes(timeframe))


def bar_end(ts: datetime, timeframe: str) -> datetime:
    """Bars are stamped at their START (Alpaca and Yahoo alike)."""
    return ts + tf_delta(timeframe)


def floor_to_bar(ts: datetime, timeframe: str) -> datetime:
    minutes = tf_minutes(timeframe)
    if minutes >= 1440:
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    total = ts.hour * 60 + ts.minute
    floored = (total // minutes) * minutes
    return ts.replace(hour=floored // 60, minute=floored % 60, second=0, microsecond=0)


def bars_per_session(timeframe: str, session_minutes: int = 390) -> int:
    return max(1, session_minutes // tf_minutes(timeframe))
