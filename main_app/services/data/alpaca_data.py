"""Alpaca market data.

Free (Basic) plan rules that matter:
- History: SIP (consolidated tape) is fine as long as `end` is at least 15
  minutes old. We always ask for split-adjusted prices so history lines up
  with live quotes.
- Live polling: must say `feed=iex` explicitly. Without it the server
  silently caps `end` at now-15min and the loop trades on stale bars.
- Crypto has its own client and needs no feed argument.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pandas as pd
from django.conf import settings

from ..timeframes import tf_minutes
from .providers import BarProvider, empty_frame, normalize_frame

log = logging.getLogger('moneytree.data.alpaca')
SIP_LAG = timedelta(minutes=16)


def _timeframe(timeframe: str):
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    if timeframe == '1Day':
        return TimeFrame.Day
    return TimeFrame(tf_minutes(timeframe), TimeFrameUnit.Minute)


def _to_frame(bars, symbol: str) -> pd.DataFrame:
    df = bars.df
    if df is None or len(df) == 0:
        return empty_frame()
    if isinstance(df.index, pd.MultiIndex):
        try:
            df = df.xs(symbol, level='symbol')
        except KeyError:
            return empty_frame()
    return normalize_frame(df)


class AlpacaDataProvider(BarProvider):
    name = 'alpaca'

    def __init__(self, api_key: str | None = None, secret_key: str | None = None, live_feed: bool = False):
        self.api_key = api_key or settings.ALPACA_API_KEY
        self.secret_key = secret_key or settings.ALPACA_SECRET_KEY
        # live_feed=True → IEX with no lag (the polling loop); False → SIP history.
        self.live_feed = live_feed
        self._stock = None
        self._crypto = None

    @property
    def stock_client(self):
        if self._stock is None:
            from alpaca.data.historical import StockHistoricalDataClient
            self._stock = StockHistoricalDataClient(self.api_key, self.secret_key)
        return self._stock

    @property
    def crypto_client(self):
        if self._crypto is None:
            from alpaca.data.historical import CryptoHistoricalDataClient
            self._crypto = CryptoHistoricalDataClient(self.api_key, self.secret_key)
        return self._crypto

    def source_label(self, asset_class: str) -> str:
        if asset_class == 'crypto':
            return 'alpaca:crypto'
        return 'alpaca:iex' if self.live_feed else 'alpaca:sip:split'

    def get_bars(self, symbol, timeframe, start, end, asset_class='stock'):
        if asset_class == 'crypto':
            from alpaca.data.requests import CryptoBarsRequest
            req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=_timeframe(timeframe),
                                    start=start, end=end, limit=10000)
            return _to_frame(self.crypto_client.get_crypto_bars(req), symbol)

        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        if self.live_feed:
            feed, end_arg = DataFeed.IEX, None
        else:
            feed = DataFeed.SIP
            end_arg = min(end, datetime.now(UTC) - SIP_LAG)
            if end_arg <= start:
                return empty_frame()
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=_timeframe(timeframe),
                               start=start, end=end_arg, feed=feed, adjustment=Adjustment.SPLIT, limit=10000)
        return _to_frame(self.stock_client.get_stock_bars(req), symbol)

    def latest_bars(self, symbols, timeframe, since, asset_class='stock'):
        """One multi-symbol request per tick (200 req/min is plenty)."""
        if not symbols:
            return {}
        if asset_class == 'crypto':
            from alpaca.data.requests import CryptoBarsRequest
            req = CryptoBarsRequest(symbol_or_symbols=list(symbols), timeframe=_timeframe(timeframe), start=since)
            bars = self.crypto_client.get_crypto_bars(req)
        else:
            from alpaca.data.enums import Adjustment, DataFeed
            from alpaca.data.requests import StockBarsRequest
            req = StockBarsRequest(symbol_or_symbols=list(symbols), timeframe=_timeframe(timeframe), start=since,
                                   feed=DataFeed.IEX, adjustment=Adjustment.SPLIT)
            bars = self.stock_client.get_stock_bars(req)
        return {s: _to_frame(bars, s) for s in symbols}
