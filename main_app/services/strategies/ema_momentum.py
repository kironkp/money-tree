"""EMA momentum.

Fast EMA crossing above the slow EMA, RSI in a healthy band (not overbought),
relative volume confirming. ATR stop, R-multiple target, and a cross back
down closes the trade early.
"""
from __future__ import annotations

import numpy as np

from .. import indicators as ind
from .base import Context, Param, Rule, Signal, Strategy


class EmaMomentum(Strategy):
    key = 'ema_momentum'
    name = 'EMA Momentum'
    description = 'EMA 9/21 cross with RSI + relative-volume filters; ATR stop; exit on cross back.'
    asset_classes = ('stock', 'etf', 'crypto', 'forex')
    params = (
        Param('fast', 'int', 9, 5, 13, 2, help='Fast EMA length'),
        Param('slow', 'int', 21, 15, 34, 4, help='Slow EMA length'),
        Param('rsi_min', 'float', 50, 40, 60, 5, help='RSI floor at entry'),
        Param('rsi_max', 'float', 70, 65, 80, 5, help='RSI ceiling at entry'),
        Param('stop_atr_mult', 'float', 1.5, 0.5, 2.5, 0.5, help='Stop distance in ATRs'),
        Param('rr', 'float', 2.0, 1.0, 4.0, 0.5, help='Target as a multiple of risk'),
        Param('min_relvol', 'float', 1.0, 0.0, 2.0, 0.5, help='Minimum relative volume at entry'),
    )
    warmup_bars = 40

    def prepare(self, df, asset_class='stock', timeframe='5Min'):
        df = df.copy()
        session = ind.session_key(df.index, asset_class)
        df['session'] = session.astype(str)
        df['bar_pos'] = ind.bar_position(session)
        df['ema_fast'] = ind.ema(df['close'], int(self.p['fast']))
        df['ema_slow'] = ind.ema(df['close'], int(self.p['slow']))
        df['ema_diff'] = df['ema_fast'] - df['ema_slow']
        df['ema_diff_prev'] = df['ema_diff'].shift(1)
        df['rsi'] = ind.rsi(df['close'], 14)
        df['atr'] = ind.atr(df, 14)
        df['relvol'] = ind.relative_volume(df, session, 10)
        return df

    def on_bar(self, ctx: Context, bar, df, i) -> list[Signal]:
        if np.isnan(bar.ema_diff_prev) or np.isnan(bar.atr) or bar.atr <= 0:
            return []
        pos = ctx.position
        cross_up = bar.ema_diff > 0 and bar.ema_diff_prev <= 0
        cross_down = bar.ema_diff < 0 and bar.ema_diff_prev >= 0
        if pos is not None:
            if pos.qty > 0 and cross_down:
                return [Signal('close', ctx.symbol, ctx.ts, float(bar.close), reason='EMA cross down')]
            if pos.qty < 0 and cross_up:
                return [Signal('close', ctx.symbol, ctx.ts, float(bar.close), reason='EMA cross up')]
            return []
        if bar.bar_pos < 2 or bar.relvol < self.p['min_relvol']:
            return []
        risk = self.p['stop_atr_mult'] * bar.atr
        if cross_up and self.p['rsi_min'] <= bar.rsi <= self.p['rsi_max']:
            return [Signal('buy', ctx.symbol, ctx.ts, float(bar.close), float(bar.close - risk),
                           float(bar.close + self.p['rr'] * risk),
                           reason=f'EMA{int(self.p["fast"])}>{int(self.p["slow"])} cross, RSI {bar.rsi:.0f}')]
        if ctx.asset_class != 'crypto' and cross_down and (100 - self.p['rsi_max']) <= bar.rsi <= (100 - self.p['rsi_min']):
            return [Signal('sell', ctx.symbol, ctx.ts, float(bar.close), float(bar.close + risk),
                           float(bar.close - self.p['rr'] * risk),
                           reason=f'EMA{int(self.p["fast"])}<{int(self.p["slow"])} cross, RSI {bar.rsi:.0f}')]
        return []

    def rules(self, ctx: Context, bar) -> list[Rule]:
        if np.isnan(bar.ema_diff_prev):
            return [Rule('warmup', False, 'still warming up')]
        fast, slow = int(self.p['fast']), int(self.p['slow'])
        if ctx.position is not None:
            return [Rule('holding', True, f'holding; EMA{fast} {bar.ema_fast:,.2f} vs EMA{slow} {bar.ema_slow:,.2f} (exits on a cross down)')]
        cross_up = bar.ema_diff > 0 and bar.ema_diff_prev <= 0
        cross_down = bar.ema_diff < 0 and bar.ema_diff_prev >= 0
        can_short = ctx.asset_class != 'crypto'
        if cross_up:
            cross_text = f'EMA{fast} just crossed above EMA{slow} (long setup)'
        elif cross_down and can_short:
            cross_text = f'EMA{fast} just crossed below EMA{slow} (short setup)'
        elif bar.ema_diff > 0:
            cross_text = f'EMA{fast} is above EMA{slow} (bullish) but did not newly cross'
        else:
            cross_text = f'EMA{fast} is below EMA{slow} (bearish)' + (' but did not newly cross' if can_short else '')
        out = [Rule('cross', bool(cross_up or (cross_down and can_short)), cross_text, value=bar.ema_diff, threshold=0.0)]
        lo, hi = self.p['rsi_min'], self.p['rsi_max']
        if cross_down and can_short:
            lo, hi = 100 - self.p['rsi_max'], 100 - self.p['rsi_min']
        in_band = lo <= bar.rsi <= hi
        out.append(Rule('rsi', bool(in_band), f'RSI {bar.rsi:.0f} ' + ('within' if in_band else 'outside') +
                        f' the {lo:.0f}–{hi:.0f} band', value=bar.rsi, threshold=lo))
        vol_ok = bar.relvol >= self.p['min_relvol']
        out.append(Rule('volume', bool(vol_ok), f'relative volume {bar.relvol:.1f} vs {self.p["min_relvol"]:.1f} required',
                        value=bar.relvol, threshold=self.p['min_relvol']))
        return out
