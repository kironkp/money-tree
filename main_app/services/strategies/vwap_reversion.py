"""VWAP mean reversion.

When price stretches more than `entry_z` standard deviations below the
session VWAP, buy and aim for a return to VWAP with an ATR stop. Mirrored
shorts optional. Works on crypto too (sessions anchored at 00:00 UTC), though
25 bps taker fees make it a hard game there — the strategy page says so.
"""
from __future__ import annotations

import numpy as np

from .. import indicators as ind
from .base import Context, Param, Rule, Signal, Strategy


class VwapReversion(Strategy):
    key = 'vwap_reversion'
    name = 'VWAP Reversion'
    description = 'Fade stretches beyond k·σ from session VWAP back to VWAP; ATR stop.'
    asset_classes = ('stock', 'etf', 'crypto')
    params = (
        Param('entry_z', 'float', 2.0, 1.0, 3.0, 0.5, help='Entry threshold in σ of (close − VWAP)'),
        Param('lookback', 'int', 30, 20, 60, 10, help='Bars for the σ estimate'),
        Param('stop_atr_mult', 'float', 1.5, 0.5, 2.5, 0.5, help='Stop distance in ATRs'),
        Param('min_bar_pos', 'int', 6, 3, 12, 3, help='Bars into the session before trading'),
        Param('max_bars_held', 'int', 24, 6, 48, 6, help='Give up after this many bars'),
        Param('trade_short', 'bool', False, help='Also fade upside stretches'),
    )
    warmup_bars = 40

    def prepare(self, df, asset_class='stock', timeframe='5Min'):
        df = df.copy()
        session = ind.session_key(df.index, asset_class)
        df['session'] = session.astype(str)
        df['bar_pos'] = ind.bar_position(session)
        df['svwap'] = ind.session_vwap(df, session)
        dev = df['close'] - df['svwap']
        sd = dev.rolling(int(self.p['lookback'])).std()
        df['z'] = (dev / sd.replace(0, np.nan))
        df['atr'] = ind.atr(df, 14)
        return df

    def on_bar(self, ctx: Context, bar, df, i) -> list[Signal]:
        if np.isnan(bar.z) or np.isnan(bar.atr) or bar.atr <= 0:
            return []
        pos = ctx.position
        if pos is not None:
            if pos.qty > 0 and bar.close >= bar.svwap:
                return [Signal('close', ctx.symbol, ctx.ts, float(bar.close), reason='back at VWAP')]
            if pos.qty < 0 and bar.close <= bar.svwap:
                return [Signal('close', ctx.symbol, ctx.ts, float(bar.close), reason='back at VWAP')]
            if pos.bars_held >= self.p['max_bars_held']:
                return [Signal('close', ctx.symbol, ctx.ts, float(bar.close), reason='max bars held')]
            return []
        if bar.bar_pos < self.p['min_bar_pos']:
            return []
        risk = self.p['stop_atr_mult'] * bar.atr
        if bar.z <= -self.p['entry_z'] and bar.svwap > bar.close:
            return [Signal('buy', ctx.symbol, ctx.ts, float(bar.close), float(bar.close - risk), float(bar.svwap),
                           strength=min(1.0, abs(bar.z) / 3), reason=f'z={bar.z:.2f} below VWAP {bar.svwap:.2f}')]
        if self.p['trade_short'] and bar.z >= self.p['entry_z'] and bar.svwap < bar.close:
            return [Signal('sell', ctx.symbol, ctx.ts, float(bar.close), float(bar.close + risk), float(bar.svwap),
                           strength=min(1.0, abs(bar.z) / 3), reason=f'z={bar.z:.2f} above VWAP {bar.svwap:.2f}')]
        return []

    def rules(self, ctx: Context, bar) -> list[Rule]:
        if np.isnan(bar.z):
            return [Rule('warmup', False, 'still warming up (not enough bars for the σ estimate)')]
        if ctx.position is not None:
            return [Rule('holding', True, f'holding, z {bar.z:+.2f}, {ctx.position.bars_held} bars; exits at VWAP {bar.svwap:,.2f}')]
        out = []
        ready = bar.bar_pos >= self.p['min_bar_pos']
        out.append(Rule('session', bool(ready), 'far enough into the session' if ready
                        else f'too early in the session ({int(bar.bar_pos) + 1}/{self.p["min_bar_pos"]} bars)',
                        value=bar.bar_pos, threshold=self.p['min_bar_pos']))
        stretched = bar.z <= -self.p['entry_z']
        out.append(Rule('stretch', bool(stretched),
                        f'z-score {bar.z:+.2f} vs VWAP {bar.svwap:,.2f}; this strategy buys at −{self.p["entry_z"]:.1f} or lower',
                        value=bar.z, threshold=-self.p['entry_z']))
        return out
