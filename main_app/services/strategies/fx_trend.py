"""Time-series momentum on FX majors, held for days rather than hours.

TESTED AND NOT QUALIFIED. Train gross profit factor 1.15, held-out 1.00. Kept in
the tree because the hypothesis is sound, the implementation is correct, and the
next person to have this idea should find the measurement rather than repeat the
search. See Hypothesis #4 and docs/forex-research-log.md.

The reasoning that produced it:

The whole diagnosis so far is that a flat ~1.6 bps round-trip toll is the same
size as the move these strategies capture. Filters could not fix that because
they change WHICH small move you take, not how big it is.

Holding longer changes the arithmetic instead of the selection. Capture a 3-day
trend worth 80 bps and the same 1.6 bps toll is 2% of the move rather than 100%
of it. Time-series momentum is also the most robustly documented anomaly across
asset classes, FX majors included (Moskowitz, Ooi & Pedersen, "Time Series
Momentum", JFE 2012) — which makes it a prior worth testing rather than a shape
found by searching.

Signal: the sign of the return over `lookback_h` hours, taken only when that
move is large relative to ATR so a flat market does not generate churn. Stop is
wide because a multi-day trend must be allowed to breathe. No profit target:
the exit is the stop or the clock, because capping the winner is exactly how a
trend strategy loses its reason to exist.
"""
from __future__ import annotations

import numpy as np

from .. import indicators as ind
from .base import Context, Param, Rule, Signal, Strategy


class FxTrend(Strategy):
    key = 'fx_trend'
    name = 'FX Trend'
    description = 'Time-series momentum on USD majors, held for days. Wide ATR stop, no target.'
    asset_classes = ('forex',)
    params = (
        Param('lookback_h', 'int', 240, 48, 720, 48, help='Hours of return that define the trend'),
        Param('min_move_atr', 'float', 1.0, 0.0, 4.0, 0.5, help='Trend must be this many ATRs to count'),
        Param('stop_atr_mult', 'float', 3.0, 1.0, 8.0, 1.0, help='Stop distance in ATRs'),
        Param('atr_len', 'int', 24, 12, 96, 12, help='ATR length in bars'),
        Param('cooldown_h', 'int', 24, 0, 168, 24, help='Hours before re-entering the same symbol'),
        Param('allow_short', 'bool', True, help='Take downtrends as well as uptrends'),
    )
    warmup_bars = 760
    intraday = True     # the engine still flattens at the forex week close

    def prepare(self, df, asset_class='forex', timeframe='1Hour'):
        df = df.copy()
        session = ind.session_key(df.index, asset_class)
        df['session'] = session.astype(str)
        df['bar_pos'] = ind.bar_position(session)
        lb = int(self.p['lookback_h'])
        df['atr'] = ind.atr(df, int(self.p['atr_len']))
        df['trend'] = df['close'] - df['close'].shift(lb)
        df['trend_atr'] = df['trend'] / df['atr'].replace(0, np.nan)
        return df

    def on_bar(self, ctx: Context, bar, df, i) -> list[Signal]:
        if ctx.position is not None:
            return []
        st = self.symbol_state(ctx.symbol)
        cool = int(self.p['cooldown_h'])
        last = st.get('last_exit_bar')
        if last is not None and cool and (i - last) < cool:
            return []
        atr = float(getattr(bar, 'atr', float('nan')) or float('nan'))
        ta = float(getattr(bar, 'trend_atr', float('nan')) or float('nan'))
        if not np.isfinite(atr) or atr <= 0 or not np.isfinite(ta):
            return []
        need = float(self.p['min_move_atr'])
        risk = float(self.p['stop_atr_mult']) * atr
        px = float(bar.close)
        if ta >= need:
            st['last_exit_bar'] = i
            return [Signal('buy', ctx.symbol, ctx.ts, px, px - risk, None,
                           reason=f'uptrend {ta:.1f} ATR over {int(self.p["lookback_h"])}h')]
        if self.p['allow_short'] and ta <= -need:
            st['last_exit_bar'] = i
            return [Signal('sell', ctx.symbol, ctx.ts, px, px + risk, None,
                           reason=f'downtrend {ta:.1f} ATR over {int(self.p["lookback_h"])}h')]
        return []

    def on_session_end(self, symbol: str) -> None:
        """Keep the cooldown across the day boundary.

        The base class clears a strategy's state at every session end, and a
        forex session is one day, so `cooldown_h` was wiped nightly and any value
        above 24 was silently identical to 24. Four different cooldowns produced
        byte-identical backtests, which is what exposed it. A cooldown that
        cannot outlive a day is not a cooldown.
        """

    def rules(self, ctx: Context, bar) -> list[Rule]:
        ta = float(getattr(bar, 'trend_atr', float('nan')) or float('nan'))
        need = float(self.p['min_move_atr'])
        ok = np.isfinite(ta) and abs(ta) >= need
        return [Rule('trend', bool(ok),
                     f'{int(self.p["lookback_h"])}h move {ta:+.1f} ATR vs {need:g} needed'
                     if np.isfinite(ta) else 'trend still forming',
                     value=None if not np.isfinite(ta) else ta, threshold=need)]

