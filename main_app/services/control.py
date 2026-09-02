"""Operator actions that work whether or not an agent process is running."""
from __future__ import annotations

from django.utils import timezone

from main_app.models import Account, AgentConfig, AgentRun, Instrument, RiskEvent

from .broker.sim import SimBroker
from .data.store import load_frame
from .engine import Engine, EngineConfig
from .ledger import DBRecorder, hydrate_broker, persist_broker
from .risk import RiskConfig


def running_agents() -> list[AgentRun]:
    """Live agent processes (one per account), marking dead rows stopped."""
    out = []
    for run in AgentRun.objects.filter(status='running').select_related('account').order_by('-started_at'):
        if run.is_alive:
            out.append(run)
            continue
        run.status = 'stopped'
        run.stopped_at = timezone.now()
        run.message = (run.message + ' (process gone)')[:300]
        run.save(update_fields=['status', 'stopped_at', 'message'])
    return out


def running_agent(account: Account | None = None) -> AgentRun | None:
    for run in running_agents():
        if account is None or run.account_id == account.pk:
            return run
    return None


def flatten_account(mode: str, reason: str = 'manual', market: str = 'stocks') -> int:
    """Close every position on an account at the last known prices."""
    cfg = AgentConfig.get()
    account = Account.for_mode(mode, market)
    instruments = {i.symbol: i for i in Instrument.objects.filter(asset_class__in=account.asset_classes)}
    ac = {s: i.asset_class for s, i in instruments.items()}
    if mode in ('sim', 'replay'):
        broker = SimBroker(float(account.cash), immediate_fills=True, slippage_bps=float(cfg.slippage_bps), asset_classes=ac)
        hydrate_broker(account, broker)
        for symbol in list(broker.positions):
            df = load_frame(instruments[symbol], cfg.timeframe, limit=1)
            if len(df):
                broker.last_price[symbol] = float(df['close'].iloc[-1])
    else:
        from .broker.alpaca import AlpacaBroker
        broker = AlpacaBroker(paper=(mode == 'paper'), asset_classes=ac, mode_is_live=(cfg.mode == 'live'))
        hydrate_broker(account, broker)
        broker.sync()
    engine = Engine([], broker, EngineConfig(timeframe=cfg.timeframe, mode=mode, asset_classes=ac,
                                             risk=RiskConfig.from_model(cfg)), DBRecorder(account, instruments))
    n = engine.flatten_all(timezone.now(), reason)
    persist_broker(account, broker, instruments)
    RiskEvent.objects.create(account=account, kind='flatten', message=f'{reason}: closed {n} positions from the dashboard')
    return n
