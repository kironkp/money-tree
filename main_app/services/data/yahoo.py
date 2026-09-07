"""Keyless fallback via yfinance. Honest limits: 7 days of 1-min bars, 60 days
of intraday bars, aggressive rate limiting (HTTP 429). Good enough to try the
app without an Alpaca account; not something to run a live loop on."""
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

import pandas as pd

from ..timeframes import YAHOO_INTERVAL, tf_delta
from .providers import BarProvider, empty_frame, normalize_frame

log = logging.getLogger('moneytree.data.yahoo')

INTRADAY_LOOKBACK = {'1Min': timedelta(days=7), '5Min': timedelta(days=59), '15Min': timedelta(days=59),
                     '30Min': timedelta(days=59), '1Hour': timedelta(days=729)}


def yahoo_symbol(symbol: str, asset_class: str = 'stock') -> str:
    if asset_class == 'forex':
        return symbol.replace('/', '') + '=X'  # EUR/USD -> EURUSD=X
    return symbol.replace('/', '-')  # BTC/USD -> BTC-USD


class YahooProvider(BarProvider):
    name = 'yahoo'

    def __init__(self, max_retries: int = 3):
        self.max_retries = max_retries

    def get_bars(self, symbol, timeframe, start, end, asset_class='stock'):
        import yfinance as yf

        now = datetime.now(UTC)
        floor = now - INTRADAY_LOOKBACK.get(timeframe, timedelta(days=3650))
        start = max(start, floor + timedelta(minutes=1))
        if start >= end:
            return empty_frame()
        interval = YAHOO_INTERVAL[timeframe]
        delay = 2.0
        for attempt in range(self.max_retries):
            try:
                raw = yf.download(
                    yahoo_symbol(symbol, asset_class), start=start, end=end, interval=interval,
                    prepost=False, auto_adjust=False, progress=False, threads=False,
                    multi_level_index=False,
                )
                break
            except Exception as exc:  # network / 429 / parsing
                log.warning('yahoo %s attempt %d failed: %s', symbol, attempt + 1, exc)
                time.sleep(delay)
                delay *= 2
        else:
            return empty_frame()
        if raw is None or len(raw) == 0:
            return empty_frame()
        df = raw.rename(columns={'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'})
        df = df[['open', 'high', 'low', 'close', 'volume']]
        df = normalize_frame(df)
        # The last row is the in-progress candle whenever its end is in the future.
        if len(df) and df.index[-1] + tf_delta(timeframe) > now:
            df = df.iloc[:-1]
        return df

    def latest_prices(self, symbols, asset_class='stock') -> dict[str, float]:
        """Last 1-minute close per symbol, one Yahoo call for the whole list
        (the forex pulse). Yahoo prints forex to the minute; volume is absent."""
        import yfinance as yf

        if not symbols:
            return {}
        names = {yahoo_symbol(s, asset_class): s for s in symbols}
        raw = yf.download(list(names), period='1d', interval='1m', prepost=False, auto_adjust=False,
                          progress=False, threads=True)
        if raw is None or len(raw) == 0:
            return {}
        closes = raw['Close'] if 'Close' in raw.columns.get_level_values(0) else raw
        out = {}
        if isinstance(closes, pd.DataFrame):
            for col in closes.columns:
                series = closes[col].dropna()
                if len(series):
                    out[names.get(col, col)] = float(series.iloc[-1])
        else:
            series = closes.dropna()
            if len(series):
                out[symbols[0]] = float(series.iloc[-1])
        return out
