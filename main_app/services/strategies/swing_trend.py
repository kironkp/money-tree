"""Multi-day momentum on equities, held across sessions.

Chosen by measurement rather than hunch. The median absolute move, divided by
the lane's round-trip toll, at each horizon:

                toll     1h     4h    24h   120h   480h
    forex       1.6b    3.2    6.7   19.0   44.5   87.9
    stocks      7.0b    4.2   11.0   31.0   69.0  187.1
    crypto     56.0b    0.5    1.0    2.7    6.4   15.7
    degen      56.0b    0.7    1.3    3.9    8.8   17.2

Two things fall out. Crypto and degen cannot work intraday at all — at one hour
the typical move does not even pay the toll, which is the whole explanation for
the degen lane's -$3,288. And the horizon effect is worth 25-45x in every lane,
far more than any filter or parameter has ever been worth here.

Equities at multi-day horizons is where those two facts point: the best ratio on
the board, and a five-day move worth sixty-nine tolls instead of the two or three
that intraday forex offers. Nothing about the signal is clever. The entire thesis
is that the toll stops mattering when the move is large enough, and this is the
place where the move is largest relative to the toll.

Requires flatten_intraday=False, since the engine otherwise closes equity
positions at the bell and the whole point is to hold through it.
"""
from __future__ import annotations

import numpy as np

from .. import indicators as ind
from .base import Context, Param, Rule, Signal, Strategy


class SwingTrend(Strategy):
    key = 'swing_trend'
    name = 'Swing Trend'
    description = 'Multi-day momentum on equities; wide ATR stop, no target, held across sessions.'
    asset_classes = ('stock', 'etf')
    params = (
        Param('lookback_bars', 'int', 780, 78, 3120, 78, help='Bars of return defining the trend'),
        Param('min_move_atr', 'float', 1.0, 0.0, 5.0, 0.5, help='Trend must be this many ATRs'),
        Param('stop_atr_mult', 'float', 4.0, 1.0, 10.0, 1.0, help='Stop distance in ATRs'),
        Param('atr_len', 'int', 78, 14, 390, 26, help='ATR length in bars'),
        Param('cooldown_bars', 'int', 78, 0, 780, 78, help='Bars before re-entering a symbol'),
        Param('allow_short', 'bool', True, help='Take downtrends too'),
        # Sign of the bet. Momentum is the famous effect but it lives at 3-12
        # MONTH horizons; at the daily-to-weekly horizon traded here the
        # documented effect is short-term REVERSAL (Jegadeesh 1990, Lehmann
        # 1990), and the first run of this strategy said the same thing loudly:
        # momentum came back at gross profit factor 0.58-0.93, which is a signal
        # that is right about the size of the move and wrong about its sign.
        Param('mode', 'choice', 'reversal', choices=('momentum', 'reversal'),
              help='Bet with the recent move or against it'),
    )
    warmup_bars = 900
    intraday = False        # the position is meant to survive the close

    def __init__(self, params=None):
        super().__init__(params)
        # The warm-up is a property of the parameters, not a constant. A fixed
        # 900 is right for 5-minute bars and absurd for daily ones, where it
        # would silently discard three and a half years before the strategy was
        # allowed to act.
        self.warmup_bars = int(self.p['lookback_bars']) + int(self.p['atr_len']) + 10

    def prepare(self, df, asset_class='stock', timeframe='5Min'):
        df = df.copy()
        session = ind.session_key(df.index, asset_class)
        df['session'] = session.astype(str)
        df['bar_pos'] = ind.bar_position(session)
        df['atr'] = ind.atr(df, int(self.p['atr_len']))
        df['trend'] = df['close'] - df['close'].shift(int(self.p['lookback_bars']))
        df['trend_atr'] = df['trend'] / df['atr'].replace(0, np.nan)
        return df

    def on_bar(self, ctx: Context, bar, df, i) -> list[Signal]:
        if ctx.position is not None:
            return []
        st = self.symbol_state(ctx.symbol)
        cool = int(self.p['cooldown_bars'])
        last = st.get('last_entry_bar')
        if last is not None and cool and (i - last) < cool:
            return []
        atr = float(getattr(bar, 'atr', float('nan')) or float('nan'))
        ta = float(getattr(bar, 'trend_atr', float('nan')) or float('nan'))
        if not np.isfinite(atr) or atr <= 0 or not np.isfinite(ta):
            return []
        need = float(self.p['min_move_atr'])
        risk = float(self.p['stop_atr_mult']) * atr
        px = float(bar.close)
        rev = self.p.get('mode', 'reversal') == 'reversal'
        stretched_up, stretched_down = ta >= need, ta <= -need
        buy = stretched_down if rev else stretched_up
        sell = stretched_up if rev else stretched_down
        word = 'stretched' if rev else 'trend'
        if buy:
            st['last_entry_bar'] = i
            return [Signal('buy', ctx.symbol, ctx.ts, px, px - risk, None,
                           reason=f'{word} {ta:+.1f} ATR — long')]
        if self.p['allow_short'] and sell:
            st['last_entry_bar'] = i
            return [Signal('sell', ctx.symbol, ctx.ts, px, px + risk, None,
                           reason=f'{word} {ta:+.1f} ATR — short')]
        return []

    def on_session_end(self, symbol: str) -> None:
        """Deliberately does NOT clear state: the whole point is to hold across
        sessions, and forgetting the cooldown at every bell would re-enter daily."""

    def rules(self, ctx: Context, bar) -> list[Rule]:
        ta = float(getattr(bar, 'trend_atr', float('nan')) or float('nan'))
        need = float(self.p['min_move_atr'])
        ok = np.isfinite(ta) and abs(ta) >= need
        return [Rule('trend', bool(ok),
                     f'move {ta:+.1f} ATR vs {need:g} needed' if np.isfinite(ta) else 'forming',
                     value=None if not np.isfinite(ta) else ta, threshold=need)]
