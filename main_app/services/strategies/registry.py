from .burst import MomentumBurst
from .ema_momentum import EmaMomentum
from .fx_trend import FxTrend
from .news_catalyst import NewsCatalyst
from .orb import OpeningRangeBreakout
from .swing_trend import SwingTrend
from .vwap_reversion import VwapReversion

STRATEGIES = {cls.key: cls for cls in (OpeningRangeBreakout, VwapReversion, EmaMomentum, MomentumBurst,
                                      NewsCatalyst, FxTrend, SwingTrend)}


def get_strategy_class(key: str):
    try:
        return STRATEGIES[key]
    except KeyError:
        raise KeyError(f'unknown strategy {key!r}; known: {", ".join(STRATEGIES)}') from None


def make_strategy(key: str, params: dict | None = None):
    return get_strategy_class(key)(params)


def all_strategies():
    return list(STRATEGIES.values())
