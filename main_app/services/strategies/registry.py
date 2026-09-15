from .burst import MomentumBurst
from .ema_momentum import EmaMomentum
from .news_catalyst import NewsCatalyst
from .orb import OpeningRangeBreakout
from .vwap_reversion import VwapReversion

STRATEGIES = {cls.key: cls for cls in (OpeningRangeBreakout, VwapReversion, EmaMomentum, MomentumBurst,
                                      NewsCatalyst)}


def get_strategy_class(key: str):
    try:
        return STRATEGIES[key]
    except KeyError:
        raise KeyError(f'unknown strategy {key!r}; known: {", ".join(STRATEGIES)}') from None


def make_strategy(key: str, params: dict | None = None):
    return get_strategy_class(key)(params)


def all_strategies():
    return list(STRATEGIES.values())
