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


def yahoo_symbol(symbol: str) -> str:
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
                    yahoo_symbol(symbol), start=start, end=end, interval=interval,
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
