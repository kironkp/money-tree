"""Opening Range Breakout.

The first `range_minutes` of the session define a range; a close beyond it
(with enough relative volume) enters in the breakout direction with an
ATR-based stop and an R-multiple target. One trade per symbol per session,
entries only inside the first `entry_window_minutes`. Stocks only — crypto
has no opening bell.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .. import indicators as ind
from ..timeframes import tf_minutes
from .base import Context, Param, Signal, Strategy


class OpeningRangeBreakout(Strategy):
    key = 'orb'
    name = 'Opening Range Breakout'
    description = ('Breakout of the first N minutes of the session, ATR stop, R-multiple target, '
                   'one trade per symbol per day (Zarattini & Aziz style).')
    asset_classes = ('stock', 'etf')
    params = (
        Param('range_minutes', 'choice', 15, choices=(5, 15, 30), help='Opening range length'),
        Param('stop_atr_mult', 'float', 1.0, 0.5, 2.0, 0.5, help='Stop distance in ATRs'),
        Param('rr', 'float', 2.0, 1.0, 4.0, 0.5, help='Target as a multiple of risk'),
        Param('min_relvol', 'float', 1.0, 0.0, 2.0, 0.5, help='Minimum relative volume on the breakout bar'),
        Param('entry_window_minutes', 'int', 120, 60, 240, 60, help='Only enter this long after the open'),
        Param('trade_short', 'bool', False, help='Also fade breakdowns (needs allow_short in Settings)'),
    )
    warmup_bars = 20

    def prepare(self, df, asset_class='stock', timeframe='5Min'):
        df = df.copy()
        session = ind.session_key(df.index, asset_class)
        tfm = tf_minutes(timeframe)
        n_bars = max(1, int(self.p['range_minutes']) // tfm)
        df['session'] = session.astype(str)
        df['bar_pos'] = ind.bar_position(session)
        df['or_high'], df['or_low'] = ind.opening_range(df, session, n_bars)
        df['atr'] = ind.atr(df, 14)
        df['relvol'] = ind.relative_volume(df, session, 10)
        df['minutes_since_open'] = df['bar_pos'] * tfm
        return df

    def on_bar(self, ctx: Context, bar, df, i) -> list[Signal]:
        if ctx.position is not None:
            return []  # exits are stop/target/time, handled by the engine
        st = self.symbol_state(ctx.symbol)
        if st.get('traded_session') == bar.session:
            return []
        if np.isnan(bar.or_high) or np.isnan(bar.atr) or bar.atr <= 0:
            return []
        if bar.minutes_since_open > self.p['entry_window_minutes']:
            return []
        if bar.relvol < self.p['min_relvol']:
            return []
        risk = self.p['stop_atr_mult'] * bar.atr
        if bar.close > bar.or_high:
            stop = bar.close - risk
            target = bar.close + self.p['rr'] * risk
            st['traded_session'] = bar.session
            return [Signal('buy', ctx.symbol, ctx.ts, float(bar.close), float(stop), float(target),
                           reason=f'ORB↑ close {bar.close:.2f} > range high {bar.or_high:.2f}')]
        if self.p['trade_short'] and bar.close < bar.or_low:
            stop = bar.close + risk
            target = bar.close - self.p['rr'] * risk
            st['traded_session'] = bar.session
            return [Signal('sell', ctx.symbol, ctx.ts, float(bar.close), float(stop), float(target),
                           reason=f'ORB↓ close {bar.close:.2f} < range low {bar.or_low:.2f}')]
        return []
