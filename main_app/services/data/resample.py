"""Causal resampling of a lane's base bars into a strategy's coarser timeframe.

A lane polls ONE feed at its base timeframe; a strategy may run on a whole
multiple of it (forex ema_momentum on 1Hour inside a 15Min lane). Coarse bars are
built from base bars on clock-aligned boundaries and stamped at their START,
like every other bar here. A coarse bar exists only once the base bar that closes
it has completed: a 1Hour strategy cannot see the last 15 minutes of an hour until
the hour is over. An hour still in progress is not in the frame at all.

The same function serves backtest, replay and live, so research and the live desk
cannot drift apart on how a coarse bar is formed.
"""
from __future__ import annotations

import math

import pandas as pd

from ..timeframes import tf_delta, tf_minutes
from .calendar import ET


def bucket_starts(index: pd.DatetimeIndex, timeframe: str, asset_class: str) -> pd.DatetimeIndex:
    """The coarse bar each base bar belongs to (its start)."""
    minutes = tf_minutes(timeframe)
    if minutes < 1440:
        return index.floor(f'{minutes}min')          # clock-aligned, UTC
    if asset_class == 'crypto':
        return index.floor('1D')                     # crypto sessions roll at 00:00 UTC
    et = index.tz_convert(ET)
    if asset_class == 'forex':
        # The forex day rolls at the New York close, 17:00 ET.
        day = (et + pd.Timedelta(hours=7)).normalize()
        return (day - pd.DateOffset(hours=7)).tz_convert('UTC')
    return et.normalize().tz_convert('UTC')          # stocks: the ET date


def bucket_end(start: pd.Timestamp, timeframe: str, asset_class: str) -> pd.Timestamp:
    if tf_minutes(timeframe) < 1440 or asset_class == 'crypto':
        return start + tf_delta(timeframe)
    # Wall-clock day in New York, so a DST change does not move the boundary.
    return (start.tz_convert(ET) + pd.DateOffset(days=1)).tz_convert('UTC')


def resample_complete(df: pd.DataFrame, base_tf: str, target_tf: str,
                      asset_class: str) -> tuple[pd.DataFrame, list]:
    """(coarse frame of COMPLETE bars, map from base position -> coarse position or None).

    A coarse bar is emitted at the base bar that closes it. If that closing base
    bar is missing (a feed gap), it is emitted one base bar late, at the first bar
    that proves the period is over. Volume that is missing on any base bar stays
    missing on the coarse bar: a sum of partial volume would understate it.
    """
    if len(df) == 0:
        return df.iloc[0:0], []
    base_step = tf_delta(base_tf)
    starts = bucket_starts(df.index, target_tf, asset_class)
    groups = df.groupby(starts, sort=True)
    agg = pd.DataFrame({
        'open': groups['open'].first(),
        'high': groups['high'].max(),
        'low': groups['low'].min(),
        'close': groups['close'].last(),
        'volume': groups['volume'].sum(min_count=1),
    })
    if 'volume' in df:
        agg.loc[df['volume'].isna().groupby(starts).any(), 'volume'] = math.nan
    if 'trade_count' in df:
        agg['trade_count'] = groups['trade_count'].sum(min_count=1)
    if 'vwap' in df:
        pv = (df['vwap'] * df['volume']).groupby(starts).sum(min_count=1)
        agg['vwap'] = pv / agg['volume']
    # Where each bucket ends in the base frame, and whether it is complete there.
    positions = pd.Series(range(len(df)), index=df.index)
    last_pos = positions.groupby(starts).max()
    emitted, at = [], []
    for start, pos in last_pos.items():
        end = bucket_end(start, target_tf, asset_class)
        if df.index[pos] + base_step >= end:
            emitted.append(start)
            at.append(int(pos))
        elif pos + 1 < len(df):
            emitted.append(start)                     # closing bar missing; the next bar proves it is over
            at.append(int(pos) + 1)
        # else: still in progress — not a bar yet
    coarse = agg.loc[emitted]
    coarse.index.name = df.index.name
    mapping: list = [None] * len(df)
    for j, pos in enumerate(at):
        mapping[pos] = j                              # a later bucket wins a rare gap collision
    return coarse, mapping
