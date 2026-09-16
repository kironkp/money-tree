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
