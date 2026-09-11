"""The risk manager: every entry passes through here, and every 'no' is
recorded with its reason. Pure Python so the backtester shares it."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .broker.sim import round_qty
from .strategies.base import Context, Signal


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.5
    max_position_pct: float = 20.0
    max_open_positions: int = 4
    max_daily_loss_pct: float = 2.0
    max_trades_per_day: int = 12
    no_entries_before_close_min: int = 30
    flat_before_close_min: int = 5
    allow_short: bool = True  # direction is the strategy's decision; only the venue can veto (crypto spot)
    max_hold_minutes: int = 240
    slippage_bps: float = 3.0
    default_stop_pct: float = 2.0  # when a signal carries no stop
    # Round-trip cost gate: |target − entry| / entry must be ≥ min_reward_to_cost ×
    # (2 × fee + 2 × slippage). Below that the trade cannot pay for itself.
    min_reward_to_cost: float = 3.0
    fee_bps: dict = field(default_factory=lambda: {'stock': 0.5, 'etf': 0.5, 'crypto': 25.0, 'forex': 0.5})
    # Buying power as a multiple of equity. 1 = cash account (stocks, crypto);
    # forex is traded on margin, so the simulator lends like a broker would.
    leverage: float = 1.0
    # Gross notional allowed in either direction, as a percentage of equity.
    # Zero disables the cap. The Forex lane uses this because every supported
    # pair carries the same USD factor even though the symbols differ.
    max_directional_exposure_pct: float = 0.0
    # Do not turn the last scraps of capacity into statistical noise. A trade
    # must receive at least this share of the size implied by risk/allocation.
    min_entry_size_pct: float = 10.0

    @classmethod
    def from_model(cls, cfg, market: str = 'stocks') -> 'RiskConfig':
        rc = cls(
            risk_per_trade_pct=float(cfg.risk_per_trade_pct), max_position_pct=float(cfg.max_position_pct),
            max_open_positions=int(cfg.max_open_positions), max_daily_loss_pct=float(cfg.max_daily_loss_pct),
            max_trades_per_day=int(cfg.max_trades_per_day),
            no_entries_before_close_min=int(cfg.no_entries_before_close_min),
            flat_before_close_min=int(cfg.flat_before_close_min), allow_short=True,
            max_hold_minutes=int(cfg.max_hold_minutes), slippage_bps=float(cfg.slippage_bps),
            min_reward_to_cost=float(cfg.min_reward_to_cost),
            fee_bps=cfg.fee_bps(),
        )
        if market == 'forex':
            # Margin lane: positions may exceed the account, the cost model is a
            # spread, the day rolls at the New York close, and positions age out.
            rc.leverage = float(cfg.forex_leverage)
            rc.risk_per_trade_pct = float(cfg.forex_risk_per_trade_pct)
            rc.max_position_pct = float(cfg.forex_max_position_pct)
            rc.max_directional_exposure_pct = float(cfg.forex_max_directional_exposure_pct)
            rc.max_open_positions = int(cfg.forex_max_open_positions)
            rc.max_daily_loss_pct = float(cfg.forex_max_daily_loss_pct)
            rc.max_trades_per_day = int(cfg.forex_max_trades_per_day)
            rc.max_hold_minutes = int(cfg.forex_max_hold_minutes)
            rc.min_reward_to_cost = float(cfg.forex_min_reward_to_cost)
            rc.slippage_bps = float(cfg.forex_slippage_bps)
        if market == 'degen':
            # The high-risk sandbox: bigger bets, more of them, a looser cost gate,
            # a wider daily loss budget. Fake money, and it says so on the screen.
            rc.risk_per_trade_pct = float(cfg.degen_risk_per_trade_pct)
            rc.max_position_pct = float(cfg.degen_max_position_pct)
            rc.max_open_positions = int(cfg.degen_max_open_positions)
            rc.max_daily_loss_pct = float(cfg.degen_max_daily_loss_pct)
            rc.max_trades_per_day = int(cfg.degen_max_trades_per_day)
            rc.max_hold_minutes = int(cfg.degen_max_hold_minutes)
            rc.min_reward_to_cost = float(cfg.degen_min_reward_to_cost)
            rc.max_directional_exposure_pct = float(cfg.degen_max_directional_exposure_pct)
        return rc

    def round_trip_cost_pct(self, asset_class: str) -> float:
        return 2 * (self.fee_bps.get(asset_class, 0.5) + self.slippage_bps) / 1e4 * 100

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Decision:
    allowed: bool
    qty: float = 0.0
    reason: str = ''


@dataclass
class DayState:
    date: object = None
    start_equity: float = 0.0
    entries: int = 0
    halted: bool = False
    halted_reason: str = ''


class RiskManager:
    def __init__(self, cfg: RiskConfig, qty_increments: dict | None = None, news_aware: bool = False):
        self.cfg = cfg
        # Live lanes consult the news; backtests and replays must not, or the
        # past would be judged with information the past did not have.
        self.news_aware = news_aware
        self.qty_increments = qty_increments or {}
        self.day = DayState()
        self.kill_switch = False
        self.trading_enabled = True
        # Named blockers set by the agent (reconciliation diverged, data stale, …).
        self.blocks: dict[str, str] = {}

    # --- day tracking -----------------------------------------------------
    def new_day(self, date, equity: float) -> None:
        self.day = DayState(date=date, start_equity=equity)

    def restore(self, date, start_equity: float, entries: int = 0, halted: bool = False, reason: str = '') -> None:
        """Pick the day up where a previous process left it."""
        self.day = DayState(date=date, start_equity=start_equity, entries=entries, halted=halted, halted_reason=reason)

    def record_entry(self) -> None:
        self.day.entries += 1

    def day_pnl(self, equity: float) -> float:
        return equity - self.day.start_equity if self.day.start_equity else 0.0

    def daily_loss_breached(self, equity: float) -> bool:
        if not self.day.start_equity:
            return False
        limit = self.day.start_equity * self.cfg.max_daily_loss_pct / 100.0
        return self.day_pnl(equity) <= -limit

    def daily_loss_used_pct(self, equity: float) -> float:
        if not self.day.start_equity or self.cfg.max_daily_loss_pct <= 0:
            return 0.0
        loss = -min(0.0, self.day_pnl(equity))
        limit = self.day.start_equity * self.cfg.max_daily_loss_pct / 100.0
        return min(100.0, loss / limit * 100.0) if limit else 0.0

    def halt(self, reason: str) -> None:
        self.day.halted = True
        self.day.halted_reason = reason

    # --- the gate ---------------------------------------------------------
    def evaluate(self, sig: Signal, ctx: Context, account, positions: dict, asset_class: str,
                 strategy_supports: bool = True, allocation_pct: float = 100.0, strategy_exposure: float = 0.0,
                 pending_positions: int = 0, pending_exposure: float = 0.0,
                 pending_directional_exposure: float = 0.0,
                 pending_symbols: set[str] | None = None) -> Decision:
        c = self.cfg
        if self.kill_switch:
            return Decision(False, reason='kill switch is on')
        if not self.trading_enabled:
            return Decision(False, reason='trading disabled in settings')
        for reason in self.blocks.values():
            return Decision(False, reason=reason)
        if self.day.halted:
            return Decision(False, reason=f'halted for the day: {self.day.halted_reason}')
        if not strategy_supports:
            return Decision(False, reason=f'strategy does not trade {asset_class}')
        if sig.action == 'sell' and asset_class == 'crypto':
            return Decision(False, reason='crypto cannot be shorted')
        if sig.symbol in positions and positions[sig.symbol].qty != 0:
            return Decision(False, reason='already in a position')
        if self.news_aware:
            # Confirmed, market-moving story on this symbol in the last 45 min:
            # stand aside rather than pay spread into a repricing.
            try:
                from .news import entry_block
                why = entry_block(sig.symbol)
            except Exception:
                why = ''
            if why:
                return Decision(False, reason=why)
        if sig.symbol in (pending_symbols or set()):
            return Decision(False, reason='an entry is already pending for this symbol')
        live = [p for p in positions.values() if p.qty != 0]
        if len(live) + pending_positions >= c.max_open_positions:
            return Decision(False, reason=f'max open positions ({c.max_open_positions})')
        if self.day.entries >= c.max_trades_per_day:
            return Decision(False, reason=f'max trades per day ({c.max_trades_per_day})')
        if ctx.minutes_to_close is not None and ctx.minutes_to_close <= c.no_entries_before_close_min:
            return Decision(False, reason=f'inside the last {c.no_entries_before_close_min} min of the session')
        if self.daily_loss_breached(account.equity):
            self.halt('daily loss limit')
            return Decision(False, reason='daily loss limit reached')
        price = float(sig.price)
        if price <= 0 or math.isnan(price):
            return Decision(False, reason='no price')
        if sig.target is not None and c.min_reward_to_cost > 0:
            reward_pct = abs(float(sig.target) - price) / price * 100
            cost_pct = c.round_trip_cost_pct(asset_class)
            if reward_pct < c.min_reward_to_cost * cost_pct:
                return Decision(False, reason=f'target {reward_pct:.2f}% < {c.min_reward_to_cost:g}× round-trip cost {cost_pct:.2f}%')
        stop = sig.stop
        stop_dist = abs(price - stop) if stop else price * c.default_stop_pct / 100.0
        if stop_dist <= 0:
            return Decision(False, reason='stop equals entry')
        equity = account.equity
        risk_dollars = equity * c.risk_per_trade_pct / 100.0
        qty_risk = risk_dollars / stop_dist
        qty_cap = equity * c.max_position_pct / 100.0 / price
        desired_qty = min(qty_risk, qty_cap)
        if allocation_pct < 100:
            room = equity * allocation_pct / 100.0 - strategy_exposure
            if room <= 0:
                return Decision(False, reason=f'strategy allocation ({allocation_pct:g}% of equity) is fully used')
            desired_qty = min(desired_qty, room / price)
        available_buying_power = max(0.0, account.buying_power - pending_exposure)
        qty_cash = available_buying_power / (price * (1 + c.slippage_bps / 1e4))
        qty = min(desired_qty, qty_cash)
        if c.max_directional_exposure_pct > 0:
            direction = 1 if sig.action == 'buy' else -1
            same_direction = sum(
                abs(p.market_value()) for p in positions.values()
                if p.qty and (1 if p.qty > 0 else -1) == direction
            ) + pending_directional_exposure
            directional_room = equity * c.max_directional_exposure_pct / 100.0 - same_direction
            if directional_room <= 0:
                return Decision(False, reason=(
                    f'directional exposure cap reached ({c.max_directional_exposure_pct:g}% of equity)'
                ))
            qty = min(qty, directional_room / price)
        if desired_qty > 0 and qty < desired_qty * c.min_entry_size_pct / 100.0:
            return Decision(False, reason=(
                f'remaining capacity would create an undersized position '
                f'(<{c.min_entry_size_pct:g}% of planned size)'
            ))
        inc = float(self.qty_increments.get(sig.symbol, 0.0001 if asset_class == 'crypto' else 1.0))
        qty = round_qty(qty, inc)
        if qty <= 0:
            if qty_cash < inc:
                return Decision(False, reason='insufficient cash' if c.leverage <= 1
                                else f'buying power used up ({c.leverage:g}× leverage, open positions count against it)')
            return Decision(False, reason='price exceeds position cap (0 shares)')
        return Decision(True, qty=qty, reason=f'risk ${risk_dollars:.0f} / stop {stop_dist:,.6g}')
