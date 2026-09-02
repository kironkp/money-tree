"""Momentum burst — the degen lane's workhorse.

Buys a coin that just moved `min_move_pct` in `lookback` bars on a volume
surge, with a percentage stop and target and a short maximum hold. Frequent,
fast, and — by the evidence so far — not profitable: it exists so the machine
can be watched acting in real time on fake money, and so new risky ideas have
a lane to be measured in.
"""
from __future__ import annotations

import numpy as np

from .. import indicators as ind
from .base import Context, Param, Rule, Signal, Strategy


class MomentumBurst(Strategy):
    key = 'burst'
    name = 'Momentum Burst'
    description = ('Chases a sharp move (return over the last N bars above a threshold) on a volume surge; '
                   'percentage stop/target, short max hold. High frequency, high risk.')
    asset_classes = ('crypto', 'stock', 'etf')
    default_timeframe = '1Min'
    params = (
        Param('lookback', 'int', 5, 3, 15, 2, help='Bars the move is measured over'),
        Param('min_move_pct', 'float', 0.6, 0.2, 2.0, 0.2, help='Minimum move over the lookback, in %'),
        Param('min_relvol', 'float', 1.5, 0.0, 3.0, 0.5, help='Relative volume on the trigger bar'),
        Param('stop_pct', 'float', 1.0, 0.3, 3.0, 0.3, help='Stop below entry, in %'),
        Param('target_pct', 'float', 1.5, 0.5, 5.0, 0.5, help='Target above entry, in %'),
        Param('cooldown_bars', 'int', 10, 0, 60, 5, help='Bars to wait after a trade on the same symbol'),
    )
    warmup_bars = 30

    def prepare(self, df, asset_class='crypto', timeframe='1Min'):
        df = df.copy()
        session = ind.session_key(df.index, asset_class)
        df['session'] = session.astype(str)
        df['bar_pos'] = ind.bar_position(session)
        n = int(self.p['lookback'])
        df['move_pct'] = (df['close'] / df['close'].shift(n) - 1) * 100
        df['relvol'] = ind.relative_volume(df, session, 10)
        df['atr'] = ind.atr(df, 14)
        return df

    def _cooling(self, ctx: Context, bar) -> bool:
        st = self.symbol_state(ctx.symbol)
        last = st.get('last_trade_bar')
        return last is not None and (int(bar.bar_pos) - last) < self.p['cooldown_bars'] and st.get('last_session') == bar.session

    def on_bar(self, ctx: Context, bar, df, i) -> list[Signal]:
        if ctx.position is not None or np.isnan(bar.move_pct):
            return []
        if self._cooling(ctx, bar):
            return []
        if bar.move_pct >= self.p['min_move_pct'] and bar.relvol >= self.p['min_relvol']:
            st = self.symbol_state(ctx.symbol)
            st['last_trade_bar'], st['last_session'] = int(bar.bar_pos), bar.session
            price = float(bar.close)
            return [Signal('buy', ctx.symbol, ctx.ts, price, price * (1 - self.p['stop_pct'] / 100),
                           price * (1 + self.p['target_pct'] / 100), strength=min(1.0, bar.move_pct / (2 * self.p['min_move_pct'])),
                           reason=f'burst +{bar.move_pct:.2f}% in {int(self.p["lookback"])} bars on {bar.relvol:.1f}× volume')]
        return []

    def rules(self, ctx: Context, bar) -> list[Rule]:
        if np.isnan(bar.move_pct):
            return [Rule('warmup', False, 'still warming up')]
        if ctx.position is not None:
            return [Rule('holding', True, f'holding from {ctx.position.avg_price:,.6g}; stop {ctx.position.stop:,.6g}, target {ctx.position.target:,.6g}')]
        out = []
        if self._cooling(ctx, bar):
            out.append(Rule('cooldown', False, 'cooling down after the last trade here'))
        moved = bar.move_pct >= self.p['min_move_pct']
        out.append(Rule('move', bool(moved), f'moved {bar.move_pct:+.2f}% over {int(self.p["lookback"])} bars (needs +{self.p["min_move_pct"]:.1f}%)',
                        value=bar.move_pct, threshold=self.p['min_move_pct']))
        vol = bar.relvol >= self.p['min_relvol']
        out.append(Rule('volume', bool(vol), f'relative volume {bar.relvol:.1f} vs {self.p["min_relvol"]:.1f} required',
                        value=bar.relvol, threshold=self.p['min_relvol']))
        return out
