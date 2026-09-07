"""Vectorised, causal indicators. Every function only looks backward, so a
value at bar i is identical whether or not bars after i exist — that is what
makes backtest and live runs comparable."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .data import calendar as cal


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(50.0)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df['close'].shift(1)
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - prev_close).abs(),
        (df['low'] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.fillna(df['high'] - df['low'])


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / n, adjust=False).mean()


def session_key(index: pd.DatetimeIndex, asset_class: str = 'stock') -> pd.Series:
    """Session label per bar: ET date for stocks, UTC date for crypto, the
    New York-close day (rolls 17:00 ET) for forex."""
    if asset_class == 'crypto':
        return pd.Series(index.tz_convert('UTC').date, index=index)
    if asset_class == 'forex':
        return pd.Series((index.tz_convert(cal.ET) + pd.Timedelta(hours=7)).date, index=index)
    return pd.Series(index.tz_convert(cal.ET).date, index=index)


def bar_position(session: pd.Series) -> pd.Series:
    """0-based bar number within its session."""
    return session.groupby(session).cumcount()


def session_vwap(df: pd.DataFrame, session: pd.Series) -> pd.Series:
    typical = (df['high'] + df['low'] + df['close']) / 3
    vol = df['volume'].replace(0, np.nan).fillna(1.0)
    pv = (typical * vol).groupby(session).cumsum()
    vv = vol.groupby(session).cumsum()
    return pv / vv


def opening_range(df: pd.DataFrame, session: pd.Series, n_bars: int) -> tuple[pd.Series, pd.Series]:
    """High/low of the first n_bars of each session. NaN until the range is
    complete (bar position >= n_bars), so no bar can trade its own range."""
    pos = bar_position(session)
    in_range = pos < n_bars
    hi = df['high'].where(in_range).groupby(session).cummax().groupby(session).ffill()
    lo = df['low'].where(in_range).groupby(session).cummin().groupby(session).ffill()
    complete = pos >= n_bars
    return hi.where(complete), lo.where(complete)


def relative_volume(df: pd.DataFrame, session: pd.Series, n_sessions: int = 10) -> pd.Series:
    """Volume vs the average volume at the same bar position over the previous
    n sessions. Causal: the current session is excluded via shift(1)."""
    pos = bar_position(session)
    vol = df['volume'].replace(0, np.nan)  # missing volume (Yahoo crypto) must not read as "no interest"
    base = vol.groupby(pos).transform(lambda s: s.shift(1).rolling(n_sessions, min_periods=1).mean())
    return (vol / base.replace(0, np.nan)).fillna(1.0)


def bollinger(s: pd.Series, n: int = 20, k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = s.rolling(n).mean()
    sd = s.rolling(n).std()
    return mid, mid + k * sd, mid - k * sd


def zscore(s: pd.Series, n: int) -> pd.Series:
    mean = s.rolling(n).mean()
    sd = s.rolling(n).std()
    return (s - mean) / sd.replace(0, np.nan)


def minutes_to_close(index: pd.DatetimeIndex, asset_class: str = 'stock') -> pd.Series:
    """Minutes from bar START to the session close; NaN for crypto. Forex has
    one close a week (Friday 17:00 ET), so the week is its session."""
    if asset_class == 'crypto':
        return pd.Series(np.nan, index=index)
    if asset_class == 'forex':
        return forex_minutes_to_close(index)
    out = np.full(len(index), np.nan)
    cache = {}
    for i, ts in enumerate(index):
        d = ts.tz_convert(cal.ET).date()
        s = cache.get(d)
        if s is None:
            s = cache[d] = cal.session_for(d)
        if s is not None:
            out[i] = (s.close_utc - ts.to_pydatetime()).total_seconds() / 60
    return pd.Series(out, index=index)


def forex_minutes_to_close(index: pd.DatetimeIndex) -> pd.Series:
    """Minutes to the Friday 17:00 ET close of each bar's week; NaN on the weekend."""
    if len(index) == 0:
        return pd.Series(np.nan, index=index)
    et = index.tz_convert(cal.ET)
    wd = np.asarray(et.weekday)
    mins = np.asarray(et.hour) * 60 + np.asarray(et.minute)
    closed = (wd == 5) | ((wd == 4) & (mins >= 17 * 60)) | ((wd == 6) & (mins < 17 * 60))
    days_to_friday = (4 - wd) % 7
    local_midnight = et.tz_localize(None).normalize()
    friday_close = (local_midnight + pd.to_timedelta(days_to_friday, unit='D') + pd.Timedelta(hours=17)).tz_localize(
        cal.ET, ambiguous='NaT', nonexistent='shift_forward')
    out = (friday_close.tz_convert('UTC') - index.tz_convert('UTC')).total_seconds() / 60.0
    out = np.where(closed, np.nan, out)
    return pd.Series(out, index=index)
