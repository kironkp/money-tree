"""Shared fixtures: deterministic synthetic bars and a seeded DB."""
from datetime import UTC, date, datetime

from django.contrib.auth import get_user_model

from main_app.models import AgentConfig, Instrument, Strategy
from main_app.services.data.store import upsert_bars
from main_app.services.data.synthetic import SyntheticProvider

WEEK_START = datetime(2026, 8, 24, tzinfo=UTC)   # Mon
WEEK_END = datetime(2026, 8, 29, tzinfo=UTC)     # Sat (exclusive)


def frames_for(symbols, start=WEEK_START, end=WEEK_END, seed=3, vol=0.6, timeframe='5Min'):
    prov = SyntheticProvider(seed=seed, annual_vol=vol)
    out = {}
    for s in symbols:
        ac = 'crypto' if '/' in s else 'stock'
        out[s] = prov.get_bars(s, timeframe, start, end, ac)
    return out


def seed_db(symbols=('QQQ', 'NVDA'), with_bars=True, start=WEEK_START, end=WEEK_END):
    cfg = AgentConfig.get()
    instruments = {}
    for s in symbols:
        instruments[s] = Instrument.objects.create(symbol=s, name=s, asset_class='crypto' if '/' in s else 'stock',
                                                   qty_increment='0.0001' if '/' in s else '1')
    if with_bars:
        for s, df in frames_for(symbols, start, end).items():
            upsert_bars(instruments[s], cfg.timeframe, df, 'synthetic')
    return cfg, instruments


def make_user(staff=True):
    u = get_user_model().objects.create_user('tester', 't@example.com', 'pw-tester-1')
    u.is_staff = staff
    u.save()
    return u


def enable_strategy(key='orb', params=None, symbols=None, stage='sprout', market='stocks'):
    from main_app.services.strategies import get_strategy_class
    cls = get_strategy_class(key)
    row, _ = Strategy.objects.get_or_create(key=key, market=market, defaults={'name': cls.name, 'params': cls.defaults(),
                                                                               'timeframe': cls.default_timeframe})
    row.params = {**cls.defaults(), **(params or {})}
    row.symbols = symbols or list(Instrument.objects.values_list('symbol', flat=True))
    row.enabled = True
    row.stage = stage
    row.save()
    return row
