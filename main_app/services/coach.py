"""The coach: a Claude review of recent sessions that ends in concrete,
runnable experiments. It reads evidence (trades, blocks, drift, baselines);
it never predicts prices. Dormant without ANTHROPIC_API_KEY."""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from main_app.models import Account, AgentConfig, ApiUsage, JournalEntry, Strategy

from .journal import day_summary
from .promotion import baseline_metrics, graduation_checklist
from .strategies import get_strategy_class

log = logging.getLogger('moneytree.coach')

# $ per million tokens (input, output). Used only for the usage ledger.
PRICING = {'claude-opus-5': (5.0, 25.0), 'claude-sonnet-5': (2.0, 10.0), 'claude-haiku-4-5': (1.0, 5.0),
           'claude-fable-5-1': (10.0, 50.0), 'claude-opus-4-8': (5.0, 25.0)}

SYSTEM = """You are the trading coach for MoneyTree, a day-trading agent that trades fake currency (and later real
money) with rule-based intraday strategies: opening range breakout, VWAP reversion, EMA momentum. You review
evidence — closed trades, blocked signals, risk events, drift versus backtests — and propose experiments the
operator can run with one click. Rules:
- Never predict prices or recommend specific trades. Reason from the statistics you are given.
- Prefer fewer, better-founded observations. Say when the sample is too small to conclude anything.
- Proposals must be parameter searches on the listed strategies using ONLY parameters from the schema, with values
  inside the documented min/max. 2–4 values per parameter, at most 3 parameters per proposal.
- Flag anything that looks like overfitting, fee/slippage burden (crypto pays 25 bps taker), or a risk limit doing
  more work than the strategy.
Respond in JSON matching the schema."""

SCHEMA = {
    'type': 'object',
    'properties': {
        'summary': {'type': 'string'},
        'observations': {'type': 'array', 'items': {'type': 'string'}},
        'hypotheses': {'type': 'array', 'items': {'type': 'string'}},
        'risk_flags': {'type': 'array', 'items': {'type': 'string'}},
        'proposals': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'title': {'type': 'string'},
                    'strategy_key': {'type': 'string'},
                    'method': {'type': 'string', 'enum': ['grid', 'random', 'walk_forward']},
                    'param_grid': {'type': 'object', 'additionalProperties': {'type': 'array', 'items': {'type': ['number', 'string', 'boolean']}}},
                    'rationale': {'type': 'string'},
                },
                'required': ['title', 'strategy_key', 'method', 'param_grid', 'rationale'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['summary', 'observations', 'hypotheses', 'risk_flags', 'proposals'],
    'additionalProperties': False,
}


def build_context(account: Account, d: date, days: int = 10) -> dict:
    cfg = AgentConfig.get()
    recent = []
    for entry in JournalEntry.objects.filter(account=account, kind='auto_eod', date__lte=d).order_by('-date')[:days]:
        m = entry.metrics or {}
        recent.append({k: m.get(k) for k in ('date', 'trades', 'net_pnl', 'win_rate', 'profit_factor', 'expectancy',
                                             'blocked', 'exit_reasons', 'per_strategy', 'slippage_median_bps', 'risk_events')})
    strategies = []
    for row in Strategy.objects.filter(market=account.market):
        cls = get_strategy_class(row.key)
        check = graduation_checklist(row, account, cfg)
        strategies.append({
            'key': row.key, 'market': row.market, 'name': row.name, 'enabled': row.enabled, 'stage': row.stage, 'version': row.version,
            'params': row.params, 'schema': cls.schema(), 'asset_classes': list(cls.asset_classes),
            'symbols': row.symbols, 'baseline_backtest': baseline_metrics(row), 'live_stats': check['stats'],
        })
    return {
        'today': day_summary(account, d), 'recent_days': recent, 'strategies': strategies,
        'risk_limits': {'risk_per_trade_pct': float(cfg.risk_per_trade_pct), 'max_position_pct': float(cfg.max_position_pct),
                        'max_open_positions': cfg.max_open_positions, 'max_daily_loss_pct': float(cfg.max_daily_loss_pct),
                        'max_trades_per_day': cfg.max_trades_per_day, 'allow_short': cfg.allow_short,
                        'slippage_bps': float(cfg.slippage_bps), 'timeframe': cfg.timeframe},
        'account': {'mode': account.mode, 'starting_cash': float(account.starting_cash), 'equity': float(account.equity)},
    }


def _record_usage(model: str, usage, purpose: str = 'coach') -> None:
    inp = getattr(usage, 'input_tokens', 0) or 0
    out = getattr(usage, 'output_tokens', 0) or 0
    cache_read = getattr(usage, 'cache_read_input_tokens', 0) or 0
    pin, pout = PRICING.get(model, (5.0, 25.0))
    cost = (inp * pin + cache_read * pin * 0.1 + out * pout) / 1e6
    ApiUsage.objects.create(model=model, purpose=purpose, input_tokens=inp + cache_read, output_tokens=out,
                            cost_usd=Decimal(str(round(cost, 5))))


def _call(client, model: str, context: dict):
    user = ('Review the evidence below and respond per the schema.\n\n' + json.dumps(context, default=str))
    kwargs = dict(model=model, max_tokens=8000, system=SYSTEM,
                  messages=[{'role': 'user', 'content': user}],
                  output_config={'format': {'type': 'json_schema', 'schema': SCHEMA}})
    try:
        # Server-side refusal fallbacks (routes by refusal category) — default on for Opus 5 / Fable.
        return client.beta.messages.create(betas=['server-side-fallback-2026-07-01'], fallbacks='default', **kwargs)
    except TypeError:
        return client.messages.create(**kwargs)
    except Exception as exc:  # beta not available on this account/SDK → plain call
        log.info('fallback beta unavailable (%s); plain call', exc)
        return client.messages.create(**kwargs)


def sanitize_proposals(proposals: list) -> list:
    """Keep only known strategies/params, coerce values, clip to the schema range."""
    clean = []
    for p in proposals or []:
        try:
            cls = get_strategy_class(p.get('strategy_key', ''))
        except KeyError:
            continue
        grid = {}
        for name, values in (p.get('param_grid') or {}).items():
            spec = cls.param_map().get(name)
            if spec is None or not isinstance(values, list):
                continue
            vals = []
            for v in values[:4]:
                try:
                    cv = spec.coerce(v)
                except (TypeError, ValueError):
                    continue
                if spec.type in ('int', 'float') and spec.min is not None and spec.max is not None:
                    cv = spec.coerce(min(max(cv, spec.min), spec.max))
                if spec.type == 'choice' and cv not in spec.choices:
                    continue
                vals.append(cv)
            if vals:
                grid[name] = sorted(set(vals), key=lambda x: (str(type(x)), x))
        if grid:
            clean.append({'title': str(p.get('title', 'Experiment'))[:120], 'strategy_key': cls.key,
                          'method': p.get('method') if p.get('method') in ('grid', 'random', 'walk_forward') else 'grid',
                          'param_grid': grid, 'rationale': str(p.get('rationale', ''))[:1000]})
    return clean[:5]


def coach_review(account: Account, d: date | None = None) -> JournalEntry:
    if not settings.COACH_ENABLED:
        raise RuntimeError('ANTHROPIC_API_KEY is not set — the coach is dormant')
    import anthropic
    d = d or timezone.localdate()
    model = settings.COACH_MODEL
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    context = build_context(account, d)
    response = _call(client, model, context)
    _record_usage(model, response.usage)
    if response.stop_reason == 'refusal':
        raise RuntimeError('coach request was refused by the model')
    text = next((b.text for b in response.content if b.type == 'text'), '{}')
    data = json.loads(text)
    proposals = sanitize_proposals(data.get('proposals', []))
    body = [data.get('summary', '')]
    if data.get('observations'):
        body.append('**Observations**\n' + '\n'.join(f'- {o}' for o in data['observations']))
    if data.get('hypotheses'):
        body.append('**Hypotheses**\n' + '\n'.join(f'- {h}' for h in data['hypotheses']))
    if data.get('risk_flags'):
        body.append('**Risk flags**\n' + '\n'.join(f'- {r}' for r in data['risk_flags']))
    entry = JournalEntry.objects.create(date=d, kind='coach', account=account,
                                        title=f'Coach review ({model})', body='\n\n'.join(b for b in body if b),
                                        metrics={'model': model, 'proposals': len(proposals)}, proposals=proposals)
    return entry
