"""Trade what the News Agent decided.

This is the arm, not the brain. The News Agent scores a story out of ten every
four hours; anything at or above its threshold becomes a standing instruction
for one symbol. This strategy is what the lane's trading loop consults on each
bar: if there is a live instruction for this symbol, it emits the corresponding
signal and marks the instruction used.

Everything downstream is unchanged — the risk manager still sizes it, still
refuses it if the lane is halted or the cost gate fails, and the simulator still
fills it. A news trade is an ordinary trade with an unusual reason.

LIVE ONLY. `self.live` is set by the agent and never in a backtest or replay,
because the verdict table is written in the present: letting a backtest read it
would be looking up the answers. A backtest of this strategy therefore does
nothing at all, which is the honest result rather than a flattering one.
"""
from __future__ import annotations

import numpy as np

from .. import indicators as ind
from .base import Context, Param, Rule, Signal, Strategy


class NewsCatalyst(Strategy):
    key = 'news_catalyst'
    name = 'News Catalyst'
    description = ("Acts on the News Agent's verdicts: a story scored at or above the threshold "
                   "becomes a position, sized by the usual risk rules.")
    asset_classes = ('stock', 'etf', 'crypto', 'forex')
    params = (
        Param('min_score', 'int', 5, 4, 9, 1, help='Lowest verdict score that may open a position'),
        Param('stop_atr_mult', 'float', 1.5, 0.5, 3.0, 0.5, help='Stop distance in ATRs'),
        Param('rr', 'float', 2.0, 1.0, 4.0, 0.5, help='Target as a multiple of risk'),
    )
    warmup_bars = 20
    live = False          # the agent sets this; backtests must never see verdicts

    def prepare(self, df, asset_class='stock', timeframe='5Min'):
        df = df.copy()
        session = ind.session_key(df.index, asset_class)
        df['session'] = session.astype(str)
        df['bar_pos'] = ind.bar_position(session)
        df['atr'] = ind.atr(df, 14)
        return df

    def _verdict(self, symbol):
        if not self.live:
            return None
        from ..news_agent import pending_for
        try:
            return pending_for(symbol)
        except Exception:
            return None

    def on_bar(self, ctx: Context, bar, df, i) -> list[Signal]:
        if ctx.position is not None:
            return []                      # the exit is the stop, the target or max hold
        if np.isnan(bar.atr) or bar.atr <= 0:
            return []
        v = self._verdict(ctx.symbol)
        if v is None or v.score < int(self.p['min_score']):
            return []

        from ..news_agent import mark_acted
        price = float(bar.close)
        risk = float(self.p['stop_atr_mult']) * float(bar.atr)
        rr = float(self.p['rr'])
        reason = f'news {v.score}/10: {v.thesis[:120]}'
        # One verdict, one order: claim it before the signal can be acted on twice.
        mark_acted(v)
        if v.direction == 'buy':
            return [Signal('buy', ctx.symbol, ctx.ts, price, price - risk, price + rr * risk,
                           strength=min(1.0, v.score / 10), reason=reason)]
        if v.direction == 'short' and ctx.asset_class not in ('crypto',):
            return [Signal('sell', ctx.symbol, ctx.ts, price, price + risk, price - rr * risk,
                           strength=min(1.0, v.score / 10), reason=reason)]
        return []

    def rules(self, ctx: Context, bar) -> list[Rule]:
        if not self.live:
            return [Rule('live', False, 'news verdicts are only read by a live agent')]
        v = self._verdict(ctx.symbol)
        if ctx.position is not None:
            return [Rule('holding', True, 'holding a news position; exits on stop, target or max hold')]
        if v is None:
            return [Rule('verdict', False, 'no live News Agent instruction for this symbol',
                         value=0, threshold=float(self.p['min_score']))]
        ok = v.score >= int(self.p['min_score'])
        return [Rule('verdict', bool(ok),
                     f'News Agent scored this {v.score}/10 → {v.call_text}'
                     + ('' if ok else f' (needs {int(self.p["min_score"])})'),
                     value=float(v.score), threshold=float(self.p['min_score']))]

    def explain(self, ctx: Context, bar) -> str:
        v = self._verdict(ctx.symbol)
        return f'news instruction {v.score}/10 {v.call_text}' if v else 'no news instruction'
