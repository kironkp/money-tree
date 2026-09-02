"""Bar providers. Each returns a DataFrame indexed by UTC bar-start timestamps
with columns open, high, low, close, volume, vwap, trade_count."""
from __future__ import annotations

from datetime import datetime

import pandas as pd

COLUMNS = ['open', 'high', 'low', 'close', 'volume', 'vwap', 'trade_count']


def empty_frame() -> pd.DataFrame:
    df = pd.DataFrame({c: pd.Series(dtype='float64') for c in COLUMNS})
    df.index = pd.DatetimeIndex([], tz='UTC', name='ts')
    return df


def normalize_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce any provider output into the house shape."""
    if df is None or len(df) == 0:
        return empty_frame()
    df = df.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    if df.index.tz is None:
        df.index = df.index.tz_localize('UTC')
    else:
        df.index = df.index.tz_convert('UTC')
    df.index.name = 'ts'
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = float('nan')
    df = df[COLUMNS].astype('float64')
    df = df[~df.index.duplicated(keep='last')].sort_index()
    df = df.dropna(subset=['open', 'high', 'low', 'close'])
    return df


class BarProvider:
    name = 'abstract'

    def supports(self, asset_class: str) -> bool:
        return True

    def get_bars(self, symbol: str, timeframe: str, start: datetime, end: datetime,
                 asset_class: str = 'stock') -> pd.DataFrame:
        raise NotImplementedError

    def latest_bars(self, symbols: list[str], timeframe: str, since: datetime,
                    asset_class: str = 'stock') -> dict[str, pd.DataFrame]:
        """Bars from `since` (exclusive) to now. Default: one get_bars per symbol."""
        now = datetime.now(tz=since.tzinfo)
        return {s: self.get_bars(s, timeframe, since, now, asset_class) for s in symbols}
