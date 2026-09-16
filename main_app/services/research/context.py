"""Where the price is, measured from bars we hold.

These are `tier='measured'`: true by construction about our own data, which is
not the same as true about the market. They matter because they are the facts
that decide whether a story is already in the price — the question the headline
agent could never answer, because it was handed a headline and nothing else.

SPY and QQQ appear here only as regime and confirmation context. They get no
company dossier: Yahoo returns a P/E for them and nothing else, and an index is
not a company whose earnings can be read.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from main_app.models import Instrument

from .facts import Fact, FactSheet

log = logging.getLogger('moneytree.research.context')

REGIME_SYMBOLS = ('SPY', 'QQQ')
TIMEFRAMES = ('5Min', '15Min', '1Hour', '4Hour')


# Bars in one trading day, per timeframe. Used to size the load: a "20-day range"
# computed from a fixed 600-bar window is a 7.7-day range wearing the wrong label,
# and a mislabelled number is worse than a missing one.
BARS_PER_DAY = {'5Min': 78, '15Min': 26, '1Hour': 7, '4Hour': 6, '1Day': 1}
LOOKBACK_DAYS = 50


def _frame(symbol: str, days: int = LOOKBACK_DAYS):
    from ..data.store import covering_frame
    inst = Instrument.objects.filter(symbol=symbol).first()
    if inst is None:
        return None, None
    for tf in TIMEFRAMES:
        per_day = BARS_PER_DAY.get(tf, 78)
        df = covering_frame(inst, tf, limit=per_day * days + 50)
        if len(df) >= 30:
            return df, tf
    return None, None


def _m(label, value, unit, tf, now, note='') -> Fact | None:
    if value is None or value != value:
        return None
    return Fact(label=label, value=float(value), unit=unit, tier='measured',
                source=f'bars:{tf}', as_of=now, fetched_at=now, note=note)


def price_context(symbol: str, sheet: FactSheet | None = None) -> FactSheet:
    """Last price, volatility, and where in its own recent range it sits."""
    import numpy as np

    from ..indicators import atr as atr_of
    sheet = sheet or FactSheet(symbol=symbol)
    df, tf = _frame(symbol)
    if df is None:
        sheet.errors.append(f'{symbol}: no bars held for any timeframe')
        return sheet

    now = timezone.now()
    close = float(df['close'].iloc[-1])
    bars_per_day = BARS_PER_DAY.get(tf, 78)
    days_held = len(df) / bars_per_day
    sheet.facts.append(Fact(label='last_close', value=close, unit='usd', tier='measured',
                            source=f'bars:{tf}', as_of=df.index[-1].to_pydatetime(),
                            fetched_at=now, note=f'last {tf} bar held'))

    atr = float(atr_of(df, 14).iloc[-1])
    sheet.add(_m('atr', atr, 'usd', tf, now), 'atr')
    if atr == atr and close:
        sheet.add(_m('atr_pct_of_price', atr / close * 100, 'pct', tf, now,
                     'how far price typically travels in one bar'), 'atr_pct_of_price')

    # Only claim a 20-day range when 20 days of bars are actually held. Below
    # that the window is named for what it really covers.
    if days_held >= 18:
        window = df.tail(bars_per_day * 20)
        lo, hi = float(window['low'].min()), float(window['high'].max())
        if hi > lo:
            sheet.add(_m('position_in_20d_range', (close - lo) / (hi - lo) * 100, 'pct', tf, now,
                         '0 = at the 20-day low, 100 = at the high'), 'position_in_20d_range')
        sheet.add(_m('low_20d', lo, 'usd', tf, now), 'low_20d')
        sheet.add(_m('high_20d', hi, 'usd', tf, now), 'high_20d')
    else:
        sheet.missing.extend(['position_in_20d_range', 'low_20d', 'high_20d'])
        sheet.errors.append(f'{symbol}: only {days_held:.0f} days of {tf} bars held; '
                            'the 20-day range is not available')

    if days_held >= 45:
        ma = float(df['close'].tail(bars_per_day * 50).mean())
        sheet.add(_m('distance_from_50d_mean_pct', (close / ma - 1) * 100, 'pct', tf, now),
                  'distance_from_50d_mean_pct')
    else:
        sheet.missing.append('distance_from_50d_mean_pct')

    five_day = df.tail(bars_per_day * 5)
    if len(five_day) > 1:
        first = float(five_day['close'].iloc[0])
        sheet.add(_m('move_5d_pct', (close / first - 1) * 100, 'pct', tf, now), 'move_5d_pct')
        rets = np.diff(np.log(five_day['close'].to_numpy()))
        if len(rets) > 2:
            vol = float(np.std(rets) * np.sqrt(bars_per_day * 252) * 100)
            sheet.add(_m('realised_vol_annual_pct', vol, 'pct', tf, now,
                         'from the last 5 days of bars'), 'realised_vol_annual_pct')
    return sheet


def regime() -> FactSheet:
    """What the market as a whole is doing. Context only — never a company view."""
    sheet = FactSheet(symbol='MARKET')
    for symbol in REGIME_SYMBOLS:
        one = price_context(symbol)
        for f in one.facts:
            if f.label in ('move_5d_pct', 'realised_vol_annual_pct', 'position_in_20d_range',
                           'distance_from_50d_mean_pct'):
                f.label = f'{symbol.lower()}_{f.label}'
                sheet.facts.append(f)
        sheet.errors.extend(one.errors)
    return sheet


def barrier_base_rate(symbol: str, stop_atr: float = 1.5, rr: float = 2.0,
                      hold_minutes: int = 240, samples: int = 800) -> dict | None:
    """What the tape does unprompted, on this symbol, with this geometry.

    The model is asked for the probability that price touches the target before
    the stop. That number is meaningless without knowing what it is from a
    standing start — a 30% forecast is bearish if the base rate is 45% and
    wildly bullish if it is 12%. So the base rate is measured here, on this
    symbol's own bars, and handed to the model as a supplied fact rather than
    left for it to guess.

    Entries are sampled long-only; the short base rate is measured separately by
    passing the mirrored geometry. Both barriers inside one bar counts as the
    stop, the same conservative tie-break the grader uses, because OHLC cannot
    say which came first and the flattering assumption is how a backtest lies.
    """
    import numpy as np

    from ..indicators import atr as atr_of
    df, tf = _frame(symbol)
    if df is None or len(df) < 200:
        return None
    per_day = BARS_PER_DAY.get(tf, 78)
    horizon = max(2, int(hold_minutes / (390 / per_day))) if per_day else 48
    atr = atr_of(df, 14).to_numpy()
    close, high, low = df['close'].to_numpy(), df['high'].to_numpy(), df['low'].to_numpy()
    usable = len(df) - horizon - 1
    if usable < 50:
        return None
    stride = max(1, usable // samples)

    out = {'target': 0, 'stop': 0, 'timeout': 0}
    for side in ('long',):
        for i in range(14, usable, stride):
            a = atr[i]
            if not (a == a) or a <= 0:
                continue
            entry = close[i]
            stop = entry - stop_atr * a
            target = entry + rr * stop_atr * a
            window = slice(i + 1, i + 1 + horizon)
            hit_stop = np.nonzero(low[window] <= stop)[0]
            hit_target = np.nonzero(high[window] >= target)[0]
            first_stop = hit_stop[0] if len(hit_stop) else None
            first_target = hit_target[0] if len(hit_target) else None
            if first_stop is None and first_target is None:
                out['timeout'] += 1
            elif first_target is None or (first_stop is not None and first_stop <= first_target):
                out['stop'] += 1                     # ties go to the stop
            else:
                out['target'] += 1
    n = sum(out.values())
    if n < 50:
        return None
    return {'n': n, 'timeframe': tf, 'horizon_bars': horizon,
            'p_target_first': out['target'] / n, 'p_stop_first': out['stop'] / n,
            'p_timeout': out['timeout'] / n}
