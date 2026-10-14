"""VWAP mean reversion.

When price stretches more than `entry_z` standard deviations below the
session VWAP, buy and aim for a return to VWAP with an ATR stop. Mirrored
shorts optional. Works on crypto too (sessions anchored at 00:00 UTC), though
25 bps taker fees make it a hard game there — the strategy page says so.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .. import indicators as ind
from ..data.resample import resample_complete
from ..timeframes import tf_minutes
from .base import Context, Param, Rule, Signal, Strategy, entry_session_param

# The trend guard's definition, fixed rather than searched: the lane's own trend
# rule (ema_momentum's 13/27) on completed 1Hour bars, so "against the trend"
# means what the lane's trend strategy means by it.
TREND_TF, TREND_FAST, TREND_SLOW = '1Hour', 13, 27


def completed_trend(df: pd.DataFrame, base_tf: str, asset_class: str) -> pd.Series:
    """+1 / -1 / 0 per base bar: EMA13 against EMA27 of the last COMPLETE 1Hour bar
    as of that base bar's close.

    Built with the engine's own causal resample, so an hour counts only once the
    base bar that closes it has completed, and no value can change when later bars
    arrive. NaN until 27 complete hours exist, and throughout when the base is
    coarser than an hour.
    """
    out = pd.Series(np.nan, index=df.index)
    if len(df) == 0 or tf_minutes(base_tf) > tf_minutes(TREND_TF):
        return out
    hourly, emitted_at = resample_complete(df, base_tf, TREND_TF, asset_class)
    if len(hourly) == 0:
        return out
    trend = np.sign(ind.ema(hourly['close'], TREND_FAST) - ind.ema(hourly['close'], TREND_SLOW))
    trend.iloc[:TREND_SLOW] = np.nan          # an EMA seeded by a handful of hours is not a trend
    vals = [np.nan] * len(df)
    for pos, j in enumerate(emitted_at):
        if j is not None:
            vals[pos] = trend.iloc[j]
    return pd.Series(vals, index=df.index).ffill()


class VwapReversion(Strategy):
    key = 'vwap_reversion'
    name = 'VWAP Reversion'
    description = 'Fade stretches beyond k·σ from session VWAP back to VWAP; ATR stop.'
    asset_classes = ('stock', 'etf', 'crypto', 'forex')
    params = (
        Param('entry_z', 'float', 2.0, 1.0, 3.0, 0.5, help='Entry threshold in σ of (close − VWAP)'),
        Param('lookback', 'int', 30, 20, 60, 10, help='Bars for the σ estimate'),
        Param('stop_atr_mult', 'float', 1.5, 0.5, 2.5, 0.5, help='Stop distance in ATRs'),
        Param('min_bar_pos', 'int', 6, 3, 12, 3, help='Bars into the session before trading'),
        Param('max_bars_held', 'int', 24, 6, 48, 6, help='Give up after this many bars'),
        entry_session_param(),
        # Re-entry filters. Both off by default, so a row that does not name them
        # trades exactly as before, and searched only when an experiment asks.
        Param('stop_cooldown', 'bool', False, search=False,
              help='After a stop, no new entry on that symbol in that direction until the session rolls'),
        Param('trend_guard', 'bool', False, search=False,
              help='No fade against the 1Hour trend: no shorts while EMA13 > EMA27, no longs while below'),
    )
    warmup_bars = 40

    def prepare(self, df, asset_class='stock', timeframe='5Min'):
        df = df.copy()
        session = ind.session_key(df.index, asset_class)
        df['session'] = session.astype(str)
        df['bar_pos'] = ind.bar_position(session)
        df['svwap'] = ind.session_vwap(df, session)
        df['vwap_has_volume'] = ind.session_has_volume(df, session)
        dev = df['close'] - df['svwap']
        sd = dev.rolling(int(self.p['lookback'])).std()
        df['z'] = (dev / sd.replace(0, np.nan))
        df['atr'] = ind.atr(df, 14)
        if self.p['trend_guard']:
            df['trend'] = completed_trend(df, timeframe, asset_class)
        return df

    def on_trade_closed(self, trade) -> None:
        # Keyed on the stop's EXIT bar: a stop fills inside its bar, so live and a
        # backtest stamp it the same, where entries differ by one bar. The memory is
        # in-process: a backtest assumes it survives the whole run, while a live agent
        # restarted mid-day forgets that day's stops. Rebuilding it at start-up from
        # the day's stop trades is a precondition for installing this.
        if self.p['stop_cooldown'] and trade.exit_reason == 'stop':
            self.symbol_state(trade.symbol).setdefault('stopped', {})[trade.side] = trade.exit_ts

    def _held_back(self, ctx: Context, bar, side: str) -> str:
        """Why a re-entry filter refuses an entry on this side, or ''."""
        if self.p['stop_cooldown']:
            stopped = self.symbol_state(ctx.symbol).get('stopped', {}).get(side)
            if stopped is not None and str(ind.session_key(pd.DatetimeIndex([stopped]),
                                                           ctx.asset_class).iloc[0]) == bar.session:
                return f'stopped out of a {side} on this symbol earlier this session'
        if self.p['trend_guard']:
            if np.isnan(bar.trend):
                return '1Hour trend not measurable yet'
            if side == 'short' and bar.trend > 0:
                return 'would short against a rising 1Hour trend (EMA13 > EMA27)'
            if side == 'long' and bar.trend < 0:
                return 'would buy against a falling 1Hour trend (EMA13 < EMA27)'
        return ''

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
        anchor = 'VWAP' if bar.vwap_has_volume else 'equal-weighted session mean (volume unavailable)'
        if bar.z <= -self.p['entry_z'] and bar.svwap > bar.close:
            if self._held_back(ctx, bar, 'long'):
                return []
            return [Signal('buy', ctx.symbol, ctx.ts, float(bar.close), float(bar.close - risk), float(bar.svwap),
                           strength=min(1.0, abs(bar.z) / 3), reason=f'z={bar.z:.2f} below {anchor} {bar.svwap:.6g}')]
        if ctx.asset_class != 'crypto' and bar.z >= self.p['entry_z'] and bar.svwap < bar.close:
            if self._held_back(ctx, bar, 'short'):
                return []
            return [Signal('sell', ctx.symbol, ctx.ts, float(bar.close), float(bar.close + risk), float(bar.svwap),
                           strength=min(1.0, abs(bar.z) / 3), reason=f'z={bar.z:.2f} above {anchor} {bar.svwap:.6g}')]
        return []

    def rules(self, ctx: Context, bar) -> list[Rule]:
        if np.isnan(bar.z):
            return [Rule('warmup', False, 'still warming up (not enough bars for the σ estimate)')]
        if ctx.position is not None:
            anchor = 'VWAP' if bar.vwap_has_volume else 'price-only session mean (volume unavailable)'
            return [Rule('holding', True, f'holding, z {bar.z:+.2f}, {ctx.position.bars_held} bars; exits at {anchor} {bar.svwap:,.6g}')]
        out = []
        ready = bar.bar_pos >= self.p['min_bar_pos']
        out.append(Rule('session', bool(ready), 'far enough into the session' if ready
                        else f'too early in the session ({int(bar.bar_pos) + 1}/{self.p["min_bar_pos"]} bars)',
                        value=bar.bar_pos, threshold=self.p['min_bar_pos']))
        can_short = ctx.asset_class != 'crypto'
        stretched = bar.z <= -self.p['entry_z'] or (can_short and bar.z >= self.p['entry_z'])
        side_note = f'buys at −{self.p["entry_z"]:.1f} or lower' + (f', shorts at +{self.p["entry_z"]:.1f} or higher' if can_short else '')
        anchor = 'VWAP' if bar.vwap_has_volume else 'price-only session mean (volume unavailable)'
        out.append(Rule('stretch', bool(stretched), f'z-score {bar.z:+.2f} vs {anchor} {bar.svwap:,.6g}; {side_note}',
                        value=bar.z, threshold=-self.p['entry_z']))
        if self.p['stop_cooldown'] or self.p['trend_guard']:
            side = 'long' if bar.z < 0 else 'short'
            held = self._held_back(ctx, bar, side)
            out.append(Rule('filters', not held, held or f're-entry filters clear for a {side}'))
        return out
