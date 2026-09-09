"""The 17:30 daily report: what each lane LEARNED today, what it made or lost,
and what it will do differently tomorrow.

The learning section is derived, never decorative. Every line traces to a row:
last night's walk-forward verdict, the exit-reason mix of today's trades, the
reason signals were blocked, the rule that most often failed to fire, and the
gap between live expectancy and the backtest that justified the parameters.

One section per lane — stocks, crypto, degen, forex — every day, whether or not
the lane traded. A lane that did nothing still has to say why.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time as dtime, timedelta
from statistics import median

from django.utils import timezone

from main_app.models import (Account, AgentConfig, AgentRun, Experiment, JournalEntry, Market, Mode, RiskEvent,
                             Signal, Strategy, SymbolState, Trade)

from .data import calendar as cal
from .promotion import baseline_metrics, live_stats, qualification_assessment
from .spend import day_spend, projected_monthly, range_spend
from .journal import DRIFT_MIN_TRADES as DRIFT_TRADES
from .risk import RiskConfig

LANES = (Market.STOCKS, Market.CRYPTO, Market.DEGEN, Market.FOREX)
LANE_TITLE = {Market.STOCKS: 'Stocks', Market.CRYPTO: 'Crypto', Market.DEGEN: 'Degen', Market.FOREX: 'Forex'}


def _bounds(d: date) -> tuple[datetime, datetime]:
    """The report day in Eastern time, which is how the app stamps everything."""
    start = datetime.combine(d, dtime(0, 0), tzinfo=cal.ET)
    return start, start + timedelta(days=1)


def _pct(n, d):
    return (n / d * 100.0) if d else 0.0


# --- the three sections ----------------------------------------------------

def lane_pnl(account: Account, d: date) -> dict:
    a, b = _bounds(d)
    trades = list(Trade.objects.filter(account=account, exit_ts__gte=a, exit_ts__lt=b).select_related('instrument'))
    pnls = [float(t.pnl) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    positions = list(account.positions.select_related('instrument'))
    return {
        'trades': len(trades), 'net': sum(pnls), 'fees': sum(float(t.fees) for t in trades),
        'wins': len(wins), 'losses': len(losses), 'win_rate': _pct(len(wins), len(trades)),
        'gross_win': sum(wins), 'gross_loss': -sum(losses),
        'profit_factor': (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else (sum(wins) if wins else 0.0),
        'expectancy': (sum(pnls) / len(pnls)) if pnls else 0.0,
        'best': max(trades, key=lambda t: t.pnl, default=None),
        'worst': min(trades, key=lambda t: t.pnl, default=None),
        'per_strategy': _group(trades, lambda t: t.strategy_key or '-'),
        'per_symbol': _group(trades, lambda t: t.instrument.symbol),
        'exit_reasons': Counter(t.exit_reason for t in trades),
        'equity': float(account.equity), 'starting_cash': float(account.starting_cash),
        'total_pnl': float(account.total_pnl),
        'open_positions': len(positions),
        'unrealized': sum(float(p.unrealized_pnl) for p in positions),
    }


def _group(trades, key):
    out: dict[str, dict] = {}
    for t in trades:
        row = out.setdefault(key(t), {'trades': 0, 'pnl': 0.0, 'wins': 0})
        row['trades'] += 1
        row['pnl'] += float(t.pnl)
        row['wins'] += 1 if t.pnl > 0 else 0
    return dict(sorted(out.items(), key=lambda kv: kv[1]['pnl']))


def lane_learned(account: Account, d: date, cfg: AgentConfig) -> list[dict]:
    """Everything the lane found out today, each item traced to its source."""
    a, b = _bounds(d)
    out: list[dict] = []
    rows = list(Strategy.objects.filter(market=account.market))
    enabled = [r for r in rows if r.enabled]

    # 1. Research: what last night's walk-forward actually concluded.
    for entry in JournalEntry.objects.filter(kind='auto_eod', created_at__gte=a - timedelta(days=1),
                                             account=account).order_by('-created_at')[:6]:
        if entry.title.startswith('Auto-research'):
            out.append({'source': 'research', 'text': entry.title.replace('Auto-research ', ''),
                        'detail': (entry.body or '')[:400]})
    if not any(i['source'] == 'research' for i in out):
        if not enabled:
            out.append({'source': 'research', 'text': 'no research ran for this lane',
                        'detail': 'The nightly walk-forward only visits ENABLED strategies, and every strategy here '
                                  'is parked. A parked lane cannot generate the evidence needed to unpark it.'})
        else:
            out.append({'source': 'research', 'text': 'no research verdict recorded in the last 24 h',
                        'detail': 'Check run/research.log and the 02:10 launchd job.'})

    # 2. Execution: what the day's trades revealed, read from the exit mix.
    p = lane_pnl(account, d)
    if p['trades']:
        ex = p['exit_reasons']
        top, n = ex.most_common(1)[0]
        share = _pct(n, p['trades'])
        meaning = {
            'stop': 'price went against the entry before the target came into reach — entries are early or stops sit inside the noise',
            'target': 'trades are reaching their targets — the entry timing is working',
            'time': 'trades expired at max hold without resolving — targets are too far for the holding window',
            'eod': 'trades were still open at the close and were flattened, so the session was too short for the target',
            'signal': 'the strategy closed on its own reversal rule before stop or target',
            'manual': 'positions were closed by an operator action or a restart, not by the strategy',
        }.get(top, 'mixed exits')
        out.append({'source': 'execution',
                    'text': f'{n} of {p["trades"]} exits were "{top}" ({share:.0f}%): {meaning}',
                    'detail': 'Exit mix: ' + ', '.join(f'{k} {v}' for k, v in ex.most_common())})
        for key, s in p['per_strategy'].items():
            out.append({'source': 'execution',
                        'text': f'{key}: {s["trades"]} trades, {s["pnl"]:+,.2f}, {_pct(s["wins"], s["trades"]):.0f}% won',
                        'detail': ''})
        if p['fees'] and abs(p['net']) > 0:
            out.append({'source': 'execution',
                        'text': f'fees were ${p["fees"]:,.2f} against a gross of ${p["net"] + p["fees"]:+,.2f}',
                        'detail': 'Costs are the difference between a marginal edge and a loss in this lane.'
                                  if p['fees'] > abs(p['net'] + p['fees']) * 0.5 else ''})

    # 3. Gates: what stopped trades that wanted to happen.
    sigs = Signal.objects.filter(account=account, ts__gte=a, ts__lt=b)
    total, acted = sigs.count(), sigs.filter(acted=True).count()
    blocked = Counter(s.split(' (')[0] for s in
                      sigs.filter(acted=False).exclude(blocked_reason='').values_list('blocked_reason', flat=True))
    if blocked:
        reason, n = blocked.most_common(1)[0]
        out.append({'source': 'gate',
                    'text': f'{sum(blocked.values())} of {total} signals were blocked; the binding constraint was "{reason}" (×{n})',
                    'detail': '; '.join(f'{k} ×{v}' for k, v in blocked.most_common(5))})
    elif total and acted == total:
        out.append({'source': 'gate', 'text': f'all {total} signals passed risk and were traded', 'detail': ''})

    # 4. Near misses: with no signal at all, which rule kept failing, and by how much.
    if not total:
        near = _near_misses(account)
        if near:
            out.append({'source': 'setup', 'text': f'no setup fired all day; the rule that failed most was {near["rule"]}',
                        'detail': near['detail']})
        elif not enabled:
            out.append({'source': 'setup', 'text': 'lane is parked — no strategy is enabled, so nothing was evaluated',
                        'detail': 'Strategies: ' + ', '.join(f'{r.key} ({r.qualification})' for r in rows)})
        else:
            out.append({'source': 'setup', 'text': 'no signals were generated', 'detail': ''})

    # 5. Evidence: live results versus the backtest that justified the parameters.
    for row in enabled:
        stats = live_stats(row, account)
        base = baseline_metrics(row)
        if stats['trades'] and base.get('expectancy'):
            gap = stats['expectancy'] - float(base['expectancy'])
            verdict = 'holding up' if gap >= 0 else 'underperforming'
            out.append({'source': 'evidence',
                        'text': f'{row.key} is {verdict}: live expectancy {stats["expectancy"]:+.2f}/trade over '
                                f'{stats["trades"]} trades versus {float(base["expectancy"]):+.2f} in the backtest',
                        'detail': f'profit factor live {stats["profit_factor"]:.2f}'})
        qa = qualification_assessment(row, account)
        out.append({'source': 'evidence', 'text': f'{row.key} evidence state: {qa["state"]}',
                    'detail': qa.get('reason', '')})

    # 6. Operations: anything that interfered with the lane doing its job.
    run = AgentRun.objects.filter(account=account).order_by('-started_at').first()
    if run is None or run.status != 'running':
        out.append({'source': 'ops', 'text': 'no agent process was running for this lane', 'detail': ''})
    for e in RiskEvent.objects.filter(account=account, ts__gte=a, ts__lt=b).exclude(kind='qualification')[:5]:
        out.append({'source': 'ops', 'text': f'{e.kind}: {e.message[:160]}', 'detail': ''})
    return out


def _near_misses(account: Account) -> dict | None:
    """The rule that most often stood between this lane and a trade."""
    states = list(SymbolState.objects.filter(account=account))
    fails: Counter = Counter()
    gaps: dict[str, list] = {}
    for st in states:
        for r in st.rules or []:
            if r.get('ok'):
                continue
            name = f'{r.get("strategy", "?")}.{r.get("rule", "?")}'
            fails[name] += 1
            if r.get('value') is not None and r.get('threshold') is not None:
                gaps.setdefault(name, []).append(abs(float(r['threshold']) - float(r['value'])))
    if not fails:
        return None
    rule, n = fails.most_common(1)[0]
    g = gaps.get(rule) or []
    detail = f'failed on {n} of the {len(states)} symbols last evaluated'
    if g:
        detail += f'; typical distance from the threshold {median(g):.4g}'
    sample = [st.summary for st in states if st.summary][:1]
    if sample:
        detail += f'. Example: {sample[0][:200]}'
    return {'rule': rule, 'detail': detail}


# The rule that most often stands between a lane and a trade, and the parameter
# behind it — so "how it gets better" names something the search can actually vary.
RULE_PARAM = {
    'cross': ('fast / slow', 'the moving-average lengths that define a fresh cross'),
    'volume': ('min_relvol', 'the relative-volume floor an entry bar must clear'),
    'rsi': ('rsi_min / rsi_max', 'the momentum band an entry must sit inside'),
    'breakout': ('range_minutes', 'how long the opening range is measured before a break counts'),
    'stretch': ('entry_z', 'how far from VWAP price must travel before it is faded'),
    'move': ('min_move_pct', 'the size of the burst that counts as a move'),
    'session': ('min_bar_pos', 'how far into the session trading may start'),
    'warmup': ('warmup_bars / history depth', 'how many stored bars the indicators need'),
}


def lane_improve(account: Account, d: date, cfg: AgentConfig, learned: list[dict], p: dict) -> list[str]:
    """What changes tomorrow, derived from what today showed."""
    out: list[str] = []
    rows = list(Strategy.objects.filter(market=account.market))
    enabled = [r for r in rows if r.enabled]
    risk = RiskConfig.from_model(cfg, account.market)

    if not enabled:
        out.append(f'This lane is parked: {", ".join(r.key for r in rows) or "no strategies"} are disabled, so it '
                   f'cannot trade or learn from trading. Only a research promotion can restart it, and the nightly '
                   f'research skips disabled strategies — so it will stay parked until that is changed.')

    # What last night's research concluded, turned into what happens next.
    for item in learned:
        if item['source'] != 'research':
            continue
        t = item['text']
        if 'NOT confirmed' in t or 'failed the held-out gate' in t or 'no edge' in t:
            key = t.split(' (')[0]
            out.append(f'{key} did not clear the held-out gate last night, so it keeps its current version and stays '
                       f'unqualified. It will be re-searched tonight against one more day of bars; if it fails again '
                       f'the honest conclusion is that this strategy has no edge on this lane and it should be retired '
                       f'rather than re-tuned.')
        elif 'PROMOTED' in t:
            out.append(f'{t} — the agent picks the new parameters up on its next restart.')

    # The rule that most often blocked an entry names the parameter to vary.
    for item in learned:
        if item['source'] != 'setup' or 'failed most was' not in item['text']:
            continue
        rule = item['text'].rsplit('was ', 1)[-1].strip()
        short = rule.split('.')[-1]
        param, meaning = RULE_PARAM.get(short, (None, None))
        if param:
            out.append(f'The rule that stood between this lane and a trade was {rule} — {meaning}. Tonight\'s search '
                       f'varies {param}; if a setup still never fires, the filter is too strict for this lane\'s '
                       f'volatility rather than mis-tuned.')

    gate_blocked = sum(1 for i in learned if i['source'] == 'gate' and 'round-trip cost' in i['text'])
    if gate_blocked:
        need = risk.min_reward_to_cost * risk.round_trip_cost_pct(account.lane_asset_class)
        out.append(f'The cost gate is what stands in the way: a trade here must show a target of at least '
                   f'{need:.2f}% to clear {risk.min_reward_to_cost:g}× the {risk.round_trip_cost_pct(account.lane_asset_class):.2f}% '
                   f'round trip. Tonight\'s research will be pointed at parameters whose targets clear that, or the '
                   f'lane needs a longer timeframe where the average move is bigger.')

    ex = p['exit_reasons']
    if p['trades'] >= 3:
        top, n = ex.most_common(1)[0]
        if top == 'stop' and _pct(n, p['trades']) >= 50:
            out.append('Most exits were stops, so tonight\'s search will widen the stop multiple and re-test entry '
                       'timing rather than chase a bigger target.')
        elif top == 'time' and _pct(n, p['trades']) >= 50:
            out.append('Most trades expired at max hold, so the search will test smaller targets and a shorter hold '
                       'rather than a wider stop.')
        elif top == 'target':
            out.append('Targets are being reached, so the search will test whether a larger target still clears the '
                       'same win rate.')

    for row in enabled:
        stats = live_stats(row, account)
        base = baseline_metrics(row)
        if stats['trades'] >= 10 and base.get('expectancy') and stats['expectancy'] < 0 < float(base['expectancy']):
            out.append(f'{row.key} has drifted below the backtest that justified it: live {stats["expectancy"]:+.2f} '
                       f'per trade against {float(base["expectancy"]):+.2f} expected, over {stats["trades"]} trades. '
                       f'It will be re-validated, and auto-disabled at {DRIFT_TRADES} trades if the gap holds.')
        qa = qualification_assessment(row, account)
        if qa['state'] == 'quarantine':
            out.append(f'{row.key} is quarantined ({qa.get("reason", "")[:120]}) and will not trade until new '
                       f'out-of-sample evidence clears the gate.')

    if not out:
        out.append('Nothing changed today that warrants a parameter change; the lane keeps its current version and '
                   'the nightly walk-forward re-tests it against the newest bars.')
    return out


# --- assembly --------------------------------------------------------------

def build_report(d: date | None = None, mode: str = Mode.SIM) -> dict:
    d = d or cal.session_date(timezone.now())
    cfg = AgentConfig.get()
    lanes = []
    total_net = total_trades = 0
    total_equity = total_start = 0.0
    for market in LANES:
        account = Account.for_mode(mode, market)
        p = lane_pnl(account, d)
        learned = lane_learned(account, d, cfg)
        improve = lane_improve(account, d, cfg, learned, p)
        lanes.append({'market': market, 'title': LANE_TITLE[market], 'account': account,
                      'pnl': p, 'learned': learned, 'improve': improve,
                      'strategies': list(Strategy.objects.filter(market=market).order_by('key'))})
        total_net += p['net']
        total_trades += p['trades']
        total_equity += p['equity']
        total_start += p['starting_cash']
    spend = day_spend(d)
    spend['month_to_date'] = range_spend(30)
    spend['projected_monthly'] = projected_monthly(30)
    return {'date': d, 'mode': mode, 'lanes': lanes, 'generated_at': timezone.now(), 'spend': spend,
            'total': {'net': total_net, 'trades': total_trades, 'equity': total_equity,
                      'starting_cash': total_start, 'total_pnl': total_equity - total_start}}


def render_text(rep: dict) -> str:
    L = []
    t = rep['total']
    L.append(f"MoneyTree — {rep['date']:%A %B %-d, %Y}")
    L.append('=' * 60)
    L.append(f"Today across all four lanes: {t['net']:+,.2f} on {t['trades']} trades")
    L.append(f"Equity {t['equity']:,.2f} of {t['starting_cash']:,.2f} seeded ({t['total_pnl']:+,.2f} lifetime)")
    L.append('')
    for lane in rep['lanes']:
        p = lane['pnl']
        L.append(f"── {lane['title'].upper()} " + '─' * (56 - len(lane['title'])))
        L.append(f"P&L today {p['net']:+,.2f} on {p['trades']} trades"
                 + (f" ({p['win_rate']:.0f}% won, fees {p['fees']:,.2f}, expectancy {p['expectancy']:+.2f}/trade)"
                    if p['trades'] else '')
                 + f" · equity {p['equity']:,.2f} ({p['total_pnl']:+,.2f} lifetime)")
        if p['open_positions']:
            L.append(f"  {p['open_positions']} position(s) still open, {p['unrealized']:+,.2f} unrealized")
        L.append('')
        L.append('  WHAT IT LEARNED')
        for item in lane['learned']:
            L.append(f"   • [{item['source']}] {item['text']}")
            if item.get('detail'):
                L.append(f"       {item['detail'][:300]}")
        L.append('')
        L.append('  HOW IT GETS BETTER')
        for line in lane['improve']:
            L.append(f"   → {line}")
        L.append('')
    sp = rep.get('spend') or {}
    L.append('── API SPEND ' + '─' * 46)
    L.append(f"Today ${sp.get('total', 0):.2f} across {sp.get('calls', 0)} call(s) · "
             f"last 30 days ${(sp.get('month_to_date') or {}).get('total', 0):.2f} · "
             f"at this rate ${sp.get('projected_monthly', 0):.2f}/month")
    for label, key in (('by project', 'by_project'), ('by provider', 'by_provider'), ('by purpose', 'by_purpose')):
        rows = (sp.get('month_to_date') or {}).get(key) or {}
        if rows:
            L.append(f"  30-day {label}: " + ', '.join(f'{k} ${v["cost"]:.2f}' for k, v in list(rows.items())[:6]))
    if not sp.get('calls'):
        L.append('  Nothing recorded today. Only calls that write to the ledger appear here — see the')
        L.append('  spend command for which projects report in.')
    L.append('')
    L.append(f"Generated {rep['generated_at'].astimezone(cal.ET):%Y-%m-%d %H:%M ET} · MoneyTree runs on fake money.")
    return '\n'.join(L)


def render_html(rep: dict) -> str:
    t = rep['total']

    def money(v):
        cls = 'up' if v > 0 else ('down' if v < 0 else 'flat')
        return f'<span class="{cls}">{v:+,.2f}</span>'

    P = ['<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:720px;margin:0 auto;'
         'background:#0f1211;color:#e7eae8;padding:24px">',
         '<style>.up{color:#4ade80}.down{color:#f87171}.flat{color:#9ca3af}'
         'h2{border-bottom:1px solid #2a2f2c;padding-bottom:6px;margin-top:28px}'
         'li{margin:4px 0;line-height:1.45}.src{color:#7dd3a0;font-size:12px;text-transform:uppercase;letter-spacing:.4px}'
         '.det{color:#9ca3af;font-size:13px;margin-left:4px}</style>',
         f'<h1 style="margin:0">🌳 MoneyTree · {rep["date"]:%A %B %-d}</h1>',
         f'<p style="font-size:20px;margin:8px 0">Today: {money(t["net"])} on {t["trades"]} trades</p>',
         f'<p class="det">Equity {t["equity"]:,.2f} of {t["starting_cash"]:,.2f} seeded · lifetime {money(t["total_pnl"])}</p>']
    for lane in rep['lanes']:
        p = lane['pnl']
        P.append(f'<h2>{lane["title"]} &nbsp; {money(p["net"])} <span class="det">on {p["trades"]} trades · '
                 f'equity {p["equity"]:,.2f} · lifetime {money(p["total_pnl"])}</span></h2>')
        if p['trades']:
            P.append(f'<p class="det">{p["win_rate"]:.0f}% won · fees {p["fees"]:,.2f} · '
                     f'expectancy {p["expectancy"]:+.2f}/trade · exits: '
                     + ', '.join(f'{k} {v}' for k, v in p['exit_reasons'].most_common()) + '</p>')
        P.append('<p><strong>What it learned</strong></p><ul>')
        for item in lane['learned']:
            det = f'<div class="det">{item["detail"][:300]}</div>' if item.get('detail') else ''
            P.append(f'<li><span class="src">{item["source"]}</span> {item["text"]}{det}</li>')
        P.append('</ul><p><strong>How it gets better</strong></p><ul>')
        for line in lane['improve']:
            P.append(f'<li>{line}</li>')
        P.append('</ul>')
    sp = rep.get('spend') or {}
    mtd = sp.get('month_to_date') or {}
    P.append(f'<h2>API spend <span class="det">today ${sp.get("total", 0):.2f} · 30 days ${mtd.get("total", 0):.2f} · '
             f'at this rate ${sp.get("projected_monthly", 0):.2f}/month</span></h2>')
    rows = mtd.get('by_project') or {}
    if rows:
        P.append('<ul>' + ''.join(
            f'<li>{k} <span class="det">${v["cost"]:.2f} over {v["calls"]} calls</span></li>'
            for k, v in list(rows.items())[:8]) + '</ul>')
    else:
        P.append('<p class="det">Nothing recorded yet — only calls that write to the ledger appear here.</p>')
    P.append(f'<p class="det" style="margin-top:32px">Generated {rep["generated_at"].astimezone(cal.ET):%Y-%m-%d %H:%M ET}. '
             'Fake money until it earns its stripes.</p></div>')
    return '\n'.join(P)


def save_journal(rep: dict) -> JournalEntry:
    """One row per day per lane, so the app's Journal carries the same story."""
    last = None
    for lane in rep['lanes']:
        p = lane['pnl']
        body = ['**What it learned**', '']
        body += [f"- _{i['source']}_ — {i['text']}" + (f"  \n  {i['detail'][:300]}" if i.get('detail') else '')
                 for i in lane['learned']]
        body += ['', '**How it gets better**', '']
        body += [f'- {line}' for line in lane['improve']]
        last, _ = JournalEntry.objects.update_or_create(
            date=rep['date'], account=lane['account'], kind='report',
            defaults={'title': f"{lane['title']} — {p['net']:+,.2f} on {p['trades']} trades",
                      'body': '\n'.join(body),
                      'metrics': {'net': p['net'], 'trades': p['trades'], 'fees': p['fees'],
                                  'equity': p['equity'], 'total_pnl': p['total_pnl'],
                                  'win_rate': p['win_rate'], 'expectancy': p['expectancy']}})
    return last
