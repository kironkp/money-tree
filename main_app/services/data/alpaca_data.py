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
from . import calendar as cal
from .providers import BarProvider, empty_frame, normalize_frame

log = logging.getLogger('moneytree.data.alpaca')
SIP_LAG = timedelta(minutes=16)
PAGE_CAP = 10000  # a single request never returns more than this, whatever the range


def regular_session_only(df: pd.DataFrame) -> pd.DataFrame:
    """Alpaca stock bars cover 04:00–20:00 ET; strategies only know the
    09:30–16:00 session (opening range, VWAP, bar positions all assume it)."""
    if len(df) == 0:
        return df
    keep = [cal.session_at(ts.to_pydatetime()) is not None for ts in df.index]
    return df[keep]


def windows(start: datetime, end: datetime, timeframe: str, asset_class: str):
    """Date windows small enough that each stays under the page cap."""
    per_day = (24 * 60 if asset_class == 'crypto' else 16 * 60) / max(1, tf_minutes(timeframe))
    days = max(1, int(PAGE_CAP * 0.8 / per_day)) if timeframe != '1Day' else 3650
    a = start
    while a < end:
        b = min(end, a + timedelta(days=days))
        yield a, b
        a = b


def _timeframe(timeframe: str):
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    if timeframe == '1Day':
        return TimeFrame.Day
    minutes = tf_minutes(timeframe)
    if minutes % 60 == 0:
        return TimeFrame(minutes // 60, TimeFrameUnit.Hour)
    return TimeFrame(minutes, TimeFrameUnit.Minute)


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
        parts = []
        for a, b in windows(start, end, timeframe, asset_class):
            parts.append(self._get_window(symbol, timeframe, a, b, asset_class))
        parts = [p for p in parts if len(p)]
        if not parts:
            return empty_frame()
        df = normalize_frame(pd.concat(parts))
        return df if asset_class == 'crypto' or timeframe == '1Day' else regular_session_only(df)

    def _get_window(self, symbol, timeframe, start, end, asset_class):
        if asset_class == 'crypto':
            from alpaca.data.requests import CryptoBarsRequest
            req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=_timeframe(timeframe), start=start, end=end, limit=PAGE_CAP)
            return _to_frame(self.crypto_client.get_crypto_bars(req), symbol)
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        if self.live_feed:
            feed, end_arg = DataFeed.IEX, end  # IEX has no 15-minute rule; history and live share one feed
        else:
            feed = DataFeed.SIP
            end_arg = min(end, datetime.now(UTC) - SIP_LAG)
            if end_arg <= start:
                return empty_frame()
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=_timeframe(timeframe), start=start, end=end_arg,
                               feed=feed, adjustment=Adjustment.SPLIT, limit=PAGE_CAP)
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
            return {s: regular_session_only(_to_frame(bars, s)) for s in symbols}
        return {s: _to_frame(bars, s) for s in symbols}


    def latest_prices(self, symbols, asset_class='stock') -> dict[str, float]:
        """One request: the current mid price per symbol (for the live pulse).

        Quotes, not trades: Alpaca's crypto venue prints trades sparsely, so the
        last trade can sit still for minutes while the market moves."""
        if not symbols:
            return {}
        out = {}
        try:
            if asset_class == 'crypto':
                from alpaca.data.requests import CryptoLatestQuoteRequest
                res = self.crypto_client.get_crypto_latest_quote(CryptoLatestQuoteRequest(symbol_or_symbols=list(symbols)))
            else:
                from alpaca.data.enums import DataFeed
                from alpaca.data.requests import StockLatestQuoteRequest
                res = self.stock_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=list(symbols), feed=DataFeed.IEX))
            for s in symbols:
                q = res.get(s)
                bid, ask = float(getattr(q, 'bid_price', 0) or 0), float(getattr(q, 'ask_price', 0) or 0)
                if bid > 0 and ask > 0:
                    out[s] = (bid + ask) / 2
                elif bid > 0 or ask > 0:
                    out[s] = bid or ask
        except Exception as exc:
            log.warning('latest quotes failed: %s', exc)
        missing = [s for s in symbols if s not in out]
        if missing:
            try:
                if asset_class == 'crypto':
                    from alpaca.data.requests import CryptoLatestTradeRequest
                    res = self.crypto_client.get_crypto_latest_trade(CryptoLatestTradeRequest(symbol_or_symbols=missing))
                else:
                    from alpaca.data.enums import DataFeed
                    from alpaca.data.requests import StockLatestTradeRequest
                    res = self.stock_client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=missing, feed=DataFeed.IEX))
                for s in missing:
                    t = res.get(s)
                    if t is not None and getattr(t, 'price', None):
                        out[s] = float(t.price)
            except Exception as exc:
                log.warning('latest trades failed: %s', exc)
        return out
