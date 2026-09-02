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
from .base import Context, Param, Rule, Signal, Strategy


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
        if ctx.asset_class != 'crypto' and bar.close < bar.or_low:
            stop = bar.close + risk
            target = bar.close - self.p['rr'] * risk
            st['traded_session'] = bar.session
            return [Signal('sell', ctx.symbol, ctx.ts, float(bar.close), float(stop), float(target),
                           reason=f'ORB↓ close {bar.close:.2f} < range low {bar.or_low:.2f}')]
        return []

    def rules(self, ctx: Context, bar) -> list[Rule]:
        if ctx.position is not None:
            pos = ctx.position
            return [Rule('holding', True, f'holding {pos.side} from {pos.avg_price:,.2f}, stop {pos.stop:,.2f}, target {pos.target:,.2f}')]
        st = self.symbol_state(ctx.symbol)
        if st.get('traded_session') == bar.session:
            return [Rule('fresh', False, 'already traded this session (one trade per day)')]
        out = []
        range_ok = not np.isnan(bar.or_high)
        if range_ok:
            out.append(Rule('range', True, f'opening range {bar.or_low:,.2f}–{bar.or_high:,.2f} is set'))
            broke_up = bar.close > bar.or_high
            broke_down = bar.close < bar.or_low and ctx.asset_class != 'crypto'
            if broke_up:
                text = f'close {bar.close:,.2f} broke above the range high {bar.or_high:,.2f} (long)'
            elif broke_down:
                text = f'close {bar.close:,.2f} broke below the range low {bar.or_low:,.2f} (short)'
            else:
                text = f'close {bar.close:,.2f} is inside the range (long above {bar.or_high:,.2f}, short below {bar.or_low:,.2f})'
            out.append(Rule('breakout', bool(broke_up or broke_down), text, value=bar.close, threshold=bar.or_high))
        else:
            out.append(Rule('range', False, f'opening range still forming ({int(bar.bar_pos) + 1} bars in)'))
        in_window = bar.minutes_since_open <= self.p['entry_window_minutes']
        out.append(Rule('window', bool(in_window), 'within the entry window' if in_window else 'entry window has closed for today',
                        value=bar.minutes_since_open, threshold=self.p['entry_window_minutes']))
        vol_ok = bar.relvol >= self.p['min_relvol']
        out.append(Rule('volume', bool(vol_ok), f'relative volume {bar.relvol:.1f} vs {self.p["min_relvol"]:.1f} required',
                        value=bar.relvol, threshold=self.p['min_relvol']))
        return out
