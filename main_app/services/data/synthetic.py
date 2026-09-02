"""Seeded synthetic bars for tests, demos and a keyless first run.

Geometric Brownian motion with a mild intraday volume smile. Deterministic
per (seed, symbol) so tests can assert exact signals.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from ..timeframes import tf_delta, tf_minutes
from . import calendar as cal
from .providers import BarProvider, normalize_frame

BASE_PRICES = {'SPY': 640.0, 'QQQ': 570.0, 'AAPL': 230.0, 'NVDA': 175.0, 'TSLA': 340.0,
               'AMD': 165.0, 'MSFT': 505.0, 'AMZN': 225.0, 'META': 740.0,
               'BTC/USD': 110000.0, 'ETH/USD': 4400.0}


def _seed_for(seed: int, symbol: str) -> int:
    h = hashlib.sha256(f'{seed}:{symbol}'.encode()).digest()
    return int.from_bytes(h[:4], 'big')


class SyntheticProvider(BarProvider):
    name = 'synthetic'

    def __init__(self, seed: int = 7, annual_vol: float = 0.30, drift: float = 0.0):
        self.seed = seed
        self.annual_vol = annual_vol
        self.drift = drift

    def _timestamps(self, timeframe: str, start: datetime, end: datetime, asset_class: str):
        step = tf_delta(timeframe)
        if asset_class == 'crypto':
            first = start.replace(second=0, microsecond=0)
            first -= timedelta(minutes=first.minute % tf_minutes(timeframe))
            return pd.date_range(first, end, freq=step, inclusive='left', tz='UTC')
        stamps = []
        for s in cal.sessions_between(cal.session_date(start), cal.session_date(end)):
            t = s.open_utc
            while t < s.close_utc:
                if start <= t < end:
                    stamps.append(t)
                t += step
        return pd.DatetimeIndex(stamps, tz='UTC')

    def get_bars(self, symbol, timeframe, start, end, asset_class='stock'):
        idx = self._timestamps(timeframe, start, end, asset_class)
        n = len(idx)
        if n == 0:
            return normalize_frame(None)
        rng = np.random.default_rng(_seed_for(self.seed, symbol))
        per_year = 252 * (390 / tf_minutes(timeframe)) if asset_class != 'crypto' else 365 * 1440 / tf_minutes(timeframe)
        sigma = self.annual_vol / np.sqrt(per_year)
        mu = self.drift / per_year
        # Price path continues from a deterministic offset so different date
        # ranges of the same symbol don't restart at the same level.
        base = BASE_PRICES.get(symbol, 100.0)
        offset_steps = int((idx[0] - pd.Timestamp('2024-01-01', tz='UTC')).total_seconds() // 60) // tf_minutes(timeframe)
        rng2 = np.random.default_rng(_seed_for(self.seed + 1, symbol) + (offset_steps % 100000))
        level = base * float(np.exp(rng2.normal(0, 0.05)))
        rets = rng.normal(mu, sigma, n)
        closes = level * np.exp(np.cumsum(rets))
        opens = np.empty(n)
        opens[0] = level
        opens[1:] = closes[:-1] * np.exp(rng.normal(0, sigma * 0.15, n - 1))
        wiggle = np.abs(rng.normal(0, sigma, n)) * closes
        highs = np.maximum(opens, closes) + wiggle
        lows = np.minimum(opens, closes) - np.abs(rng.normal(0, sigma, n)) * closes
        # Volume smile: heavy at the open and close of each session.
        if asset_class == 'crypto':
            vol = rng.lognormal(mean=np.log(3.0), sigma=0.4, size=n)
        else:
            pos = np.array([((t.hour * 60 + t.minute) - 570) / 390 for t in idx.tz_convert(cal.ET)])
            smile = 1.0 + 2.5 * (np.abs(pos - 0.5) * 2) ** 2
            vol = rng.lognormal(mean=np.log(50_000), sigma=0.5, size=n) * smile
        vwap = (highs + lows + closes) / 3
        df = pd.DataFrame({'open': opens, 'high': highs, 'low': lows, 'close': closes,
                           'volume': np.round(vol), 'vwap': vwap, 'trade_count': np.round(vol / 100)},
                          index=idx)
        return normalize_frame(df)
