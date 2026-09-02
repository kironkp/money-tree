"""Bar storage: upsert provider frames, load frames, gap-fill history, and the
quality gate every frame passes through before a strategy sees it."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
from django.db import transaction

from main_app.models import Bar, Instrument

from ..timeframes import tf_delta, tf_minutes
from . import calendar as cal
from .providers import COLUMNS, BarProvider, empty_frame, normalize_frame

log = logging.getLogger('moneytree.data.store')
BATCH = 1000


@dataclass
class QualityReport:
    rows_in: int = 0
    rows_out: int = 0
    dropped_bad_ohlc: int = 0
    dropped_dupes: int = 0
    gaps: list = field(default_factory=list)      # (ts_before, ts_after, bars_missing)
    split_suspects: list = field(default_factory=list)  # (ts, pct_gap)

    @property
    def issues(self) -> list[str]:
        out = []
        if self.dropped_bad_ohlc:
            out.append(f'{self.dropped_bad_ohlc} bars with high/low outside open/close dropped')
        if self.dropped_dupes:
            out.append(f'{self.dropped_dupes} duplicate timestamps dropped')
        if self.gaps:
            out.append(f'{len(self.gaps)} gaps > 3 bars (possible halts)')
        for ts, pct in self.split_suspects:
            out.append(f'{pct:+.0%} overnight gap at {ts:%Y-%m-%d} — split? re-sync the symbol')
        return out


def quality_gate(df: pd.DataFrame, timeframe: str, asset_class: str = 'stock') -> tuple[pd.DataFrame, QualityReport]:
    rep = QualityReport(rows_in=len(df))
    if len(df) == 0:
        return df, rep
    df = df.sort_index()
    dupes = df.index.duplicated(keep='last')
    rep.dropped_dupes = int(dupes.sum())
    df = df[~dupes]
    bad = (df['high'] < df[['open', 'close']].max(axis=1) - 1e-9) | (df['low'] > df[['open', 'close']].min(axis=1) + 1e-9)
    bad |= df[['open', 'high', 'low', 'close']].le(0).any(axis=1)
    rep.dropped_bad_ohlc = int(bad.sum())
    df = df[~bad]
    if len(df) > 1:
        step = tf_delta(timeframe)
        diffs = df.index.to_series().diff()
        if asset_class == 'crypto':
            gap_mask = diffs > 3 * step
        else:
            # Same session only: an overnight jump is not a halt.
            same_day = df.index.to_series().dt.tz_convert(cal.ET).dt.date
            gap_mask = (diffs > 3 * step) & (same_day == same_day.shift(1))
        for ts in df.index[gap_mask.fillna(False).to_numpy()]:
            prev = df.index[df.index.get_loc(ts) - 1]
            rep.gaps.append((prev, ts, int((ts - prev) / step) - 1))
        # Split suspects: > 40% jump between consecutive bars.
        jumps = df['open'] / df['close'].shift(1) - 1
        for ts, pct in jumps[jumps.abs() > 0.40].items():
            rep.split_suspects.append((ts, float(pct)))
    rep.rows_out = len(df)
    return df, rep


def upsert_bars(instrument: Instrument, timeframe: str, df: pd.DataFrame, source: str) -> int:
    if df is None or len(df) == 0:
        return 0
    df = normalize_frame(df)
    rows = []
    for ts, o, h, l, c, v, vw, tc in zip(df.index, df['open'], df['high'], df['low'], df['close'],
                                         df['volume'], df['vwap'], df['trade_count']):
        rows.append(Bar(
            instrument=instrument, timeframe=timeframe, ts=ts.to_pydatetime(),
            open=float(o), high=float(h), low=float(l), close=float(c),
            volume=float(v) if not np.isnan(v) else 0.0,
            vwap=None if np.isnan(vw) else float(vw),
            trade_count=None if np.isnan(tc) else int(tc),
            source=source,
        ))
    before = Bar.objects.filter(instrument=instrument, timeframe=timeframe, source=source).count()
    with transaction.atomic():
        # A bar fetched seconds after its close is often still filling in; a
        # later fetch must be allowed to correct it.
        Bar.objects.bulk_create(rows, batch_size=BATCH, update_conflicts=True,
                                update_fields=['open', 'high', 'low', 'close', 'volume', 'vwap', 'trade_count'],
                                unique_fields=['instrument', 'timeframe', 'source', 'ts'])
    after = Bar.objects.filter(instrument=instrument, timeframe=timeframe, source=source).count()
    return after - before


SOURCE_PRIORITY = ['alpaca:sip:split', 'alpaca:crypto', 'yahoo', 'alpaca:iex', 'synthetic']


def best_source(instrument: Instrument, timeframe: str, exclude_sources: list | None = None) -> str | None:
    have = set(Bar.objects.filter(instrument=instrument, timeframe=timeframe).values_list('source', flat=True).distinct())
    have -= set(exclude_sources or [])
    for src in SOURCE_PRIORITY:
        if src in have:
            return src
    return next(iter(sorted(have)), None)


def load_frame(instrument: Instrument, timeframe: str, start: datetime | None = None,
               end: datetime | None = None, limit: int | None = None,
               exclude_sources: list | None = None, source: str | None = None) -> pd.DataFrame:
    """One feed's bars. Without `source`, the best available feed is used, so
    two feeds for the same timestamps never get mixed."""
    if source is None:
        source = best_source(instrument, timeframe, exclude_sources)
        if source is None:
            return empty_frame()
    qs = Bar.objects.filter(instrument=instrument, timeframe=timeframe, source=source)
    if start is not None:
        qs = qs.filter(ts__gte=start)
    if end is not None:
        qs = qs.filter(ts__lt=end)
    qs = qs.order_by('-ts' if limit else 'ts')
    if limit:
        qs = qs[:limit]
    rows = list(qs.values_list('ts', 'open', 'high', 'low', 'close', 'volume', 'vwap', 'trade_count'))
    if not rows:
        return empty_frame()
    if limit:
        rows.reverse()
    df = pd.DataFrame(rows, columns=['ts'] + COLUMNS)
    df['ts'] = pd.to_datetime(df['ts'], utc=True)
    df = df.set_index('ts')
    df = df.astype({c: 'float64' for c in COLUMNS})
    return df


def latest_ts(instrument: Instrument, timeframe: str, source: str | None = None) -> datetime | None:
    qs = Bar.objects.filter(instrument=instrument, timeframe=timeframe)
    if source:
        qs = qs.filter(source=source)
    return qs.order_by('-ts').values_list('ts', flat=True).first()


def coverage(instrument: Instrument, timeframe: str) -> dict:
    qs = Bar.objects.filter(instrument=instrument, timeframe=timeframe)
    first = qs.order_by('ts').values_list('ts', flat=True).first()
    last = qs.order_by('-ts').values_list('ts', flat=True).first()
    per_source = {}
    for src in qs.values_list('source', flat=True).distinct():
        sq = qs.filter(source=src)
        per_source[src] = {'count': sq.count(), 'first': sq.order_by('ts').values_list('ts', flat=True).first(),
                           'last': sq.order_by('-ts').values_list('ts', flat=True).first()}
    return {'first': first, 'last': last, 'count': qs.count(), 'sources': list(per_source), 'per_source': per_source}


def sync_bars(instrument: Instrument, timeframe: str, start: datetime, end: datetime,
              provider: BarProvider, source: str | None = None) -> dict:
    """Fetch what's missing between start and end and store it.

    Two ranges at most: before the first stored bar and after the last one.
    Interior holes are left alone (halts are real; re-fetching them daily
    would burn the rate limit for nothing) — `resync` wipes and refetches.
    """
    source = source or getattr(provider, 'source_label', lambda ac: provider.name)(instrument.asset_class)
    first = Bar.objects.filter(instrument=instrument, timeframe=timeframe, source=source).order_by('ts').values_list('ts', flat=True).first()
    last = latest_ts(instrument, timeframe, source)
    ranges = []
    if first is None:
        ranges.append((start, end))
    else:
        if start < first - tf_delta(timeframe):
            ranges.append((start, first))
        if end > last + tf_delta(timeframe):
            ranges.append((last + tf_delta(timeframe), end))
    added = 0
    fetched = 0
    issues: list[str] = []
    for a, b in ranges:
        df = provider.get_bars(instrument.symbol, timeframe, a, b, instrument.asset_class)
        fetched += len(df)
        # Never store the bar that is still forming: a history sync run mid-bar
        # would freeze a partial bar into the record.
        df = complete_bars_only(df, timeframe, datetime.now(UTC), 0)
        df, rep = quality_gate(df, timeframe, instrument.asset_class)
        issues.extend(rep.issues)
        added += upsert_bars(instrument, timeframe, df, source)
    return {'symbol': instrument.symbol, 'timeframe': timeframe, 'fetched': fetched, 'added': added,
            'ranges': ranges, 'issues': issues}


def resync(instrument: Instrument, timeframe: str, start: datetime, end: datetime, provider: BarProvider) -> dict:
    source = getattr(provider, 'source_label', lambda ac: provider.name)(instrument.asset_class)
    Bar.objects.filter(instrument=instrument, timeframe=timeframe, source=source).delete()
    return sync_bars(instrument, timeframe, start, end, provider)


def complete_bars_only(df: pd.DataFrame, timeframe: str, now: datetime, grace_s: int) -> pd.DataFrame:
    """Drop bars whose end has not passed (plus a grace period). This is the
    single rule that keeps the live loop from acting on a half-finished bar."""
    if len(df) == 0:
        return df
    cutoff = now - tf_delta(timeframe) - timedelta(seconds=grace_s)
    return df[df.index <= cutoff]


def synthetic_symbols(instruments, timeframe: str) -> list[str]:
    """Watchlist symbols whose stored history is (partly) synthetic."""
    return sorted(set(Bar.objects.filter(instrument__in=list(instruments), timeframe=timeframe, source='synthetic')
                      .values_list('instrument__symbol', flat=True)))
