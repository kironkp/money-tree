"""The machine, as a graph. Every bot, what it reads, what it writes.

This is the source of truth behind /map/. It is written by hand rather than
reflected out of the registry on purpose: the point of the map is to say what
each part is FOR, in a sentence a person who does not read Python can follow,
and no amount of introspection produces that. What reflection does give — which
strategies exist, which lanes are live, what the numbers are right now — is
merged in at request time by `state.py`, so the map cannot quietly drift into
describing a system that no longer exists.

Node kinds have genuinely different shapes on screen, so the machine is readable
at a zoom where no text is legible:

    bot        a process or service that decides something
    store      a table or file where state lives
    source     something outside this machine that we read
    venue      something outside this machine that we send orders to
    equation   a formula that actually gates a decision
    paper      a news article or a filing — the raw material
    gate       a rule that stops things

Lane colour is chrome only — the spine of a node, the stroke of a cable. It never
touches type, because green and red already mean money won and money lost and two
colour languages arguing on one screen is how a dashboard becomes decoration.
"""
from __future__ import annotations

# --- lanes ------------------------------------------------------------------
LANES = {
    'stocks': {'name': 'Stocks', 'hue': 'stocks', 'note': 'US shares and ETFs, 09:30–16:00 ET'},
    'crypto': {'name': 'Crypto', 'hue': 'crypto', 'note': 'BTC and ETH, around the clock'},
    'degen': {'name': 'Degen', 'hue': 'degen', 'note': 'twelve altcoins, the risky sandbox'},
    'forex': {'name': 'Forex', 'hue': 'forex', 'note': 'four USD pairs on 10× margin, 24/5'},
    'all': {'name': 'Shared', 'hue': 'all', 'note': 'used by every lane'},
    'infra': {'name': 'Operations', 'hue': 'infra', 'note': 'keeping the lights on'},
}

PAYLOADS = {
    'bars': {'label': 'price bars', 'slack': 1.06, 'dash': '', 'width': 1.6},
    'signal': {'label': 'a signal', 'slack': 1.12, 'dash': '', 'width': 2.2},
    'order': {'label': 'an order', 'slack': 1.12, 'dash': '', 'width': 2.6},
    'money': {'label': 'money', 'slack': 1.18, 'dash': '', 'width': 3.2},
    'news': {'label': 'a story', 'slack': 1.30, 'dash': '2 6', 'width': 1.8},
    'number': {'label': 'a number', 'slack': 1.10, 'dash': '6 4', 'width': 1.6},
    'control': {'label': 'control', 'slack': 1.08, 'dash': '1 5', 'width': 1.4},
    'money_out': {'label': 'dollars spent', 'slack': 1.20, 'dash': '4 4', 'width': 1.8},
}


def _n(id, kind, name, lane, icon, one_liner, what, **kw):
    node = {'id': id, 'kind': kind, 'name': name, 'lane': lane, 'icon': icon,
            'one_liner': one_liner, 'what': what, 'cadence': kw.get('cadence', ''),
            'model': kw.get('model', ''), 'file': kw.get('file', ''),
            'reads': kw.get('reads', []), 'writes': kw.get('writes', []),
            'cluster': kw.get('cluster', 'core'), 'col': kw.get('col', 0),
            'row': kw.get('row', 0), 'eq': kw.get('eq'), 'note': kw.get('note', ''),
            'live': kw.get('live', '')}
    return node


def nodes() -> list[dict]:
    out: list[dict] = []
    a = out.append

    # ---------------------------------------------------------------- sources
    a(_n('src.alpaca', 'source', 'Alpaca', 'all', 'antenna',
         'The broker, and where most of our prices and headlines come from.',
         'Three separate free feeds on one account: minute and daily price bars for stocks and '
         'crypto, a Benzinga news wire tagged by ticker, and the paper trading venue itself. '
         'The stock history is the consolidated tape; the live loop reads the IEX feed instead, '
         'because that is the one that keeps arriving in real time.',
         cadence='polled each tick', file='main_app/services/data/alpaca_data.py',
         writes=['price bars', 'headlines'], cluster='data', col=0, row=1))
    a(_n('src.yahoo', 'source', 'Yahoo Finance', 'forex', 'antenna',
         'The only place we can get currency prices, and where company fundamentals come from.',
         'Alpaca does not carry forex, so every EUR/USD bar on this desk comes from here. It also '
         'supplies the vendor fundamentals — the multiple, revenue growth, analyst targets, and '
         'which way earnings estimates are being revised. It is an unofficial scraper with no '
         'contract and no uptime guarantee, so nothing it says is ever treated as confirmed.',
         cadence='per sync; fundamentals on demand', file='main_app/services/research/fundamentals.py',
         writes=['forex bars', 'vendor fundamentals'], cluster='data', col=0, row=2))
    a(_n('src.edgar', 'source', 'SEC EDGAR', 'all', 'archive',
         'The filings themselves. The only source here that can be called authoritative.',
         'A keyless public API returning exactly what a company told the regulator, with the '
         'accession number, the form type, the fiscal period and the XBRL tag attached. That '
         'identity is the whole point: a person can open the filing and check the number against '
         'the dossier that quoted it.',
         cadence='on demand, a few calls an hour', file='main_app/services/research/edgar.py',
         writes=['filed figures'], cluster='data', col=0, row=3))
    a(_n('src.openai', 'source', 'OpenAI', 'all', 'brain',
         'The models that read, classify and reason.',
         'Four different jobs on three tiers: a cheap model classifies every headline, a mid tier '
         'writes the company dossiers with web search, and the flagship runs the four-hourly News '
         'Agent sitting. Every call is priced and written to the ledger before it is made.',
         cadence='hourly and 4-hourly', model='gpt-4o-mini · gpt-5.6-terra · gpt-5.6-sol',
         file='main_app/services/spend.py', writes=['judgements', 'invoices'],
         cluster='data', col=0, row=4))

    # ------------------------------------------------------------------ store
    a(_n('store.bars', 'store', 'Bar store', 'all', 'database',
         'Every price bar we have ever downloaded, kept per feed.',
         'One row per instrument, timeframe, feed and timestamp. Feeds are kept apart rather than '
         'merged, because IEX prints about 2% of the volume the consolidated tape does and '
         'comparing one against the other silently corrupts any volume measure. Everything passes '
         'a quality gate first: impossible highs and lows dropped, duplicate stamps dropped, gaps '
         'flagged as possible halts, a 40% overnight jump flagged as a probable stock split.',
         file='main_app/services/data/store.py', reads=['Alpaca', 'Yahoo'],
         writes=['bars for every strategy'], cluster='data', col=1, row=2))
    a(_n('store.calendar', 'store', 'Market calendar', 'all', 'calendar',
         'When each market is open, including the awkward days.',
         'NYSE sessions with early closes and daylight-saving handled, crypto always open, and '
         'forex rolling its day at 17:00 New York and its week on Friday. Session rules are '
         'offsets from the close rather than clock times, so a half-day just works.',
         file='main_app/services/data/calendar.py', writes=['open/closed', 'session bounds'],
         cluster='data', col=1, row=4))

    # ------------------------------------------------------------- lane agents
    lane_rows = {'stocks': 0, 'crypto': 1, 'degen': 2, 'forex': 3}
    lane_specs = {
        'stocks': ('Stocks Agent', 'bot-stocks',
                   'Trades US shares and ETFs while the bell is open.',
                   'Wakes when a five-minute bar completes, asks its strategies what they think, '
                   'puts anything they propose through the risk gate, and flattens everything '
                   'before the close. Holds an exclusive lock so two copies can never run on one '
                   'account, and checks for a stop instruction every two seconds, so "kill" means '
                   'now rather than next bar.', '5-minute bars · Alpaca IEX'),
        'crypto': ('Crypto Agent', 'bot-crypto',
                   'Trades Bitcoin and Ether, and never sleeps.',
                   'The same loop as the stocks agent, but the lane never shuts. Instead of '
                   'flattening at a bell it rolls the day at midnight New York, writes the '
                   'journal and keeps its positions — the exit is the stop, the target, or four '
                   'hours, whichever comes first. Spot crypto cannot be sold short and the risk '
                   'manager refuses it outright.', '4-hour bars · 24/7'),
        'degen': ('Degen Agent', 'bot-degen',
                  'The high-risk altcoin sandbox. Expected to lose, on fake money.',
                  'Twelve volatile altcoins on fifteen-minute bars. It once ran on one-minute '
                  'bars and lost $2,210 of which $1,698 was fees — at that timeframe the typical '
                  'target was smaller than the round trip, so no set of parameters could have '
                  'won. It is still the worst lane on the desk and the map does not hide that.',
                  '15-minute bars · 24/7'),
        'forex': ('Forex Agent', 'bot-forex',
                  'Trades four currency pairs on ten-times margin, Sunday night to Friday night.',
                  'The only lane that borrows. Buying power is ten times equity, each direction '
                  'capped at five times, and the cost model is a spread rather than a commission. '
                  'Yahoo is the only feed, so there is no centralised volume and the strategies '
                  'say so instead of inventing one. There is no forex broker adapter, so this '
                  'lane cannot leave the simulator.', '15-minute bars · 24/5'),
    }
    for lane, (name, icon, one, what, cadence) in lane_specs.items():
        a(_n(f'agent.{lane}', 'bot', name, lane, icon, one, what,
             cadence=cadence, file='main_app/services/agent.py',
             reads=['bars', 'strategies', 'risk settings'],
             writes=['orders', 'trades', 'the live feed'],
             cluster='lanes', col=3, row=lane_rows[lane]))

    # -------------------------------------------------------- shared internals
    a(_n('core.engine', 'bot', 'Decision Engine', 'all', 'cpu',
         'Turns one finished price bar into a decision, an order and a sentence.',
         'The single most important thing in the codebase: the same function runs a backtest, a '
         'replay and live trading. If they used different code the backtest would be a story '
         'about a program that does not exist. It never acts twice on the same bar, and every '
         'refusal is recorded with its reason.',
         cadence='once per completed bar, per symbol', file='main_app/services/engine.py',
         reads=['prepared bars', 'strategy signals'], writes=['orders', 'trade cards', 'feed lines'],
         cluster='lanes', col=4, row=1))
    a(_n('core.risk', 'gate', 'Risk Manager', 'all', 'shield',
         'Decides how big a trade may be, or refuses it and says why.',
         'Size comes from the risk budget divided by the distance to the stop, then gets cut by '
         'whichever cap binds first: position size, buying power, how much of one direction is '
         'already on, the daily loss budget, the number of trades today. Roughly two thirds of '
         'all signals are refused here, and each refusal is written down.',
         cadence='every signal', file='main_app/services/risk.py',
         reads=['a signal', 'the account', 'open positions'], writes=['a size, or a refusal'],
         cluster='lanes', col=5, row=1))
    a(_n('core.narrator', 'bot', 'Narrator', 'all', 'quote',
         'Writes down what the agent is thinking, in English, as it happens.',
         'Every bar, signal, refusal, fill and exit becomes a line on the live feed. This is what '
         'makes an autonomous system arguable instead of mysterious: you can read why it did not '
         'trade, not just see that it did not.',
         file='main_app/services/narrator.py', writes=['the live feed'],
         cluster='lanes', col=5, row=3))
    a(_n('core.sim', 'venue', 'Simulator', 'all', 'chip',
         'A fake broker that fills orders honestly, including the parts that cost money.',
         'Fills at the next bar open or the current price, always slipped against us. Stops gap '
         'through at the open, both barriers in one bar counts as the stop, a fill may not exceed '
         '1% of the bar volume, and sales pay the regulatory fees purchases do not. The '
         'flattering assumption is how a backtest lies to itself.',
         file='main_app/services/broker/sim.py', reads=['orders'], writes=['fills', 'positions'],
         cluster='lanes', col=6, row=1))
    a(_n('core.alpaca_broker', 'venue', 'Alpaca Broker', 'all', 'bank',
         'The real paper account. Dormant until a strategy earns its way there.',
         'Reconciles our books against the venue every tick and blocks new entries while they '
         'disagree. Live trading needs an environment flag AND the mode set AND the strategy '
         'qualified — three separate locks, none of which is currently open.',
         file='main_app/services/broker/alpaca.py', writes=['real paper orders'],
         cluster='lanes', col=6, row=3, note='not currently in use'))
    a(_n('store.ledger', 'store', 'The books', 'all', 'ledger',
         'Accounts, orders, fills, trades, positions and the equity curve.',
         'Every number here is a Decimal, because money is. The simulator is rebuilt from these '
         'rows on restart, so a crash mid-position does not lose the position.',
         file='main_app/services/ledger.py', writes=['trades', 'equity snapshots'],
         cluster='lanes', col=7, row=1))

    # ------------------------------------------------------------- strategies
    strat = [
        ('orb', 'Opening Range Breakout', 'stocks',
         'Buys when price breaks the first few minutes of the day.',
         'The best-documented intraday pattern there is: mark the high and low of the opening '
         'range, then trade the break, with a volume filter to avoid the ones nobody is joining.'),
        ('vwap_reversion', 'VWAP Reversion', 'all',
         'Buys when price has stretched too far from the day\'s average price.',
         'Fades moves that have run too far from the volume-weighted average, on the assumption '
         'the day\'s fair price pulls back. On forex it has no volume to weight with and says so '
         'rather than pretending.'),
        ('ema_momentum', 'EMA Momentum', 'all',
         'Follows a trend once two moving averages agree.',
         'A fast average crossing a slow one, filtered by strength and by whether anyone is '
         'actually trading. The simplest idea on the desk and the one most likely to be already '
         'priced in.'),
        ('burst', 'Burst', 'degen',
         'Chases sudden altcoin moves on unusual volume.',
         'Built for the degen lane, where moves are violent and short. It is also the lane where '
         'fees ate two thirds of the losses, so a burst has to be large to survive the round trip.'),
        ('news_catalyst', 'News Catalyst', 'all',
         'Trades the instruction the News Agent left behind.',
         'The arm, not the brain. It takes a standing instruction, leases it so one story can '
         'only ever become one order, and puts it through the ordinary risk gate. It refuses to '
         'run in a backtest at all, because the verdict table is written in the present and '
         'reading it historically would be looking up the answers.'),
    ]
    for i, (key, name, lane, one, what) in enumerate(strat):
        a(_n(f'strat.{key}', 'bot', name, lane, 'strategy', one, what,
             file=f'main_app/services/strategies/{key}.py',
             reads=['prepared bars'], writes=['signals'], cluster='strategies',
             col=2, row=i))

    return out + _news_nodes() + _research_nodes() + _ops_nodes() + _equation_nodes()


def _news_nodes() -> list[dict]:
    a: list[dict] = []
    a.append(_n('paper.wire', 'paper', 'The wire', 'all', 'newspaper',
                'Headlines as they are published, tagged by ticker.',
                'Roughly two hundred stories an hour reach the feed; most are about companies '
                'this desk does not trade and are discarded before anything is paid for. What '
                'survives is stored with the moment it first became public — never the moment we '
                'happened to poll for it, because that would measure our latency and call it the '
                'market\'s.',
                cluster='news', col=1, row=6))
    a.append(_n('bot.reader', 'bot', 'News Reader', 'all', 'inbox',
                'Collects headlines every hour and works out which are actually new.',
                'Three kinds of sameness get told apart: the same poll returning the same story, '
                'the wire revising its own copy, and twelve outlets rewriting one story. Only the '
                'first is free to ignore. It also pulls the full article text, which the same '
                'call returns at no extra cost and which the agent went without for weeks.',
                cadence='every hour', file='main_app/services/news.py',
                reads=['the wire'], writes=['stored stories'], cluster='news', col=2, row=6))
    a.append(_n('bot.classifier', 'bot', 'Headline Classifier', 'all', 'tag',
                'Turns each new headline into a structured event.',
                'One cheap model call per story: what kind of event, which way it points, how big, '
                'how well confirmed, and over what horizon. It is deliberately sceptical — a '
                'question mark, a "could", or an analyst opinion is low confidence and usually '
                'neutral, because headlines are written to be clicked.',
                cadence='hourly, up to 40 stories', model='gpt-4o-mini',
                file='main_app/services/news.py', reads=['stored stories'],
                writes=['event type, direction, magnitude'], cluster='news', col=3, row=6))
    a.append(_n('bot.briefer', 'bot', 'Lane Briefer', 'all', 'globe',
                'Asks once per lane what is going on in the world.',
                'The headline feed answers "what happened to a symbol I hold". This answers the '
                'wider question it structurally cannot: the stories that move a whole lane are '
                'usually tagged to no ticker at all — a rate decision, a regulation, an exchange '
                'going down. Deliberately not a trading signal; its own docstring says nothing '
                'here reaches the risk manager.',
                cadence='every 4 hours, per lane', model='gpt-4o-mini + web search',
                file='main_app/services/briefing.py', writes=['the wider picture'],
                cluster='news', col=3, row=8))
    a.append(_n('bot.newsagent', 'bot', 'News Agent', 'all', 'bot-news',
                'Reads everything new and scores each story out of ten for whether it can be '
                'traded today.',
                'One batched call rather than one per story, because stories interact: two '
                'headlines can be the same trade seen twice, and a model that sees them together '
                'can say so. It may only name symbols this desk can actually trade. Anything at '
                'five or above becomes a standing instruction; everything below is kept anyway, '
                'because the record of what it declined is what makes the record of what it took '
                'mean anything.',
                cadence='every 4 hours', model='gpt-5.6-sol',
                file='main_app/services/news_agent.py',
                reads=['classified stories', 'lane briefings'],
                writes=['verdicts', 'instructions'], cluster='news', col=4, row=6))
    a.append(_n('store.verdicts', 'store', 'Verdicts', 'all', 'gavel',
                'Every judgement the News Agent has ever made, and what happened next.',
                'Each row carries the score, the direction, the thesis, the price and volatility '
                'at the time, and — once the race has run — which barrier the price touched '
                'first. Rows scored before the grader existed are marked as rebuilt and excluded '
                'from every statistic, because an outcome reconstructed afterwards is not a '
                'forecast recorded before the fact.',
                file='main_app/models.py', writes=['the scoreboard'],
                cluster='news', col=5, row=6))
    a.append(_n('gate.lease', 'gate', 'The lease', 'all', 'lock',
                'Makes sure one story can only ever become one order.',
                'An instruction is claimed before the risk manager sees it, handed back if the '
                'risk manager refuses, and only spent once a broker acknowledges the order. A '
                'database constraint makes it impossible for two workers to hold the same event. '
                'Before this existed, four sittings that each saw one story about QQQ shorted it '
                'four times into a rising market.',
                file='main_app/services/news_agent.py', cluster='news', col=6, row=6))
    return a


def _research_nodes() -> list[dict]:
    a: list[dict] = []
    a.append(_n('paper.filing', 'paper', 'Filings and fundamentals', 'all', 'document',
                'What the company actually told the regulator, plus what the vendor says.',
                'The filed numbers carry their accession, form and tag so they can be looked up. '
                'The vendor numbers — the multiple, the growth rates, which way estimates are '
                'moving — are useful and are never called confirmed. Every value stores where it '
                'came from and when it was true, and a missing number stays missing.',
                file='main_app/services/research/', cluster='research', col=2, row=9))
    a.append(_n('bot.dossier', 'bot', 'Research Analyst', 'all', 'bot-research',
                'Researches one company at a time and writes a dossier a person can argue with.',
                'It is handed the hard numbers — filings, fundamentals, where the price sits — and '
                'asked to reason about them rather than recall them. Anything it introduces itself '
                'must arrive with a verbatim quote and a working link, or it is thrown away before '
                'it is written down. Strict formatting guarantees structure, not truth.',
                cadence='every 4 hours, up to 4 companies', model='gpt-5.6-terra (flex)',
                file='main_app/services/dossier.py',
                reads=['filings', 'fundamentals', 'bars', 'stories'],
                writes=['a dossier', 'a forecast'], cluster='research', col=3, row=10,
                note='in shadow — writes no orders'))
    a.append(_n('store.dossiers', 'store', 'Dossiers', 'all', 'folder',
                'One standing view per company, with its evidence and its forecast.',
                'Three scores, because there are three questions and only one of them can move a '
                'position on a desk that closes everything within four hours: a dated catalyst '
                'may trade, context may only shrink or veto, and the months-long thesis is for '
                'reading.',
                file='main_app/models.py', cluster='research', col=4, row=10))
    a.append(_n('gate.shadow', 'gate', 'Shadow', 'all', 'ghost',
                'The research arm is switched off at the query, not by anyone remembering.',
                'Only arms listed as allowed to act can produce an instruction, and research is '
                'not on that list. Adding it changes the frozen fingerprint of the experiment, '
                'which supersedes it and restarts the evidence at zero. That cost is the point.',
                file='main_app/services/news_agent.py', cluster='research', col=5, row=10))
    a.append(_n('bot.prereg', 'bot', 'Preregistration', 'all', 'seal',
                'Freezes the experiment before any data arrives.',
                'The model, the exact prompt text, the schema, every threshold and every risk '
                'limit are hashed into one fingerprint. Move any of them and the running '
                'evaluation is superseded rather than quietly inheriting evidence gathered under '
                'different rules. A threshold chosen after seeing the data is not a threshold.',
                file='main_app/services/preregistration.py', cluster='research', col=5, row=12))
    a.append(_n('bot.evaluation', 'bot', 'The Gate', 'all', 'balance',
                'Decides whether the research arm has earned the right to trade. Nobody is asked.',
                'One question: does the research policy beat the headline policy, per day, '
                'counting the days each chose not to trade. It looks only at days written down in '
                'advance, spends its error budget across those looks, and measures with a method '
                'that survives the fact that market days are not independent of each other.',
                cadence='at 20, 40, 60, 90 and 120 trading days',
                file='main_app/services/evaluation.py', cluster='research', col=6, row=11))
    a.append(_n('store.scoreboard', 'store', 'Scoreboard', 'all', 'chart',
                'Whether any of this works, shown with its sample size.',
                'Every number sits next to how many observations it came from and what it would '
                'have to clear. A difference without an n and a threshold is a number people read '
                'as a result.',
                file='main_app/templates/news_agent/scoreboard.html',
                cluster='research', col=7, row=11))
    return a


def _ops_nodes() -> list[dict]:
    a: list[dict] = []
    a.append(_n('ops.optimizer', 'bot', 'Nightly Research', 'all', 'flask',
                'Looks for better settings overnight, and is hard to convince.',
                'Walk-forward optimisation: fit on one window, test on the next, never on the '
                'same data. A candidate must clear a profit factor of 1.10 with positive '
                'expectancy on a window it has never seen, and the pipeline it came from must '
                'have worked too. A lucky final week once suggested a forex configuration at 1.10 '
                'while four of five test windows lost; the rule that caught it is still there.',
                cadence='02:10 nightly', file='main_app/services/optimize.py',
                writes=['promoted parameters'], cluster='ops', col=8, row=0))
    a.append(_n('ops.promotion', 'gate', 'Graduation', 'all', 'ladder',
                'The ladder every strategy has to climb: Seed, Sprout, Sapling, Tree.',
                'Backtest, then fake money, then a real paper account, then real money. Paper and '
                'live additionally require the strategy be qualified on held-out evidence. All '
                'thirteen strategy rows on this desk are currently unproven, which is why nothing '
                'has left the simulator.',
                file='main_app/services/promotion.py', cluster='ops', col=8, row=1))
    a.append(_n('ops.coach', 'bot', 'The Coach', 'all', 'lightbulb',
                'Reads the week and proposes experiments.',
                'A weekly review that turns the journal and the numbers into three concrete things '
                'to try, each of which can be run with one click. Dormant without an API key, and '
                'currently silent because the Anthropic account is spend-capped.',
                cadence='weekly', model='claude-sonnet-5', file='main_app/services/coach.py',
                cluster='ops', col=8, row=2, note='dormant'))
    a.append(_n('ops.report', 'bot', 'Daily Report', 'all', 'mail',
                'The 5:30pm email: what each lane did, and what it learned.',
                'A scoreboard of all four lanes at the top, then per lane the profit and loss, '
                'what was learned, and what will be tried differently. Built as a table with '
                'inline styles because that is the only thing email clients agree on.',
                cadence='17:30 daily', file='main_app/services/report.py',
                cluster='ops', col=8, row=3))
    a.append(_n('ops.spend', 'store', 'Spend Ledger', 'all', 'receipt',
                'What the paid APIs cost, priced at the moment of the call.',
                'A cost reconstructed later from a log is a guess; a row written at call time is a '
                'fact. Calls are debited at an estimate before they are made and reconciled '
                'against actual usage after, because a timeout costs money while producing '
                'nothing. The tier that gets billed is the one the API says it served.',
                file='main_app/services/spend.py', cluster='ops', col=7, row=8))
    a.append(_n('ops.watchdog', 'bot', 'Watchdog', 'infra', 'eye',
                'Closes positions at the venue when an agent dies holding them.',
                'An independent process, on purpose: an agent that has crashed cannot be trusted '
                'to clean up after itself. It acts only on the broker-backed accounts, so today it '
                'watches and finds nothing, which is what it should do until a lane graduates. Its '
                'status here is read from its log rather than from its schedule file — the file '
                'sat in the repository for weeks looking exactly like a job that was working.',
                cadence='every 5 minutes', file='main_app/management/commands/watchdog.py',
                cluster='ops', col=8, row=4))
    a.append(_n('ops.agents', 'bot', 'Agent Supervisor', 'infra', 'heartbeat',
                'Starts any lane agent that is not running, and leaves the rest alone.',
                'Runs when the machine boots and every five minutes after. It is idempotent — it '
                'starts what is missing and touches nothing else — which is why it can be on a '
                'timer at all, where a restart job on the same timer would kill a working agent '
                'every five minutes. Before it existed the four agents were started only by the '
                '02:10 research job, so a reboot in the morning meant no trading until the next '
                'night.',
                cadence='at boot, then every 5 minutes',
                file='main_app/management/commands/ensure_agents.py',
                cluster='ops', col=2, row=1))

    a.append(_n('ops.backup', 'bot', 'Nightly Backup', 'infra', 'archive-box',
                'Pushes the code and the irreplaceable rows to a private repository at 2am.',
                'The trading ledger is dumped as plain SQL and committed, because it deltas well '
                'and reads as an account history. The full database rides a weekly release asset '
                'instead, so a 29MB binary never bloats the repository. Password hashes are '
                'deliberately excluded.',
                cadence='02:00 nightly', file='deploy/backup-data.sh',
                cluster='ops', col=8, row=5))
    return a


# --- the mathematics --------------------------------------------------------
# Rendered as HTML with a handful of CSS rules rather than KaTeX: the app has no
# build step, and a formula that gates a decision should be readable in the same
# place the decision is made. Variables are serif italic, literal numbers are
# mono — that split is what makes a substituted formula scannable.

def _eq(html: str, subs: str = '', note: str = '') -> dict:
    return {'html': html, 'subs': subs, 'note': note}


V = '<span class="v">%s</span>'
OP = '<span class="op">%s</span>'
NUM = '<span class="n">%s</span>'


def _frac(num: str, den: str) -> str:
    return (f'<span class="frac"><span class="num">{num}</span>'
            f'<span class="bar"></span><span class="den">{den}</span></span>')


def _equation_nodes() -> list[dict]:
    a: list[dict] = []

    a.append(_n('eq.atr', 'equation', 'Average True Range', 'all', 'ruler',
                'How far does this thing usually move in one bar?',
                'Everything on this desk is measured in ATRs rather than dollars, so a $330 share '
                'and a $0.000008 memecoin can be compared at all. Stops, targets, forecasts and '
                'outcomes are all denominated in it. It is the busiest node on this map.',
                file='main_app/services/indicators.py', cluster='math', col=4, row=13,
                eq=_eq(
                    f'{V % "TR"}{OP % "="}<span class="fn">max</span>'
                    f'<span class="paren">{V % "H"}{OP % "−"}{V % "L"},'
                    f'<span class="abs">{V % "H"}{OP % "−"}{V % "C"}<sub>prev</sub></span>,'
                    f'<span class="abs">{V % "L"}{OP % "−"}{V % "C"}<sub>prev</sub></span></span>',
                    'ATR is the 14-period Wilder average of TR. '
                    'AAPL right now: 1 ATR ≈ 0.19% of price.',
                    'The previous close is in there so an overnight gap counts as real movement '
                    'rather than being invisible.')))

    a.append(_n('eq.cost', 'gate', 'The cost gate', 'all', 'toll',
                'Can this trade pay for itself before it is even right?',
                'A target must be at least three times the round trip in fees and slippage, or the '
                'trade is refused. This is the single most active refusal on the desk and the '
                'reason the degen lane moved off one-minute bars: there, the typical target was '
                'smaller than the fee.',
                file='main_app/services/risk.py', cluster='math', col=5, row=14,
                eq=_eq(
                    f'{_frac(f"<span class=\"abs\">{V % "target"}{OP % "−"}{V % "entry"}</span>", V % "entry")}'
                    f'{OP % "≥"}{V % "k"}{OP % "·"}{NUM % "2"}'
                    f'<span class="paren">{V % "fee"}{OP % "+"}{V % "slip"}</span>',
                    'stocks 0.070% round trip, gate 0.210% · crypto and degen 0.560%, gate 1.680% '
                    '· forex 0.016%, gate 0.032%',
                    'Crypto costs eight times what stocks cost, so a crypto idea has to be eight '
                    'times better just to break even.')))

    a.append(_n('eq.size', 'equation', 'Position size', 'all', 'scale',
                'How many shares, given where the stop is?',
                'Risk a fixed fraction of the account, then cut it by whichever cap binds first. '
                'On this desk the position cap almost always binds before the risk budget does, '
                'so a trade intended to risk $50 arrives risking about $6 — which is why the '
                'refusal message now reports what was actually risked rather than what was '
                'budgeted.',
                file='main_app/services/risk.py', cluster='math', col=5, row=15,
                eq=_eq(
                    f'{V % "qty"}{OP % "="}<span class="fn">min</span><span class="paren">'
                    f'{_frac(f"{V % "equity"}{OP % "·"}{V % "r"}{OP % "·"}{V % "conviction"}", f"<span class=\"abs\">{V % "entry"}{OP % "−"}{V % "stop"}</span>")}'
                    f', {_frac(f"{V % "equity"}{OP % "·"}{V % "cap"}", V % "entry")}'
                    f', {_frac(V % "buying power", V % "entry")}</span>',
                    'r = 0.5% of equity · cap = 20% of equity · conviction ≤ 1.0, never above',
                    'Conviction can only ever shrink a position. There is no path in this code '
                    'that lets a model talk its way into a bigger bet.')))

    a.append(_n('eq.barrier', 'equation', 'The barrier race', 'all', 'flag',
                'Does price reach the target before it reaches the stop?',
                'Every trade here is a race between two lines drawn at fixed multiples of ATR, '
                'ending after four hours whatever has happened. This is what the research agent is '
                'asked to forecast, and what the grader replays afterwards.',
                file='main_app/services/news_agent.py', cluster='math', col=6, row=14,
                eq=_eq(
                    f'{V % "stop"}{OP % "="}{V % "C"}{OP % "−"}{NUM % "1.5"}{OP % "·"}{V % "ATR"}'
                    f'<span style="width:2em;display:inline-block"></span>'
                    f'{V % "target"}{OP % "="}{V % "C"}{OP % "+"}{NUM % "3.0"}{OP % "·"}{V % "ATR"}',
                    'Both barriers inside one bar counts as the STOP.',
                    'Open, high, low and close cannot say which came first, and the flattering '
                    'assumption is how a backtest lies to itself.')))

    a.append(_n('eq.baserate', 'equation', 'The measured prior', 'all', 'dice',
                'What does this stock do on its own, with no news at all?',
                'Sampled from the symbol\'s own history: how often does the target arrive before '
                'the stop from a standing start. Without it a forecast of 30% is meaningless — it '
                'is bearish if the base rate is 45% and wildly bullish if it is 12%. The model is '
                'handed this number and departing from it is treated as a claim.',
                file='main_app/services/research/context.py', cluster='math', col=3, row=13,
                eq=_eq(
                    f'{V % "p"}<sub>target first</sub>{OP % "="}'
                    f'{_frac(f"<span class=\"fn\">#</span><span class=\"paren\">{V % "i"} {OP % ":"} {V % "j"}<sub>target</sub>{OP % "&lt;"}{V % "j"}<sub>stop</sub></span>", V % "N")}',
                    'AAPL 35.5% · NVDA 30.1% · TSLA 32.2% — against a 33.3% break-even before '
                    'costs and about 47% after them',
                    'Measured LONG ONLY. Short forecasts are currently anchored on a prior taken '
                    'from the opposite side, which is a real limitation and is said out loud here '
                    'rather than hidden.')))

    a.append(_n('eq.netatr', 'equation', 'What it actually earned', 'all', 'coins',
                'After costs, was this call worth making?',
                'Where a news story finally becomes a number. The gross move in ATRs, minus the '
                'round trip expressed in the same units. This is the quantity the whole experiment '
                'is measured in.',
                file='main_app/services/news_agent.py', cluster='math', col=6, row=15,
                eq=_eq(
                    f'{V % "net"}<sub>ATR</sub>{OP % "="}{V % "gross"}<sub>ATR</sub>{OP % "−"}'
                    f'{_frac(f"{V % "cost"}{OP % "%"}{OP % "·"}{V % "entry"}", V % "ATR")}',
                    'On AAPL the round trip is about 0.63 ATR — so a winning 3-ATR target nets '
                    '2.37 and a loss costs 2.13, which needs a 47% hit rate to break even.',
                    'The cost is more than a third of the stop distance. That single number is '
                    'why news has to add roughly twelve points of accuracy to be worth trading.')))

    a.append(_n('eq.samplesize', 'equation', 'How long until we know?', 'all', 'hourglass',
                'How many days of evidence before the answer means anything?',
                'Both error rates, not just one: the chance of being fooled by noise and the '
                'chance of missing a real edge. The variance comes from a method that accounts '
                'for market days not being independent of one another.',
                file='main_app/services/evaluation.py', cluster='math', col=7, row=13,
                eq=_eq(
                    f'{V % "n"}<sub>days</sub>{OP % "≥"}<span class="ceil"><span class="paren">'
                    f'{_frac(f"<span class=\"paren\">{V % "z"}<sub>1−α</sub>{OP % "+"}{V % "z"}<sub>1−β</sub></span>{OP % "·"}{V % "σ"}<sub>LR</sub>", f"{V % "Δ"}<sub>min</sub>")}'
                    f'</span><sup class="pow">2</sup></span>',
                    'z(0.95) = 1.6449 · z(0.80) = 0.8416 · Δ = 0.15 ATR/day → n ≥ ⌈274.8 · σ²⌉',
                    'At a long-run sigma of 0.9 that is 223 trading days — about eleven months. '
                    'The honest number, printed where it cannot be missed.')))

    a.append(_n('eq.obf', 'equation', 'Spending the error budget', 'all', 'stopwatch',
                'How much of the chance of being wrong may this look use up?',
                'Checking a result every night and acting the first time it looks good will find a '
                'winner in pure noise almost every time. So the looks are fixed in advance and the '
                'budget is spent across them, almost nothing early and all of it at the end.',
                file='main_app/services/evaluation.py', cluster='math', col=7, row=14,
                eq=_eq(
                    f'{V % "α"}<sub>k</sub>{OP % "="}{NUM % "2"}<span class="paren">{NUM % "1"}{OP % "−"}'
                    f'<span class="fn">Φ</span><span class="paren">'
                    f'{_frac(f"{V % "z"}<sub>1−α/2</sub>", f"<span class=\"sqrt\"><span class=\"rad\">{_frac(V % "k", V % "K")}</span></span>")}'
                    f'</span></span>',
                    'look 1 → 0.0000117 · look 2 → 0.0019 · look 3 → 0.0114 · look 4 → 0.0284 · '
                    'look 5 → 0.0500',
                    'The first look is priced at about one chance in eighty-five thousand. Early '
                    'looks are nearly free; the whole budget is spent at the end.')))

    a.append(_n('eq.bootstrap', 'equation', 'The interval', 'all', 'waves',
                'How sure are we, given that market days come in runs?',
                'Whole weeks are resampled rather than individual days, so streaks of good and bad '
                'weather stay intact instead of being shuffled into a false calm. The ordinary '
                'standard error would be optimistic here by roughly the square root of that '
                'dependence.',
                file='main_app/services/evaluation.py', cluster='math', col=7, row=15,
                eq=_eq(
                    f'{V % "σ"}<sub>LR</sub>{OP % "="}<span class="sqrt"><span class="rad">'
                    f'{_frac(f"{V % "b"}{OP % "·"}{V % "Σ"}<span class=\"paren\">{V % "m"}<sub>i</sub>{OP % "−"}{V % "m̄"}</span><sup class=\"pow\">2</sup>", f"{V % "k"}{OP % "−"}{NUM % "1"}")}'
                    f'</span></span>',
                    'blocks of 5 trading days · 4,000 resamples · the lower bound must clear '
                    '+0.15 ATR/day',
                    'Batching to whole weeks absorbs the within-week dependence, so what is left '
                    'between batches is much closer to independent.')))

    a.append(_n('eq.borrow', 'equation', 'What a short really costs', 'all', 'minus-circle',
                'Selling something you do not own is not free.',
                'A borrow fee that accrues by the minute, the regulatory fees only a sale pays, '
                'and a dividend you owe if an ex-date falls inside the hold. The desk modelled '
                'shorts as a mirror of longs until this existed, which flattered exactly the side '
                'the news arm trades most.',
                file='main_app/services/broker/sim.py', cluster='math', col=6, row=16,
                eq=_eq(
                    f'{V % "cost"}{OP % "="}{V % "notional"}{OP % "·"}'
                    f'{_frac(V % "borrow", NUM % "10,000")}{OP % "·"}'
                    f'{_frac(V % "minutes", NUM % "525,600")}{OP % "+"}'
                    f'{V % "notional"}{OP % "·"}{_frac(NUM % "0.3", NUM % "10,000")}',
                    'borrow 40 bps a year for an easy-to-borrow large cap · SEC and FINRA fees '
                    '0.3 bps, charged on the sale only',
                    'Hard-to-borrow names are refused outright rather than modelled, because the '
                    'fee on those is not a number anyone can guess.')))

    return a


# --- the wiring -------------------------------------------------------------

def edges() -> list[dict]:
    def e(src, dst, payload, label=''):
        return {'from': src, 'to': dst, 'payload': payload, 'label': label}

    out = [
        e('src.alpaca', 'store.bars', 'bars', 'price bars'),
        e('src.yahoo', 'store.bars', 'bars', 'forex bars'),
        e('src.alpaca', 'paper.wire', 'news', 'headlines'),
        e('src.yahoo', 'paper.filing', 'number', 'fundamentals'),
        e('src.edgar', 'paper.filing', 'number', 'filed figures'),
    ]
    for lane in ('stocks', 'crypto', 'degen', 'forex'):
        out += [
            e('store.bars', f'agent.{lane}', 'bars', 'completed bars'),
            e('store.calendar', f'agent.{lane}', 'control', 'is the market open'),
            e(f'agent.{lane}', 'core.engine', 'control', 'one bar at a time'),
        ]
    out += [
        e('strat.orb', 'core.engine', 'signal', 'a signal'),
        e('strat.vwap_reversion', 'core.engine', 'signal', 'a signal'),
        e('strat.ema_momentum', 'core.engine', 'signal', 'a signal'),
        e('strat.burst', 'core.engine', 'signal', 'a signal'),
        e('strat.news_catalyst', 'core.engine', 'signal', 'a news signal'),
        e('store.bars', 'strat.orb', 'bars', ''),
        e('store.bars', 'strat.vwap_reversion', 'bars', ''),
        e('store.bars', 'strat.ema_momentum', 'bars', ''),
        e('store.bars', 'strat.burst', 'bars', ''),
        e('eq.atr', 'strat.news_catalyst', 'number', 'stop and target'),
        e('core.engine', 'core.risk', 'signal', 'may I?'),
        e('eq.cost', 'core.risk', 'number', 'can it pay for itself'),
        e('eq.size', 'core.risk', 'number', 'how big'),
        e('core.risk', 'core.sim', 'order', 'an approved order'),
        e('core.risk', 'core.alpaca_broker', 'order', 'dormant'),
        e('core.engine', 'core.narrator', 'control', 'what it is thinking'),
        e('core.sim', 'store.ledger', 'money', 'fills and trades'),
        e('core.alpaca_broker', 'store.ledger', 'money', ''),
        e('store.ledger', 'eq.netatr', 'number', 'what it earned'),
        e('eq.atr', 'eq.cost', 'number', ''),
        e('eq.atr', 'eq.size', 'number', ''),
        e('eq.atr', 'eq.barrier', 'number', ''),
        e('eq.borrow', 'core.sim', 'number', 'the short side'),

        e('paper.wire', 'bot.reader', 'news', 'raw headlines'),
        e('bot.reader', 'bot.classifier', 'news', 'novel stories'),
        e('src.openai', 'bot.classifier', 'control', 'a cheap read'),
        e('bot.classifier', 'bot.newsagent', 'news', 'structured events'),
        e('bot.briefer', 'bot.newsagent', 'news', 'the wider picture'),
        e('src.openai', 'bot.briefer', 'control', ''),
        e('src.openai', 'bot.newsagent', 'control', 'the flagship'),
        e('bot.newsagent', 'store.verdicts', 'number', 'scored out of ten'),
        e('store.verdicts', 'gate.lease', 'control', 'one story, one order'),
        e('gate.lease', 'strat.news_catalyst', 'signal', 'a live instruction'),
        e('eq.barrier', 'store.verdicts', 'number', 'graded afterwards'),
        e('eq.netatr', 'store.scoreboard', 'number', ''),

        e('paper.filing', 'bot.dossier', 'number', 'hard numbers'),
        e('bot.classifier', 'bot.dossier', 'news', 'the story cluster'),
        e('store.bars', 'bot.dossier', 'bars', 'where price sits'),
        e('eq.baserate', 'bot.dossier', 'number', 'the measured prior'),
        e('store.bars', 'eq.baserate', 'bars', ''),
        e('src.openai', 'bot.dossier', 'control', 'with web search'),
        e('bot.dossier', 'store.dossiers', 'number', 'a dossier'),
        e('store.dossiers', 'gate.shadow', 'control', 'blocked here'),
        e('gate.shadow', 'bot.evaluation', 'number', 'shadow results'),
        e('store.dossiers', 'eq.netatr', 'number', 'what it would have earned'),
        e('bot.prereg', 'bot.evaluation', 'control', 'the frozen rules'),
        e('eq.samplesize', 'bot.evaluation', 'number', 'how long'),
        e('eq.obf', 'bot.evaluation', 'number', 'when to look'),
        e('eq.bootstrap', 'bot.evaluation', 'number', 'how sure'),
        e('bot.evaluation', 'store.scoreboard', 'number', 'where it stands'),
        e('bot.evaluation', 'gate.shadow', 'control', 'the only thing that unlocks it'),

        e('store.ledger', 'ops.optimizer', 'number', 'what happened'),
        e('ops.optimizer', 'ops.promotion', 'number', 'a candidate'),
        e('ops.promotion', 'strat.orb', 'control', 'new parameters'),
        e('ops.promotion', 'strat.ema_momentum', 'control', ''),
        e('store.ledger', 'ops.report', 'money', 'the day'),
        e('store.verdicts', 'ops.report', 'news', 'what it learned'),
        e('ops.spend', 'ops.report', 'money_out', 'what it cost'),
        e('src.openai', 'ops.spend', 'money_out', 'every call, priced'),
        e('ops.spend', 'bot.dossier', 'control', 'the budget, and the brake'),
        e('store.ledger', 'ops.coach', 'number', ''),
        e('store.ledger', 'ops.backup', 'control', 'nightly'),
        e('store.verdicts', 'ops.backup', 'control', ''),
        e('ops.watchdog', 'core.alpaca_broker', 'control', 'closes a stranded position'),
        e('ops.agents', 'agent.stocks', 'control', 'keeps it alive'),
        e('ops.agents', 'agent.crypto', 'control', ''),
        e('ops.agents', 'agent.degen', 'control', ''),
        e('ops.agents', 'agent.forex', 'control', ''),
    ]
    return out


def graph() -> dict:
    ns = nodes()
    ids = {n['id'] for n in ns}
    es = [x for x in edges() if x['from'] in ids and x['to'] in ids]
    return {'nodes': ns, 'edges': es, 'lanes': LANES, 'payloads': PAYLOADS}
