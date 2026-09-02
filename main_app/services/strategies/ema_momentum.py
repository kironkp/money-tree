"""EMA momentum.

Fast EMA crossing above the slow EMA, RSI in a healthy band (not overbought),
relative volume confirming. ATR stop, R-multiple target, and a cross back
down closes the trade early.
"""
from __future__ import annotations

import numpy as np

from .. import indicators as ind
from .base import Context, Param, Signal, Strategy


class EmaMomentum(Strategy):
    key = 'ema_momentum'
    name = 'EMA Momentum'
    description = 'EMA 9/21 cross with RSI + relative-volume filters; ATR stop; exit on cross back.'
    asset_classes = ('stock', 'etf', 'crypto')
    params = (
        Param('fast', 'int', 9, 5, 13, 2, help='Fast EMA length'),
        Param('slow', 'int', 21, 15, 34, 4, help='Slow EMA length'),
        Param('rsi_min', 'float', 50, 40, 60, 5, help='RSI floor at entry'),
        Param('rsi_max', 'float', 70, 65, 80, 5, help='RSI ceiling at entry'),
        Param('stop_atr_mult', 'float', 1.5, 0.5, 2.5, 0.5, help='Stop distance in ATRs'),
        Param('rr', 'float', 2.0, 1.0, 4.0, 0.5, help='Target as a multiple of risk'),
        Param('min_relvol', 'float', 1.0, 0.0, 2.0, 0.5, help='Minimum relative volume at entry'),
        Param('trade_short', 'bool', False, help='Mirror for downside crosses'),
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
        if self.p['trade_short'] and cross_down and (100 - self.p['rsi_max']) <= bar.rsi <= (100 - self.p['rsi_min']):
            return [Signal('sell', ctx.symbol, ctx.ts, float(bar.close), float(bar.close + risk),
                           float(bar.close - self.p['rr'] * risk),
                           reason=f'EMA{int(self.p["fast"])}<{int(self.p["slow"])} cross, RSI {bar.rsi:.0f}')]
        return []

    def explain(self, ctx: Context, bar) -> str:
        if np.isnan(bar.ema_diff_prev):
            return 'warming up'
        rel = '>' if bar.ema_diff > 0 else '<'
        note = f'EMA{int(self.p["fast"])} {bar.ema_fast:,.2f} {rel} EMA{int(self.p["slow"])} {bar.ema_slow:,.2f}, RSI {bar.rsi:.0f}'
        if ctx.position is not None:
            return f'holding, {note}'
        if bar.relvol < self.p['min_relvol']:
            note += f', relvol {bar.relvol:.1f} low'
        return note + ' — waiting for a cross'
