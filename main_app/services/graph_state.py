"""Live numbers for the map, in as few queries as it takes.

The map is a dashboard, not a diagram, so every node that has a current state
shows it. The constraint is that this runs on every poll: two rules, both learned
by measuring rather than guessing.

  Never touch the Bar table. Any freshness query against it costs 200ms+ on a
  350MB SQLite file. `AgentRun.last_bar_ts` already knows.

  Never call the status builder per lane. It costs three queries per account, so
  a loop over twelve accounts is thirty-six queries to answer a question four
  aggregates could.

The whole thing is about twenty queries and forty milliseconds warm.
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Sum
from django.utils import timezone

from main_app.models import (AgentRun, Account, ApiUsage, Evaluation, Mode, NewsItem,
                             NewsSession, NewsVerdict, Strategy, SymbolDossier, Trade)

from .data import calendar as cal

LANES = ('stocks', 'crypto', 'degen', 'forex')


def _money(x) -> str:
    x = float(x or 0)
    sign = '+' if x > 0 else ''
    return f'{sign}{x:,.2f}'


def state() -> dict:
    """One dict of node id -> live state. Every value is cheap or absent."""
    now = timezone.now()
    today = cal.session_date(now)
    day_start = timezone.datetime.combine(today, timezone.datetime.min.time(), tzinfo=cal.ET)
    out: dict[str, dict] = {}

    # --- lanes: one query for accounts, one for runs, one for today's trades --
    accounts = {a.market: a for a in Account.objects.filter(mode=Mode.SIM)}
    runs = {}
    for r in AgentRun.objects.filter(status='running').order_by('-started_at'):
        runs.setdefault(r.market, r)
    pnl = {row['instrument__market'] if 'instrument__market' in row else row['account__market']: row['p']
           for row in Trade.objects.filter(exit_ts__gte=day_start)
           .values('account__market').annotate(p=Sum('pnl'))}
    counts = {row['account__market']: row['n']
              for row in Trade.objects.filter(exit_ts__gte=day_start)
              .values('account__market').annotate(n=Count('id'))}

    for lane in LANES:
        acct, run = accounts.get(lane), runs.get(lane)
        health = 'idle'
        detail = 'not running'
        if run is not None:
            health = getattr(run, 'health', 'running') or 'running'
            detail = f'{run.state or health} · since {run.started_at:%b %-d %H:%M}'
        equity = float(acct.equity) if acct else 0.0
        day = float(pnl.get(lane) or 0)
        halted = bool(acct and getattr(acct, 'day_halted', False))
        out[f'agent.{lane}'] = {
            'status': 'halted' if halted else ('ok' if health in ('healthy', 'running', 'waiting')
                                               else 'warn' if run else 'idle'),
            'headline': f'${equity:,.0f}',
            'sub': f'{_money(day)} today · {counts.get(lane, 0)} trades',
            'detail': (acct.day_halted_reason if halted else detail)[:140],
        }

    # --- strategies: one grouped query --------------------------------------
    quals = {row['qualification']: row['n'] for row in
             Strategy.objects.values('qualification').annotate(n=Count('id'))}
    for key in ('orb', 'vwap_reversion', 'ema_momentum', 'burst', 'news_catalyst'):
        rows = list(Strategy.objects.filter(key=key).values('enabled', 'qualification', 'market'))
        live = sum(1 for r in rows if r['enabled'])
        proven = sum(1 for r in rows if r['qualification'] == 'qualified')
        out[f'strat.{key}'] = {
            'status': 'ok' if proven else ('warn' if live else 'idle'),
            'headline': f'{live}/{len(rows)} on',
            'sub': f'{proven} proven' if proven else 'unproven',
            'detail': 'enabled means permission to observe, not proof that it works',
        }
    out['ops.promotion'] = {
        'status': 'warn' if not quals.get('qualified') else 'ok',
        'headline': f"{quals.get('qualified', 0)} qualified",
        'sub': f"{quals.get('unproven', 0)} unproven · {quals.get('quarantine', 0)} quarantined",
        'detail': 'nothing has left the simulator',
    }

    # --- the news arm --------------------------------------------------------
    sitting = NewsSession.objects.order_by('-started_at').first()
    if sitting:
        age = (now - sitting.started_at).total_seconds() / 3600
        out['bot.newsagent'] = {
            'status': 'warn' if sitting.error else ('ok' if age < 6 else 'stale'),
            'headline': f'{sitting.considered} read',
            'sub': f'{sitting.actionable} call{"" if sitting.actionable == 1 else "s"} · '
                   f'{age:.0f}h ago',
            'detail': (sitting.error or sitting.narrative or '')[:160],
        }
    stories_24h = NewsItem.objects.filter(published_at__gte=now - timedelta(hours=24)).count()
    classified = NewsItem.objects.filter(classified_at__gte=now - timedelta(hours=24)).count()
    out['bot.reader'] = {'status': 'ok' if stories_24h else 'idle',
                         'headline': f'{stories_24h}', 'sub': 'stories in 24h',
                         'detail': 'only stories touching something we trade are kept'}
    out['bot.classifier'] = {'status': 'ok' if classified else 'idle',
                             'headline': f'{classified}', 'sub': 'classified in 24h', 'detail': ''}

    verdicts = NewsVerdict.objects.filter(created_at__gte=now - timedelta(days=7))
    graded = verdicts.filter(outcome_at__isnull=False, provenance='contemporaneous').count()
    out['store.verdicts'] = {
        'status': 'ok' if graded else 'warn',
        'headline': f'{verdicts.count()}', 'sub': f'{graded} graded this week',
        'detail': 'rows from before the grader existed are marked rebuilt and never counted',
    }

    # --- the research arm ----------------------------------------------------
    doss = SymbolDossier.objects.filter(error='')
    recent = doss.order_by('-as_of')[:20]
    acting = sum(1 for d in recent if d.direction != 'none')
    cost = doss.filter(as_of__gte=day_start).aggregate(c=Sum('cost_usd'))['c'] or 0
    out['bot.dossier'] = {
        'status': 'ok' if recent else 'idle',
        'headline': f'{doss.count()}', 'sub': f'${float(cost):.2f} today',
        'detail': 'in shadow — writes no orders',
    }
    out['store.dossiers'] = {
        'status': 'warn' if recent and not acting else 'ok',
        'headline': f'{acting}/{len(recent)}',
        'sub': 'would have traded',
        'detail': ('every dossier so far has concluded no trade, so the research column of the '
                   'experiment is currently all zeros' if not acting else ''),
    }

    ev = Evaluation.objects.filter(status='collecting', kind='promotion').first()
    if ev:
        from .evaluation import daily_series
        try:
            days = len(daily_series(ev, now))
        except Exception:
            days = 0
        nxt = ev.next_checkpoint or 0
        out['bot.evaluation'] = {
            'status': 'idle' if days < 10 else 'ok',
            'headline': f'{days}/{nxt}', 'sub': 'days to the next look',
            'detail': f'{ev.identifier} · hurdle {ev.delta_min:+.2f} ATR/day',
        }
        out['bot.prereg'] = {'status': 'ok', 'headline': ev.fingerprint[:8],
                             'sub': 'frozen', 'detail': f'opened {ev.opened_at:%b %-d}'}

    # --- spend ---------------------------------------------------------------
    month = ApiUsage.objects.filter(ts__gte=now - timedelta(days=30))
    spent_today = ApiUsage.objects.filter(ts__gte=day_start).aggregate(c=Sum('cost_usd'))['c'] or 0
    spent_month = month.aggregate(c=Sum('cost_usd'))['c'] or 0
    out['ops.spend'] = {
        'status': 'ok', 'headline': f'${float(spent_today):.2f}',
        'sub': f'${float(spent_month):.2f} in 30 days',
        'detail': 'priced at the moment of the call, not reconstructed afterwards',
    }

    # --- what is actually scheduled -----------------------------------------
    out['ops.watchdog'] = {
        'status': 'error', 'headline': 'off', 'sub': 'never loaded',
        'detail': 'its schedule file exists but has not been loaded on this machine, so nothing '
                  'would close a position if an agent died holding one',
    }
    out['core.alpaca_broker'] = {'status': 'idle', 'headline': 'dormant', 'sub': 'no lane qualified',
                                 'detail': 'three separate locks, none of them open'}
    out['gate.shadow'] = {'status': 'warn', 'headline': 'closed', 'sub': 'research cannot trade',
                          'detail': 'opens only if the gate passes'}
    out['ops.coach'] = {'status': 'idle', 'headline': 'quiet', 'sub': 'spend-capped',
                        'detail': 'the Anthropic account is over its limit'}
    return out
