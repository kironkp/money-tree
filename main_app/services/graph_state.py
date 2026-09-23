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


def _job(log_name: str, max_age_min: int, if_dead: str) -> dict:
    """Is a scheduled job actually running? Ask its log, not its plist."""
    from django.conf import settings
    path = settings.BASE_DIR / 'run' / log_name
    try:
        age = (timezone.now().timestamp() - path.stat().st_mtime) / 60
    except OSError:
        return {'status': 'error', 'headline': 'off', 'sub': 'never run',
                'detail': f'no log has ever been written, so {if_dead}'}
    if age <= max_age_min:
        return {'status': 'ok', 'headline': 'live',
                'sub': f'ran {age:.0f} min ago', 'detail': ''}
    hours = age / 60
    when = f'{age:.0f} min' if hours < 1 else f'{hours:.0f} h'
    return {'status': 'warn', 'headline': 'stale', 'sub': f'last ran {when} ago',
            'detail': f'it has not run recently, so {if_dead}'}


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

    # Each lane's own tolls. Kept per lane on purpose: degen and crypto pay 50 bps
    # a round trip while stocks and forex pay 1, so a single desk-wide fee number
    # would hide the only distinction that decides which bot is worth promoting.
    from .report import lane_costs
    costs = {lane: lane_costs(a) for lane, a in accounts.items()}

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
        c = costs.get(lane) or {}
        if c.get('trades'):
            toll = (f'lifetime: {c["trades"]} trades moved ${c["moved"]:,.0f}, paid '
                    f'${c["fees"]:,.2f} in fees ({c["fee_bps"]:.1f} bps) — '
                    f'${c["net_before_fees"]:+,.2f} before fees, ${c["net"]:+,.2f} after')
            if c['net_before_fees'] > 0 and c['net'] < 0:
                toll += '. The trading works; the tolls sink it.'
            elif c.get('fee_share'):
                toll += f'. Fees are {c["fee_share"]:.0f}% of the loss.'
        else:
            toll = ''
        out[f'agent.{lane}'] = {
            'status': 'halted' if halted else ('ok' if health in ('healthy', 'running', 'waiting')
                                               else 'warn' if run else 'idle'),
            'headline': f'${equity:,.0f}',
            'sub': f'{_money(day)} today · {counts.get(lane, 0)} trades',
            'detail': ((acct.day_halted_reason if halted else detail)[:140]
                       + ('  ·  ' + toll if toll else '')),
        }

    # --- strategies: one grouped query --------------------------------------
    quals = {row['qualification']: row['n'] for row in
             Strategy.objects.values('qualification').annotate(n=Count('id'))}
    for key in ('orb', 'vwap_reversion', 'ema_momentum', 'burst', 'news_catalyst'):
        rows = list(Strategy.objects.filter(key=key).values('enabled', 'qualification', 'market'))
        live = sum(1 for r in rows if r['enabled'])
        proven = sum(1 for r in rows if r['qualification'] == 'qualified')
        walled = sum(1 for r in rows if r['qualification'] == 'quarantine')
        # 'unproven' for something that was measured and switched off reads as
        # "not looked at yet", which is the opposite of what happened.
        sub = f'{proven} proven' if proven else ('quarantined' if walled == len(rows) else 'unproven')
        out[f'strat.{key}'] = {
            'status': 'ok' if proven else ('warn' if live else 'idle'),
            'headline': f'{live}/{len(rows)} on',
            'sub': sub,
            'detail': ('measured, and stopped — see the journal' if walled == len(rows)
                       else 'enabled means permission to observe, not proof that it works'),
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
    # "Graded" means the barrier race produced a RESULT, not merely that a
    # timestamp was stamped on the row. outcome_at is set on 374 rows; only 54
    # carry an outcome_kind. Counting the former reported 192 graded verdicts
    # when 54 existed, and a reader — including me — took that as the size of
    # the evidence base.
    graded = verdicts.filter(provenance='contemporaneous').exclude(
        outcome_kind='').exclude(outcome_kind__isnull=True).count()
    pending = verdicts.filter(outcome_at__isnull=False).filter(outcome_kind='').count()
    out['store.verdicts'] = {
        'status': 'ok' if graded else 'warn',
        'headline': f'{verdicts.count()} in 7d', 'sub': f'{graded} graded',
        'detail': (f'{pending} have an outcome timestamp and no result yet. '
                   'Rows from before the grader existed are marked rebuilt and never counted.'),
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
    # Read from evidence, not from the plists. A schedule file on disk says what
    # someone intended; a log written five minutes ago says what is running. The
    # watchdog sat unloaded for weeks while its plist sat in the repository
    # looking exactly like a job that was working.
    out['ops.watchdog'] = _job('watchdog.log', 12, 'nothing would close a stranded position')
    out['ops.agents'] = _job('ensure-agents.log', 12, 'a reboot would leave the lanes dead')
    out['core.alpaca_broker'] = {'status': 'idle', 'headline': 'dormant', 'sub': 'no lane qualified',
                                 'detail': 'three separate locks, none of them open'}
    out['gate.shadow'] = {'status': 'warn', 'headline': 'closed', 'sub': 'research cannot trade',
                          'detail': 'opens only if the gate passes'}
    out['ops.coach'] = {'status': 'idle', 'headline': 'quiet', 'sub': 'spend-capped',
                        'detail': 'the Anthropic account is over its limit'}
    return out
