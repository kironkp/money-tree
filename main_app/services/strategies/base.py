"""Strategy contract.

A strategy is a pure function of prepared bars plus a little per-symbol state.
`prepare()` adds indicator columns (causal only); `on_bar()` is called once per
completed bar and returns Signals. Order handling, stops/targets, sizing and
time exits are the engine's job, so strategies stay small and testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class Param:
    name: str
    type: str = 'float'  # int float bool choice
    default: Any = 0
    min: Any = None
    max: Any = None
    step: Any = None
    choices: tuple = ()
    help: str = ''

    def coerce(self, value):
        if self.type == 'int':
            return int(round(float(value)))
        if self.type == 'float':
            return float(value)
        if self.type == 'bool':
            if isinstance(value, str):
                return value.lower() in ('1', 'true', 'on', 'yes')
            return bool(value)
        return value

    def grid(self, max_points: int = 5) -> list:
        """Evenly spaced candidate values for the optimizer."""
        if self.type == 'bool':
            return [False, True]
        if self.type == 'choice':
            return list(self.choices)
        if self.min is None or self.max is None:
            return [self.default]
        step = self.step or ((self.max - self.min) / max(1, max_points - 1))
        vals = []
        v = self.min
        while v <= self.max + 1e-9:
            vals.append(self.coerce(v))
            v += step
        if len(vals) > max_points:
            idx = [round(i * (len(vals) - 1) / (max_points - 1)) for i in range(max_points)]
            vals = [vals[i] for i in idx]
        return sorted(set(vals), key=lambda x: (x is None, x))

    def as_dict(self) -> dict:
        return {'name': self.name, 'type': self.type, 'default': self.default, 'min': self.min,
                'max': self.max, 'step': self.step, 'choices': list(self.choices), 'help': self.help}


@dataclass
class Signal:
    action: str  # buy (open long) | sell (open short) | close (exit)
    symbol: str
    ts: datetime
    price: float
    stop: float | None = None
    target: float | None = None
    strength: float = 1.0
    reason: str = ''


@dataclass
class PositionView:
    qty: float
    avg_price: float
    entry_ts: datetime
    bars_held: int = 0
    stop: float | None = None
    target: float | None = None

    @property
    def side(self) -> str:
        return 'short' if self.qty < 0 else 'long'


@dataclass
class Context:
    symbol: str
    asset_class: str
    timeframe: str
    ts: datetime
    position: PositionView | None = None
    bar_pos: int = 0                 # bar number within the session
    minutes_to_close: float | None = None  # None for crypto
    extra: dict = field(default_factory=dict)


class Strategy:
    key = 'base'
    name = 'Base'
    description = ''
    asset_classes = ('stock', 'etf', 'crypto')
    default_timeframe = '5Min'
    params: tuple[Param, ...] = ()
    warmup_bars = 30           # bars needed before on_bar may emit
    intraday = True            # engine flattens at the close

    def __init__(self, params: dict | None = None):
        self.p = self.defaults()
        for k, v in (params or {}).items():
            spec = self.param_map().get(k)
            if spec is not None:
                self.p[k] = spec.coerce(v)
        self.state: dict[str, dict] = {}

    @classmethod
    def param_map(cls) -> dict[str, Param]:
        return {p.name: p for p in cls.params}

    @classmethod
    def defaults(cls) -> dict:
        return {p.name: p.default for p in cls.params}

    @classmethod
    def schema(cls) -> list[dict]:
        return [p.as_dict() for p in cls.params]

    @classmethod
    def supports(cls, asset_class: str) -> bool:
        return asset_class in cls.asset_classes

    def symbol_state(self, symbol: str) -> dict:
        return self.state.setdefault(symbol, {})

    # --- the two methods a concrete strategy implements -----------------
    def prepare(self, df: pd.DataFrame, asset_class: str = 'stock', timeframe: str = '5Min') -> pd.DataFrame:
        raise NotImplementedError

    def on_bar(self, ctx: Context, bar, df: pd.DataFrame, i: int) -> list[Signal]:
        raise NotImplementedError

    def on_session_end(self, symbol: str) -> None:
        self.state.pop(symbol, None)
