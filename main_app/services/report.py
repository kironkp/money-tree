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
from .briefing import latest as latest_briefing
from .news import digest as news_digest
from .promotion import baseline_metrics, live_stats, qualification_assessment
from .spend import day_spend, projected_monthly, range_spend
from .journal import DRIFT_MIN_TRADES as DRIFT_TRADES
from .risk import RiskConfig

# Order the owner reads them in. Forex leads because it was the section he had to
# scroll past everything else to reach.
LANES = (Market.FOREX, Market.DEGEN, Market.CRYPTO, Market.STOCKS)
LANE_TITLE = {Market.STOCKS: 'Stocks', Market.CRYPTO: 'Crypto', Market.DEGEN: 'Degen', Market.FOREX: 'Forex'}


def _bounds(d: date) -> tuple[datetime, datetime]:
    """The report day in Eastern time, which is how the app stamps everything."""
    start = datetime.combine(d, dtime(0, 0), tzinfo=cal.ET)
    return start, start + timedelta(days=1)


def _pct(n, d):
    return (n / d * 100.0) if d else 0.0


# --- the three sections ----------------------------------------------------

def lane_costs(account: Account, all_time: bool = False) -> dict:
    """Every trade this lane has ever taken, and what the tolls cost it.

    Kept per lane rather than as one desk-wide number on purpose: the four bots
    have wildly different economics, and averaging them hides it. Crypto and degen
    pay 50 bps a round trip; stocks and forex pay 1. Degen's fees alone are
    two thirds of its entire loss, while forex's are a fifth of its. One number
    for "fees" would make both look like the same problem, and they are not — so
    a lane that is actually working can be promoted on its own while another
    keeps bleeding in the simulator.
    """
    qs = Trade.objects.filter(account=account)
    # Since the current scoring epoch by default. A reset moves the starting line
    # so the present setup can be judged on its own results; `all_time=True` is
    # what the lifetime brake and the history view ask for, and it always sees
    # everything.
    if not all_time and account.epoch_started_at:
        qs = qs.filter(exit_ts__gte=account.epoch_started_at)
    rows = list(qs.values_list('id', 'pnl', 'fees', 'entry_price', 'qty'))
    empty = {'trades': 0, 'net': 0.0, 'fees': 0.0, 'slippage': 0.0, 'costs': 0.0, 'moved': 0.0,
             'fee_bps': 0.0, 'slippage_bps': 0.0, 'cost_bps': 0.0,
             'gross': 0.0, 'net_before_fees': 0.0, 'fee_share': 0.0, 'cost_share': 0.0,
             'slippage_measured': False, 'epoch_started_at': account.epoch_started_at, 'all_time': all_time}
    if not rows:
        return empty
    net = sum(float(r[1]) for r in rows)
    fees = sum(float(r[2]) for r in rows)
    moved = sum(float(r[3]) * float(r[4]) for r in rows)
    slippage, measured = _slippage_cost(account, moved, all_time)
    # Gross is what the trades would have earned paying nothing at all. Fees come
    # out of pnl already; slippage never did — it is inside the fill price, so
    # `pnl` is net of it and it has to be added back to get the raw edge. A lane
    # whose gross is healthy and whose net is thin is an expensive strategy, not
    # a good one, and that is only visible if both numbers are on the page.
    gross = net + fees + slippage
    costs = fees + slippage
    return {
        'trades': len(rows), 'net': net, 'fees': fees, 'slippage': slippage, 'costs': costs, 'moved': moved,
        'fee_bps': (fees / moved * 10000) if moved else 0.0,
        'slippage_bps': (slippage / moved * 10000) if moved else 0.0,
        'cost_bps': (costs / moved * 10000) if moved else 0.0,
        'gross': gross,
        # Kept for callers that predate the slippage line.
        'net_before_fees': net + fees,
        # How much of the loss is the toll rather than the trading. Only
        # meaningful when the lane is down.
        'fee_share': (fees / abs(net) * 100) if net < 0 else 0.0,
        # How much of the raw edge the tolls take. Meaningful in both directions,
        # which is the point: a lane can be up and still be handing over most of
        # what it earns.
        'cost_share': (costs / abs(gross) * 100) if gross else 0.0,
        'slippage_measured': measured,
        'epoch_started_at': account.epoch_started_at,
        'all_time': all_time,
    }


def _slippage_cost(account: Account, moved: float, all_time: bool = False) -> tuple[float, bool]:
    """Dollars lost to the gap between the decision price and the fill.

    Measured from `Fill.realized_slippage_bps`, stamped per fill against the price
    the strategy actually decided at, and already SIGNED against us: positive is a
    worse fill than we asked for, negative is a better one. It is summed signed,
    not absolute — a fill that came in better is money the desk kept, and counting
    it as a cost would overstate the toll and understate the edge.

    Scoped to the same epoch as the trades it sits beside. Falls back to the
    configured assumption only when no fill carries a measurement, and says which
    of the two it used: an assumed cost reported as a measured one is how a desk
    talks itself into trusting a model it never checked.
    """
    from main_app.models import Fill
    q = Fill.objects.filter(order__account=account, realized_slippage_bps__isnull=False)
    if not all_time and account.epoch_started_at:
        q = q.filter(ts__gte=account.epoch_started_at)
    fills = list(q.values_list('realized_slippage_bps', 'qty', 'price'))
    if fills:
        return sum(float(bps) / 1e4 * float(qty) * float(px) for bps, qty, px in fills), True
    from main_app.models import AgentConfig
    cfg = AgentConfig.get()
    assumed = float(getattr(cfg, f'{account.market}_slippage_bps', 0.0) or 0.0)
    return moved * 2 * assumed / 1e4, False


def lane_pnl(account: Account, d: date) -> dict:
    a, b = _bounds(d)
    trades = list(Trade.objects.filter(account=account, exit_ts__gte=a, exit_ts__lt=b).select_related('instrument'))
    pnls = [float(t.pnl) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    positions = list(account.positions.select_related('instrument'))
    fees = sum(float(t.fees) for t in trades)
    # What the lane MOVED, not what it owns. Forex runs at 10x leverage, so it can
    # push $373,000 through a $9,400 account in a day; that number is the only one
    # a fee rate means anything against.
    moved = sum(float(t.entry_price) * float(t.qty) for t in trades)
    return {
        'trades': len(trades), 'net': sum(pnls), 'fees': fees,
        'moved': moved,
        'fee_bps': (fees / moved * 10000) if moved else 0.0,
        'net_before_fees': sum(pnls) + fees,
        'lifetime': lane_costs(account),
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

    # 4b. The news the lane read today.
    try:
        d = news_digest(account.market, hours=24)
    except Exception:
        d = None
    if d and d.total:
        mix = ', '.join(f'{k} {v}' for k, v in sorted(d.counts.items(), key=lambda kv: -kv[1]))
        out.append({'source': 'news', 'text': f'read {d.total} headline(s) about this lane ({mix})',
                    'detail': 'Headlines are classified hourly and scored against price a day later, so an '
                              'event type has to prove itself before it is allowed to influence anything.'})
        for item in d.significant:
            out.append({'source': 'news',
                        'text': f'{item.kind} · {item.direction} · {", ".join(item.symbols)}: {item.headline[:130]}',
                        'detail': item.rationale[:240]})
    elif d is not None and account.market in (Market.STOCKS, Market.CRYPTO):
        out.append({'source': 'news', 'text': 'no headlines touching this lane in the last 24 h', 'detail': ''})

    # 4c. The wide view: what the searching briefing found happening in this lane.
    try:
        brief = latest_briefing(account.market, within_hours=14)
    except Exception:
        brief = None
    if brief is not None:
        out.append({'source': 'world',
                    'text': (brief.headline or 'nothing significant') + ('' if not brief.quiet else ' (quiet)'),
                    'detail': ' · '.join(i['text'][:150] for i in (brief.items or [])[:3])})

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

    news_items = [i for i in learned if i['source'] == 'news']
    if any('· bullish ·' in i['text'] or '· bearish ·' in i['text'] for i in news_items):
        out.append('A confirmed, market-moving story ran on a symbol in this lane today. The agent does not '
                   'trade on headlines: the outcome of every classified story is scored against price 24 hours '
                   'later, and an event type only earns influence once that record shows it predicts anything. '
                   'Run `manage.py read_news --scoreboard` to see whether any type has yet.')

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
    # If every lane shares a scoring epoch, say so rather than calling a two-day
    # number "lifetime". The word is doing real work: it is the difference between
    # "this is what the desk has ever done" and "this is what the current setup
    # has done since we stopped measuring a machine that was broken".
    epochs = {ln['pnl']['lifetime'].get('epoch_started_at') for ln in lanes}
    since = 'lifetime'
    if len(epochs) == 1 and (start := epochs.pop()):
        since = f'since the reset on {timezone.localtime(start):%b %-d}'
    return {'date': d, 'mode': mode, 'lanes': lanes, 'generated_at': timezone.now(), 'spend': spend,
            'since': since,
            'total': {'net': total_net, 'trades': total_trades, 'equity': total_equity,
                      'starting_cash': total_start, 'total_pnl': total_equity - total_start}}


def render_text(rep: dict) -> str:
    L = []
    t = rep['total']
    L.append(f"MoneyTree — {rep['date']:%A %B %-d, %Y}")
    L.append('=' * 60)
    L.append(f"Today across all four lanes: {t['net']:+,.2f} on {t['trades']} trades")
    L.append(f"Equity {t['equity']:,.2f} of {t['starting_cash']:,.2f} seeded "
              f"({t['total_pnl']:+,.2f} {rep.get('since', 'lifetime')})")
    L.append('')
    L.append('  ' + '  '.join(f"{lane['title'].upper():<9}" for lane in rep['lanes']))
    L.append('  ' + '  '.join(f"{lane['pnl']['net']:>+9,.2f}" for lane in rep['lanes']))
    L.append('  ' + '  '.join(
        f"{str(lane['pnl']['trades']) + (' trade' if lane['pnl']['trades'] == 1 else ' trades'):<9}"
        for lane in rep['lanes']))
    L.append('  ' + '  '.join(f"{'fees ' + format(lane['pnl']['fees'], ',.2f'):<9}"
                              for lane in rep['lanes']))
    L.append('')
    for lane in rep['lanes']:
        p = lane['pnl']
        L.append(f"── {lane['title'].upper()} " + '─' * (56 - len(lane['title'])))
        L.append(f"P&L today {p['net']:+,.2f} on {p['trades']} trades"
                 + (f" ({p['win_rate']:.0f}% won, fees {p['fees']:,.2f}, expectancy {p['expectancy']:+.2f}/trade)"
                    if p['trades'] else '')
                 + f" · equity {p['equity']:,.2f} ({p['total_pnl']:+,.2f} "
                 + f"{rep.get('since', 'lifetime')})")
        if p['trades']:
            L.append(f"  Cost of running it today: moved {p['moved']:,.0f}, paid {p['fees']:,.2f} "
                     f"({p['fee_bps']:.1f} bps) — {p['net_before_fees']:+,.2f} before fees")
        lc = p['lifetime']
        if lc['trades']:
            span = 'Since the reset' if lc.get('epoch_started_at') else 'Lifetime'
            line = (f"  {span}: {lc['trades']} trades, moved {lc['moved']:,.0f}, "
                    f"paid {lc['fees']:,.2f} ({lc['fee_bps']:.1f} bps), net {lc['net']:+,.2f}")
            if lc['net_before_fees'] > 0 and lc['net'] < 0:
                # The most useful sentence the report can say about a lane: the
                # trading works and the tolls are eating it.
                line += (f" — it makes {lc['net_before_fees']:+,.2f} before fees, "
                         f"so the tolls are what sink it")
            elif lc['fee_share']:
                line += f" — fees are {lc['fee_share']:.0f}% of the loss"
            L.append(line)
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
    """Email HTML.

    Tables and inline styles, not flexbox and a stylesheet: Gmail strips most
    embedded CSS and does not lay out with flex, so a grid built the modern way
    collapses into one column per lane and the scoreboard stops being a
    scoreboard. The point of the top block is that all four lanes are readable
    before any scrolling.
    """
    t = rep['total']
    INK, DIM, FAINT = '#e7eae8', '#9aa8a0', '#6f7d75'
    BG, CARD, EDGE = '#0f1211', '#151b18', '#242d28'
    UP, DOWN, FLAT = '#4ade80', '#f87171', '#9ca3af'

    def tone(v):
        return UP if v > 0 else (DOWN if v < 0 else FLAT)

    def money(v, size=13, weight=600):
        return (f'<span style="color:{tone(v)};font-size:{size}px;font-weight:{weight};'
                f'font-variant-numeric:tabular-nums">{v:+,.2f}</span>')

    P = [f'<div style="margin:0;padding:20px 12px;background:{BG};'
         f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Helvetica,Arial,sans-serif;color:{INK}">',
         f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
         f'style="max-width:640px;margin:0 auto">',
         '<tr><td>',
         f'<div style="font-size:19px;font-weight:700">🌳 MoneyTree</div>',
         f'<div style="color:{DIM};font-size:13px;margin-top:2px">{rep["date"]:%A %B %-d, %Y}</div>',
         f'<div style="margin:14px 0 4px;font-size:28px;font-weight:700;color:{tone(t["net"])};'
         f'font-variant-numeric:tabular-nums">{t["net"]:+,.2f}</div>',
         f'<div style="color:{DIM};font-size:12px">across all four lanes on {t["trades"]} trade'
         f'{"" if t["trades"] == 1 else "s"} · equity {t["equity"]:,.2f} of {t["starting_cash"]:,.0f} seeded '
         f'· lifetime {t["total_pnl"]:+,.2f}</div>',
         '</td></tr>']

    # The scoreboard: two rows of two, so four lanes fit a phone without scrolling.
    P.append('<tr><td style="padding-top:16px">'
             '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">')
    lanes = rep['lanes']
    for i in range(0, len(lanes), 2):
        P.append('<tr>')
        for lane in lanes[i:i + 2]:
            pn = lane['pnl']
            note = (f'{pn["trades"]} trade{"" if pn["trades"] == 1 else "s"}'
                    if pn['trades'] else ('parked' if not lane['strategies'] or
                                          not any(r.enabled for r in lane['strategies']) else 'no trades'))
            P.append(
                f'<td width="50%" valign="top" style="padding:0 5px 10px 0">'
                f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
                f'style="background:{CARD};border:1px solid {EDGE};border-radius:10px">'
                f'<tr><td style="padding:12px 14px">'
                f'<div style="color:{DIM};font-size:11px;letter-spacing:.06em;text-transform:uppercase">'
                f'{lane["title"]}</div>'
                f'<div style="margin-top:5px;font-size:22px;font-weight:700;color:{tone(pn["net"])};'
                f'font-variant-numeric:tabular-nums">{pn["net"]:+,.2f}</div>'
                f'<div style="color:{FAINT};font-size:11px;margin-top:3px;font-variant-numeric:tabular-nums">'
                f'{note} · equity {pn["equity"]:,.0f}</div>'
                # Each lane carries its OWN fee line. One desk-wide number would
                # hide that degen pays 50 bps a round trip while forex pays 1,
                # which is exactly the difference that decides whether a lane is
                # worth promoting on its own.
                f'<div style="color:{FAINT};font-size:11px;margin-top:2px;'
                f'font-variant-numeric:tabular-nums">fees {pn["fees"]:,.2f}'
                + (f' ({pn["fee_bps"]:.1f} bps)' if pn['trades'] else '')
                + f' · {pn["net_before_fees"]:+,.2f} before fees</div>'
                f'</td></tr></table></td>')
        P.append('</tr>')
    P.append('</table></td></tr>')

    # Then the detail, in the same order as the scoreboard.
    for lane in lanes:
        pn = lane['pnl']
        P.append(f'<tr><td style="padding-top:22px">'
                 f'<div style="border-top:1px solid {EDGE};padding-top:14px">'
                 f'<span style="font-size:16px;font-weight:700">{lane["title"]}</span>'
                 f'<span style="margin-left:8px">{money(pn["net"], 15)}</span>'
                 f'<span style="color:{FAINT};font-size:11px;margin-left:8px">'
                 f'lifetime {pn["total_pnl"]:+,.2f}</span></div>')
        if pn['trades']:
            P.append(f'<div style="color:{DIM};font-size:12px;margin-top:6px">'
                     f'{pn["win_rate"]:.0f}% won · fees {pn["fees"]:,.2f} ({pn["fee_bps"]:.1f} bps) · '
                     f'expectancy {pn["expectancy"]:+.2f}/trade · exits: '
                     + ', '.join(f'{k} {v}' for k, v in pn['exit_reasons'].most_common()) + '</div>')
        if pn['open_positions']:
            P.append(f'<div style="color:{FAINT};font-size:12px;margin-top:4px">'
                     f'{pn["open_positions"]} still open · {pn["unrealized"]:+,.2f} unrealised</div>')

        P.append(f'<div style="color:{DIM};font-size:11px;letter-spacing:.06em;text-transform:uppercase;'
                 f'margin:14px 0 6px">What it learned</div><ul style="margin:0;padding-left:18px">')
        for item in lane['learned']:
            det = (f'<div style="color:{FAINT};font-size:12px;margin-top:2px">{item["detail"][:300]}</div>'
                   if item.get('detail') else '')
            P.append(f'<li style="margin:0 0 7px;font-size:13px;line-height:1.45">'
                     f'<span style="color:{UP};font-size:10px;letter-spacing:.06em;text-transform:uppercase">'
                     f'{item["source"]}</span> {item["text"]}{det}</li>')
        P.append('</ul>')
        P.append(f'<div style="color:{DIM};font-size:11px;letter-spacing:.06em;text-transform:uppercase;'
                 f'margin:14px 0 6px">How it gets better</div><ul style="margin:0;padding-left:18px">')
        for line in lane['improve']:
            P.append(f'<li style="margin:0 0 7px;font-size:13px;line-height:1.45">{line}</li>')
        P.append('</ul></td></tr>')

    sp = rep.get('spend') or {}
    mtd = sp.get('month_to_date') or {}
    P.append(f'<tr><td style="padding-top:22px"><div style="border-top:1px solid {EDGE};padding-top:14px">'
             f'<span style="font-size:16px;font-weight:700">API spend</span>'
             f'<span style="color:{DIM};font-size:12px;margin-left:8px">today ${sp.get("total", 0):.2f} · '
             f'30 days ${mtd.get("total", 0):.2f} · at this rate ${sp.get("projected_monthly", 0):.2f}/month</span>'
             f'</div></td></tr>')
    P.append(f'<tr><td style="padding-top:20px;color:{FAINT};font-size:11px;line-height:1.5">'
             f'Generated {rep["generated_at"].astimezone(cal.ET):%Y-%m-%d %H:%M ET}. '
             f'Fake money until it earns its stripes.</td></tr>')
    P.append('</table></div>')
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
